#!/usr/bin/env bash
#
# aplicar-delays.sh — coloca atraso (tc netem) nos enlaces roteador-roteador.
#
# Uso (de dentro da pasta do lab, com o lab no ar):
#   ./aplicar-delays.sh           # aplica os atrasos
#   ./aplicar-delays.sh limpar    # remove todos
#   ./aplicar-delays.sh mostrar   # lista o que está configurado
#
# O atraso é aplicado nas DUAS pontas de cada enlace, então o RTT medido pelo ping
# é o dobro do valor abaixo (ex.: R1-R3 = 30 ms por direção -> RTT ~60 ms).
# Os atrasos somem quando o lab é destruído: rode de novo depois de cada deploy.
# Os mesmos atrasos valem para OSPF, RIP e algoritmo próprio (comparação justa).

set -euo pipefail

# enlace -> atraso por direção, em ms
declare -A ATRASO=(
  [R1-R2]=5
  [R2-R3]=5
  [R3-R4]=20
  [R4-R5]=5
  [R5-R1]=10
  [R1-R3]=30    # a diagonal é a mais lenta de propósito
)

# enlace -> "roteador:interface roteador:interface" (conforme topo.clab.yml)
declare -A PONTAS=(
  [R1-R2]="r1:eth1 r2:eth1"
  [R2-R3]="r2:eth2 r3:eth1"
  [R3-R4]="r3:eth2 r4:eth1"
  [R4-R5]="r4:eth2 r5:eth1"
  [R5-R1]="r5:eth2 r1:eth2"
  [R1-R3]="r1:eth3 r3:eth3"
)

ORDEM=(R1-R2 R2-R3 R3-R4 R4-R5 R5-R1 R1-R3)
ACAO="${1:-aplicar}"

if ! docker exec clab-lab-fso-r1 tc -V >/dev/null 2>&1; then
  echo "ERRO: 'tc' não existe nos roteadores. Adicione iproute2-tc ao Dockerfile.router,"
  echo "refaça o build da imagem e o deploy do lab."
  exit 1
fi

for link in "${ORDEM[@]}"; do
  for ponta in ${PONTAS[$link]}; do
    r="${ponta%%:*}"; iface="${ponta##*:}"; c="clab-lab-fso-$r"
    case "$ACAO" in
      aplicar)
        docker exec "$c" tc qdisc replace dev "$iface" root netem delay "${ATRASO[$link]}ms"
        ;;
      limpar)
        docker exec "$c" tc qdisc del dev "$iface" root 2>/dev/null || true
        ;;
      mostrar)
        echo "$link  $r/$iface: $(docker exec "$c" tc qdisc show dev "$iface" | head -n1)"
        ;;
      *) echo "Uso: $0 [aplicar|limpar|mostrar]"; exit 1 ;;
    esac
  done
  [[ "$ACAO" == "aplicar" ]] && echo "$link: ${ATRASO[$link]} ms por direção (RTT ~$((2 * ATRASO[$link])) ms)"
done
[[ "$ACAO" == "limpar" ]] && echo "Atrasos removidos."
exit 0
