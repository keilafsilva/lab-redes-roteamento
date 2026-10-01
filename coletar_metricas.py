#!/usr/bin/env python3
"""
coletar_metricas.py — coleta as métricas do trabalho para OSPF, RIP e algoritmo próprio.

Para cada modo, o script:
  1. ativa o modo (./switch-protocol.sh <modo>) e espera h1 alcançar h2..h5
     -> conv_inicial_s: tempo até a rede ficar utilizável depois da ativação
  2. conta as rotas do kernel (exceto a rede de gerência eth0)
     -> rotas_r1, rotas_total
  3. captura 60 s de tráfego de CONTROLE em R1 (OSPF = proto 89, RIP = UDP 520)
     enquanto mede o delay fim a fim h1 -> h3 com 50 pings
     -> ctrl_pkts, ctrl_bytes, ctrl_Bps, rtt_ms, jitter_ms, rota_h1_h3
  4. derruba o enlace R1-R2 (eth1 do r1) com um ping contínuo h1 -> h2 rodando
     -> outage_s (maior intervalo sem resposta), perdidos (pacotes sem resposta)
  5. religa o enlace

Tudo vai para metricas.csv (uma linha por modo e por rodada). Use graficos.py depois.

Pré-requisitos: lab no ar, atrasos aplicados (./aplicar-delays.sh), rodar da pasta do lab.
Uso:
    python3 coletar_metricas.py                    # uma rodada, 3 modos (~10 min)
    python3 coletar_metricas.py --modos ospf rip   # só alguns modos
    python3 coletar_metricas.py --rodadas 3        # repete tudo 3 vezes (mais confiável)
    python3 coletar_metricas.py --captura 40 --janela-falha 50   # versão mais rápida

Observação sobre o algoritmo próprio: em regime permanente ele não envia pacotes de
controle (o --watch só lê o estado das interfaces). O custo dele é a medição de
latência: PINGS_MEDICAO pings por enlace, a cada ativação (ctrl_medicao_pkts, calculado).
"""

import argparse
import csv
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import algoritmo_proprio as ap

PREFIXO = "clab-lab-fso-"
MODOS = ["ospf", "rip", "proprio"]
PINGS_MEDICAO = 10   # igual ao medir_latencia.sh
N_ENLACES = 6
FILTRO_CONTROLE = "ip proto 89 or udp port 520"
CAMPOS = ["rodada", "modo", "conv_inicial_s", "rotas_r1", "rotas_total",
          "ctrl_pkts", "ctrl_bytes", "ctrl_Bps", "ctrl_medicao_pkts",
          "rtt_ms", "jitter_ms", "rota_h1_h3", "outage_s", "perdidos", "reacao_watch_s"]

# IP -> nome legível, para mostrar a rota do traceroute
NOMES_IP = {ip: r for ip, r in ap.IP_DONO.items()}
NOMES_IP.update({f"192.168.{i}.1": f"R{i}" for i in range(1, 6)})
NOMES_IP.update({f"192.168.{i}.10": f"h{i}" for i in range(1, 6)})


# ---------------------------------------------------------------------
# utilidades docker
# ---------------------------------------------------------------------

