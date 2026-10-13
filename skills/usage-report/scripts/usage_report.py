#!/usr/bin/env python3
"""Расход и баланс провайдеров → русская фраза → озвучка на акустике.

Собирает данные из одного или нескольких источников (сейчас: openrouter, deepseek),
складывает фразу и передаёт её на озвучку скрипту навыка bluos-announce
(`scripts/hermes_announce.py`). Без `--announce` только печатает текст.

Периоды: yesterday | today | week | month — для ручного запуска; auto — для
регулярного: считается по календарю (в выходные молчит, в первый рабочий день
после выходных отчитывается за прошедшие выходные), а пропущенные дни не теряются,
потому что окно тянется от конца последнего удачно озвученного отчёта.

Секреты берутся ТОЛЬКО из переменных окружения процесса; файлы с ключами
скрипт не открывает. Адреса, набор источников и путь к озвучке — в локальном
конфиге (секретов в нём нет).

Примеры:
    usage_report.py                        # вчера, источники из конфига, напечатать
    usage_report.py week --announce        # неделя + озвучить
    usage_report.py auto --announce        # календарный режим (для задания)
    usage_report.py today --json           # машинный вывод
    usage_report.py --list-sources
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CONFIG_PATH = os.path.expanduser(
    os.environ.get("USAGE_REPORT_CONFIG", "~/.config/hermes-usage-report/config.json"))
STATE_PATH = os.path.expanduser(
    os.environ.get("USAGE_REPORT_STATE", "~/.config/hermes-usage-report/state.json"))
DEFAULT_ANNOUNCE_SCRIPT = "~/.hermes/scripts/hermes_announce.py"
CATCHUP_LIMIT_DAYS = 14      # насколько глубоко догонять пропущенные дни

PERIODS = {
    "yesterday": {"phrase": "за вчера",
                  "aliases": ["yesterday", "вчера", "вчерашние", "сутки", "прошедшие сутки"]},
    "today": {"phrase": "за сегодня",
              "aliases": ["today", "сегодня", "с начала суток"]},
    "week": {"phrase": "на этой неделе",
             "aliases": ["week", "неделя", "эта неделя"]},
    "month": {"phrase": "за этот месяц",
              "aliases": ["month", "месяц", "этот месяц"]},
    "auto": {"phrase": None,          # фраза определяется окном, а не периодом
             "aliases": ["auto", "авто", "календарь"]},
}


# --------------------------------------------------------------------------- #
# Русские числительные: без них фразу на слух не понять
# --------------------------------------------------------------------------- #

_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
            "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
            "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_UNITS_F = ["", "одна", "две"] + _UNITS_M[3:]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]

# (делитель, формы существительного, женский род группы)
_GROUPS = [
    (10 ** 9, ("миллиард", "миллиарда", "миллиардов"), False),
    (10 ** 6, ("миллион", "миллиона", "миллионов"), False),
    (10 ** 3, ("тысяча", "тысячи", "тысяч"), True),
]

_CURRENCIES = {
    "USD": ("доллар", "доллара", "долларов", "цент", "цента", "центов", False),
    "EUR": ("евро", "евро", "евро", "цент", "цента", "центов", False),
    "RUB": ("рубль", "рубля", "рублей", "копейка", "копейки", "копеек", True),
    "CNY": ("юань", "юаня", "юаней", "фэнь", "фэня", "фэней", False),
}


def ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова по числу: 1 доллар / 2 доллара / 5 долларов."""
    rest = abs(int(n)) % 100
    if 11 <= rest <= 14:
        return many
    rest %= 10
    if rest == 1:
        return one
    if 2 <= rest <= 4:
        return few
    return many


def ru_under_1000(n: int, female: bool = False) -> str:
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
    if n:
        words.append((_UNITS_F if female else _UNITS_M)[n])
    return " ".join(w for w in words if w)


def ru_num(n: int, female_last: bool = False) -> str:
    """Целое число словами с названиями разрядов (до 10^12).

    `female_last` — для счётных слов женского рода: «одна тысяча», «две тысячи».
    """
    n = int(n)
    if n == 0:
        return "ноль"
    out: list[str] = []
    for div, forms, female in _GROUPS:
        if n >= div:
            part, n = n // div, n % div
            out.append(f"{ru_under_1000(part, female)} {ru_plural(part, *forms)}")
    if n:
        out.append(ru_under_1000(n, female_last))
    return " ".join(out)


