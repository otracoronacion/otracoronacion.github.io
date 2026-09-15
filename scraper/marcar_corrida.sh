#!/usr/bin/env bash
# Deja constancia de cuándo aterrizó esta corrida y qué cron la disparó.
# El `git log` de data/ultima_corrida.txt es todo nuestro registro: los logs de
# Actions caducan y no siempre se pueden bajar. Además es lo que lee turno.sh
# para saber si el día ya se publicó.
#
# Uso: marcar_corrida.sh "<github.event.schedule>" "<github.event_name>"
set -euo pipefail

ART="America/Argentina/Buenos_Aires"
marcador="${MARCADOR:-data/ultima_corrida.txt}"
cron="${1:-}"
evento="${2:-manual}"

detalle="disparo $evento"
if [ -n "$cron" ]; then
  min=$(echo "$cron" | awk '{print $1}')
  hor=$(echo "$cron" | awk '{print $2}')
  prog=$(( 10#$hor * 60 + 10#$min ))
  ahora=$(( 10#$(date -u +%H) * 60 + 10#$(date -u +%M) ))
  demora=$(( ahora - prog ))
  [ "$demora" -lt 0 ] && demora=$(( demora + 1440 ))
  detalle=$(printf 'cron %02d:%02d UTC, demora %d min' "$((10#$hor))" "$((10#$min))" "$demora")
fi

mkdir -p "$(dirname "$marcador")"
echo "$(TZ=$ART date '+%F %H:%M') ART — $detalle" > "$marcador"
cat "$marcador"
