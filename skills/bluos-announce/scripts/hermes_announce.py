#!/usr/bin/env python3
"""Озвучить текст на плеере Bluesound (BluOS) через Home Assistant, не сбив очередь.

Класс задачи: «когда попрошу — скажи это на колонке». Скрипт синтезирует mp3,
кладёт его в каталог www инстанса HA, просит HA проиграть файл на BluOS-плеере,
дожидается конца потока и возвращает плеер туда, где он был.

Порядок действий (именно такой, иначе очередь уезжает на следующий трек):

  1. синтез mp3 через OpenAI-совместимую ручку /v1/audio/speech;
  2. дописать ~0.6 с тишины в конец (окно, чтобы успеть оборвать поток);
  3. скопировать файл в <ha-config>/www (scp или docker cp);
  4. media_player.play_media -> плеер играет mp3;
  5. ждать по BluOS /Status, пока secs >= totlen - END_MARGIN, затем послать /Stop;
  6. вернуть прежний трек и позицию: /Play?seek=<сек>&id=<номер в очереди>;
  7. удалить mp3 (--keep-file отключает).

Почему нельзя проще: BluOS не умеет announce/ducking поверх музыки (у Bluesound нет
флага MEDIA_ANNOUNCE, как у Sonos, и Music Assistant объявления на BluOS не поддерживает).
Любой проигрыш URL вытесняет очередь, а по окончании потока BluOS сам возвращается
к очереди, но к СЛЕДУЮЩЕМУ треку. Поэтому поток обрывается командой /Stop, а позиция
восстанавливается вручную.

Использование:
    hermes_announce.py "текст" --hass-url http://<HA-IP>:8123 --bluos http://<player>:11000 \\
        --ssh user@<HA-host> --www /path/to/ha-config/www
    hermes_announce.py --voice-list

Все секреты читаются из env: LITELLM_API_KEY (или --api-key-env) и HASS_TOKEN
(или --token-env). Значения на экран не печатаются.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ENV_PATH = os.path.expanduser(os.environ.get("HERMES_ENV_FILE", "~/.hermes/.env"))
# Локальные (несекретные) настройки стенда: адреса HA/плеера, пути, алиас TTS.
# Секреты здесь НЕ хранятся — только то, что можно положить в git.
CONFIG_PATH = os.path.expanduser(
    os.environ.get("BLUOS_ANNOUNCE_CONFIG", "~/.config/hermes-bluos-announce/config.json"))

REMOTE_NAME = "hermes_announce.mp3"
END_MARGIN = 0.7      # за сколько секунд до конца конца потока глушить
PLAY_TIMEOUT = 30.0   # максимум ожидания начала/конца потока

# Голоса зависят от модели за алиасом TTS, а не от алиаса: при смене апстрима
# старый голос даёт 400 Unknown voice. Список — подсказка, не жёсткое ограничение.
DEFAULT_VOICES = ["matilda", "george", "sarah", "alice", "bella", "brian", "charlie",
                  "daniel", "jessica", "laura", "lily", "river", "roger", "will"]

CONFIG_KEYS = ("hass_url", "media_base", "entity", "bluos", "ssh", "www", "ssh_key",
               "container", "tts_base", "tts_model", "voice", "volume", "content_type")


def load_config(path: str = CONFIG_PATH) -> dict:
    """Прочитать локальный конфиг стенда (адреса и пути), если он есть."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} — не разобрать как JSON: {exc}")
    return {k: v for k, v in data.items() if k in CONFIG_KEYS and v not in (None, "")}


def pick(cli_value, env_name: str, cfg: dict, key: str):
    """Приоритет: аргумент CLI -> переменная окружения -> локальный конфиг."""
    if cli_value not in (None, ""):
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    return cfg.get(key)


def load_env(path: str = ENV_PATH) -> dict[str, str]:
    """Прочитать .env профиля (значения наружу не печатаем)."""
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def secret(env: dict[str, str], name: str) -> str:
    val = os.environ.get(name) or env.get(name, "")
    if not val:
        raise SystemExit(f"нет {name}: положите его в {ENV_PATH} или в окружение")
    return val


