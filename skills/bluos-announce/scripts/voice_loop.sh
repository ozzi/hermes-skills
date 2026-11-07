#!/usr/bin/env bash
# Повторять голосовой файл на Bluesound до заданного часа.
#   VOICE_FILE=/path/voice.mp3 ./voice_loop.sh [до_часа=23] [интервал_сек=180]
# Гонять отвязанным от сессии:
#   VOICE_FILE=… setsid nohup bash voice_loop.sh 23 180 >> loop.log 2>&1 & disown
# Файл в www после каждой итерации удаляется (--no-keep) — каталог публичен.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
VOICE_FILE="${VOICE_FILE:-/home/hermes/.hermes/cache/bluos_voice/voice.mp3}"
UNTIL_HOUR="${1:-23}"
INTERVAL="${2:-180}"
PLAYER="${PLAYER:-$HERE/hermes_bluos_playfile.py}"

[ -s "$VOICE_FILE" ] || echo "$(date '+%F %T') нет файла $VOICE_FILE — ожидаю"
echo "$(date '+%F %T') старт pid=$$ интервал=${INTERVAL}s до ${UNTIL_HOUR}:00"

while :; do
  # 10# обязателен: 08 и 09 в bash читаются как восьмеричные и валят арифметику
  now=$((10#$(date +%H)))
  if [ "$now" -ge "$UNTIL_HOUR" ]; then
    echo "$(date '+%F %T') доиграли до ${UNTIL_HOUR}:00 — выхожу"
    break
  fi
  if [ -s "$VOICE_FILE" ]; then
    out=$("$PLAYER" --file "$VOICE_FILE" --no-keep 2>&1)
    rc=$?
    echo "$(date '+%F %T') rc=$rc $(echo "$out" | tr -d '\n' | tail -c 300)"
  else
    echo "$(date '+%F %T') файла ещё нет"
  fi
  sleep "$INTERVAL"
done