def dx(nome, cmd, timeout=None):
    """Executa `cmd` (sh -c) dentro do contêiner e devolve stdout+stderr."""
    p = subprocess.run(["docker", "exec", PREFIXO + nome, "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout + p.stderr


def ping_ok(origem, destino):
    p = subprocess.run(["docker", "exec", PREFIXO + origem, "ping", "-c", "1", "-W", "1", destino],
                       capture_output=True, text=True)
    return p.returncode == 0


# ---------------------------------------------------------------------
# parsers (puros, testáveis sem docker)
# ---------------------------------------------------------------------

def parse_resumo_ping(saida):
    m = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", saida)
    return (float(m.group(2)), float(m.group(4))) if m else (None, None)


def parse_traceroute(saida):
    nos = []
    for linha in saida.splitlines():
        m = re.match(r"\s*\d+\s+(.*)", linha)   # só linhas de salto ("1  192.168.1.1 ...")
        if not m:
            continue
        ip = re.search(r"\d+\.\d+\.\d+\.\d+", m.group(1))
        nos.append(NOMES_IP.get(ip.group(0), ip.group(0)) if ip else "*")
    return ">".join(nos)


def parse_tcpdump(saida):
    pkts = bytes_ = 0
    for linha in saida.splitlines():
        if re.match(r"\d\d:\d\d:\d\d", linha):
            pkts += 1
            m = re.search(r"length (\d+)", linha)
            if m:
                bytes_ += int(m.group(1))
    return pkts, bytes_


def analisar_ping_continuo(saida, intervalo, t_falha):
    """A partir de `ping -D`, devolve (outage_s, perdidos).
    outage = maior intervalo entre duas respostas depois da falha, menos o intervalo normal."""
    resp = []
    for linha in saida.splitlines():
        m = re.match(r"\[([\d.]+)\].*icmp_seq=(\d+)", linha)
        if m:
            resp.append((float(m.group(1)), int(m.group(2))))
    if len(resp) < 2:
        return None, None
    resp.sort()
    perdidos = (resp[-1][1] - resp[0][1] + 1) - len(resp)
    maior = 0.0
    for (t1, _), (t2, _) in zip(resp, resp[1:]):
        if t2 >= t_falha - 0.5:
            maior = max(maior, t2 - t1)
    return round(max(0.0, maior - intervalo), 3), perdidos


# ---------------------------------------------------------------------
# medições
# ---------------------------------------------------------------------

def esperar_conectividade(maximo=150):
    t0 = time.time()
    while time.time() - t0 < maximo:
        if all(ping_ok("h1", f"192.168.{j}.10") for j in range(2, 6)):
            return round(time.time() - t0, 1)
        time.sleep(1)
    return None


def contar_rotas():
    total, r1 = 0, 0
    for i in range(1, 6):
        n = int(dx(f"r{i}", "ip route show | grep -v eth0 | wc -l").strip() or 0)
        total += n
        if i == 1:
            r1 = n
    return r1, total


def medir_rtt_e_controle(duracao):
    """Captura controle em R1 durante `duracao` s e, ao mesmo tempo, mede o RTT h1->h3."""
    dx("r1", "rm -f /tmp/c.pcap")
    cap = threading.Thread(target=lambda: dx(
        "r1", f"timeout {duracao} tcpdump -i any -nn -q -w /tmp/c.pcap '{FILTRO_CONTROLE}'",
        timeout=duracao + 20))
    cap.start()
    time.sleep(2)
    rtt, jitter = parse_resumo_ping(dx("h1", "ping -c 50 -i 0.2 -q 192.168.3.10", timeout=60))
    rota = parse_traceroute(dx("h1", "traceroute -n -w 1 -q 1 192.168.3.10", timeout=40))
    cap.join()
    pkts, bytes_ = parse_tcpdump(dx("r1", "tcpdump -nn -e -r /tmp/c.pcap 2>/dev/null"))
    return dict(rtt_ms=rtt, jitter_ms=jitter, rota_h1_h3=rota, ctrl_pkts=pkts,
                ctrl_bytes=bytes_, ctrl_Bps=round(bytes_ / duracao, 1))


def medir_falha(janela, falha_em=5, intervalo=0.05):
    """Ping contínuo h1->h2; derruba R1-R2 (r1:eth1) em `falha_em` s."""
    saida = []
    th = threading.Thread(target=lambda: saida.append(dx(
        "h1", f"ping -D -i {intervalo} -w {janela} 192.168.2.10", timeout=janela + 20)))
    th.start()
    time.sleep(falha_em)
    t_falha = time.time()
    dx("r1", "ip link set eth1 down")
    th.join()
    dx("r1", "ip link set eth1 up")
    return analisar_ping_continuo(saida[0] if saida else "", intervalo, t_falha)


# ---------------------------------------------------------------------
# orquestração
# ---------------------------------------------------------------------

def checar_atrasos():
    p = subprocess.run(["bash", "aplicar-delays.sh", "mostrar"], capture_output=True, text=True)
    if p.stdout.count("netem") < 12:
        sys.exit("ERRO: os atrasos (netem) não estão aplicados. Rode ./aplicar-delays.sh antes.")


def coletar_modo(modo, rodada, captura, janela):
    print(f"\n===== {modo.upper()} (rodada {rodada}) =====")
    p = subprocess.run(["./switch-protocol.sh", modo], capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout[-1500:], p.stderr[-800:])
        sys.exit(f"ERRO: ./switch-protocol.sh {modo} falhou.")

    watch_proc = watch_log = None
    if modo == "proprio":
        watch_log = Path("/tmp/watch_metricas.log")
        watch_proc = subprocess.Popen([sys.executable, "-u", "algoritmo_proprio.py", "--watch"],
                                      stdout=watch_log.open("w"), stderr=subprocess.STDOUT)

    linha = dict(rodada=rodada, modo=modo)
    print("  esperando a rede convergir...")
    linha["conv_inicial_s"] = esperar_conectividade()
    if linha["conv_inicial_s"] is None:
        sys.exit(f"ERRO: h1 não alcançou os outros hosts em modo {modo}.")
    time.sleep(3)

    linha["rotas_r1"], linha["rotas_total"] = contar_rotas()
    print(f"  rotas: R1={linha['rotas_r1']} total={linha['rotas_total']}")

    print(f"  capturando controle por {captura}s + RTT h1->h3...")
    linha.update(medir_rtt_e_controle(captura))
    linha["ctrl_medicao_pkts"] = 2 * PINGS_MEDICAO * N_ENLACES if modo == "proprio" else 0
    print(f"  controle: {linha['ctrl_pkts']} pkts, {linha['ctrl_Bps']} B/s | "
          f"RTT {linha['rtt_ms']} ms (jitter {linha['jitter_ms']}) | rota {linha['rota_h1_h3']}")

    print(f"  derrubando R1-R2 durante ping contínuo h1->h2 ({janela}s)...")
    linha["outage_s"], linha["perdidos"] = medir_falha(janela)
    linha["reacao_watch_s"] = ""
    if watch_proc:
        time.sleep(1)
        m = re.findall(r"R1-R2 -> rotas reaplicadas em ([\d.]+)s", watch_log.read_text())
        linha["reacao_watch_s"] = m[0] if m else ""
        watch_proc.terminate()
    print(f"  falha: outage {linha['outage_s']} s, {linha['perdidos']} pacotes sem resposta")
    time.sleep(5)
    return linha


def main():
    ap_ = argparse.ArgumentParser(description="Coleta de métricas OSPF x RIP x próprio")
    ap_.add_argument("--modos", nargs="*", default=MODOS, choices=MODOS)
    ap_.add_argument("--rodadas", type=int, default=1)
    ap_.add_argument("--captura", type=int, default=60, help="segundos de captura de controle")
    ap_.add_argument("--janela-falha", type=int, default=70, help="duração do ping contínuo")
    ap_.add_argument("--saida", default="metricas.csv")
    args = ap_.parse_args()

    checar_atrasos()
    novo = not Path(args.saida).exists()
    with open(args.saida, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo:
            w.writeheader()
        for r in range(1, args.rodadas + 1):
            for modo in args.modos:
                w.writerow(coletar_modo(modo, r, args.captura, args.janela_falha))
                f.flush()
    print(f"\nPronto: {args.saida}. Gere os gráficos com: python3 graficos.py")


if __name__ == "__main__":
    main()