def http(url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 20.0) -> str:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def tts(text: str, voice: str, out_path: str, base_url: str, model: str, timeout: int = 90) -> int:
    """Синтез в mp3. Возвращает размер файла."""
    key = secret(load_env(), os.environ.get("TTS_KEY_ENV", "LITELLM_API_KEY"))
    payload = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
    proc = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST", f"{base_url.rstrip('/')}/audio/speech",
         "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
         "-d", json.dumps(payload), "-o", out_path],
        capture_output=True, text=True)
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if proc.returncode != 0 or size < 3000:
        detail = ""
        if size:
            detail = open(out_path, encoding="utf-8", errors="replace").read()[:200]
        else:
            detail = (proc.stderr or "")[:200]
        raise SystemExit(f"синтез не удался (голос {voice}): {detail}")
    return size


def pad_tail(path: str, seconds: float = 0.6) -> None:
    """Дописать тишину: даёт окно, чтобы успеть оборвать поток до возврата очереди."""
    if not shutil.which("ffmpeg"):
        return
    tmp = path + ".pad.mp3"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path, "-af", f"apad=pad_dur={seconds}",
         "-c:a", "libmp3lame", "-q:a", "4", tmp], capture_output=True, text=True)
    if proc.returncode == 0 and os.path.getsize(tmp) > 3000:
        os.replace(tmp, path)
    elif os.path.exists(tmp):
        os.remove(tmp)


