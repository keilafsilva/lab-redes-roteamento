#!/usr/bin/env bash
#
# switch-protocol.sh — Controlador de Protocolos de Roteamento (FSO)
#
# Uso (de dentro da pasta do lab, com o lab no ar):
#   ./switch-protocol.sh ospf
#   ./switch-protocol.sh rip
#   ./switch-protocol.sh proprio

set -euo pipefail
cd "$(dirname "$0")"

PROTOCOLO="${1:-}"

if [[ "$PROTOCOLO" != "ospf" && "$PROTOCOLO" != "rip" && "$PROTOCOLO" != "proprio" ]]; then
  echo "Uso: $0 {ospf|rip|proprio}"
  exit 1
fi

ROTEADORES=("r1" "r2" "r3" "r4" "r5")

# ---------------------------------------------------------------------
# 1. Endereçamento das redes de acesso (idempotente)
# ---------------------------------------------------------------------
garantir_infra_acesso() {
  echo "=== Garantindo endereçamento LAN e gateways dos hosts ==="
  docker exec clab-lab-fso-r1 ip addr add 192.168.1.1/24 dev eth4 2>/dev/null || true
  docker exec clab-lab-fso-r2 ip addr add 192.168.2.1/24 dev eth3 2>/dev/null || true
  docker exec clab-lab-fso-r3 ip addr add 192.168.3.1/24 dev eth4 2>/dev/null || true
  docker exec clab-lab-fso-r4 ip addr add 192.168.4.1/24 dev eth3 2>/dev/null || true
  docker exec clab-lab-fso-r5 ip addr add 192.168.5.1/24 dev eth3 2>/dev/null || true

  for i in 1 2 3 4 5; do
    docker exec clab-lab-fso-h$i ip addr add 192.168.$i.10/24 dev eth1 2>/dev/null || true
    docker exec clab-lab-fso-h$i ip link set eth1 up 2>/dev/null || true
    docker exec clab-lab-fso-h$i ip route replace default via 192.168.$i.1 dev eth1 2>/dev/null || true
  done
}

# ---------------------------------------------------------------------
# 2. Limpeza das rotas de redes 192.168.X.0/24 instaladas por outro modo.
#    Só remove rotas com "via" — NUNCA a rota conectada (dev ethX scope link)
#    da LAN do próprio roteador.
# ---------------------------------------------------------------------
limpar_rotas_kernel() {
  echo "Limpando rotas 'via' das redes de acesso (mantendo as conectadas)..."
  for r in "${ROTEADORES[@]}"; do
    for i in 1 2 3 4 5; do
      docker exec "clab-lab-fso-$r" sh -c \
        "if ip route show 192.168.$i.0/24 | grep -q via; then ip route del 192.168.$i.0/24; fi" || true
    done
  done
}

# ---------------------------------------------------------------------
# 3. OSPF / RIP: copia o template NO HOST para active.conf (que é o arquivo
#    montado em /etc/frr/frr.conf) e reinicia só os processos do FRR.
#    (os templates frr-*.conf NÃO existem dentro do contêiner)
# ---------------------------------------------------------------------
ativar_frr_protocolo() {
  local proto="$1"
  echo "=== Ativando protocolo FRR: ${proto^^} ==="

  limpar_rotas_kernel

  for r in "${ROTEADORES[@]}"; do
    if [[ ! -f "configs/$r/frr-${proto}.conf" ]]; then
      echo "ERRO: configs/$r/frr-${proto}.conf não encontrado"; exit 1
    fi
    cp "configs/$r/frr-${proto}.conf" "configs/$r/active.conf"   # mesma inode: o bind mount enxerga
    echo "Aplicando ${proto^^} em $r (frr-reload, sem reiniciar nada)..."
    # NUNCA use frrinit.sh stop/restart aqui: ele derruba o PID 1 do contêiner, o Docker
    # reinicia o contêiner e os enlaces eth1..eth3 do containerlab somem.
    if ! docker exec "clab-lab-fso-$r" python3 /usr/lib/frr/frr-reload.py --reload /etc/frr/frr.conf \
         >"/tmp/frr-reload-$r.log" 2>&1; then
      echo "AVISO: frr-reload retornou erro em $r:"; tail -n 15 "/tmp/frr-reload-$r.log"
    fi
  done

  echo "Protocolo ${proto^^} ativado. Aguarde a convergência (RIP: até ~30 s; OSPF: ~10-40 s)."
  echo "Confira: docker exec clab-lab-fso-r1 vtysh -c 'show ip route'"
}