def ru_count(n: int, one: str, few: str, many: str) -> str:
    """«двадцать одна тысяча» — слово по хвосту числа."""
    return f"{ru_num(n)} {ru_plural(int(n) % 1000, one, few, many)}"


def ru_tokens(n: int) -> str:
    """Токены — округлённо: на слух нужен порядок, а не каждая единица.

    До миллиона — тысячи, дальше — целые миллионы. Точное значение остаётся в --json.
    """
    n = int(n)
    if n >= 950_000:
        millions = max(1, int(round(n / 1_000_000)))
        return f"{ru_num(millions)} {ru_plural(millions, 'миллион', 'миллиона', 'миллионов')} токенов"
    if n >= 1_000:
        thousands = int(round(n / 1_000))
        return (f"{ru_num(thousands, female_last=True)} "
                f"{ru_plural(thousands, 'тысяча', 'тысячи', 'тысяч')} токенов")
    return ru_count(n, "токен", "токена", "токенов")


def ru_money(amount: float, currency: str = "USD") -> str:
    """7.64 USD → «семь долларов шестьдесят четыре цента»."""
    cur = _CURRENCIES.get((currency or "USD").upper(),
                          (currency, currency, currency, "", "", "", False))
    whole_s, frac_s = f"{abs(amount):.2f}".split(".")
    whole, frac = int(whole_s), int(frac_s)
    while frac and frac % 10 == 0:
        frac //= 10
    parts = []
    if whole or not frac:
        parts.append(f"{ru_num(whole)} {ru_plural(whole, cur[0], cur[1], cur[2])}")
    if frac:
        parts.append(f"{ru_under_1000(frac, cur[6])} {ru_plural(frac, cur[3], cur[4], cur[5])}")
    return ("минус " if amount < 0 else "") + " ".join(parts)


# --------------------------------------------------------------------------- #
# Периоды: окно + готовая фраза для озвучки
# --------------------------------------------------------------------------- #

def midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def period_window(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    """Границы периода (в локальной зоне машины) и фраза для озвучки."""
    day0 = midnight(now)
    if period == "yesterday":
        return day0 - timedelta(days=1), day0, PERIODS["yesterday"]["phrase"]
    if period == "today":
        return day0, now + timedelta(minutes=1), PERIODS["today"]["phrase"]
    if period == "week":                       # неделя с понедельника
        return (day0 - timedelta(days=day0.weekday()), now + timedelta(minutes=1),
                PERIODS["week"]["phrase"])
    if period == "month":                      # с 1-го числа
        return day0.replace(day=1), now + timedelta(minutes=1), PERIODS["month"]["phrase"]
    raise ValueError(f"неизвестный период: {period}")


def auto_window(now: datetime, state: dict, skip_dates: set[str]) -> tuple | None:
    """Календарное окно для регулярного отчёта.

    Возвращает (start, end, phrase) либо None, если озвучивать сегодня нечего.
    Выходные и даты из skip_dates пропускаются, но их расход НЕ теряется: окно
    тянется от конца последнего удачно озвученного отчёта (state["last_end"]),
    поэтому первый рабочий день после выходных закрывает и выходные.
    """
    day0 = midnight(now)
    if now.weekday() >= 5 or day0.date().isoformat() in skip_dates:
        return None                                    # нерабочий день: молчим

    start = day0 - timedelta(days=3 if now.weekday() == 0 else 1)   # пн забирает выходные
    last_end = state.get("last_end")
    if last_end:
        try:
            stamp = datetime.fromisoformat(last_end).astimezone()
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None:
            if stamp >= day0:
                return None                                # за этот день уже отчитались
            start = max(stamp, day0 - timedelta(days=CATCHUP_LIMIT_DAYS))

    span = (day0 - start).days
    covered = [start.date() + timedelta(days=i) for i in range(span)]
    workdays = [d for d in covered if d.weekday() < 5]
    if not workdays:
        phrase = "за прошедшие выходные"          # включая случай span == 1 (суббота)
    elif span == 1:
        phrase = PERIODS["yesterday"]["phrase"]
    elif len(workdays) == 1 and workdays[0].weekday() == 4:
        phrase = "за пятницу и прошедшие выходные"
    else:
        phrase = "за последние " + ru_count(span, "день", "дня", "дней")
    return start, day0, phrase


def resolve_period(value: str | None) -> str:
    if not value:
        return "yesterday"
    low = value.strip().lower()
    for name, meta in PERIODS.items():
        if low == name or low in [a.lower() for a in meta["aliases"]]:
            return name
    raise SystemExit(f"период «{value}» неизвестен. Доступно: " + ", ".join(PERIODS))


# --------------------------------------------------------------------------- #
# Источники данных (реестр — точка расширения)
# --------------------------------------------------------------------------- #

SOURCES: dict[str, dict] = {}


class SourceError(RuntimeError):
    """Источник не смог отдать данные: нет ключа, отказ API, сеть."""


def source(name: str, key_env: str, title: str, currency: str = "USD"):
    """Регистрация источника. Новый провайдер = одна такая функция."""
    def deco(fn):
        SOURCES[name] = {"collect": fn, "key_env": key_env, "title": title,
                         "currency": currency}
        return fn
    return deco


def http_json(url: str, key: str, payload: dict | None = None, timeout: float = 40.0,
              retries: int = 1) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last: Exception = SourceError("нет ответа")
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = SourceError(f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:160]}")
        except Exception as exc:  # noqa: BLE001
            last = SourceError(str(exc))
    raise last


