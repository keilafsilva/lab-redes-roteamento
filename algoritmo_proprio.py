#!/usr/bin/env python3
"""
Algoritmo próprio de roteamento — Trabalho I (Fundamentos de Sistemas Operacionais)

IDEIA CENTRAL
-------------
OSPF e RIP escolhem rotas por custo fixo ou número de saltos. Este algoritmo
considera:
  1. A LATÊNCIA REAL de cada link (medida com ping, em latencias.json).
  2. Um BÔNUS DE REDUNDÂNCIA para links que fazem parte de ciclos.

    custo_efetivo(link) = latencia_medida(link) - bonus_redundancia(link)
    bonus_redundancia   = min(ALPHA * n_caminhos_alternativos, 60% da latência)
    custo_rota          = soma dos custos efetivos dos links do caminho

Para cada roteador e cada rede de acesso remota, escolhe o caminho simples de
menor custo (empate: menos saltos) e instala no kernel do contêiner:
    ip route replace <rede> via <ip do vizinho> dev <iface>

Como todos os roteadores usam o mesmo grafo e custos positivos, o próximo
salto de cada um está sobre um caminho de custo mínimo e a distância até o
destino diminui a cada salto: não há loops. O script confere isso offline
antes de aplicar.

Reação a falhas (--watch): a cada ~1 s lê o estado das interfaces (operstate),
recalcula sem os enlaces caídos e reaplica. O tempo impresso é o "delay de
convergência" do algoritmo próprio.

USO (de dentro da pasta do lab, com o lab no ar)
------------------------------------------------
    python3 algoritmo_proprio.py                # calcula, confere e aplica
    python3 algoritmo_proprio.py --dry-run      # só mostra, não toca nos contêineres
    python3 algoritmo_proprio.py --analise      # etapas 1-3 (tabelas p/ o relatório)
    python3 algoritmo_proprio.py --testar       # ping host->host em todos os pares
    python3 algoritmo_proprio.py --sem R1-R3    # calcula/aplica sem esse enlace
    python3 algoritmo_proprio.py --watch        # reage sozinho a enlaces caindo/voltando
"""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import networkx as nx

# ---------------------------------------------------------------------
# 1. Topologia (idêntica ao topo.clab.yml)
# ---------------------------------------------------------------------

ROTEADORES = ["R1", "R2", "R3", "R4", "R5"]

LINKS = [
    ("R1", "R2", "R1-R2"),
    ("R2", "R3", "R2-R3"),
    ("R3", "R4", "R3-R4"),
    ("R4", "R5", "R4-R5"),
    ("R5", "R1", "R5-R1"),
    ("R1", "R3", "R1-R3"),  # diagonal
]

# IPs das duas pontas de cada enlace /30
LINK_IPS = {
    "R1-R2": {"R1": "10.0.12.1", "R2": "10.0.12.2"},
    "R2-R3": {"R2": "10.0.23.1", "R3": "10.0.23.2"},
    "R3-R4": {"R3": "10.0.34.1", "R4": "10.0.34.2"},
    "R4-R5": {"R4": "10.0.45.1", "R5": "10.0.45.2"},
    "R5-R1": {"R5": "10.0.51.1", "R1": "10.0.51.2"},
    "R1-R3": {"R1": "10.0.13.1", "R3": "10.0.13.2"},
}

ACCESS_NET = {
    "R1": "192.168.1.0/24",
    "R2": "192.168.2.0/24",
    "R3": "192.168.3.0/24",
    "R4": "192.168.4.0/24",
    "R5": "192.168.5.0/24",
}

IFACE_POR_LINK = {
    ("R1", "R1-R2"): "eth1", ("R2", "R1-R2"): "eth1",
    ("R2", "R2-R3"): "eth2", ("R3", "R2-R3"): "eth1",
    ("R3", "R3-R4"): "eth2", ("R4", "R3-R4"): "eth1",
    ("R4", "R4-R5"): "eth2", ("R5", "R4-R5"): "eth1",
    ("R5", "R5-R1"): "eth2", ("R1", "R5-R1"): "eth2",
    ("R1", "R1-R3"): "eth3", ("R3", "R1-R3"): "eth3",
}

