#!/usr/bin/env python3
"""Проиграть готовую голосовую запись на Bluesound (BluOS) через Home Assistant.

Отличие от hermes_announce.py: текст не синтезируется, берётся готовый файл
(--file, mp3/wav/ogg/…). Дальше — как в навыке: конвертация в mp3, доставка в
каталог www инстанса HA, play_media, ожидание конца потока, возврат очереди и
позиции.

Кладётся рядом с hermes_announce.py (импортирует из него разбор конфига стенда,
BluOS API и доставку файла).

    hermes_bluos_playfile.py --file /path/voice.ogg
    hermes_bluos_playfile.py --file /path/voice.ogg --dry-run
    hermes_bluos_playfile.py --file voice.mp3 --no-keep     # удалить из www после

Имя файла в www по умолчанию фиксированное (--name меняет): повтор с таймстампом
в имени копит файлы в публичном каталоге.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermes_announce as ha  # noqa: E402

DEFAULT_NAME = "hermes_voice_play.mp3"


def duration(path: str) -> float:
    """Длительность файла в секундах (0, если не удалось определить)."""
    if not shutil.which("ffprobe"):
        return 0.0
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def prepare(src: str, workdir: str) -> str:
    """Сделать mp3 (BluOS надёжно играет mp3), с ~0.6 с тишины в конце."""
    out = os.path.join(workdir, "converted.mp3")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-af", "apad=pad_dur=0.6",
         "-c:a", "libmp3lame", "-q:a", "4", out], capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 3000:
        # без конвертации не падаем: если файл уже mp3 — отдадим как есть
        if src.lower().endswith(".mp3"):
            return src
        raise SystemExit(f"не удалось сконвертировать в mp3: {(proc.stderr or '')[:300]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Проиграть голосовой файл на Bluesound")
    ap.add_argument("--file", required=True, help="путь к голосовой записи")
    ap.add_argument("--config", default=os.environ.get("BLUOS_ANNOUNCE_CONFIG"),
                    help="файл настроек стенда")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"имя файла в www (по умолчанию {DEFAULT_NAME})")
    ap.add_argument("--volume", type=float, help="громкость 0..1 (по умолчанию из конфига)")
    ap.add_argument("--token-env", default=None, help="имя переменной с токеном HA")
    ap.add_argument("--keep-file", action="store_true", default=True,
                    help="оставить файл в www (по умолчанию так и есть; для повторов — --no-keep)")
    ap.add_argument("--no-keep", dest="keep_file", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    args = ap.parse_args()

    src = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.exists(src) or os.path.getsize(src) < 500:
        raise SystemExit(f"нет файла или он пустой: {src}")

    cfg_file = ha.load_config(os.path.expanduser(args.config)) if args.config else ha.load_config()
    missing = [k for k in ("hass_url", "entity", "bluos") if not cfg_file.get(k)]
    if not (cfg_file.get("www") or cfg_file.get("container")):
        missing.append("www/container")
    if missing:
        raise SystemExit("в конфиге стенда не задано: " + ", ".join(missing))

    token_env = args.token_env or cfg_file.get("token_env") or "HASS_TOKEN"
    cfg = {
        "hass_url": cfg_file["hass_url"],
        "media_base": cfg_file.get("media_base") or cfg_file["hass_url"],
        "entity": cfg_file["entity"],
        "bluos": cfg_file["bluos"],
        "ssh": cfg_file.get("ssh"),
        "www": cfg_file.get("www"),
        "ssh_key": cfg_file.get("ssh_key"),
        "container": cfg_file.get("container"),
        "token_env": token_env,
        "volume": args.volume if args.volume is not None else cfg_file.get("volume"),
        "content_type": cfg_file.get("content_type") or "music",
        "keep_file": args.keep_file,
    }

    remote_name = args.name if args.name.endswith(".mp3") else args.name + ".mp3"
    media_url = f"{cfg['media_base'].rstrip('/')}/local/{remote_name}"

    if args.dry_run:
        print(json.dumps({"file": src, "duration": duration(src), "remote": remote_name,
                          "media_url": media_url, "entity": cfg["entity"],
                          "bluos": cfg["bluos"], "volume": cfg["volume"],
                          "token_env": token_env,
                          "token_env_set": bool(os.environ.get(token_env))},
                         ensure_ascii=False, indent=1))
        return 0

    token = ha.secret(token_env)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    workdir = tempfile.mkdtemp(prefix="bluos_playfile_")
    served = prepare(src, workdir)
    ha.push_file(served, remote_name, cfg["ssh"], cfg["www"], cfg["ssh_key"], cfg["container"])

    bluos = ha.BluOS(cfg["bluos"])
    before = bluos.status()
    was = (before.get("state") or "").lower()
    # играло = state play/stream И источник — своя очередь; внешний вход (service=Capture)
    # не считаем: BluOS держит его в play часами, и /Play?seek&id включил бы музыку.
    # Проверка одна на оба скрипта — ha.was_really_playing.
    was_playing = ha.was_really_playing(before)
    saved_song, saved_secs = before.get("song"), before.get("secs")
    ext_input = ha.current_input(before)
    report = {"media_url": media_url, "before": before, "was_playing": was_playing,
              "file_duration": duration(served)}

    try:
        if cfg["volume"] is not None:
            ha.http(f"{cfg['hass_url'].rstrip('/')}/api/services/media_player/volume_set",
                    {"entity_id": cfg["entity"], "volume_level": cfg["volume"]}, headers)
        ha.http(f"{cfg['hass_url'].rstrip('/')}/api/services/media_player/play_media",
                {"entity_id": cfg["entity"], "media_content_id": media_url,
                 "media_content_type": cfg["content_type"]}, headers)

        deadline = time.time() + 40
        started = False
        stopped = False
        while time.time() < deadline:
            time.sleep(0.35)
            st = bluos.status()
            totlen, secs = st.get("totlen"), st.get("secs")
            streaming = not st.get("serviceName") and totlen not in (None, "0")
            if streaming:
                started = True
                try:
                    if secs is not None and float(secs) >= float(totlen) - 0.7:
                        stopped = bluos.stop_confirmed()
                        break
                except ValueError:
                    pass
            elif started:
                break
        if not stopped:
            stopped = bluos.stop_confirmed()
        report.update(started=started, stopped=stopped)

        if saved_song is not None:
            cmd = f"/Play?seek={int(float(saved_secs or 0))}&id={int(float(saved_song))}"
            bluos.cmd(cmd)
            report["restore_cmd"] = cmd
            if not was_playing:
                time.sleep(0.8)
                # /Pause одним выстрелом может не встать: проверяем по /Status и повторяем
                for _ in range(3):
                    bluos.cmd("/Pause")
                    time.sleep(0.4)
                    if (bluos.status().get("state") or "").lower() not in ("play", "stream"):
                        break
                report["restore_cmd"] += " + /Pause"
        # Плеер стоял на внешнем входе — вернуть его (см. ha.restore_input).
        if ext_input:
            ha.restore_input(bluos, ext_input, was, report)
        time.sleep(1.2)
        report["after"] = bluos.status()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if not cfg["keep_file"]:
            ha.remove_file(remote_name, cfg["ssh"], cfg["www"], cfg["ssh_key"], cfg["container"])

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("started") else 1


if __name__ == "__main__":
    sys.exit(main())
