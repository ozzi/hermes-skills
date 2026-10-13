#!/usr/bin/env bash
# Отчёт о расходе и балансе с озвучкой на Bluesound. Задание cron (no_agent).
#
# Режим auto: в выходные молчит, в первый рабочий день после выходных
# отчитывается за прошедшие выходные, пропущенные дни не теряются.
# При успехе скрипт тоже молчит — любой вывод задания означает сбой
# (пустой stdout = задание ничего не отправило).
set -uo pipefail

SCRIPT="$HOME/.hermes/scripts/usage_report.py"
LOG="$HOME/.hermes/cron/output/usage-report.log"
mkdir -p "$(dirname "$LOG")"

out=$(/usr/bin/env python3 "$SCRIPT" "${1:-auto}" --announce 2>&1)
rc=$?
printf '%s | rc=%s | %s\n' "$(date -Is)" "$rc" "$(printf '%s' "$out" | tr '\n' ' ')" >> "$LOG"

if [ "$rc" -ne 0 ] \
  || printf '%s' "$out" | grep -qiE 'Traceback|данных нет|! [[:alpha:]]|озвучка: rc=[^0]'; then
  printf '%s\n' "$out"
  exit 1
fi