class BluOS:
    """Прямой BluOS API плеера (порт 11000, XML на /Status)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def cmd(self, cmd: str, timeout: float = 10.0) -> str:
        try:
            return http(self.base + cmd, timeout=timeout)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _tag(xml: str, tag: str) -> str | None:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
        return m.group(1) if m else None

    def status(self) -> dict[str, str | None]:
        xml = self.cmd("/Status")
        return {t: self._tag(xml, t)
                for t in ("state", "serviceName", "title1", "song", "secs", "totlen")}


def push_file(local: str, remote_name: str, target: str | None, www_dir: str | None,
              ssh_key: str | None, container: str | None) -> None:
    """Доставить mp3 туда, откуда его скачает плеер: docker cp, scp или копия на диске."""
    if container:
        subprocess.run(["docker", "exec", container, "mkdir", "-p", "/config/www"],
                       capture_output=True)
        proc = subprocess.run(["docker", "cp", local, f"{container}:/config/www/{remote_name}"],
                              capture_output=True, text=True)
    elif target and www_dir:
        cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if ssh_key:
            cmd += ["-i", os.path.expanduser(ssh_key)]
        proc = subprocess.run(cmd + [local, f"{target}:{www_dir}/{remote_name}"],
                              capture_output=True, text=True)
    elif www_dir:
        os.makedirs(www_dir, exist_ok=True)
        shutil.copy2(local, os.path.join(www_dir, remote_name))
        proc = subprocess.CompletedProcess([], 0, "", "")
    else:
        raise SystemExit("нужен один из вариантов доставки: --ssh+--www, --www или --docker-container")
    if proc.returncode != 0:
        raise SystemExit(f"доставка файла не удалась: {(proc.stderr or '')[:200]}")


def remove_file(remote_name: str, target: str | None, www_dir: str | None,
                ssh_key: str | None, container: str | None) -> None:
    if not www_dir:
        return
    path = f"{www_dir}/{remote_name}"
    if container:
        subprocess.run(["docker", "exec", container, "rm", "-f", f"/config/www/{remote_name}"],
                       capture_output=True)
    elif target:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if ssh_key:
            cmd += ["-i", os.path.expanduser(ssh_key)]
        subprocess.run(cmd + [target, f"rm -f {path}"], capture_output=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def announce(text: str, voice: str, cfg: dict) -> dict:
    """Озвучить текст, вернув очередь на прежнее место. Возвращает отчёт."""
    env = load_env()
    token = secret(env, cfg["token_env"])
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    local_mp3 = os.path.join(os.environ.get("TMPDIR", "/tmp"), "hermes_announce_src.mp3")
    tts(text, voice, local_mp3, cfg["base_url"], cfg["model"])
    pad_tail(local_mp3)
    push_file(local_mp3, REMOTE_NAME, cfg["ssh"], cfg["www"], cfg["ssh_key"], cfg["container"])

    media_url = f"{cfg['media_base'].rstrip('/')}/local/{REMOTE_NAME}"
    bluos = BluOS(cfg["bluos"])
    before = bluos.status()
    was = (before.get("state") or "").lower()
    saved_song, saved_secs = before.get("song"), before.get("secs")
    # Играла ли музыка ДО объявления — это и определяет, что делать после:
    # play/stream -> возобновляем с той же секунды; иначе (stop/pause/idle) -> пауза.
    was_playing = was in ("play", "stream")
    had_track = saved_song is not None

    report: dict = {
        "voice": voice,
        "before": before,
        "was_playing": was_playing,
        "action": "resume" if was_playing else "pause",
        "media_url": media_url,
    }

    if cfg["volume"] is not None:
        http(f"{cfg['hass_url'].rstrip('/')}/api/services/media_player/volume_set",
             {"entity_id": cfg["entity"], "volume_level": cfg["volume"]}, headers)

    http(f"{cfg['hass_url'].rstrip('/')}/api/services/media_player/play_media",
         {"entity_id": cfg["entity"], "media_content_id": media_url,
          "media_content_type": cfg["content_type"]}, headers)

    # ждём, пока поток дойдёт до конца, и глушим его до возврата очереди
    deadline = time.time() + PLAY_TIMEOUT
    stopped = False
    while time.time() < deadline:
        time.sleep(0.35)
        st = bluos.status()
        totlen, secs = st.get("totlen"), st.get("secs")
        streaming = st.get("serviceName") is None and totlen not in (None, "0")
        if streaming and secs is not None:
            try:
                if float(secs) >= float(totlen) - END_MARGIN:
                    bluos.cmd("/Stop")
                    stopped = True
                    break
            except ValueError:
                pass
        elif not streaming and time.time() > deadline - PLAY_TIMEOUT + 4:
            break  # объявление даже не началось
    if not stopped:
        bluos.cmd("/Stop")
    report["stopped"] = stopped

    if was_playing and had_track:
        try:
            cmd = f"/Play?seek={int(float(saved_secs or 0))}&id={int(float(saved_song))}"
        except ValueError:
            cmd = "/Play"
        bluos.cmd(cmd)
        report["restore_cmd"] = cmd
    else:
        # Не играло -> встаём на паузу. Важно: /Pause из состояния stop не держится
        # (BluOS сразу возвращается в stop), поэтому сначала возвращаем прежний трек
        # и позицию через /Play?seek&id, и только затем ставим паузу — тогда плеер
        # действительно стоит на паузе на том же месте, без перескока на следующий трек.
        if had_track:
            try:
                seek_cmd = f"/Play?seek={int(float(saved_secs or 0))}&id={int(float(saved_song))}"
            except ValueError:
                seek_cmd = "/Play"
            bluos.cmd(seek_cmd)
            time.sleep(0.8)
            bluos.cmd("/Pause")
            report["restore_cmd"] = f"{seek_cmd} + /Pause"
        else:
            bluos.cmd("/Stop")
            report["restore_cmd"] = "/Stop"
    time.sleep(1.2)
    report["after"] = bluos.status()

    if not cfg["keep_file"]:
        remove_file(REMOTE_NAME, cfg["ssh"], cfg["www"], cfg["ssh_key"], cfg["container"])
        report["file_removed"] = True
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Озвучить текст на Bluesound через Home Assistant")
    ap.add_argument("text", nargs="?", help="текст для озвучки")
    ap.add_argument("--config", default=os.environ.get("BLUOS_ANNOUNCE_CONFIG"),
                    help="файл локальных настроек стенда (по умолчанию "
                         "~/.config/hermes-bluos-announce/config.json)")
    ap.add_argument("--hass-url", help="адрес инстанса HA по API, напр. http://192.168.1.10:8123")
    ap.add_argument("--media-base", help="адрес, по которому mp3 скачивает САМ плеер (LAN-IP HA); "
                                         "по умолчанию = --hass-url")
    ap.add_argument("--entity", help="media_player.…")
    ap.add_argument("--bluos", help="прямой BluOS API плеера, напр. http://192.168.1.20:11000")
    ap.add_argument("--ssh", help="ssh-цель хоста HA для scp, напр. user@192.168.1.10")
    ap.add_argument("--www", help="каталог www инстанса HA (внутри контейнера — /config/www)")
    ap.add_argument("--ssh-key", help="приватный ключ для scp/ssh")
    ap.add_argument("--docker-container", help="контейнер HA: доставка через docker cp вместо scp")
    ap.add_argument("--base-url", help="OpenAI-совместимый TTS-эндпоинт (напр. база LiteLLM)")
    ap.add_argument("--model", help="модель/алиас TTS (по умолчанию audio/tts)")
    ap.add_argument("--voice", help="голос (по умолчанию matilda, см. --voice-list)")
    ap.add_argument("--volume", type=float, help="громкость 0..1 перед объявлением")
    ap.add_argument("--content-type", help="media_content_type (по умолчанию music)")
    ap.add_argument("--token-env", default="HASS_TOKEN",
                    help="имя env-переменной с токеном нужного инстанса HA")
    ap.add_argument("--key-env", default="LITELLM_API_KEY",
                    help="имя env-переменной с ключом TTS")
    ap.add_argument("--keep-file", action="store_true", help="не удалять mp3 с хоста HA")
    ap.add_argument("--voice-list", action="store_true", help="показать известные голоса")
    ap.add_argument("--write-config", metavar="PATH",
                    help="сохранить переданные настройки стенда в файл и выйти "
                         "(секреты туда не пишутся)")
    args = ap.parse_args()

    if args.voice_list:
        print(" ".join(DEFAULT_VOICES))
        return 0

    cfg_file = load_config(os.path.expanduser(args.config)) if args.config else load_config()

    if args.write_config:
        to_save = {
            "hass_url": args.hass_url, "media_base": args.media_base, "entity": args.entity,
            "bluos": args.bluos, "ssh": args.ssh, "www": args.www, "ssh_key": args.ssh_key,
            "container": args.docker_container, "tts_base": args.base_url, "tts_model": args.model,
            "voice": args.voice, "volume": args.volume, "content_type": args.content_type,
        }
        to_save = {k: v for k, v in to_save.items() if v not in (None, "")}
        path = os.path.expanduser(args.write_config)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(to_save, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"настройки стенда сохранены в {path}")
        return 0

    if not args.text:
        ap.error("нужен текст для озвучки")

    resolved = {
        "hass_url": pick(args.hass_url, "HASS_ANNOUNCE_HASS_URL", cfg_file, "hass_url"),
        "media_base": pick(args.media_base, "HASS_ANNOUNCE_MEDIA_BASE", cfg_file, "media_base"),
        "entity": pick(args.entity, "HASS_ANNOUNCE_ENTITY", cfg_file, "entity"),
        "bluos": pick(args.bluos, "HASS_ANNOUNCE_BLUOS", cfg_file, "bluos"),
        "ssh": pick(args.ssh, "HASS_ANNOUNCE_SSH", cfg_file, "ssh"),
        "www": pick(args.www, "HASS_ANNOUNCE_WWW", cfg_file, "www"),
        "ssh_key": pick(args.ssh_key, "HASS_ANNOUNCE_SSH_KEY", cfg_file, "ssh_key"),
        "container": pick(args.docker_container, "HASS_ANNOUNCE_CONTAINER", cfg_file, "container"),
        "tts_base": pick(args.base_url, "HASS_ANNOUNCE_TTS_BASE", cfg_file, "tts_base"),
        "tts_model": pick(args.model, "HASS_ANNOUNCE_TTS_MODEL", cfg_file, "tts_model"),
        "voice": pick(args.voice, "HASS_ANNOUNCE_VOICE", cfg_file, "voice"),
        "volume": args.volume if args.volume is not None else cfg_file.get("volume"),
        "content_type": pick(args.content_type, "HASS_ANNOUNCE_CONTENT_TYPE", cfg_file,
                             "content_type"),
    }

    missing = [n for n, v in (("--hass-url", resolved["hass_url"]), ("--entity", resolved["entity"]),
                              ("--bluos", resolved["bluos"]), ("--base-url", resolved["tts_base"]))
               if not v]
    if not (resolved["www"] or resolved["container"]):
        missing.append("--www или --docker-container")
    if missing:
        ap.error("не задано: " + ", ".join(missing)
                 + f" (аргумент, переменная окружения или {CONFIG_PATH})")

    os.environ["TTS_KEY_ENV"] = args.key_env
    cfg = {
        "hass_url": resolved["hass_url"],
        "media_base": resolved["media_base"] or resolved["hass_url"],
        "entity": resolved["entity"],
        "bluos": resolved["bluos"],
        "ssh": resolved["ssh"] or None,
        "www": resolved["www"] or None,
        "ssh_key": resolved["ssh_key"] or None,
        "container": resolved["container"] or None,
        "base_url": resolved["tts_base"],
        "model": resolved["tts_model"] or "audio/tts",
        "token_env": args.token_env,
        "volume": resolved["volume"],
        "content_type": resolved["content_type"] or "music",
        "keep_file": args.keep_file,
    }
    report = announce(args.text, resolved["voice"] or "matilda", cfg)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("after") else 1


if __name__ == "__main__":
    sys.exit(main())
