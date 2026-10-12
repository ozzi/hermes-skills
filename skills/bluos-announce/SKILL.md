---
name: bluos-announce
description: Use when announcing text on a Bluesound/BluOS player without losing the queue.
version: 1.0.0
author: Alexander Zhukov
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [bluesound, bluos, home-assistant, tts, media_player, announce, homelab]
    related_skills: [voice-pe-hermes-integration, litellm-gateway, rpi-docker-host]
required_environment_variables:
  - name: HASS_TOKEN
    prompt: "Long-lived token инстанса Home Assistant, к которому подключён плеер"
    required_for: "вызов play_media через HA API"
  - name: LITELLM_API_KEY
    prompt: "Ключ LiteLLM (прокси для TTS)"
    required_for: "синтез речи"
---

# Озвучка на Bluesound (BluOS) без потери очереди

## When to Use

Задача «скажи это на колонке» / «озвучь ответ на аудиосистеме», когда плеер — **Bluesound
или NAD на BluOS** (HA-интеграция `bluesound`, прямой API на порту `11000`, XML на `/Status`).

Не использовать для Sonos (у него есть настоящий `MEDIA_ANNOUNCE` и ducking — там задача решается
штатно) и для сателлитов Assist (Voice PE, ESP32 — там отвечает сам сателлит).

## Почему это отдельная задача, а не один вызов play_media

- **У Bluesound нет announce/ducking.** В `supported_features` нет `MEDIA_ANNOUNCE`
  (проверять у сущности, а не у домена: у Sonos флаг есть, у bluesound-матчера — нет).
  Официальный BluOS API знает только `/Doorbell?play=1` — фиксированный файл с самих колонок,
  свой mp3 туда не подставить.
- **Music Assistant объявления на BluOS не поддерживает** (в разделе «Not Yet Supported»
  провайдера Bluesound прямо стоят Announcements и синхронное воспроизведение).
- **Любой проигрыш URL вытесняет очередь.** По окончании потока BluOS возвращается к очереди
  сам, но **к следующему треку** (наблюдалось `song=0` → `song=1`). Пользователь слышит перескок.
- **`/Pause` из состояния `stop` не держится** — BluOS сразу откатывается в `stop`, и очередь
  при этом уезжает. Поэтому «не играло» тоже требует предварительного `/Play?seek&id`.

## Порядок объявления

1. **Синтез** mp3 через OpenAI-совместимый `/v1/audio/speech` (`response_format: mp3`;
   `opus`/`wav` на этих апстримах дают 400).
2. **Дописать ~0.6 с тишины** (`ffmpeg -af apad=pad_dur=0.6`) — окно, чтобы успеть оборвать поток.
3. **Доставить файл плееру**: в `<ha-config>/www/`, URL `http://<LAN-IP-HA>:8123/local/<файл>.mp3`.
   Каталог `www` HA регистрирует как `/local` только если он существует; отдаёт **без авторизации**.
   Для контейнерной HA: `docker cp` в `/config/www/` или `scp` под владельцем хоста.
4. **`media_player.play_media`** с `media_content_type` (обязателен, иначе 400). Сервис отвечает
   `[]` и при неудаче тоже — судить по состоянию плеера, а не по ответу.
5. **Ждать по `/Status`**, пока `serviceName` пуст и `secs >= totlen - 0.7`, затем послать `/Stop`.
6. **Вернуть состояние по правилу «играло или нет»**:
   - было `play`/`stream` → `/Play?seek=<secs>&id=<song>` — музыка продолжается с той же секунды;
   - было `stop`/`pause`/idle → сначала тот же `/Play?seek&id`, затем `/Pause`.
7. **Удалить mp3** (`--keep-file` отключает).

## Настройка (без правки кода)

Адреса и пути стенда нигде не зашиты. Значения берутся по приоритету:

**аргумент CLI → переменная окружения → локальный конфиг стенда.**

Локальный конфиг — `~/.config/hermes-bluos-announce/config.json` (меняется `--config`
или `BLUOS_ANNOUNCE_CONFIG`), обычный JSON:

