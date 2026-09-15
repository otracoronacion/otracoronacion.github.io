#!/usr/bin/env bash
# ¿Le toca trabajar a ESTA corrida?
#
# Desde el 27/08 GitHub encola los crons entre 3 y 11 horas, así que ya no
# alcanza con un disparo: hay varios escalonados y este guardia elige cuál
# trabaja. La regla es una sola línea:
#
#     corre la PRIMERA corrida que aterrice a partir de las 07 ART
#     en un día que todavía no se publicó.
#
# Todas las demás salen en 10 segundos sin scrapear ni llamar a la IA (gratis).
# Si ninguna llega temprano, la última igual publica: más vale tarde que nunca.
#
# Uso:  turno.sh <github.event_name> [ruta_del_marcador]
# Test: TURNO_HOY=2026-09-16 TURNO_HORA=6 turno.sh schedule
set -euo pipefail

ART="America/Argentina/Buenos_Aires"
UMBRAL=7   # "despertate coronado", no "trasnochate coronado"

evento="${1:-schedule}"
marcador="${2:-data/ultima_corrida.txt}"

hoy="${TURNO_HOY:-$(TZ=$ART date +%F)}"
hora="${TURNO_HORA:-$(TZ=$ART date +%-H)}"

if [ "$evento" != "schedule" ]; then
  echo "Disparo manual ($evento): corre sin mirar el reloj."
  echo "correr=true"; exit 0
fi

# `if` explícito y no `[ -f ] && ...`: bajo `set -e` esa forma puede cortar el
# script cuando el archivo no existe — justo el caso del primer día.
ultima=""
if [ -f "$marcador" ]; then
  ultima=$(head -1 "$marcador" | cut -d' ' -f1)
fi

if [ "$ultima" = "$hoy" ]; then
  echo "El $hoy ya se publicó. Esta corrida no hace nada."
  echo "correr=false"; exit 0
fi

if [ "$hora" -lt "$UMBRAL" ]; then
  echo "Son las ${hora}h ART: muy temprano (umbral ${UMBRAL}h). Le dejo el turno al próximo cron."
  echo "correr=false"; exit 0
fi

echo "Son las ${hora}h ART y el $hoy sigue sin publicarse. Le toca a esta corrida."
echo "correr=true"