def secret(key_env: str) -> str:
    """Секрет — только из окружения процесса."""
    val = os.environ.get(key_env, "")
    if not val:
        raise SourceError(f"нет {key_env} в окружении")
    return val


@source("openrouter", "OPENROUTER_MGMT_KEY", "Опенроутер")
def collect_openrouter(cfg: dict, window: tuple, now: datetime) -> dict:
    """Расход за окно + остаток баланса аккаунта.

    `analytics/query` (management key) принимает произвольный ISO-интервал, поэтому
    локальные сутки считаются точно; `/activity` умеет только завершённые UTC-дни.
    Баланс — `/credits`: у management-ключа `limit_remaining` пуст, а баланс
    аккаунта отдаётся только там.
    """
    key = secret(cfg["key_env"])
    base = (cfg.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    start, end = window[0], window[1]
    iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = http_json(f"{base}/analytics/query", key, {
        "metrics": ["total_usage", "tokens_total", "tokens_prompt", "tokens_completion",
                    "request_count"],
        "granularity": "hour",
        "time_range": {"start": iso(start), "end": iso(end)},
    })
    rows = (body.get("data") or {}).get("data") or []
    credits = http_json(f"{base}/credits", key).get("data", {})
    return {
        "title": cfg.get("title", "Опенроутер"),
        "currency": cfg.get("currency", "USD"),
        "spend": round(sum(float(r.get("total_usage") or 0) for r in rows), 6),
        "tokens": sum(int(float(r.get("tokens_total") or 0)) for r in rows),
        "tokens_prompt": sum(int(float(r.get("tokens_prompt") or 0)) for r in rows),
        "tokens_completion": sum(int(float(r.get("tokens_completion") or 0)) for r in rows),
        "requests": sum(int(float(r.get("request_count") or 0)) for r in rows),
        "balance": round(float(credits.get("total_credits", 0))
                         - float(credits.get("total_usage", 0)), 4),
        "window": [start.isoformat(), end.isoformat()],
    }


@source("deepseek", "DEEPSEEK_API_KEY", "Дипсик")
def collect_deepseek(cfg: dict, window: tuple, now: datetime) -> dict:
    """Только баланс: публичной ручки расхода за период у DeepSeek нет.

    Образец источника без части полей — фраза не упоминает отсутствующее.
    """
    key = secret(cfg["key_env"])
    base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    infos = http_json(f"{base}/user/balance", key).get("balance_infos") or []
    if not infos:
        raise SourceError("пустой ответ /user/balance")
    info = infos[0]
    return {"title": cfg.get("title", "Дипсик"),
            "currency": info.get("currency") or cfg.get("currency", "USD"),
            "balance": float(info.get("total_balance") or 0)}


# --------------------------------------------------------------------------- #
# Фраза
# --------------------------------------------------------------------------- #

def cbr_rate(currency: str = "USD") -> float | None:
    """Курс ЦБ РФ (публичный XML, без ключей)."""
    try:
        req = urllib.request.Request("https://www.cbr.ru/scripts/XML_daily.asp",
                                     headers={"User-Agent": "usage-report"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode("cp1251", "replace")
    except Exception:  # noqa: BLE001
        return None
    for chunk in xml.split("<Valute ")[1:]:
        if chunk.split("<CharCode>")[1].split("</CharCode>")[0].upper() != currency.upper():
            continue
        value = chunk.split("<Value>")[1].split("</Value>")[0].replace(",", ".")
        nominal = float(chunk.split("<Nominal>")[1].split("</Nominal>")[0].replace(",", "."))
        return float(value) / nominal
    return None


def compose(items: list[dict], phrase: str, convert: str | None = None) -> str:
    """Фраза для озвучки: по одному предложению на источник."""
    sentences = []
    for it in items:
        bits = [f"{it['title']}: {phrase}"]
        if it.get("spend") is not None:
            if not it["spend"] and not it.get("requests"):
                bits.append("расхода не было")      # нулевая трата ≠ «потрачено ноль долларов»
            else:
                bits.append(f"потрачено {ru_money(it['spend'], it.get('currency', 'USD'))}")
        if it.get("tokens"):
            bits.append("сожжено " + ru_tokens(it["tokens"]))
        if it.get("balance") is not None:
            bits.append("остаток баланса — " + ru_money(it["balance"], it.get("currency", "USD")))
        line = ", ".join(bits) + "."
        if convert and it.get("spend") and it.get("currency", "USD") != convert:
            rate = cbr_rate(it.get("currency", "USD"))
            if rate:
                line += (" По курсу ЦБ это примерно "
                         + ru_money(it["spend"] * rate, convert) + ".")
        sentences.append(line)
    return " ".join(sentences)


# --------------------------------------------------------------------------- #
# Озвучка и состояние
# --------------------------------------------------------------------------- #

def announce(text: str, cfg: dict) -> dict:
    """Отдать фразу скрипту озвучки (навык bluos-announce) и разобрать его отчёт."""
    script = os.path.expanduser(cfg.get("script") or DEFAULT_ANNOUNCE_SCRIPT)
    if not os.path.exists(script):
        raise SystemExit(f"скрипт озвучки не найден: {script} "
                         "(нужен навык bluos-announce или --announce-script)")
    cmd = [sys.executable, script, text]
    for extra in cfg.get("args") or []:
        cmd.append(str(extra))
    for flag, value in (cfg.get("flags") or {}).items():
        cmd += [f"--{flag}", str(value)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "").strip()
    report = None
    try:
        report = json.loads(out) if out else None
    except json.JSONDecodeError:
        pass
    ok = bool(report and report.get("after"))
    return {"rc": proc.returncode, "ok": ok, "report": report,
            "stderr": (proc.stderr or "")[-300:] or None}


def read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(path: str, end: datetime) -> None:
    """Запомнить конец озвученного окна: от него считается следующий отчёт."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"last_end": end.isoformat(), "updated_at": datetime.now().isoformat()},
                  fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} — не разобрать как JSON: {exc}")


def build_sources(config: dict, requested: list[str] | None) -> dict:
    """Включённые источники: аргумент CLI → конфиг → openrouter."""
    conf = config.get("sources") or []
    if isinstance(conf, dict):
        enabled = [n for n, c in conf.items() if (c or {}).get("enabled", True)]
    else:
        enabled = [n for n in conf if n]
    names = requested or enabled or ["openrouter"]
    out: dict[str, dict] = {}
    for name in names:
        if name not in SOURCES:
            raise SystemExit(f"источник «{name}» неизвестен; доступные: "
                             + ", ".join(sorted(SOURCES)))
        cfg = {"key_env": SOURCES[name]["key_env"], "title": SOURCES[name]["title"],
               "currency": SOURCES[name]["currency"]}
        block = conf.get(name) if isinstance(conf, dict) else None
        if isinstance(block, dict):
            cfg.update({k: v for k, v in block.items() if v is not None})
        out[name] = cfg
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Расход и баланс провайдеров → озвучка")
    ap.add_argument("period", nargs="?",
                    help="yesterday | today | week | month | auto (можно по-русски); "
                         "по умолчанию yesterday")
    ap.add_argument("--config", default=CONFIG_PATH,
                    help=f"файл настроек (по умолчанию {CONFIG_PATH})")
    ap.add_argument("--state-file", default=STATE_PATH,
                    help=f"состояние режима auto (по умолчанию {STATE_PATH})")
    ap.add_argument("--source", action="append",
                    help="источник; можно несколько, напр. --source openrouter")
    ap.add_argument("--announce", action="store_true",
                    help="озвучить фразу (иначе только напечатать)")
    ap.add_argument("--announce-script", help="скрипт озвучки (по умолчанию — из конфига)")
    ap.add_argument("--announce-arg", action="append", default=[],
                    help="доп. аргумент скрипта озвучки, напр. --announce-arg=--volume=0.25")
    ap.add_argument("--convert", metavar="CUR",
                    help="добавить пересчёт траты по курсу ЦБ (напр. RUB)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--list-sources", action="store_true", help="показать известные источники")
    ap.add_argument("--write-config", metavar="PATH",
                    help="сохранить настройки (без секретов) и выйти")
    ap.add_argument("--now", help="подменить текущее время (ISO) — для проверок")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать окно и источники, ничего не озвучивая и не трогая состояние")
    args = ap.parse_args()

    if args.list_sources:
        for name, meta in sorted(SOURCES.items()):
            print(f"{name:12s} {meta['title']:12s} ключ: {meta['key_env']}")
        return 0

    if args.write_config:
        data = {
            "sources": {n: {"enabled": True} for n in (args.source or ["openrouter"])},
            "convert": args.convert,
            "skip_dates": [],          # праздники: даты дописываются вручную
            "announce": {"script": args.announce_script or DEFAULT_ANNOUNCE_SCRIPT,
                         "args": args.announce_arg or []},
        }
        path = os.path.expanduser(args.write_config)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"настройки сохранены в {path}")
        return 0

    config = load_config(os.path.expanduser(args.config))
    period = resolve_period(args.period)
    now = datetime.fromisoformat(args.now).astimezone() if args.now else datetime.now().astimezone()
    convert = args.convert or config.get("convert")
    state_path = os.path.expanduser(args.state_file)
    state = read_state(state_path)
    skip_dates = {str(d) for d in (config.get("skip_dates") or [])}

    if period == "auto":
        window = auto_window(now, state, skip_dates)
        if window is None:
            if args.json:
                print(json.dumps({"period": "auto", "skipped": "нерабочий день или уже отчитались"},
                                 ensure_ascii=False))
            return 0
        start, end, phrase = window
    else:
        start, end, phrase = period_window(period, now)

    ann = dict(config.get("announce") or {})
    if args.announce_script:
        ann["script"] = args.announce_script
    if args.announce_arg:
        ann["args"] = list(ann.get("args") or []) + list(args.announce_arg)

    if args.dry_run:
        print(json.dumps({"period": period, "phrase": phrase,
                          "window": [start.isoformat(), end.isoformat()],
                          "sources": sorted(build_sources(config, args.source)),
                          "state_last_end": state.get("last_end")},
                         ensure_ascii=False, indent=1))
        return 0

    items, errors = [], []
    for name, cfg in build_sources(config, args.source).items():
        try:
            items.append(SOURCES[name]["collect"](cfg, (start, end), now))
        except SourceError as exc:
            errors.append(f"{name}: {exc}")

    if not items:
        print("данных нет (" + ("; ".join(errors) or "источники молчат") + ")", file=sys.stderr)
        return 2

    text = compose(items, phrase, convert)
    result = {"period": period, "phrase": phrase,
              "window": [start.isoformat(), end.isoformat()],
              "text": text, "sources": items, "errors": errors}
    if args.announce:
        result["announce"] = announce(text, ann)
        # состояние двигаем только после успешной озвучки: иначе пропущенные дни
        # остались бы неотчитанными навсегда
        if period == "auto" and result["announce"]["ok"]:
            write_state(state_path, end)
            result["state_updated"] = end.isoformat()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(text)
        for err in errors:
            print(f"! {err}", file=sys.stderr)
        if args.announce:
            a = result["announce"]
            print(f"озвучка: rc={a['rc']} ok={a['ok']}"
                  + (f" {a['stderr']}" if a.get("stderr") else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