# nome do enlace -> ((roteador, iface), (roteador, iface))
LINK_IFACES = {nome: ((a, IFACE_POR_LINK[(a, nome)]), (b, IFACE_POR_LINK[(b, nome)]))
               for a, b, nome in LINKS}

# IP de enlace -> roteador dono (para seguir next-hops na verificação offline)
IP_DONO = {ip: r for pontas in LINK_IPS.values() for r, ip in pontas.items()}

ALPHA = 0.02
MAX_ALT_PATH_LEN = 4
DEFAULT_LATENCY_MS = 0.1
PREFIXO = "clab-lab-fso-"


# ---------------------------------------------------------------------
# 2. Algoritmo
# ---------------------------------------------------------------------

def carregar_latencias(path="latencias.json"):
    medidas = {}
    p = Path(path)
    if p.exists():
        try:
            medidas = json.loads(p.read_text())
        except Exception as e:
            print(f"AVISO: não consegui ler {path} ({e}); usando valor padrão.")
    for _, _, nome in LINKS:
        medidas.setdefault(nome, DEFAULT_LATENCY_MS)
    return {k: float(v) for k, v in medidas.items()}


def montar_grafo(sem=()):
    """Grafo da topologia, sem os enlaces em `sem` (enlaces caídos)."""
    g = nx.Graph()
    g.add_nodes_from(ROTEADORES)
    for a, b, nome in LINKS:
        if nome not in sem:
            g.add_edge(a, b, nome=nome)
    return g


def contar_caminhos_alternativos(g, a, b):
    g2 = g.copy()
    g2.remove_edge(a, b)
    if not nx.has_path(g2, a, b):
        return 0
    return sum(1 for _ in nx.all_simple_paths(g2, a, b, cutoff=MAX_ALT_PATH_LEN))


def calcular_custos_efetivos(g, latencias, verbose=True):
    custos = {}
    if verbose:
        print(f"{'Link':<8}{'Latência (ms)':>15}{'Alt. paths':>12}{'Bônus':>10}{'Custo efetivo':>16}")
        print("-" * 61)
    for a, b, dados in g.edges(data=True):
        nome = dados["nome"]
        lat = latencias[nome]
        alt = contar_caminhos_alternativos(g, a, b)
        bonus = min(ALPHA * alt, lat * 0.6)
        custos[nome] = lat - bonus
        if verbose:
            print(f"{nome:<8}{lat:>15.3f}{alt:>12}{bonus:>10.3f}{custos[nome]:>16.3f}")
    return custos


def custo_do_caminho(caminho, g, custos):
    return sum(custos[g[a][b]["nome"]] for a, b in zip(caminho, caminho[1:]))


def melhor_rota(g, custos, origem, destino):
    """Menor custo efetivo; empate -> menos saltos."""
    candidatos = [(custo_do_caminho(c, g, custos), c)
                  for c in nx.all_simple_paths(g, origem, destino)]
    candidatos.sort(key=lambda x: (round(x[0], 9), len(x[1])))
    return candidatos[0]


def rota_por_saltos(g, origem, destino):
    caminho = nx.shortest_path(g, origem, destino)
    return len(caminho) - 1, caminho


def calcular_tabela(g, custos):
    """tabela[origem][destino] = dict(rede, via, iface, caminho, custo) ou None."""
    tabela = {}
    for o in ROTEADORES:
        tabela[o] = {}
        for d in ROTEADORES:
            if d == o:
                continue
            if not nx.has_path(g, o, d):
                tabela[o][d] = None
                continue
            custo, caminho = melhor_rota(g, custos, o, d)
            prox = caminho[1]
            nome = g[o][prox]["nome"]
            tabela[o][d] = dict(rede=ACCESS_NET[d], via=LINK_IPS[nome][prox],
                                iface=IFACE_POR_LINK[(o, nome)],
                                caminho=caminho, custo=custo)
    return tabela


def verificar_tabela(tabela):
    """Segue os next-hops de cada par e devolve a lista de problemas (vazia = ok)."""
    problemas = []
    for o in ROTEADORES:
        for d in ROTEADORES:
            if o == d:
                continue
            atual, visitados = o, [o]
            while atual != d:
                rota = tabela[atual].get(d)
                if rota is None:
                    problemas.append(f"{o}->{d}: sem rota em {atual}")
                    break
                atual = IP_DONO[rota["via"]]
                if atual in visitados:
                    problemas.append(f"{o}->{d}: LOOP {'->'.join(visitados + [atual])}")
                    break
                visitados.append(atual)
    return problemas


