#!/usr/bin/env bash
# Отчёт о расходе и балансе с озвучкой на Bluesound. Задание cron (no_agent).
#
# Режим auto: в выходные молчит, в первый рабочий день после выходных
# отчитывается за прошедшие выходные, пропущенные дни не теряются.
# При успехе скрипт тоже молчит — любой вывод задания означает сбой
# (пустой stdout = задание ничего не отправило).
set -uo pipefail

SCRIPT="$(dirname "$0")/usage_report.py"
LOG="$HOME/.hermes/cron/output/usage-report.log"
mkdir -p "$(dirname "$LOG")"

out=$(/usr/bin/env python3 "$SCRIPT" "${1:-auto}" --announce 2>&1)
rc=$?
printf '%s | rc=%s | %s\n' "$(date -Is)" "$rc" "$(printf '%s' "$out" | tr '\n' ' ')" >> "$LOG"

# Тишина по замыслу (выходной, skip_dates) — это тоже успех: скрипт возвращает 0 и
# ничего не печатает. Раньше такой прогон падал в «нет ok=True» и выглядел сбоем.
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
  exit 0
fi

# Озвучка может сорваться с кодом 0 (скрипт вернул отчёт без признака успеха),
# поэтому мало смотреть на rc: отдельно требуем подтверждение ok=True.
if [ "$rc" -ne 0 ] \
  || printf '%s' "$out" | grep -qiE 'Traceback|данных нет|! [[:alpha:]]' \
  || ! printf '%s' "$out" | grep -q 'ok=True'; then
  printf '%s\n' "$out"
  exit 1
fi
