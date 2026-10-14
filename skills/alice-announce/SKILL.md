---
name: alice-announce
description: Use when making a Yandex Station say text with Alice's voice via HA.
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [alice, yandex-station, yandexstation, home-assistant, tts, media_player, announce]
    related_skills: [bluos-announce]
required_environment_variables:
  - name: ANNOUNCE_HA_TOKEN
    prompt: "Long-lived token of the Home Assistant instance the speaker is connected to"
    required_for: "calling media_player.play_media through the HA API"
---

# Speaking text with Alice's voice (Yandex Station via HA)

## When to Use

"Say this on the Alice speaker", "announce it in Alice's voice", "send text to the Station".
Works when a Yandex speaker is connected to Home Assistant through the **`AlexxIT/YandexStation`**
custom component and you want **Alice's voice** — the speaker does the synthesis itself.

Not this skill: output to a Bluesound player (`bluos-announce`), to an Assist satellite
(the satellite answers), to a phone/browser (whoever started the pipeline run). If you need
**your own** TTS (ElevenLabs etc.) on a Yandex speaker, that is a different path — see
"Your own voice instead of Alice's" below.

## The core: one call, three modes

One call: `media_player.play_media` with a mandatory `media_content_type`.

| `media_content_type` | What it does | Where it works |
|---|---|---|
| `text` | the speaker **speaks** the text in Alice's voice | everywhere |
| `command` | the speaker **executes** the text as an Alice command ("turn on my music") | everywhere |
| `dialog` | speaks the text and **keeps listening**; supports TTS effects | local mode only |

`media_content_id` is the text itself. The call returns **`[]` on success and on failure
alike** — judge by the speaker state and by the component log, never by the service response.

## Cloud vs local mode decides the limit and the feedback

- **Cloud**: text is **truncated at 100 characters** (the component does it), and there is no
  feedback — you cannot tell whether the speaker plays anything or what its volume is.
- **Local** (Glagol/mDNS, enabled automatically on supported speakers): no length limit, the
  speaker keeps listening in `dialog` mode, TTS effects work, the current track is visible,
  seeking works, and TTS volume is restored afterwards.

**How to tell without UI and without listening** — by entity attributes: in local mode
`alice_state`, `media_title`, `media_position` and `media_duration` are populated; in cloud mode
they are absent. Second hint: `source_list` contains other `media_player`s (streaming exists
only in local mode).

**Third hint — the component log.** With `debug` on `custom_components.yandex_station` every
send prints `... => local | {'command': 'serverAction', ... 'repeat_phrase'}`. `=> local` means
the command went over the LAN, so the text arrived in full. That is how you prove the length
limit is gone without relying on ears.

## What actually reaches the speaker in each mode (verified from logs)

Local-mode sends use different Glagol commands — the log shows which one fired:

| mode | command in the log | slot |
|---|---|---|
| `text` | `serverAction` → `...quasar.iot.repeat_phrase` | `phrase_to_repeat` — the full text |
| `command` | `sendText` | `text` |
| `dialog` | `serverAction` → `...repeat_after_me` | `request` — the text **IN UPPERCASE** |

**`dialog` uppercases the text** before sending — it does not affect speech, but do not be
surprised by the case mismatch in logs. It also keeps listening ("repeat after me" scenario),
while `text` speaks and stops.

## Verifying that the speaker really spoke

- A successful service call **proves nothing**: HA returns `[]` either way. The final word
  belongs to a human ear at the speaker.
- Indirect confirmation: watch `alice_state` — `IDLE` → `NONE` while speaking → `IDLE`.
  Watch for the transition, not for an absolute value.
- Confident confirmation without listening: the component's debug log (`=> local` plus the
  full text in the `repeat_phrase` slot) together with the entity attributes.
- **The text in the log is the text that was sent.** This is the best probe for long phrases:
  the slot value shows whether anything truncated it.

## TTS effects (dialog + local mode only)

```
<speaker is_whisper="true">quietly</speaker>
<speaker effect="megaphone">loudly</speaker>
sil <[500]>pause before the phrase
```
The script accepts `--effects whisper|megaphone|slow|pause` and switches to `dialog` by itself.
`whisper` was verified by ear on a Station Midi; `megaphone`/`sil` come from the component docs
only — check by ear before relying on them.

