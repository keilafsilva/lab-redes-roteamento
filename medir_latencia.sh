#!/bin/bash
# Mede a latência real (RTT médio) de cada link roteador-roteador do laboratório,
# usando ping diretamente entre as interfaces de enlace. Salva em latencias.json
# no formato que o algoritmo_proprio.py espera.
#
# Rode de dentro da pasta lab-fso, com o lab no ar.

set -e
OUT="latencias.json"
PINGS=10          # 10 pings a cada 0,2 s = ~2 s por link
INTERVALO=0.2     # (iputils só aceita < 0,2 s como root)

# enlace -> "contêiner_origem ip_do_vizinho"
declare -A LINKS=(
  ["R1-R2"]="clab-lab-fso-r1 10.0.12.2"
  ["R2-R3"]="clab-lab-fso-r2 10.0.23.2"
  ["R3-R4"]="clab-lab-fso-r3 10.0.34.2"
  ["R4-R5"]="clab-lab-fso-r4 10.0.45.2"
  ["R5-R1"]="clab-lab-fso-r5 10.0.51.2"
  ["R1-R3"]="clab-lab-fso-r1 10.0.13.2"
)

echo "{" > "$OUT"
first=true
for link in R1-R2 R2-R3 R3-R4 R4-R5 R5-R1 R1-R3; do
  read -r container target <<< "${LINKS[$link]}"
  echo "Medindo $link ($container -> $target)..."
  saida=$(timeout 15 docker exec "$container" ping -c "$PINGS" -i "$INTERVALO" -W 1 -q "$target" 2>&1) || true
  # pega o 2º número (média) da linha "rtt min/avg/max/mdev = a/b/c/d ms"
  avg=$(echo "$saida" | grep -oP '=\s*\K[0-9.]+/[0-9.]+' | cut -d/ -f2 || true)
  if [ -z "$avg" ]; then
    echo "  AVISO: não consegui extrair a latência de $link. Saída do ping:"
    echo "$saida" | sed 's/^/    /'
    avg="0.1"
  else
    echo "  média: $avg ms"
  fi

  if [ "$first" = true ]; then first=false; else echo "," >> "$OUT"; fi
  printf '  "%s": %s' "$link" "$avg" >> "$OUT"
done
echo "" >> "$OUT"
echo "}" >> "$OUT"

echo ""
echo "Latências salvas em $OUT:"
cat "$OUT"