# ---------------------------------------------------------------------
# 4. Algoritmo próprio: derruba o FRR (nada de OSPF/RIP), mede latências
#    e deixa o algoritmo_proprio.py instalar as rotas no kernel.
# ---------------------------------------------------------------------
ativar_algoritmo_proprio() {
  echo "=== Ativando Algoritmo Próprio de Roteamento ==="

  # Desliga OSPF e RIP só na configuração em execução. Os daemons continuam de pé:
  # parar o FRR com frrinit.sh derruba o contêiner e destrói os enlaces do lab.
  for r in "${ROTEADORES[@]}"; do
    echo "Removendo OSPF/RIP de $r..."
    # uma chamada do vtysh por protocolo, e só se ele estiver configurado: o vtysh aborta os
    # -c seguintes quando um comando falha (ex.: "no router ospf" sem OSPF configurado),
    # e o "no router rip" nunca chegava a rodar.
    saida=""
    for p in ospf rip; do
      if docker exec "clab-lab-fso-$r" vtysh -c "show running-config" 2>/dev/null | grep -q "^router $p"; then
        saida+=$(docker exec "clab-lab-fso-$r" vtysh -c "configure terminal" -c "no router $p" 2>&1) || true
      fi
    done
    restante=$(docker exec "clab-lab-fso-$r" vtysh -c "show running-config" 2>/dev/null | grep -E "^router (ospf|rip)" || true)
    if [[ -n "$restante" ]]; then
      echo "  ERRO: '$restante' continua ativo em $r. Saída do vtysh:"
      echo "$saida" | sed 's/^/    /'
      exit 1
    fi
  done
  sleep 2   # o zebra remove as rotas aprendidas

  limpar_rotas_kernel

  if [[ -f "medir_latencia.sh" ]]; then
    echo "Medindo latências dos enlaces..."
    bash ./medir_latencia.sh
  else
    echo "AVISO: medir_latencia.sh não encontrado; usando latencias.json existente."
  fi

  echo "Calculando e aplicando rotas do algoritmo próprio..."
  python3 algoritmo_proprio.py

  echo "Algoritmo próprio ativo."
  echo "Para testar:            python3 algoritmo_proprio.py --testar"
  echo "Para reagir a falhas:   python3 algoritmo_proprio.py --watch   (deixe rodando em outro terminal)"
}

# ---------------------------------------------------------------------
verificar_lab() {
  echo "=== Verificando interfaces dos roteadores ==="
  local ok=1
  for r in "${ROTEADORES[@]}"; do
    for i in eth1 eth2 eth3; do
      if ! docker exec "clab-lab-fso-$r" ip link show "$i" >/dev/null 2>&1; then
        echo "  FALTA $i em $r"; ok=0
      fi
    done
  done
  if [[ $ok -eq 0 ]]; then
    echo "ERRO: os enlaces do containerlab não existem nos contêineres."
    echo "Refaça o lab: sudo containerlab destroy -t topo.clab.yml --cleanup && sudo containerlab deploy -t topo.clab.yml"
    exit 1
  fi
}

verificar_lab
garantir_infra_acesso

case "$PROTOCOLO" in
  ospf)    ativar_frr_protocolo "ospf" ;;
  rip)     ativar_frr_protocolo "rip" ;;
  proprio) ativar_algoritmo_proprio ;;
esac