## The tool

`scripts/alice_say.py` — a stdlib-only REST wrapper:

```bash
alice_say.py --hass-url http://10.0.0.5:8123 "Dinner is ready"   # speak
alice_say.py "turn on my music" --mode command                    # command the speaker
alice_say.py "text" --effects whisper                             # whisper
alice_say.py "text" --wait --json                                 # wait for the end, JSON out
alice_say.py --list                                               # detected speaker and mode
alice_say.py "text" --dry-run                                     # show the payload, send nothing
```

The speaker is auto-detected by the `sound_mode_list`/`alice_state` attributes (only
YandexStation adds them), so the entity id is optional when there is a single speaker.

## Setup

Addresses and the entity are resolved in this order: **CLI flag → environment → config file**.
The config file holds addresses only and no secrets, so it is safe to keep in dotfiles:

```json
{
  "hass_url": "http://192.168.1.10:8123",
  "entity": "media_player.yandex_station"
}
```

Default location: `~/.config/hermes-alice-announce/config.json` (change with `--config` or
`ALICE_ANNOUNCE_CONFIG`). Write it with `--write-config PATH --hass-url ... --entity ...`
rather than by hand. A ready-made example ships in `examples/config.example.json` of the
collection repository.

The token is read **only from the process environment** — the script never opens secret files.
Secrets are never printed.

**Name the variable with `--token-env`, and keep that name in the config.** The Hermes core
strips `HASS_TOKEN` (and `HASS_URL`) from the environment of **every** child process — terminal,
`execute_code`, cron scripts — because Home Assistant moved out of the core into a catalog
plugin. A hardcoded default therefore works in a hand-run shell and fails silently everywhere
the agent calls it. So the resolved name comes from: `--token-env` → `$ALICE_TOKEN_ENV` →
`token_env` in the config → `HASS_TOKEN`. Put your working name (e.g. `ANNOUNCE_HA_TOKEN`) into
the config once and the agent stops needing the flag:

```json
{
  "hass_url": "https://ha.example.com",
  "entity": "media_player.yandex_station",
  "token_env": "ANNOUNCE_HA_TOKEN"
}
```

`terminal.env_passthrough` is not needed for such a custom name, and `HASS_TOKEN` cannot be
declared there at all — the sanitizer refuses operator config that would lift the seal.
Symptom of the stripped default: `no HA token in the environment: set HASS_TOKEN`.

## Pitfalls

- **`tts.speak` is not this.** It announces through the configured TTS engine, not in Alice's
  voice. Note also that `tts.speak` wants `entity_id` = the TTS **engine** and the player in
  `media_player_entity_id`; passing the player as `entity_id` returns an empty list and does
  nothing.
- **`HassBroadcast` (HA MCP) is also a different path**: it goes through the HA pipeline TTS,
  so the voice will not be Alice's.
- **A shared speaker never appears in HA at all.** The component deliberately skips devices
  carrying `sharing_info` ("skip shared devices"): the speaker is visible in Yandex's API yet
  missing from `media_player.*` and from the entity registry entirely. The fix is to log in as
  the account that **owns** the speaker. One-step diagnosis: compare the device's
  `sharing_info.owner_id` with `storage.user.uid`.
- **Service scenes named `ХА <device_id>`** are created by the component itself in the Yandex
  app — do not delete them.
- **The component talks to `yandex.ru/quasar`, which Yandex periodically breaks** (connection
  dropped, the config entry sticks in `setup_retry`). Symptom and workaround are documented in
  the community issue tracker of `AlexxIT/YandexStation`; check there before assuming a local fault.
- **In `dialog` mode the speaker keeps listening** — the next human phrase goes to Alice, not to
  a scene. Use `text` when that matters.
- **A speaker as a loudspeaker is not an assistant**: there is no microphone on this path, you
  cannot ask it anything. The human always starts the dialogue.

## Your own voice instead of Alice's

Alice's voice is not always what you want. The other route: synthesize an mp3 and **hand the
speaker a link** — local mode supports `audio_play`/`radio_play` directives, but on recent
firmware `radio_play` is broken (it plays radio instead of the link), so `audio_play` is the one
to use. It requires a public HTTPS URL reachable by the speaker. For file delivery and queue
handling see `bluos-announce`.