# ---------------------------------------------------------------------
# 3. Aplicação nos contêineres
# ---------------------------------------------------------------------

def comando_do_roteador(rotas):
    cmds = ["set -e", "sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true"]
    for d, r in rotas.items():
        rede = ACCESS_NET[d]
        if r:
            cmds.append(f"ip route replace {rede} via {r['via']} dev {r['iface']}")
        else:  # destino inalcançável: remove só rota com 'via' (nunca a conectada)
            cmds.append(f"if ip route show {rede} | grep -q via; then ip route del {rede}; fi")
    return "; ".join(cmds)


def aplicar(tabela, dry_run=False, verbose=True):
    if verbose:
        for o in ROTEADORES:
            for d, r in tabela[o].items():
                if r:
                    print(f"  {o}: {r['rede']:<15} via {r['via']:<10} dev {r['iface']}  "
                          f"[{'->'.join(r['caminho'])}]")
                else:
                    print(f"  {o}: {ACCESS_NET[d]:<15} INALCANÇÁVEL")
    procs = {}
    for o in ROTEADORES:
        cmd = comando_do_roteador(tabela[o])
        if dry_run:
            print(f"[dry-run] docker exec {PREFIXO}{o.lower()} sh -c '{cmd}'")
            continue
        procs[o] = subprocess.Popen(["docker", "exec", PREFIXO + o.lower(), "sh", "-c", cmd],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ok = True
    for o, p in procs.items():
        saida, _ = p.communicate()
        if p.returncode != 0:
            ok = False
            print(f"ERRO ao aplicar em {o} (código {p.returncode}): {saida.strip()}")
    return ok


def links_caidos():
    """Enlaces com alguma ponta fora de 'up'. None se o docker falhar."""
    estado = {}
    for o in ROTEADORES:
        p = subprocess.run(
            ["docker", "exec", PREFIXO + o.lower(), "sh", "-c",
             "for i in eth1 eth2 eth3; do echo $i $(cat /sys/class/net/$i/operstate 2>/dev/null); done"],
            capture_output=True, text=True)
        if p.returncode != 0:
            return None
        for linha in p.stdout.splitlines():
            partes = linha.split()
            if len(partes) == 2:
                estado[(o, partes[0])] = partes[1]
    return frozenset(nome for nome, (a, b) in LINK_IFACES.items()
                     if estado.get(a) != "up" or estado.get(b) != "up")


def watch(intervalo, latencias, dry_run=False):
    print(f"Monitorando enlaces a cada {intervalo:.1f}s (Ctrl+C para parar)...")
    anterior = None
    while True:
        t0 = time.time()
        caidos = links_caidos()
        if caidos is not None and caidos != anterior:
            t_ini = time.time()
            g = montar_grafo(caidos)
            tabela = calcular_tabela(g, calcular_custos_efetivos(g, latencias, verbose=False))
            problemas = verificar_tabela(tabela) if not caidos else []
            if problemas:
                print("AVISO:", "; ".join(problemas))
            aplicar(tabela, dry_run=dry_run, verbose=False)
            print(f"[{time.strftime('%H:%M:%S')}] enlaces caídos: "
                  f"{', '.join(sorted(caidos)) or 'nenhum'} -> rotas reaplicadas em "
                  f"{time.time() - t_ini:.2f}s")
            anterior = caidos
        time.sleep(max(0.0, intervalo - (time.time() - t0)))


def testar_conectividade():
    """Ping host->host em todos os pares (h1..h5)."""
    pares = [(i, j) for i in range(1, 6) for j in range(1, 6) if i != j]

    def ping(par):
        i, j = par
        p = subprocess.run(["docker", "exec", f"{PREFIXO}h{i}", "ping", "-c", "1", "-W", "1",
                            f"192.168.{j}.10"], capture_output=True, text=True)
        return par, p.returncode == 0

    with ThreadPoolExecutor(max_workers=10) as ex:
        res = dict(ex.map(ping, pares))
    print("\nConectividade host -> host (✓ = respondeu)")
    print("      " + "".join(f"h{j:<3}" for j in range(1, 6)))
    for i in range(1, 6):
        print(f"  h{i}  " + "".join(("-   " if i == j else ("✓   " if res[(i, j)] else "✗   "))
                                     for j in range(1, 6)))
    falhas = [f"h{i}->h{j}" for (i, j), ok in res.items() if not ok]
    print(f"\n{len(pares) - len(falhas)}/{len(pares)} pares OK" +
          (f"; falharam: {', '.join(falhas)}" if falhas else ""))
    return not falhas


# ---------------------------------------------------------------------
# 4. Análise para o relatório (etapas 1 a 3)
# ---------------------------------------------------------------------

def analise(latencias):
    g = montar_grafo()
    print("=" * 61)
    print("ETAPA 1 — Custo efetivo de cada link (latência - bônus de redundância)")
    print("=" * 61)
    custos = calcular_custos_efetivos(g, latencias)

    print("\n" + "=" * 61)
    print("ETAPA 2 — Rotas a partir de R1 (algoritmo próprio x critério do RIP)")
    print("=" * 61)
    print(f"{'Destino':<17}{'Algoritmo próprio':<22}{'Custo':>8}   {'RIP (saltos)':<20}{'Saltos':>7}")
    print("-" * 80)
    for d in ROTEADORES[1:]:
        custo, caminho = melhor_rota(g, custos, "R1", d)
        saltos, cam_rip = rota_por_saltos(g, "R1", d)
        print(f"{ACCESS_NET[d]:<17}{'->'.join(caminho):<22}{custo:>8.3f}   "
              f"{'->'.join(cam_rip):<20}{saltos:>7}")

    print("\n" + "=" * 61)
    print("ETAPA 3 — Efeito de derrubar o link diagonal (R1-R3)")
    print("=" * 61)
    g2 = montar_grafo(sem=("R1-R3",))
    custos2 = calcular_custos_efetivos(g2, latencias)
    print(f"\n{'Destino':<17}{'Rota c/ diagonal':<18}{'Custo':>8}   {'Rota s/ diagonal':<18}{'Custo':>8}")
    print("-" * 74)
    for d in ROTEADORES[1:]:
        c1, p1 = melhor_rota(g, custos, "R1", d)
        c2, p2 = melhor_rota(g2, custos2, "R1", d)
        print(f"{ACCESS_NET[d]:<17}{'->'.join(p1):<18}{c1:>8.3f}   {'->'.join(p2):<18}{c2:>8.3f}")


def main():
    ap = argparse.ArgumentParser(description="Algoritmo próprio de roteamento")
    ap.add_argument("--dry-run", action="store_true", help="não executa nada nos contêineres")
    ap.add_argument("--analise", action="store_true", help="só imprime as etapas 1-3")
    ap.add_argument("--testar", action="store_true", help="ping host->host em todos os pares")
    ap.add_argument("--watch", action="store_true", help="reage a enlaces caindo/voltando")
    ap.add_argument("--sem", nargs="*", default=[], metavar="LINK", help="ex.: --sem R1-R3")
    ap.add_argument("--intervalo", type=float, default=1.0)
    args = ap.parse_args()

    lat = carregar_latencias()
    if args.analise:
        analise(lat)
        return
    if args.testar:
        raise SystemExit(0 if testar_conectividade() else 1)
    if args.watch:
        try:
            watch(args.intervalo, lat, args.dry_run)
        except KeyboardInterrupt:
            print("\nEncerrado.")
        return

    g = montar_grafo(args.sem)
    print("Custo efetivo de cada link:")
    custos = calcular_custos_efetivos(g, lat)
    tabela = calcular_tabela(g, custos)
    problemas = verificar_tabela(tabela) if not args.sem else []
    print("\nVerificação offline:",
          "OK (todos os pares alcançáveis, sem loops)" if not problemas else problemas)
    print("\nRotas:")
    ok = aplicar(tabela, dry_run=args.dry_run)
    if not ok:
        raise SystemExit("Falha ao aplicar rotas em algum roteador (veja os erros acima).")
    print("\n(dry-run: nada foi aplicado)" if args.dry_run else "\nRotas aplicadas.")


if __name__ == "__main__":
    main()
