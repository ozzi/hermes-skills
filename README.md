# hermes-skills

Публичный набор навыков (skills) для [Hermes Agent](https://hermes-agent.nousresearch.com/docs) —
устанавливается как **tap**: один репозиторий, навыки папками внутри `skills/`.

## Установка

```bash
hermes skills tap add ozzi/hermes-skills     # подключить репозиторий как источник
hermes skills search bluos                   # найти навык
hermes skills install ozzi/hermes-skills/skills/bluos-announce
```

Навык появляется в `skills_list` и становится доступен как слэш-команда в новой сессии.

## Что внутри

| Навык | Зачем |
|---|---|
| [`bluos-announce`](skills/bluos-announce/SKILL.md) | Озвучить текст на плеере Bluesound/BluOS через Home Assistant, не потеряв очередь воспроизведения |
| [`alice-announce`](skills/alice-announce/SKILL.md) | Заставить Яндекс Станцию произнести текст голосом Алисы через Home Assistant (режимы text/command/dialog, спецэффекты, обход лимита 100 символов) |

## Структура

```
skills/
├── bluos-announce/
│   ├── SKILL.md              # инструкции, грабли, проверка результата
│   └── scripts/
│       └── hermes_announce.py
└── alice-announce/
    ├── SKILL.md              # режимы play_media, спецэффекты, диагностика
    └── scripts/
        └── alice_say.py
examples/                     # примеры файлов настроек (без секретов)
skills.sh.json                # группировки для Skills Hub
```

Навыки самодостаточны: никаких секретов и адресов конкретного стенда в репозитории нет.
Адреса и пути задаются аргументами, переменными окружения или локальным файлом настроек
(см. раздел «Настройка» в SKILL.md), секреты — только из окружения.

## Лицензия

MIT.
