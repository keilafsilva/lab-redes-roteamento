#!/usr/bin/env python3
"""
graficos.py — gera os gráficos comparativos (OSPF x RIP x algoritmo próprio)
a partir do metricas.csv produzido por coletar_metricas.py.

Saída (pasta graficos/): 01_tabela_rotas.png, 02_controle.png, 03_rtt.png,
04_convergencia.png e comparacao.png (os quatro juntos).

Uso:  python3 graficos.py [metricas.csv]      (precisa de: pip install matplotlib)
Com várias rodadas no CSV, mostra a média e o desvio padrão por modo.
"""

import csv
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ORDEM = ["ospf", "rip", "proprio"]
NOMES = {"ospf": "OSPF", "rip": "RIP", "proprio": "Algoritmo\npróprio"}
CORES = {"ospf": "#2b6cb0", "rip": "#dd6b20", "proprio": "#2f855a"}


def ler(caminho):
    dados = {m: [] for m in ORDEM}
    with open(caminho, newline="") as f:
        for linha in csv.DictReader(f):
            if linha["modo"] in dados:
                dados[linha["modo"]].append(linha)
    return {m: v for m, v in dados.items() if v}


def serie(dados, campo):
    """media e desvio por modo para um campo numérico (ignora vazios)."""
    med, dp = [], []
    for m in dados:
        xs = [float(l[campo]) for l in dados[m] if l.get(campo) not in (None, "", "None")]
        med.append(statistics.mean(xs) if xs else 0.0)
        dp.append(statistics.stdev(xs) if len(xs) > 1 else 0.0)
    return med, dp


def barras(ax, dados, campo, titulo, ylabel, fmt="{:.1f}", log=False):
    modos = list(dados)
    med, dp = serie(dados, campo)
    x = range(len(modos))
    ax.bar(x, med, yerr=dp if any(dp) else None, capsize=4,
           color=[CORES[m] for m in modos])
    for i, v in enumerate(med):
        ax.text(i, v, fmt.format(v), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels([NOMES[m] for m in modos])
    ax.set_title(titulo, fontsize=11)
    ax.set_ylabel(ylabel)
    if log:
        ax.set_yscale("log")
    ax.spines[["top", "right"]].set_visible(False)


def grupo(ax, dados, campos, rotulos, titulo, ylabel):
    modos = list(dados)
    largura = 0.8 / len(campos)
    for k, (campo, rot) in enumerate(zip(campos, rotulos)):
        med, _ = serie(dados, campo)
        pos = [i + k * largura for i in range(len(modos))]
        ax.bar(pos, med, largura, label=rot, alpha=0.6 + 0.3 * k, color="#4a5568")
        for p, v in zip(pos, med):
            ax.text(p, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks([i + largura * (len(campos) - 1) / 2 for i in range(len(modos))])
    ax.set_xticklabels([NOMES[m] for m in modos])
    ax.set_title(titulo, fontsize=11)
    ax.set_ylabel(ylabel)
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)


def desenhar(ax, dados, qual):
    if qual == 1:
        grupo(ax, dados, ["rotas_r1", "rotas_total"], ["em R1", "nos 5 roteadores"],
              "Tamanho da tabela de rotas", "rotas no kernel")
    elif qual == 2:
        barras(ax, dados, "ctrl_Bps", "Tráfego de controle em R1 (regime permanente)",
               "bytes por segundo", fmt="{:.0f}")
    elif qual == 3:
        barras(ax, dados, "rtt_ms", "Delay fim a fim h1 → h3 (RTT médio)", "ms")
    elif qual == 4:
        barras(ax, dados, "outage_s", "Tempo sem resposta após queda de R1–R2",
               "segundos (escala log)", fmt="{:.2f}", log=True)


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "metricas.csv"
    dados = ler(csv_path)
    if not dados:
        sys.exit(f"Nenhum dado em {csv_path}.")
    Path("graficos").mkdir(exist_ok=True)
    nomes = ["01_tabela_rotas", "02_controle", "03_rtt", "04_convergencia"]
    for i, nome in enumerate(nomes, 1):
        fig, ax = plt.subplots(figsize=(5.5, 4))
        desenhar(ax, dados, i)
        fig.tight_layout()
        fig.savefig(f"graficos/{nome}.png", dpi=160)
        plt.close(fig)
    fig, axs = plt.subplots(2, 2, figsize=(11, 8))
    for i, ax in enumerate(axs.flat, 1):
        desenhar(ax, dados, i)
    fig.suptitle("OSPF x RIP x algoritmo próprio", fontsize=13)
    fig.tight_layout()
    fig.savefig("graficos/comparacao.png", dpi=160)
    print("Gráficos salvos em graficos/")


if __name__ == "__main__":
    main()
