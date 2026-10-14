#!/usr/bin/env python3
"""Say text out loud with Alice's voice on a Yandex Station, via Home Assistant.

The speaker synthesizes the text itself (Alice's voice, local Glagol mode) — no external
TTS is involved. Path: HA REST `media_player.play_media` with `media_content_type:
text|command|dialog` → the YandexStation custom component → the speaker.

Why not `tts.speak` or `HassBroadcast`: `tts.speak` uses the configured TTS engine, and
`HassBroadcast` goes through the HA pipeline TTS — neither gives Alice's voice.

Usage:
    alice_say.py --hass-url http://10.0.0.5:8123 "Dinner is ready"
    alice_say.py "turn on my music" --mode command
    alice_say.py "text" --mode dialog --effects whisper
    alice_say.py --list                    # show the detected speaker and its mode
    alice_say.py "text" --wait --json      # wait for speech to finish, machine-readable

Addresses may come from CLI flags, environment variables, or a config file; the token is
taken from the environment only (`--token-env`, default HASS_TOKEN).

Environment:
    ALICE_HA_URL, ALICE_ENTITY  — same as --hass-url / --entity
    HASS_URL                    — fallback for the HA base URL
    HASS_TOKEN                  — long-lived HA token (or pass --token-env NAME)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# default config location; holds addresses only, never secrets
CONFIG_PATH = os.path.expanduser(
    os.environ.get("ALICE_ANNOUNCE_CONFIG", "~/.config/hermes-alice-announce/config.json")
)

# cloud mode truncates text at 100 characters; local mode does not
CLOUD_LIMIT = 100

# TTS effects — LOCAL MODE ONLY
EFFECTS = {
    "whisper": '<speaker is_whisper="true">{text}</speaker>',
    "megaphone": '<speaker effect="megaphone">{text}</speaker>',
    "slow": "sil <[200]>{text}",
    "pause": "sil <[500]>{text}",
}


def load_config(path: str | None) -> dict:
    """Read the address config file. Never contains secrets, so it is safe to share."""
    if not path or not os.path.isfile(os.path.expanduser(path)):
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        print(f"warning: cannot read config {path}: {exc}", file=sys.stderr)
        return {}


def pick(*values):
    """First value that is set (CLI flag > environment > config file)."""
    for v in values:
        if v:
            return v
    return None


class HA:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise SystemExit(f"HA answered {e.code}: {raw[:300]}")
        except urllib.error.URLError as e:
            raise SystemExit(f"HA unreachable at {self.url}: {e.reason}")
        return json.loads(raw) if raw.strip() else None

    def states(self):
        return self.call("GET", "/api/states") or []

    def find_speaker(self) -> str:
        """Auto-detect the Yandex speaker: only YandexStation adds these attributes."""
        candidates = []
        for s in self.states():
            eid = s["entity_id"]
            if not eid.startswith("media_player."):
                continue
            if s["attributes"].get("sound_mode_list") or "alice_state" in s["attributes"]:
                candidates.append(eid)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise SystemExit("no Yandex speaker found in HA; pass --entity <entity_id>")
        raise SystemExit("several candidates, pass --entity: " + ", ".join(candidates))


def local_mode(ha: HA, entity: str) -> bool:
    """Local mode = the speaker reports back (alice_state / track position)."""
    st = ha.call("GET", f"/api/states/{entity}") or {}
    a = st.get("attributes", {})
    return "alice_state" in a or a.get("media_duration") is not None


def wait_done(ha: HA, entity: str, timeout: float = 60.0) -> dict:
    """Wait for speech to end: alice_state leaves NONE and comes back to IDLE."""
    t0 = time.time()
    seen_busy = False
    last: dict = {}
    while time.time() - t0 < timeout:
        last = ha.call("GET", f"/api/states/{entity}") or {}
        a = last.get("attributes", {})
        if a.get("alice_state") in ("NONE", "SPEAKING") or last.get("state") == "playing":
            seen_busy = True
        elif seen_busy:
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    p = argparse.ArgumentParser(description="Say text with Alice's voice on a Yandex Station")
    p.add_argument("text", nargs="?", help="text to speak (or a command in --mode command)")
    p.add_argument("--config", default=CONFIG_PATH, help=f"address config file (default {CONFIG_PATH})")
    p.add_argument("--hass-url", help="HA base URL, e.g. http://10.0.0.5:8123")
    p.add_argument("--entity", help="media_player entity of the speaker (auto-detected otherwise)")
    p.add_argument("--token-env", default=None,
                   help="name of the environment variable holding the HA token "
                        "(default: $ALICE_TOKEN_ENV, then 'token_env' in the config, "
                        "then HASS_TOKEN)")
    p.add_argument("--mode", default="text", choices=("text", "command", "dialog"),
                   help="text = speak it; command = run it as an Alice command; "
                        "dialog = speak and keep listening (local mode only)")
    p.add_argument("--effects", choices=tuple(EFFECTS),
                   help="TTS effect (forces --mode dialog; local mode only)")
    p.add_argument("--list", action="store_true", help="show the speaker and its mode, then exit")
    p.add_argument("--wait", action="store_true", help="wait until speech finishes")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve settings and print what would be sent, without sending")
    p.add_argument("--write-config", metavar="PATH",
                   help="write the resolved addresses to a config file and exit")
    args = p.parse_args()

    cfg = load_config(args.config)
    url = pick(args.hass_url, os.environ.get("ALICE_HA_URL"), cfg.get("hass_url"),
               os.environ.get("HASS_URL"))
    entity = pick(args.entity, os.environ.get("ALICE_ENTITY"), cfg.get("entity"))
    # Имя переменной с токеном тоже настраивается: ядро Hermes вырезает HASS_TOKEN
    # из окружения дочерних процессов, поэтому рабочее имя задаётся в конфиге.
    token_env = pick(args.token_env, os.environ.get("ALICE_TOKEN_ENV"),
                     cfg.get("token_env"), "HASS_TOKEN")
    token = os.environ.get(token_env)

    if args.write_config:
        if not url and not entity:
            raise SystemExit("nothing to write: pass --hass-url and/or --entity")
        path = os.path.expanduser(args.write_config)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in
                       {"hass_url": url, "entity": entity,
                        "token_env": args.token_env}.items() if v},
                      fh, ensure_ascii=False, indent=2)
        print(f"written: {path} (no secrets inside)")
        return 0

    if not url:
        raise SystemExit("no HA URL: pass --hass-url, set ALICE_HA_URL/HASS_URL, "
                         f"or put hass_url into {args.config}")
    if not token:
        raise SystemExit(f"no HA token in the environment: set {token_env} "
                         "(secrets are never read from files)")

    ha = HA(url, token)
    if args.dry_run and not args.text:
        print(json.dumps({"hass_url": url, "entity": entity or "(auto-detect needs HA)",
                          "token_env": token_env}, ensure_ascii=False, indent=1))
        return 0

    # effects imply dialog mode; do this before any payload is built
    if args.effects and args.mode != "dialog":
        print("note: TTS effects work only in dialog mode (local); switching to dialog",
              file=sys.stderr)
        args.mode = "dialog"

    if args.dry_run and entity:
        # nothing left to discover: report the payload without touching the network
        text = EFFECTS[args.effects].format(text=args.text) if args.effects else args.text
        print(json.dumps({"would_send": {"entity_id": entity, "media_content_id": text,
                                         "media_content_type": args.mode},
                          "chars": len(text), "hass_url": url},
                         ensure_ascii=False, indent=1))
        return 0
    entity = entity or ha.find_speaker()
    local = local_mode(ha, entity)
    if args.list:
        out = {"entity": entity, "hass_url": url, "local_mode": local}
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json
              else f"speaker: {entity}\nHA: {url}\nlocal mode: "
                   f"{'yes' if local else 'no/unknown — cloud mode truncates at 100 chars'}")
        return 0

    if not args.text:
        raise SystemExit("nothing to say: pass text, or use --list")

    text = EFFECTS[args.effects].format(text=args.text) if args.effects else args.text

    if args.mode == "text" and not local and len(text) > CLOUD_LIMIT:
        print(f"warning: the speaker looks cloud-only and the text is {len(text)} characters — "
              f"the component truncates to {CLOUD_LIMIT}", file=sys.stderr)

    payload = {"entity_id": entity, "media_content_id": text, "media_content_type": args.mode}
    if args.dry_run:
        print(json.dumps({"would_send": payload, "chars": len(text), "local_mode": local},
                         ensure_ascii=False, indent=1))
        return 0

    before = (ha.call("GET", f"/api/states/{entity}") or {}).get("attributes", {})
    ha.call("POST", "/api/services/media_player/play_media", payload)

    result = {"entity": entity, "mode": args.mode, "chars": len(text),
              "local_mode": local, "sent": True}
    if args.wait:
        after = wait_done(ha, entity)
        result["alice_state_after"] = after.get("attributes", {}).get("alice_state")
        result["state_after"] = after.get("state")
        result["playing_before"] = before.get("media_title")
        result["playing_after"] = after.get("attributes", {}).get("media_title")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"sent to {entity} ({args.mode}, {len(text)} characters)"
              + (f"; after speech: alice_state={result.get('alice_state_after')}"
                 if args.wait else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