```json
{
  "hass_url": "http://192.168.1.10:8123",
  "entity": "media_player.living_room",
  "bluos": "http://192.168.1.20:11000",
  "ssh": "user@192.168.1.10",
  "www": "/home/user/ha-config/www",
  "tts_base": "https://llm.example.com/v1",
  "tts_model": "audio/tts",
  "voice": "matilda",
  "volume": 0.3
}
```

Записать его можно командой, а не руками:

```bash
hermes_announce.py --write-config ~/.config/hermes-bluos-announce/config.json \
  --hass-url http://192.168.1.10:8123 --entity media_player.living_room \
  --bluos http://192.168.1.20:11000 --ssh user@192.168.1.10 \
  --www /home/user/ha-config/www --volume 0.3
```

**В конфиге нет секретов** — только адреса, пути и алиас TTS, поэтому его можно хранить
в dotfiles. Секреты читаются из окружения: `HASS_TOKEN` (токен нужного инстанса HA; имя
переопределяется `--token-env`) и `LITELLM_API_KEY` (ключ TTS; `--key-env`). Скрипт также
подхватывает `~/.hermes/.env` профиля (`HERMES_ENV_FILE` меняет путь). Значения ключей
никогда не печатаются.

Env-переопределения: `HASS_ANNOUNCE_HASS_URL`, `HASS_ANNOUNCE_MEDIA_BASE`,
`HASS_ANNOUNCE_ENTITY`, `HASS_ANNOUNCE_BLUOS`, `HASS_ANNOUNCE_SSH`, `HASS_ANNOUNCE_WWW`,
`HASS_ANNOUNCE_SSH_KEY`, `HASS_ANNOUNCE_CONTAINER`, `HASS_ANNOUNCE_TTS_BASE`,
`HASS_ANNOUNCE_TTS_MODEL`, `HASS_ANNOUNCE_VOICE`, `HASS_ANNOUNCE_CONTENT_TYPE`.

## Quick Reference

```bash
# озвучить (все адреса — из конфига стенда)
hermes_announce.py "Котлеты готовы"

# или всё аргументами
hermes_announce.py "Котлеты готовы" \
  --hass-url http://192.168.1.10:8123 --entity media_player.living_room \
  --bluos http://192.168.1.20:11000 \
  --ssh user@192.168.1.10 --www /home/user/ha-config/www \
  --volume 0.3

hermes_announce.py --voice-list          # известные голоса текущего апстрима
```

BluOS-эндпоинты: `/Status` (XML), `/Play?seek=<сек>&id=<номер в очереди>`, `/Pause`, `/Stop`.

Признаки в состоянии плеера: идёт объявление — `serviceName` пуст и `totlen` задан;
вернулись в очередь — `serviceName=Library`; для HA — `source=http` и `media_duration` ≈ длине mp3.

## Pitfalls

- **Голоса привязаны к модели за алиасом, а не к алиасу.** При смене апстрима TTS старый голос
  даёт `400 Unknown voice`, и по симптому это похоже на поломку пайплайна. Проверять живым
  синтезом перед выкладкой.
- **`media_content_type` обязателен**; URL строится по LAN-IP HA — файл качает сам плеер,
  `homeassistant.local` и имя за реверс-прокси для него не работают.
- **Проверять доступность конкретного файла**, а не каталога: `/local/` отдаёт 404 и тогда,
  когда маршрут вообще не зарегистрирован, так что «404 = доступ без токена есть» — ложный вывод.
- **Каталог `www` общедоступен в LAN.** Класть туда только произносимый текст и удалять сразу.
- **Плейлист/очередь объявлений не поддерживаются**: один вызов = один URL. Не строить
  «несколько фраз подряд» одним вызовом.
- **`bluesound` в HA — матчер, а не гарантия BluOS**: под этим доменом может лежать
  профессиональный UPnP-плеер. Сверять `data.host` и порт (BluOS = 11000 + XML на `/Status`).

## Verification

Успех подтверждается **состоянием плеера, а не кодом ответа**:
`source=http` и `media_duration` ≈ длине mp3, затем `serviceName=Library` и прежний трек;
при `--volume` громкость возвращается в исходную. Скрипт печатает JSON-отчёт с полями
`before`, `stopped`, `restore_cmd`, `after` — по ним и проверять.
