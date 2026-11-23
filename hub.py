#!/usr/bin/env python3
"""橙芯绘话 - a desktop workbench for local models, drawing and speech.

It ties together whatever you have installed locally:

    chat models  ->  llama.cpp llama-server processes started from .bat files
    drawing      ->  Stable Diffusion WebUI (AUTOMATIC1111 / Akiba bundle)
    speech       ->  IndexTTS2 (Gradio app, voice cloning)

All paths and ports come from config.json (see config.py). The hub is a tiny
local server that

  * serves the single-page UI in ./ui,
  * proxies /v1/* to whichever model you picked (no CORS / mixed-origin trouble),
  * starts / stops / restarts the services and reports their state,
  * enforces "one GPU service at a time" so a second model never evicts the first,
  * opens Microsoft Edge in --app mode so it feels like a native window.

Everything binds to 127.0.0.1 and refuses foreign Host headers, so no other
machine on the LAN can drive your GPU.

    pythonw hub.py                 # normal start (no console window)
    python  hub.py --no-browser    # server only, open http://127.0.0.1:8090 yourself
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import comfy_backend
import config
import download

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
LOG_DIR = os.path.join(HERE, "logs")

HOST = "127.0.0.1"
PORT = 8090

# Single source of truth for the version. It shows up in the HTTP Server header,
# in /api/state, and in the sidebar footer, so it is defined once here.
APP_NAME = "5070-AI"
APP_VERSION = "1.6"
APP_TITLE = "橙芯绘话"          # window title, also how we find the window

# --------------------------------------------------------------------------
# model definitions - mirrors the two desktop shortcuts
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Everything machine-specific comes from config.json (see config.py). The repo
# ships config.example.json; copy it to config.json and point the paths at your
# own copies of llama.cpp, Stable Diffusion and IndexTTS.
# --------------------------------------------------------------------------
CFG = config.CFG

UNCDIR = CFG.get("llama_dir") or ""


def _abs(base: str, path: str) -> str:
    """Resolve `path` against `base` unless it is already absolute."""
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(base, path)


def _build_models() -> dict:
    out = {}
    for raw in CFG.get("models") or []:
        mid = str(raw.get("id") or "").strip()
        if not mid:
            continue
        out[mid] = {
            "id": mid,
            "label": raw.get("label") or mid.upper(),
            "short": raw.get("short") or mid.upper(),
            "blurb": raw.get("blurb") or "",
            "color": raw.get("color") or "#8ce99a",
            "port": int(raw.get("port") or 0),
            "bat": _abs(UNCDIR, raw.get("bat") or ""),
            "model": _abs(UNCDIR, raw.get("model") or ""),
            "mmproj": _abs(UNCDIR, raw.get("mmproj") or ""),
            "ctx": int(raw.get("ctx") or 40960),
            "args": list(raw.get("args") or []),
            "log": os.path.join(LOG_DIR, f"server-{mid}.log"),
            "shortcut": raw.get("shortcut") or "",
            "about": raw.get("about") or "",
            # where to fetch the weights from when they are missing
            "url": raw.get("url") or "",
            "hf_repo": raw.get("hf_repo") or "",
            "hf_file": raw.get("hf_file") or "",
            "mmproj_url": raw.get("mmproj_url") or "",
            "mmproj_hf_file": raw.get("mmproj_hf_file") or "",
        }
    return out


MODELS = _build_models()
DEFAULT_MODEL = CFG.get("default_model") or (next(iter(MODELS), ""))


def _build_sd() -> dict:
    raw = CFG.get("sd") or {}
    base = raw.get("dir") or ""
    py = raw.get("python") or ""
    if not py and base:
        # the Akiba bundle keeps its interpreter here; other installs override it
        py = os.path.join(base, "python", "python.exe")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "label": raw.get("label") or "SD 绘图",
        "dir": base,
        "python": py,
        "port": int(raw.get("port") or 7860),
        "args": list(raw.get("args") or
                     ["launch.py", "--api", "--no-hashing",
                      "--skip-python-version-check"]),
        "log": os.path.join(LOG_DIR, "sd.log"),
    }


SD = _build_sd()


def _build_tts() -> dict:
    raw = CFG.get("tts") or {}
    base = raw.get("dir") or ""
    py = raw.get("python") or ""
    if not py and base:
        py = os.path.join(base, ".venv", "Scripts", "python.exe")
    ref = raw.get("ref_dir") or ""
    if not ref and base:
        ref = os.path.join(base, "examples")
    tts_port = int(raw.get("port") or 7862)
    return {
        "enabled": bool(raw.get("enabled", True)),
        "label": raw.get("label") or "语音合成",
        "dir": base,
        "python": py,
        "port": tts_port,
        # tts_launch.py works around a kaldifst/torch DLL clash; see that file
        "launcher": os.path.join(HERE, "tts_launch.py"),
        "args": list(raw.get("args") or
                     ["--port", str(tts_port), "--host", "127.0.0.1"]),
        "log": os.path.join(LOG_DIR, "tts.log"),
        "ref_dir": ref,
        "user_voices": os.path.join(HERE, "data", "voices"),
        "default_voice": raw.get("default_voice") or "",
        "allow_bundled": list(raw.get("allow_bundled") or []),
        "hide": list(raw.get("hide") or []),
    }


TTS = _build_tts()

# Appended to every SD prompt. These are style guards, not content filters: they
# keep the default look photoreal instead of drifting into the model's default
# illustration style.
SD_STYLE_GUARD = ((CFG.get("sd") or {}).get("style_guard")
                  or "detailed skin texture, natural lighting, film grain, sharp focus")

# per-checkpoint display name / note for the drawing panel
SD_MODEL_INFO = ((CFG.get("sd") or {}).get("checkpoints") or {})


def bat_port(path: str) -> int | None:
    """Extract the --port value a .bat passes to llama-server.

    The port is declared twice - in config.json and inside the .bat - and a
    mismatch is invisible: the server starts fine on its own port, while the hub
    polls the other one and eventually reports "process exited", which sends
    people looking in the wrong place. Reading the file is cheap, so check it.
    Returns None when the file cannot be read or has no --port.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    # `--port 8082` or `--port=8082`, in any argument position
    m = re.search(r"--port[=\s]+(\d{2,5})", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def config_problems() -> list[str]:
    """Catch the mistakes that would otherwise surface as a confusing failure.

    Ports are declared twice (once here, once inside each .bat), so a typo shows
    up as "hub started it but nothing is listening" instead of an obvious error.
    Check the cheap things up front and report them in the UI.
    """
    out = []
    seen: dict[int, str] = {}
    for cfg in MODELS.values():
        port = cfg["port"]
        if not 1 <= port <= 65535:
            out.append(f"{cfg['label']} 的端口 {port} 不合法")
            continue
        if port in seen:
            out.append(f"端口 {port} 被 {seen[port]} 和 {cfg['label']} 同时使用")
        seen[port] = cfg["label"]
        if not cfg["bat"]:
            out.append(f"{cfg['label']} 没配置启动脚本（bat）")
        elif not os.path.exists(cfg["bat"]):
            out.append(f"{cfg['label']} 的启动脚本不存在：{cfg['bat']}")
        else:
            # the .bat may bind a different port than config.json claims
            declared = bat_port(cfg["bat"])
            if declared is None:
                out.append(f"{cfg['label']} 的启动脚本里找不到 --port，"
                           f"启动后 hub 将无法确认它在监听哪个端口")
            elif declared != port:
                out.append(f"{cfg['label']} 端口不一致：config.json 写的是 {port}，"
                           f"但 {os.path.basename(cfg['bat'])} 里是 --port {declared}。"
                           f"这会导致启动后一直显示「加载中」，请改成同一个值")
        if not cfg["model"]:
            out.append(f"{cfg['label']} 没配置模型文件（model）")
        elif not os.path.exists(cfg["model"]):
            out.append(f"{cfg['label']} 的模型文件不存在：{cfg['model']}")
        # a downloadable model whose local name differs from the download target
        # will "finish downloading" and still fail to start
        hf_file = cfg.get("hf_file") or ""
        if hf_file and cfg["model"] and \
                os.path.basename(cfg["model"]) != os.path.basename(hf_file):
            out.append(f"{cfg['label']} 的 model（{os.path.basename(cfg['model'])}）"
                       f"和 hf_file（{os.path.basename(hf_file)}）不一致，"
                       f"下载完成后仍然找不到文件")
    for name, cfg in (("SD", SD), ("语音合成", TTS)):
        if not cfg.get("enabled"):
            continue
        port = cfg["port"]
        if port in seen:
            out.append(f"端口 {port} 被 {seen[port]} 和 {name} 同时使用")
        seen[port] = name
        if not cfg.get("dir"):
            out.append(f"{name} 没配置安装目录（dir），功能不可用")
        elif not os.path.isdir(cfg["dir"]):
            out.append(f"{name} 的目录不存在：{cfg['dir']}")
    if PORT in seen:
        out.append(f"端口 {PORT} 被 hub 自己和 {seen[PORT]} 同时使用")
    return out

HTTP_TIMEOUT_DOWNLOAD = 3600      # one big prefill may take minutes
_spawn_lock = threading.Lock()


# --------------------------------------------------------------------------
# process helpers (Windows, no external deps)
# --------------------------------------------------------------------------
def port_open(port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((HOST, port)) == 0


def server_pids(port: int) -> list[int]:
    """Every llama-server.exe bound to `port` (plus children of the bat box)."""
    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(pids)


def kill_port(port: int) -> list[int]:
    killed = []
    for pid in server_pids(port):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        killed.append(pid)
    return killed


def wait_port_free(port: int, timeout: float = 20.0) -> bool:
    """Block until `port` stops listening.

    taskkill returning is not the same as the process being gone: llama-server
    keeps its socket for a moment and, more importantly, the driver has not
    released the VRAM yet. Starting the next model before that finishes is what
    makes a swap fail with "out of memory" on a single card.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port) and not server_pids(port):
            return True
        time.sleep(0.25)
    return False


def gpu_memory() -> dict | None:
    """Free/total VRAM in MiB, or None when nvidia-smi is unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
        if not out:
            return None
        first = out.splitlines()[0]
        total, used, free = (int(x.strip()) for x in first.split(",")[:3])
        return {"total": total, "used": used, "free": free}
    except Exception:  # noqa: BLE001
        return None


def wait_gpu_freed(min_free_mib: int, timeout: float = 25.0) -> dict:
    """Wait until at least `min_free_mib` is available.

    Returns {"ok": bool, "free": int, "total": int}. When nvidia-smi is missing
    we cannot judge, so report ok and let the launch decide.
    """
    mem = gpu_memory()
    if mem is None:
        return {"ok": True, "free": 0, "total": 0, "unknown": True}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mem["free"] >= min_free_mib:
            return {"ok": True, **mem}
        time.sleep(0.5)
        mem = gpu_memory() or mem
    return {"ok": mem["free"] >= min_free_mib, **mem}


# --------------------------------------------------------------------------
# health probing
# --------------------------------------------------------------------------
_health_cache: dict[str, tuple[float, dict]] = {}


def probe(cfg: dict, fresh: bool = False) -> dict:
    """Ping /v1/models on the model port; cached for 2 s."""
    mid, port = cfg["id"], cfg["port"]
    now = time.time()
    if not fresh:
        cached = _health_cache.get(mid)
        if cached and now - cached[0] < 2.0:
            return cached[1]

    state = {
        "id": mid, "label": cfg["label"], "short": cfg["short"],
        "blurb": cfg["blurb"], "color": cfg["color"], "port": port,
        "ctx": cfg["ctx"], "about": cfg["about"],
        "bat": cfg["bat"], "shortcut": cfg["shortcut"], "gguf": cfg["model"],
        "up": False, "loading": False, "error": None,
        "model": None, "slots": None, "tokens": None,
        "n_ctx": cfg["ctx"],      # real value once /props answers
        # what still has to be downloaded before this model can start
        "missing": [p for _f, p in download.missing(cfg)],
        "downloadable": bool(cfg.get("url") or
                             (cfg.get("hf_repo") and cfg.get("hf_file"))),
    }
    if not port_open(port):
        # a load that never binds its port is the classic "stuck in 加载中"
        dead = check_spawn_health(cfg)
        if dead:
            state["error"] = dead["error"]
            state["loading"] = False
            state["dead"] = True
        else:
            state["loading"] = spawn_in_progress(mid)
    else:
        # a bound port without a working API means the weights are still loading
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/v1/models", timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            listed = data.get("data") or []
            state["model"] = listed[0].get("id") if listed else None
            state["up"] = True
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            state["error"] = str(exc)[:200]
            state["loading"] = spawn_in_progress(mid)

        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/props", timeout=4) as r:
                props = json.loads(r.read().decode("utf-8", "replace"))
            settings = props.get("default_generation_settings") or {}
            state["tokens"] = (props.get("total_slots") or 0) and (
                settings.get("n_ctx") or cfg["ctx"])
            state["n_ctx"] = settings.get("n_ctx") or cfg["ctx"]
        except Exception:
            pass

    _health_cache[mid] = (now, state)
    return state


_spawning: dict[str, float] = {}


def spawn_in_progress(mid: str) -> bool:
    started = _spawning.get(mid)
    if started is None:
        return False
    if time.time() - started > 600:
        _spawning.pop(mid, None)
        return False
    return True


def _launch(cmd: list[str], log_path: str, cwd: str | None,
            env: dict | None, banner: str) -> dict:
    """Spawn a service with its stdout going straight to a log file.

    Shared by the chat models, SD and TTS: they differ only in the command line,
    the working directory and any extra environment, and every one of them needs
    the same three precautions:

      * stdout to a file, never a pipe - an undrained pipe fills up and blocks
        the child mid-startup (this is what made model loading hang);
      * stdin DEVNULL, so a trailing `pause` in a .bat sees EOF and exits;
      * CREATE_NO_WINDOW, so nothing flashes on screen.

    Returns {"ok": bool, "message": str}; on success the caller records its own
    "started at" timestamp.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        log = open(log_path, "wb", buffering=0)
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "message": f"无法写入日志：{exc}"}
    log.write(f"\n\n===== hub start {banner} "
              f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode("utf-8"))
    try:
        subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"启动失败：{exc}"}
    finally:
        log.close()
    return {"ok": True, "message": ""}


def _free_gpu(keep_id: str = "", need_mib: int = 0,
              wait_timeout: float = 25.0) -> dict:
    """Stop every other GPU user, then wait for the memory to actually come back.

    taskkill returning is not the same as the VRAM being released: the driver
    needs a moment, and starting the next service before that finishes is what
    produces an out-of-memory that looks like a model bug.

    `keep_id` names the chat model to leave alone (empty means stop everything).
    Returns {"ok": bool, "freed": [labels], "message": str}.
    """
    global _sd_spawning, _tts_spawning  # noqa: PLW0603
    freed: list[str] = []
    ports: list[int] = []

    for other in MODELS.values():
        if other["id"] == keep_id:
            continue
        if kill_port(other["port"]):
            freed.append(other["label"])
            ports.append(other["port"])
        _spawning.pop(other["id"], None)
        _health_cache.pop(other["id"], None)

    if SD.get("enabled") and kill_port(SD["port"]):
        freed.append(SD["label"])
        _sd_spawning = 0.0
        ports.append(SD["port"])
    if TTS.get("enabled") and kill_port(TTS["port"]):
        freed.append(TTS["label"])
        _tts_spawning = 0.0
        ports.append(TTS["port"])
    # ComfyUI is a GPU user too, and it is not one of the services above:
    # leaving it running is exactly the "two services on one card" case.
    if keep_id != "comfy":
        try:
            if comfy_backend.is_up():
                comfy_backend.stop()
                freed.append("Qwen 图像")
                ports.append(comfy_backend.COMFY_PORT)
        except Exception:  # noqa: BLE001
            pass

    if not freed:
        return {"ok": True, "freed": [], "message": ""}

    for port in ports:
        wait_port_free(port, timeout=15)

    if need_mib:
        mem = wait_gpu_freed(need_mib, timeout=wait_timeout)
        if not mem.get("ok"):
            return {
                "ok": False, "freed": freed,
                "message": f"显存没释放干净：当前空闲 {mem.get('free', 0)} MB"
                           f" / 共 {mem.get('total', 0)} MB，低于需要的约 "
                           f"{need_mib} MB。可能有别的程序占着显卡，"
                           f"关掉再试（或重启一次）。",
            }
    return {"ok": True, "freed": freed, "message": ""}


def start_model(cfg: dict) -> dict:
    mid = cfg["id"]
    if port_open(cfg["port"]) or server_pids(cfg["port"]) or spawn_in_progress(mid):
        return {"ok": True, "message": "已经在运行或正在加载"}

    bat = cfg["bat"]
    if not os.path.exists(bat):
        return {"ok": False, "message": f"启动脚本不存在：{bat}"}
    if not os.path.exists(cfg["model"]):
        return {"ok": False, "message": f"模型文件不存在：{cfg['model']}"}

    # The caller already confirmed replacing whatever is loaded (see
    # _model_action); free it now so this one gets the whole card.
    # 9B needs ~7GB, 35B ~20GB - ask for a floor and report a real number.
    need = 6000 if mid == "9b" else 12000
    freed = _free_gpu(keep_id=mid, need_mib=need)
    if not freed["ok"]:
        return {"ok": False, "message": freed["message"]}

    env = os.environ.copy()
    env["PATH"] = os.path.join(UNCDIR, "bin") + os.pathsep + env.get("PATH", "")
    res = _launch(["cmd", "/c", bat], cfg["log"], UNCDIR, env, cfg["label"])
    if not res["ok"]:
        return res

    _spawning[mid] = time.time()
    _health_cache.pop(mid, None)
    note = f"（已先停掉 {', '.join(freed['freed'])}）" if freed["freed"] else ""
    return {"ok": True, "message": f"{cfg['label']} 开始加载{note}，通常 30-60 秒"}


def spawn_timeout(cfg: dict) -> float:
    """How long a model may sit in "loading" before we call it dead."""
    return 180.0 if cfg["id"] == "35b" else 120.0


# A launcher that is still not listening after this long is not loading any more.
CRASH_CHECK_AFTER = 12.0

# Other ports worth probing when ours stays closed: if one of them answers, the
# .bat and config.json simply disagree, which needs a different message.
def _other_ports(cfg: dict) -> list[int]:
    found = []
    declared = bat_port(cfg["bat"]) if cfg.get("bat") else None
    if declared and declared != cfg["port"]:
        found.append(declared)
    for other in MODELS.values():
        if other["id"] != cfg["id"] and other["port"] != cfg["port"]:
            found.append(other["port"])
    for other in (SD, TTS):
        if other.get("enabled") and other.get("port") not in (None, cfg["port"]):
            found.append(other["port"])
    return found


def check_spawn_health(cfg: dict) -> dict | None:
    """Report a stuck or crashed load instead of showing "加载中" forever.

    Returns None while it is still plausibly loading, or a dict describing the
    failure. Called from probe() so every poll benefits.

    Three outcomes, because each needs a different fix:
      * another port answers -> config.json and the .bat disagree on --port
      * process is gone      -> it crashed; read the log
      * alive, still nothing -> out of VRAM, or a model too large for the card
    """
    mid = cfg["id"]
    started = _spawning.get(mid)
    if not started:
        return None
    if port_open(cfg["port"]):
        return None                      # bound the port, give it time to answer

    elapsed = time.time() - started

    # 1) our port is closed but a sibling port answers: a typo, not a crash.
    #    Checked early because waiting out the timeout explains nothing.
    if elapsed >= 6.0:
        for other_port in _other_ports(cfg):
            if port_open(other_port):
                _spawning.pop(mid, None)
                _health_cache.pop(mid, None)
                return {
                    "error": f"端口不一致：模型似乎监听在 {other_port}，但 "
                             f"config.json 配的是 {cfg['port']}。"
                             f"把 .bat 里的 --port 和 config.json 的 port "
                             f"改成同一个值",
                    "hint": "config",
                }

    # 2) nothing has bound after the grace period: the launcher died
    if elapsed >= CRASH_CHECK_AFTER and not server_pids(cfg["port"]):
        _spawning.pop(mid, None)
        _health_cache.pop(mid, None)
        return {"error": "启动失败：进程已退出，看日志确认原因",
                "hint": "log"}

    # 3) still there but never ready - usually VRAM
    if elapsed < spawn_timeout(cfg):
        return None
    _spawning.pop(mid, None)
    _health_cache.pop(mid, None)
    if not server_pids(cfg["port"]):
        return {"error": "启动失败：进程已退出，看日志确认原因",
                "hint": "log"}
    return {"error": f"加载超时（{int(elapsed)} 秒仍未就绪），可能是显存不足",
            "hint": "log"}


def stop_model(cfg: dict) -> dict:
    mid, port = cfg["id"], cfg["port"]
    killed = kill_port(port)
    _spawning.pop(mid, None)
    _health_cache.pop(mid, None)
    if not killed:
        return {"ok": True, "message": "本来就没在运行"}
    # confirm the port is really gone so the next start sees a clean slate
    gone = wait_port_free(port, timeout=15)
    mem = gpu_memory()
    tail = ""
    if mem:
        tail = f"，现存空闲 {mem['free']} MB"
    if not gone:
        return {"ok": False,
                "message": f"已发送停止指令（PID {', '.join(map(str, killed))}），"
                           f"但端口 {port} 仍被占用，稍等几秒再试"}
    return {"ok": True, "message": f"已停止（PID {', '.join(map(str, killed))}）{tail}"}


# --------------------------------------------------------------------------
# Stable Diffusion control
# --------------------------------------------------------------------------
_sd_spawning = 0.0


# Display names and short notes for the four checkpoints. The real filenames
# stay untouched: renaming a checkpoint breaks SD's internal references.
SD_MODEL_INFO = {
    "AOM3A2": {
        "display": "AOM3A2 · 二次元",
        "note": "动漫/插画风，角色立绘、皮肤通透；轻量 2GB，出图快。适合二次元人物、同人图。",
        "tags": "二次元 · 立绘",
    },
    "AbyssOrangeMix2_nsfw": {
        "display": "AOM2 · 成人向",
        "note": "橙漫 2 系的成人向版本，二次元画风、色彩浓。适合成人向插画；5GB，加载稍慢。",
        "tags": "成人向 · 二次元",
    },
    "majicmixRealistic_v7": {
        "display": "MajicMix v7 · 写实人像",
        "note": "写实人像首选，皮肤质感细腻、光影自然；你原来的默认模型。适合真人、人像特写。",
        "tags": "写实 · 人像",
    },
    "Realistic_Vision_V2.0": {
        "display": "Realistic Vision V2 · 通用写实",
        "note": "写实底模，风景、场景、物体都稳，构图耐看。适合风景、街景、非人物题材。",
        "tags": "写实 · 风景场景",
    },
}


def sd_model_info(name: str) -> dict:
    """Look up the display name / note for a checkpoint filename."""
    base = str(name or "").split(" [")[0]
    for key, info in SD_MODEL_INFO.items():
        if base.startswith(key):
            return info
    return {"display": base, "note": "", "tags": ""}


# SD1.5 and SDXL are trained at different resolutions and want different
# sampler settings. Detect the family from the checkpoint name/hash so the UI can
# offer sane defaults instead of generating 512px SDXL (blurry) or 1024px SD1.5
# (duplicated subjects).
SDXL_HINTS = ("sdxl", "xl_", "_xl", "sd_xl", "sd3", "pony", "illustrious",
              "noobai", "flux")


def checkpoint_family(name: str, info: dict | None = None) -> str:
    """'sdxl' or 'sd15'. SDXL checkpoints carry a different UNet signature, and
    the sd-models endpoint reports the hash only - so fall back to the name."""
    text = f"{name or ''}"
    low = text.lower()
    if any(h in low for h in SDXL_HINTS):
        return "sdxl"
    # SDXL weights are ~2x larger; the filename often carries the size in the
    # 6.5GB range and SD1.5 in the 2GB range, but that is not in the API response.
    return "sd15"


def family_defaults(family: str) -> dict:
    """Recommended width/height/steps/cfg for a checkpoint family."""
    if family == "sdxl":
        return {"width": 1024, "height": 1024, "steps": 28, "cfg": 5.0,
                "note": "SDXL 原生 1024×1024；低于 768 会糊，高于 1280 会重复主体"}
    return {"width": 832, "height": 832, "steps": 28, "cfg": 7.0,
            "note": "SD1.5 原生 512 基准；768-832 是常用折中"}


def sd_probe() -> dict:
    """State of the SD WebUI, including which checkpoints it offers."""
    port = SD["port"]
    state = {
        "label": SD["label"], "port": port, "dir": SD["dir"],
        "up": False, "loading": False, "error": None,
        "model": None, "checkpoints": [],
        # available regardless of whether SD is running: these come from disk
        "controlnet": controlnet_models(),
    }
    if not port_open(port):
        started = _sd_spawning
        state["loading"] = bool(started) and (time.time() - started) < 900
        return state

    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/sdapi/v1/options", timeout=8) as r:
            opts = json.loads(r.read().decode("utf-8", "replace"))
        state["up"] = True
        state["model"] = opts.get("sd_model_checkpoint")
        fam = checkpoint_family(state["model"] or "")
        state["family"] = fam
        state["defaults"] = family_defaults(fam)
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)[:160]
        state["loading"] = True     # port bound but API not answering yet
        return state

    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/sdapi/v1/sd-models", timeout=8) as r:
            models = json.loads(r.read().decode("utf-8", "replace"))
        cps = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("title") or m.get("model_name")
            info = sd_model_info(name)
            fam = checkpoint_family(name)
            cps.append({
                "title": m.get("model_name") or m.get("title"),
                "name": name,
                "display": info["display"],
                "note": info["note"],
                "tags": info["tags"],
                "family": fam,
                "defaults": family_defaults(fam),
            })
        state["checkpoints"] = cps
    except Exception:  # noqa: BLE001
        pass
    return state


# --------------------------------------------------------------------------
# IndexTTS control
# --------------------------------------------------------------------------
_tts_spawning = 0.0


def tts_voices() -> list[dict]:
    """Reference voices offered for cloning.

    The user's own recording comes first and is the default. Bundled samples are
    limited to TTS["allow_bundled"] so the picker stays short; everything else
    stays on disk but hidden.
    """
    allowed = set(TTS.get("allow_bundled") or [])
    hidden = set(TTS.get("hide") or [])
    mine = []
    others = []
    seen = set()

    for base in (TTS["user_voices"], TTS["ref_dir"]):
        if not base or not os.path.isdir(base):
            continue
        is_user = base == TTS["user_voices"]
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                continue
            if name in hidden or name in seen:
                continue
            # bundled samples only when explicitly allowed
            if not is_user and name not in allowed:
                continue
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            seen.add(name)
            entry = {
                "name": name,
                "path": path,
                "bytes": os.path.getsize(path),
                "user": is_user,
                "default": name == TTS.get("default_voice"),
            }
            (mine if is_user else others).append(entry)

    # user recordings first, and within them the chosen default at the very top
    mine.sort(key=lambda v: (not v["default"], v["name"]))
    return mine + others


def tts_default_voice() -> str:
    """The voice to use when the user has not picked one."""
    want = TTS.get("default_voice")
    for v in tts_voices():
        if v["name"] == want:
            return want
    first = tts_voices()
    return first[0]["name"] if first else ""


def tts_probe() -> dict:
    """State of the IndexTTS service."""
    port = TTS["port"]
    state = {
        "label": TTS["label"], "port": port, "dir": TTS["dir"],
        "up": False, "loading": False, "error": None,
        "voices": tts_voices(),
        "defaultVoice": tts_default_voice(),
    }
    if not port_open(port):
        started = _tts_spawning
        state["loading"] = bool(started) and (time.time() - started) < 900
        return state
    # Gradio answers /config once its app is built; that is our readiness probe
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/config", timeout=8) as r:
            json.loads(r.read().decode("utf-8", "replace"))
        state["up"] = True
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)[:160]
        state["loading"] = True        # port bound, app still building
    return state


def tts_start() -> dict:
    """Start IndexTTS2 on its own port, unloading the other GPU users."""
    global _tts_spawning  # noqa: PLW0603
    if port_open(TTS["port"]):
        return {"ok": True, "message": "语音合成已经在运行"}
    py = TTS["python"]
    if not os.path.exists(py):
        return {"ok": False, "message": f"找不到 IndexTTS 的 Python：{py}"}
    if not os.path.exists(os.path.join(TTS["dir"], "webui.py")):
        return {"ok": False, "message": f"找不到 webui.py：{TTS['dir']}"}

    freed = _free_gpu(need_mib=9000)   # one GPU, so the others must go
    if not freed["ok"]:
        return {"ok": False, "message": freed["message"]}

    os.makedirs(TTS["user_voices"], exist_ok=True)
    env = os.environ.copy()
    # use the mirror so first-run model fetching works behind the GFW
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["HF_HOME"] = os.path.join(TTS["dir"], ".huggingface")
    launcher = TTS.get("launcher")
    if launcher and os.path.exists(launcher):
        cmd = [py, launcher, TTS["dir"], *TTS["args"]]
    else:
        cmd = [py, os.path.join(TTS["dir"], "webui.py"), *TTS["args"]]

    res = _launch(cmd, TTS["log"], TTS["dir"], env, "TTS")
    if not res["ok"]:
        return res

    _tts_spawning = time.time()
    note = f"（已卸载 {'、'.join(freed['freed'])}）" if freed["freed"] else ""
    return {"ok": True, "message": f"语音合成开始启动{note}，首次加载要 1-3 分钟"}


def tts_stop() -> dict:
    global _tts_spawning  # noqa: PLW0603
    killed = kill_port(TTS["port"])
    _tts_spawning = 0.0
    if not killed:
        return {"ok": True, "message": "语音合成本来就没在运行"}
    return {"ok": True, "message": f"语音合成已停止（PID {', '.join(map(str, killed))}）"}


def _tts_upload(path: str) -> str | None:
    """Upload a local audio file to the Gradio app and return its saved path.

    Gradio only accepts files that arrived through its upload endpoint, so the
    reference voice has to be copied over before gen_single will take it.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    name = os.path.basename(path)
    boundary = "----strata" + hashlib.sha1(raw[:64] + name.encode()).hexdigest()[:16]
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        raw,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"http://{HOST}:{TTS['port']}/gradio_api/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            files = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        _dbg(f"tts upload failed: {exc}")
        return None
    if isinstance(files, list) and files and isinstance(files[0], str):
        return files[0]
    return None


def _pick_voice(name: str) -> str:
    """Resolve a voice name to a file path; falls back to the default voice."""
    wanted = (name or "").strip()
    voices = tts_voices()
    if wanted:
        for v in voices:
            if v["name"] == wanted:
                return v["path"]
    # tts_voices() puts the user's default first
    return voices[0]["path"] if voices else ""


def _tts_args(text: str, uploaded: str) -> list:
    """The 24 positional values gen_single expects, in UI declaration order.

    Gradio refuses local paths it did not receive through its upload endpoint,
    so `uploaded` must be the server-side copy returned by _tts_upload.
    """
    def file_data(path: str) -> dict:
        return {"path": path, "meta": {"_type": "gradio.FileData"}}

    return [
        "与音色参考音频相同",   # 0  emotion control: follow the reference voice
        file_data(uploaded),   # 1  音色参考音频 (cloning prompt)
        text,                  # 2  要合成的文字
        None,                  # 3  uploaded emotion reference (unused)
        0.65,                  # 4  emotion weight
        0.0, 0.0, 0.0, 0.0,    # 5-8   emotion sliders 1-4
        0.0, 0.0, 0.0, 0.0,    # 9-12  emotion sliders 5-8
        "",                    # 13 emotion description text
        False,                 # 14 random emotion
        120,                   # 15 max tokens per segment
        True,                  # 16 do_sample
        0.8,                   # 17 top_p
        30,                    # 18 top_k
        0.8,                   # 19 temperature
        0.0,                   # 20 length_penalty
        3,                     # 21 num_beams
        10.0,                  # 22 repetition_penalty
        1500,                  # 23 max_mel_tokens
    ]


class _ProgressTicker:
    """Estimate TTS progress while Gradio sends nothing but heartbeats.

    IndexTTS reports no progress of its own, so we read our own log to learn
    which stage it reached and advance a percentage over time. Deliberately
    rough: the UI labels it as an estimate.
    """

    def __init__(self, log_path: str, start_offset: int = 0):
        self.log_path = log_path
        self.offset = start_offset
        self._stop = threading.Event()
        self._value = 0.10
        self._thread: threading.Thread | None = None

    def _stage(self) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(self.offset)
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        if "starting inference" in tail:
            return "inference"
        if "weights restored" in tail or "loaded" in tail:
            return "loading"
        return ""

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            stage = self._stage()
            # a short sentence finishes in ~10 s, a long one in ~40 s, so nudge
            # faster once inference is actually running
            if stage == "inference":
                self._value = min(0.95, max(0.45, self._value) + 0.035)
                job_set("tts", progress=self._value, label="正在合成语音")
            elif stage == "loading":
                self._value = min(0.44, self._value + 0.010)
                job_set("tts", progress=self._value, label="加载模型")
            else:
                self._value = min(0.40, self._value + 0.012)
                job_set("tts", progress=self._value, label="准备中")

    def start(self) -> None:
        job_set("tts", progress=self._value, label="加载模型")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _tts_stream(base: str, event_id: str, ticker: _ProgressTicker) -> dict:
    """Read Gradio's SSE stream until the audio block arrives.

    Returns {"ok": bool, "result": list|None, "message": str}. Always stops the
    ticker and closes the stream, including on the error paths - a leaked ticker
    would keep moving the bar after the job already failed.
    """
    stream = None
    result = None
    error_line = None
    try:
        stream = urllib.request.urlopen(
            f"{base}/gradio_api/call/gen_single/{event_id}", timeout=900)
        event = ""
        data_parts: list[str] = []
        for raw_line in stream:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                # end of one SSE block
                if event == "error":
                    error_line = "\n".join(data_parts)[:300]
                    break
                if event in ("generating", "complete") and data_parts:
                    try:
                        parsed = json.loads("\n".join(data_parts))
                    except Exception:  # noqa: BLE001
                        parsed = None
                    if isinstance(parsed, list) and parsed and event == "complete":
                        result = parsed
                event = ""
                data_parts = []
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].strip())
    finally:
        ticker.stop()
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    if error_line:
        return {"ok": False, "result": None, "message": f"合成出错：{error_line}"}
    return {"ok": True, "result": result, "message": ""}


def _tts_resolve_audio(result: list | None) -> str:
    """Turn gen_single's output into an absolute path on disk.

    Gradio may echo the file, or finish without it (in which case the newest
    fresh wav under outputs/ is ours). It hands back either a plain path or a
    file://-style URL.
    """
    audio = None
    if result:
        first = result[0]
        if isinstance(first, dict):
            audio = first.get("name") or first.get("path") or first.get("url")
        elif isinstance(first, str):
            audio = first

    if not audio:
        outdir = os.path.join(TTS["dir"], "outputs")
        newest, newest_mtime = None, 0.0
        try:
            for name in os.listdir(outdir):
                if not name.lower().endswith(".wav"):
                    continue
                p = os.path.join(outdir, name)
                mt = os.path.getmtime(p)
                if mt > newest_mtime:
                    newest, newest_mtime = p, mt
        except OSError:
            pass
        # only trust it if it is fresh (generated by this call)
        if newest and (time.time() - newest_mtime) < 900:
            audio = newest

    if not audio:
        return ""

    path = str(audio)
    if path.startswith("file://"):
        path = path[7:]
    if path.startswith("/file="):
        path = path[6:]
    if not os.path.isabs(path):
        path = os.path.join(TTS["dir"], path.lstrip("/\\"))
    return path if os.path.exists(path) else ""


def tts_speak(payload: dict) -> dict:
    """Generate speech for `text` with the chosen reference voice.

    Pipeline, each step in its own helper:
      1. _pick_voice      - which reference wav to clone
      2. _tts_upload      - Gradio only accepts files it uploaded itself
      3. _tts_args        - the 24 positional parameters gen_single wants
      4. POST /gradio_api/call/gen_single -> event id
      5. _tts_stream      - read SSE (with an estimated progress ticker)
      6. _tts_resolve_audio - find the produced wav
      7. copy into data/speech so /audio/<name> can serve it
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "没有要朗读的文字"}
    if not port_open(TTS["port"]):
        return {"ok": False, "message": "语音合成没有在运行，先点“启动”"}

    voice_path = _pick_voice(str(payload.get("voice") or ""))
    if not voice_path:
        return {"ok": False, "message": "没有可用的参考音频：把一段 wav 放到 data/voices 里"}

    uploaded = _tts_upload(voice_path)
    if not uploaded:
        return {"ok": False, "message": "参考音频上传失败，检查文件是否可读"}

    base = f"http://{HOST}:{TTS['port']}"
    job_begin("tts", label=text[:40])

    try:
        req = urllib.request.Request(
            f"{base}/gradio_api/call/gen_single",
            data=json.dumps({"data": _tts_args(text, uploaded)}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            started = json.loads(r.read().decode("utf-8", "replace"))
        event_id = started.get("event_id")
        if not event_id:
            job_finish("tts", error="no event id")
            return {"ok": False, "message": f"没有拿到 event_id：{started}"}

        try:
            log_start = os.path.getsize(TTS["log"])
        except OSError:
            log_start = 0
        ticker = _ProgressTicker(TTS["log"], log_start)
        ticker.start()

        streamed = _tts_stream(base, event_id, ticker)
        if not streamed["ok"]:
            job_finish("tts", error=streamed["message"])
            return {"ok": False, "message": streamed["message"]}
        job_set("tts", progress=0.97, label="收尾")
    except Exception as exc:  # noqa: BLE001
        job_finish("tts", error=str(exc)[:200])
        return {"ok": False, "message": f"合成失败：{exc}。点“日志”看详细原因"}

    path = _tts_resolve_audio(streamed["result"])
    if not path:
        job_finish("tts", error="no audio in result")
        return {"ok": False, "message": "没有拿到音频结果"}

    # copy it next to the app so the browser can fetch it through /audio/<name>
    os.makedirs(SPEECH_DIR, exist_ok=True)
    name = f"speech-{time.strftime('%Y%m%d-%H%M%S')}.wav"
    dest = os.path.join(SPEECH_DIR, name)
    try:
        shutil.copyfile(path, dest)
    except OSError as exc:  # noqa: BLE001
        job_finish("tts", error=str(exc)[:120])
        return {"ok": False, "message": f"无法复制音频：{exc}"}

    out = {"ok": True, "path": dest, "url": f"/audio/{name}",
           "bytes": os.path.getsize(dest), "text": text, "voice": voice_path,
           "message": "已生成"}
    job_finish("tts", result={"ok": True, "url": out["url"],
                              "bytes": out["bytes"]})
    return out


def model_download(mid: str) -> dict:
    """Fetch a model's missing weight files in the background.

    The transfer runs on a worker thread so the HTTP request returns at once and
    the UI can watch /api/progress. Only one download runs at a time: the files
    are huge and the disk is the bottleneck.
    """
    cfg = MODELS.get(mid)
    if not cfg:
        return {"ok": False, "message": "未知模型"}
    if job_snapshot("download").get("state") == "running":
        return {"ok": False, "message": "已有下载在进行，等它结束再试"}

    todo = download.missing(cfg)
    if not todo:
        return {"ok": True, "message": "文件都在，不需要下载"}

    dl = CFG.get("download") or {}
    endpoint = dl.get("hf_endpoint") or "https://huggingface.co"

    # build the work list first, so a missing URL fails before the thread starts
    plan = []
    for field, path in todo:
        # the same model entry carries one URL; mmproj falls back to a sibling
        spec = dict(cfg)
        if field == "mmproj":
            spec["url"] = cfg.get("mmproj_url") or ""
            spec["hf_file"] = cfg.get("mmproj_hf_file") or ""
        url = download.resolve_url(spec, endpoint)
        if not url:
            return {"ok": False,
                    "message": f"{os.path.basename(path)} 没有配置下载地址"
                               f"（给它加 url 或 hf_repo + hf_file）"}
        plan.append((path, url))

    job_set("download", state="running", progress=0.0,
            label=os.path.basename(plan[0][0]), started=time.time(),
            result=None, error=None, done_bytes=0, total_bytes=0, speed=0.0)

    def worker() -> None:
        total_done = 0
        for index, (path, url) in enumerate(plan):
            name = os.path.basename(path)
            job_set("download", label=f"{name}（{index + 1}/{len(plan)}）")

            def progress(done: int, total: int, speed: float,
                         _i=index) -> None:
                # progress across the whole plan, so multiple files still add up
                frac = (done / total) if total else 0.0
                overall = (_i + frac) / len(plan)
                job_set("download", progress=min(0.999, overall),
                        done_bytes=done, total_bytes=total, speed=speed,
                        label=f"{name}  {download.human(done)}"
                              f"{' / ' + download.human(total) if total else ''}"
                              f"  {download.human(speed)}/s")

            result = download.download(url, path, on_progress=progress)
            if not result.get("ok"):
                job_finish("download", error=result.get("message"))
                return
            total_done += result.get("bytes") or 0

        job_finish("download", result={"ok": True, "bytes": total_done,
                                       "count": len(plan)})

    threading.Thread(target=worker, daemon=True).start()
    names = "、".join(os.path.basename(p) for p, _u in plan)
    return {"ok": True, "message": f"开始下载 {names}（可关掉窗口，下载会继续）"}


def sd_start() -> dict:
    r"""Start the SD WebUI with --api, unloading the language models first.

    Runs the bundle's own python\python.exe directly. Calling webui.bat instead
    would create an empty venv and start downloading torch, which is what made
    earlier attempts flash open and close.
    """
    global _sd_spawning  # noqa: PLW0603
    if port_open(SD["port"]):
        return {"ok": True, "message": "SD 已经在运行"}
    py = SD["python"]
    if not os.path.exists(py):
        return {"ok": False, "message": f"找不到整合包自带的 Python：{py}"}
    if not os.path.exists(os.path.join(SD["dir"], "launch.py")):
        return {"ok": False, "message": f"找不到 launch.py：{SD['dir']}"}

    # One GPU: SD needs the whole card, so everything else must go.
    freed = _free_gpu(need_mib=8000)
    if not freed["ok"]:
        return {"ok": False, "message": freed["message"]}

    res = _launch([py, *SD["args"]], SD["log"], SD["dir"], None, "SD")
    if not res["ok"]:
        return res

    _sd_spawning = time.time()
    note = f"（已卸载 {'、'.join(freed['freed'])}）" if freed["freed"] else ""
    return {"ok": True,
            "message": f"SD 开始启动{note}，首次启动可能要 1-3 分钟"}


def sd_stop() -> dict:
    global _sd_spawning  # noqa: PLW0603
    killed = kill_port(SD["port"])
    _sd_spawning = 0.0
    if not killed:
        return {"ok": True, "message": "SD 本来就没在运行"}
    return {"ok": True, "message": f"SD 已停止（PID {', '.join(map(str, killed))}）"}


def sd_interrupt() -> dict:
    """Tell SD to stop the running generation (/sdapi/v1/interrupt).

    SD keeps working on the current txt2img request until it finishes or is
    interrupted; without this the user has to sit through the whole render.
    """
    port = SD["port"]
    if not port_open(port):
        return {"ok": False, "message": "SD 没有在运行"}
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{port}/sdapi/v1/interrupt",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"中断失败：{exc}"}
    return {"ok": True, "message": "已中断当前生成"}


def images_cleanup(keep_days: int = 30) -> dict:
    """Delete chat images older than `keep_days` that no chat still references.

    images/ otherwise grows forever: a chat that is kept around keeps its
    pictures, but orphaned files (from dropped chats or older saves) pile up.
    """
    if not os.path.isdir(CHAT_IMAGES):
        return {"ok": True, "removed": 0, "freed": 0}

    with _chats_lock:                     # never read the file mid-write
        stored = _read_chats_unlocked()
    used = {im.get("file") for c in stored.get("chats", [])
            for m in c.get("messages", []) for im in (m.get("images") or [])
            if im.get("file")}
    # files still referenced by a chat are never deleted, however old they are
    cutoff = time.time() - keep_days * 86400
    removed = 0
    freed = 0
    for name in os.listdir(CHAT_IMAGES):
        if name in used:
            continue
        path = os.path.join(CHAT_IMAGES, name)
        try:
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
            if st.st_mtime >= cutoff:
                continue
            os.remove(path)
            removed += 1
            freed += st.st_size
        except OSError:
            continue
    return {"ok": True, "removed": removed, "freed": freed,
            "message": f"清理了 {removed} 个未引用的图片（{freed // 1024} KB）"}


def export_chat(chat_id: str = "") -> dict:
    """Export one conversation (or all) as a self-contained HTML file.

    Images are inlined as data URLs so the file can be opened or shared on its
    own, without this app or the images folder.
    """
    import html

    with _chats_lock:                     # never read the file mid-write
        stored = _read_chats_unlocked()
    # base64 decoding is slow, so restore outside the lock
    data = _restore_images(stored)
    chats = data.get("chats", [])
    if chat_id:
        chats = [c for c in chats if c.get("id") == chat_id]
    if not chats:
        return {"ok": False, "message": "没有可导出的对话"}

    parts = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<title>橙芯绘话 导出</title><style>",
        "body{background:#0d1117;color:#e6edf3;font:15px/1.65 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;",
        "max-width:820px;margin:0 auto;padding:28px 20px 60px}",
        "h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:34px 0 12px;color:#8b949e;",
        "border-bottom:1px solid #30363d;padding-bottom:6px}",
        ".m{margin:14px 0;padding:12px 14px;border-radius:10px;background:#161b22;border:1px solid #30363d}",
        ".u{background:#1b2a3f}.who{font-size:12px;color:#8b949e;margin-bottom:5px}",
        ".t{white-space:pre-wrap;word-break:break-word}",
        "img{max-width:100%;border-radius:8px;margin-top:8px;display:block}",
        "pre{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px;overflow:auto}",
        "code{font-family:Consolas,monospace;font-size:13px}",
        ".meta{color:#8b949e;font-size:12px;margin-top:6px}",
        "</style></head><body>",
    ]
    total_imgs = 0
    for c in chats:
        title = html.escape(str(c.get("title") or "未命名对话"))
        parts.append(f"<h1>{title}</h1>")
        parts.append(f"<p class='meta'>{len(c.get('messages') or [])} 条消息 · 模型 {html.escape(str(c.get('model') or ''))}</p>")
        for m in c.get("messages") or []:
            role = m.get("role")
            cls = "m u" if role == "user" else "m"
            who = "你" if role == "user" else ("Stable Diffusion" if m.get("model") == "sd" else html.escape(str(m.get("model") or "模型")))
            parts.append(f"<div class='{cls}'><div class='who'>{who}</div>")
            body = str(m.get("content") or "")
            if body:
                parts.append(f"<div class='t'>{html.escape(body)}</div>")
            for im in m.get("images") or []:
                src = im.get("dataUrl") or ""
                if src.startswith("data:"):
                    parts.append(f"<img src='{src}' alt=''>")
                    total_imgs += 1
            parts.append("</div>")
    parts.append("</body></html>")

    outdir = os.path.join(HERE, "exports")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"chat-{stamp}.html"
    path = os.path.join(outdir, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(parts))
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "message": f"写入失败：{exc}"}
    return {"ok": True, "path": path, "images": total_imgs,
            "message": f"已导出到 {path}"}


def sd_save(payload: dict) -> dict:
    """Write a generated picture to the outputs folder so it can be kept.

    Accepts a data URL plus a filename; the filename is sanitised because this
    is reachable over the local HTTP surface.
    """
    import base64

    name = os.path.basename(str(payload.get("name") or "").strip() or "draw.png")
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._- ") or "draw.png"
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        name += ".png"

    data_url = str(payload.get("dataUrl") or "")
    if not data_url.startswith("data:"):
        return {"ok": False, "message": "need a data URL"}
    try:
        b64 = data_url.split(",", 1)[1]
        raw = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"decode failed: {exc}"}

    outdir = os.path.join(HERE, "outputs")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, name)
    # never overwrite: the user may want several takes of the same prompt
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(path):
        path = os.path.join(outdir, f"{stem}({n}){ext}")
        n += 1
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "message": f"write failed: {exc}"}
    return {"ok": True, "path": path, "bytes": len(raw)}


def sd_select(payload: dict) -> dict:
    """Switch the live SD checkpoint. This is a one-way load and takes ~30 s.

    The old checkpoint stays resident until the new one has been loaded by the
    WebUI, so the API stays up; only generation has to wait.
    """
    port = SD["port"]
    if not port_open(port):
        return {"ok": False, "message": "SD 没有在运行，先点“启动”"}
    checkpoint = str(payload.get("checkpoint") or "").strip()
    if not checkpoint:
        return {"ok": False, "message": "没指定模型"}

    body = json.dumps({"sd_model_checkpoint": checkpoint}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{port}/sdapi/v1/options", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"切换失败：{exc}"}
    return {"ok": True, "model": checkpoint,
            "seconds": round(time.time() - t0, 1),
            "message": f"已切换到 {checkpoint}"}


def sd_translate(text: str) -> str:
    """Translate Chinese to English with our local CPU translation service.

    The service (translator.py) runs opus-mt-zh-en on the CPU: fully offline,
    no VRAM use, no waiting for the GPU models to be swapped. Falls back to the
    original text if the service is not available.
    """
    if not text:
        return text
    if not any('\u4e00' <= ch <= '\u9fff' for ch in text):
        return text

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8091/translate",
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if data.get("ok") and data.get("translated"):
            return str(data["translated"]).strip()
    except Exception as exc:  # noqa: BLE001
        _dbg(f"local translate failed: {exc}")
    return text


def _start_translator() -> None:
    """Launch the local translation service if it is not already up."""
    tr = CFG.get("translator") or {}
    if not tr.get("enabled", True):
        return
    tr_port = int(tr.get("port") or 8091)
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{tr_port}/health", timeout=2) as r:
            if r.status == 200:
                return
    except Exception:  # noqa: BLE001
        pass
    # the translator needs torch + transformers; reuse SD's interpreter when it
    # has them, otherwise fall back to whatever python is running this
    py = tr.get("python") or SD.get("python") or sys.executable
    script = os.path.join(HERE, "translator.py")
    if not (os.path.exists(py) and os.path.exists(script)):
        return
    log = os.path.join(LOG_DIR, "translator.log")
    try:
        fh = open(log, "ab", buffering=0)
        subprocess.Popen(
            [py, script], stdin=subprocess.DEVNULL,
            stdout=fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _dbg("started local translator service")
    except Exception as exc:  # noqa: BLE001
        _dbg(f"could not start translator: {exc}")


# --------------------------------------------------------------------------
# ControlNet
#
# The extension is already installed with 14 models on disk. The API takes the
# control image as a base64 data URL inside alwayson_scripts, plus a unit entry
# describing which model to use and how strongly.
# --------------------------------------------------------------------------

# Friendly names for the models found in models/ControlNet. The key is matched
# as a substring of the .pth filename, so any v11 checkpoint works.
CONTROLNET_MODELS = {
    "canny":      ("control_v11p_sd15_canny",      "边缘检测 · 保留轮廓线"),
    "openpose":   ("control_v11p_sd15_openpose",   "姿势 · 照着人物骨骼摆姿势"),
    "depth":      ("control_v11f1p_sd15_depth",    "深度 · 保留前后空间关系"),
    "lineart":    ("control_v11p_sd15_lineart",    "线稿 · 给线稿上色"),
    "anime":      ("control_v11p_sd15s2_lineart_anime", "动漫线稿 · 二次元上色"),
    "scribble":   ("control_v11p_sd15_scribble",   "涂鸦 · 草图变成品"),
    "softedge":   ("control_v11p_sd15_softedge",   "柔和边缘 · 宽容度更高的轮廓"),
    "seg":        ("control_v11p_sd15_seg",        "语义分割 · 按色块分区上色"),
    "normalbae":  ("control_v11p_sd15_normalbae",  "法线贴图 · 保留表面朝向"),
    "mlsd":       ("control_v11p_sd15_mlsd",       "直线检测 · 建筑和室内"),
    "tile":       ("control_v11f1e_sd15_tile",     "平铺细节 · 放大时补细节"),
    "inpaint":    ("control_v11p_sd15_inpaint",    "局部重绘"),
    "ip2p":       ("control_v11e_sd15_ip2p",       "指令改图"),
    "shuffle":    ("control_v11e_sd15_shuffle",    "随机打散重排"),
}



# --------------------------------------------------------------------------
# speech-to-text (faster-whisper, CPU)
# --------------------------------------------------------------------------
def stt_conf() -> dict:
    return CFG.get("stt") or {}


def stt_up() -> bool:
    cfg = stt_conf()
    return bool(cfg.get("enabled")) and port_open(int(cfg.get("port") or 8092))


def _start_stt() -> None:
    """Launch the speech-to-text service if voice input is enabled."""
    cfg = stt_conf()
    if not cfg.get("enabled"):
        return
    port = int(cfg.get("port") or 8092)
    if port_open(port):
        return
    script = os.path.join(HERE, "speech2text.py")
    if not os.path.exists(script):
        return
    py = cfg.get("python") or sys.executable
    if not os.path.exists(py):
        _dbg(f"stt: python not found: {py}")
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        log = open(os.path.join(LOG_DIR, "stt.log"), "ab", buffering=0)
        subprocess.Popen(
            [py, script], cwd=HERE,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        _dbg(f"stt start failed: {exc}")


def stt_transcribe(raw: bytes) -> dict:
    """Forward recorded audio to the local whisper service."""
    cfg = stt_conf()
    if not cfg.get("enabled"):
        return {"ok": False, "message": "语音输入未启用（config.json 的 stt.enabled）"}
    port = int(cfg.get("port") or 8092)
    if not port_open(port):
        # try once, the service may have died
        _start_stt()
        time.sleep(1.0)
        if not port_open(port):
            return {"ok": False,
                    "message": "语音识别服务没在运行。首次启动需要下载模型，"
                               "点「语音」卡片上的启动，或看 logs/stt.log"}
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{port}/transcribe", data=raw,
            headers={"Content-Type": "application/octet-stream"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"识别请求失败：{exc}"}


def comfy_probe() -> dict:
    """State of the ComfyUI / Qwen-Image backend."""
    try:
        return comfy_backend.probe()
    except Exception as exc:  # noqa: BLE001
        return {"label": "Qwen 图像", "up": False, "loading": False,
                "port": comfy_backend.COMFY_PORT, "dir": comfy_backend.COMFY_DIR,
                "error": str(exc)[:160], "models": [], "ready": False}


def comfy_start() -> dict:
    """Start ComfyUI, releasing the GPU from whoever holds it."""
    # The others have to go even when ComfyUI is already up: otherwise a
    # manually started instance leaves SD holding half the card.
    freed = stop_all_models(keep="comfy")
    if comfy_backend.is_up():
        note = f"（已卸载 {'、'.join(freed)}）" if freed else ""
        return {"ok": True, "message": "ComfyUI 已经在运行" + note}
    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, "comfy.log")
    res = comfy_backend.start(log)
    if res.get("ok"):
        note = f"（已卸载 {'、'.join(freed)}）" if freed else ""
        res["message"] = res.get("message", "") + note
    return res


def comfy_stop() -> dict:
    return comfy_backend.stop()


def comfy_generate(payload: dict) -> dict:
    """Render with Qwen-Image through ComfyUI."""
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "message": "提示词是空的"}
    if not comfy_backend.is_up():
        return {"ok": False, "message": "ComfyUI 没有在运行，先启动它"}

    # Fall back to the built-in list: without it, prompts that ask for heavy
    # skin texture come back looking dirty at 1024 px.
    negative = str(payload.get("negative_prompt") or "").strip()
    if not negative:
        negative = comfy_backend.QWEN_NEGATIVE
    seed = int(payload.get("seed") or -1)
    steps = max(1, min(60, int(payload.get("steps") or comfy_backend.QWEN_STEPS)))
    cfg = float(payload.get("cfg_scale") or comfy_backend.QWEN_CFG)
    width = max(256, min(2048, int(payload.get("width") or 1024)))
    height = max(256, min(2048, int(payload.get("height") or 1024)))

    job_begin("comfy", label=prompt[:40])

    # ComfyUI reports no percentage for this workflow, so the bar is an
    # estimate. The first render after a start also loads ~10 GB of weights and
    # takes a minute or two; later ones settle in about 2 s. Using one curve for
    # both made the bar sit at 95% for over a minute, which reads as a hang.
    cold = not comfy_backend.warm()

    def progress(elapsed: float) -> None:
        if cold:
            # long load up front, then a quick finish: reach 90% over 90 s and
            # creep the rest, so the bar never freezes
            share = min(0.9, elapsed / 90.0)
            rest = min(0.09, max(0.0, (elapsed - 90.0) / 600.0))
            pct = share + rest
            label = "正在加载模型" if elapsed < 60 else "正在生成"
        else:
            # a warm render measures 14-20 s here, so spread the bar over 20 s
            # and let the tail creep instead of parking at 95%
            pct = min(0.9, elapsed / 20.0)
            if elapsed > 20.0:
                pct = min(0.98, 0.9 + (elapsed - 20.0) / 120.0)
            label = "正在生成"
        job_set("comfy", progress=pct, label=label)

    # Same shape as sd_generate: the render runs on a worker and this thread
    # only waits for it. Doing the whole thing inline blocks the handler while
    # ComfyUI is busy, and the browser drops the connection.
    result_box: dict = {}

    def worker() -> None:
        try:
            result_box["res"] = comfy_backend.generate(
                prompt, negative, seed, steps, cfg, width, height,
                on_progress=progress)
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = str(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout=900)

    if result_box.get("error"):
        job_finish("comfy", error=result_box["error"])
        return {"ok": False, "message": f"生成失败：{result_box['error']}"}
    res = result_box.get("res") or {}
    if not res.get("ok"):
        job_finish("comfy", error=res.get("message"))
        return res

    job_finish("comfy", result={"ok": True, "count": len(res.get("images") or [])})
    res["original_prompt"] = prompt
    return res


def stt_probe() -> dict:
    cfg = stt_conf()
    port = int(cfg.get("port") or 8092)
    state = {"enabled": bool(cfg.get("enabled")), "port": port,
             "model": cfg.get("model") or "small", "up": False, "error": None}
    if not port_open(port):
        return state
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/health", timeout=5) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
        state["up"] = bool(info.get("loaded"))
        state["error"] = info.get("error")
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)[:160]
    return state

def controlnet_models() -> list[dict]:
    """Which ControlNet models actually exist on disk.

    Reading the directory beats trusting the hard-coded table: the user may have
    added or deleted .pth files since this was written.
    """
    base = os.path.join(SD.get("dir") or "", "models", "ControlNet")
    found = {}
    if os.path.isdir(base):
        try:
            for name in os.listdir(base):
                if not name.lower().endswith((".pth", ".safetensors")):
                    continue
                stem = os.path.splitext(name)[0]
                for key, (prefix, label) in CONTROLNET_MODELS.items():
                    if stem.startswith(prefix):
                        found[key] = {"key": key, "file": name, "label": label}
                        break
        except OSError:
            pass
    return [found[k] for k in CONTROLNET_MODELS if k in found]


def _controlnet_args(payload: dict) -> tuple[list, str]:
    """Build the alwayson_scripts block for ControlNet.

    Returns (alwayson_scripts, error). An empty list means "plain txt2img".
    """
    cn = payload.get("controlnet") or {}
    if not cn or not cn.get("image"):
        return [], ""

    key = str(cn.get("model") or "").strip()
    models = {m["key"]: m for m in controlnet_models()}
    if key not in models:
        return [], f"ControlNet 模型不存在：{key}。" \
                    f"可用的有：{', '.join(models) or '（一个都没有）'}"

    image = str(cn["image"])
    # SD wants a bare base64 string or a data URL; pass data URLs through as-is
    if not image.startswith("data:"):
        image = f"data:image/png;base64,{image}"

    weight = float(cn.get("weight") or 1.0)
    weight = max(0.0, min(2.0, weight))
    guidance_end = cn.get("guidance_end")
    try:
        guidance_end = float(guidance_end) if guidance_end is not None else 1.0
    except (TypeError, ValueError):
        guidance_end = 1.0
    guidance_end = max(0.1, min(1.0, guidance_end))

    unit = {
        "enabled": True,
        "module": str(cn.get("module") or "none"),
        "model": models[key]["file"],
        "weight": weight,
        "guidance_start": 0.0,
        "guidance_end": guidance_end,
        "control_mode": 0,          # 0 = balanced
        "pixel_perfect": True,
        "resize_mode": 1,           # crop and resize
        "image": image,
    }
    # "none" as the preprocessor means the input is already a control map
    if unit["module"] == "none" and cn.get("invert"):
        unit["control_mode"] = 1    # invert (useful for depth/normal maps)

    return [{"name": "controlnet", "args": [[unit]]}], ""


def sd_generate(payload: dict) -> dict:
    """POST to the SD txt2img API and return the decoded images."""
    port = SD["port"]
    if not port_open(port):
        return {"ok": False, "message": "SD 没有在运行，先点“启动 SD”"}

    original_prompt = (payload.get("prompt") or "").strip()
    if not original_prompt:
        return {"ok": False, "message": "提示词是空的"}

    prompt = sd_translate(original_prompt)
    if prompt != original_prompt:
        _dbg(f"sd prompt: {original_prompt!r} -> {prompt!r}")

    negative = (payload.get("negative_prompt") or "").strip()
    width = int(payload.get("width") or 768)
    height = int(payload.get("height") or 768)
    steps = int(payload.get("steps") or 28)
    cfg = float(payload.get("cfg_scale") or 7)
    batch = max(1, min(4, int(payload.get("batch_size") or 1)))
    seed = int(payload.get("seed") or -1)

    body = {
        "prompt": f"{prompt}, {SD_STYLE_GUARD}",
        "negative_prompt": negative,
        "width": max(256, min(1536, width)),
        "height": max(256, min(1536, height)),
        "steps": max(1, min(60, steps)),
        "cfg_scale": max(1, min(20, cfg)),
        "batch_size": batch,
        "seed": seed,
    }
    if payload.get("checkpoint"):
        body["override_settings"] = {"sd_model_checkpoint": payload["checkpoint"]}
        body["override_settings_restore_afterwards"] = False

    scripts, cn_error = _controlnet_args(payload)
    if cn_error:
        return {"ok": False, "message": cn_error}
    if scripts:
        body["alwayson_scripts"] = {"controlnet": {"args": scripts[0]["args"]}}

    data = json.dumps(body).encode("utf-8")

    # Run the request in a worker and mirror SD's own progress into JOBS, so the
    # UI can show a percentage while the picture is being rendered.
    result_box: dict = {}

    def worker() -> None:
        req = urllib.request.Request(
            f"http://{HOST}:{port}/sdapi/v1/txt2img", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                result_box["resp"] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = str(exc)

    job_begin("sd", label=f"{original_prompt[:40]}")
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    job_set("sd", progress=0.02)
    while th.is_alive():
        th.join(timeout=0.7)
        if not th.is_alive():
            break
        # ask SD how far along it is; the endpoint is cheap and always answers
        try:
            with urllib.request.urlopen(
                    f"http://{HOST}:{port}/sdapi/v1/progress?skip_current_image=true",
                    timeout=4) as pr:
                p = json.loads(pr.read().decode("utf-8", "replace"))
            frac = float(p.get("progress") or 0.0)
            elapsed = time.time() - (job_snapshot("sd").get("started") or time.time())
            if frac > 0:
                job_set("sd", progress=min(0.98, frac),
                        label=p.get("state", {}).get("job") or f"{original_prompt[:40]}")
            elif elapsed > 3:
                # SD reports 0 while it loads weights / swaps checkpoint, which
                # can take a minute; say so instead of sitting at 2%
                job_set("sd", progress=0.02 * min(1.0, elapsed / 60.0) + 0.01,
                        label="加载模型" if elapsed < 55 else "还在加载模型…")
        except Exception:  # noqa: BLE001
            pass

    if result_box.get("error"):
        job_finish("sd", error=result_box["error"])
        return {"ok": False, "message": f"生成失败：{result_box['error']}"}
    resp = result_box.get("resp") or {}

    images = resp.get("images") or []
    if not images:
        job_finish("sd", error="SD 没有返回图片")
        return {"ok": False, "message": "SD 没有返回图片"}
    # SD answers with data URLs, or with bare base64 depending on version
    out = []
    for im in images:
        out.append(im if im.startswith("data:") else f"data:image/png;base64,{im}")
    info = {}
    try:
        info = json.loads(resp.get("info") or "{}")
    except Exception:  # noqa: BLE001
        pass
    result = {"ok": True, "images": out, "seed": info.get("seed"),
              "prompt": body["prompt"], "negative": negative,
              "original_prompt": original_prompt, "translated_prompt": prompt}
    job_finish("sd", result={"ok": True, "count": len(out),
                             "seed": info.get("seed")})
    return result


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
CHATS_FILE = os.path.join(HERE, "data", "chats.json")
CHAT_IMAGES = os.path.join(HERE, "data", "images")
SPEECH_DIR = os.path.join(HERE, "data", "speech")

# Live progress for the two long jobs, so the UI can show a real percentage
# instead of an indefinite spinner. Both are single-slot: only one generation
# runs at a time on this machine.
JOBS: dict[str, dict] = {
    "tts": {"state": "idle", "progress": 0.0, "label": "", "started": 0.0,
            "result": None, "error": None},
    "sd": {"state": "idle", "progress": 0.0, "label": "", "started": 0.0,
           "result": None, "error": None},
    # model downloads reuse the same shape so one progress bar works for all
    "download": {"state": "idle", "progress": 0.0, "label": "", "started": 0.0,
                 "result": None, "error": None,
                 "done_bytes": 0, "total_bytes": 0, "speed": 0.0},
    "comfy": {"state": "idle", "progress": 0.0, "label": "", "started": 0.0,
              "result": None, "error": None},
}
_jobs_lock = threading.Lock()


def job_set(kind: str, **kw) -> None:
    with _jobs_lock:
        job = JOBS.get(kind)
        if job is not None:
            job.update(kw)


def job_snapshot(kind: str) -> dict:
    with _jobs_lock:
        return dict(JOBS.get(kind) or {})


def job_begin(kind: str, label: str = "") -> None:
    job_set(kind, state="running", progress=0.0, label=label,
            started=time.time(), result=None, error=None)


def job_finish(kind: str, result=None, error: str | None = None) -> None:
    job_set(kind, state=("error" if error else "done"), progress=1.0,
            result=result, error=error)
_chats_lock = threading.Lock()


def _extract_images(data: dict) -> dict:
    """Move inline base64 images out of chats.json into separate files.

    chats.json would otherwise grow by ~1 MB per generated picture, making
    every save and reload slower. The client keeps using data URLs; this is
    transparent to it.
    """
    os.makedirs(CHAT_IMAGES, exist_ok=True)
    import base64

    out = json.loads(json.dumps(data))   # deep copy, never mutate the caller
    for c in out.get("chats", []):
        for m in c.get("messages", []):
            for im in m.get("images") or []:
                url = str(im.get("dataUrl") or "")
                if not url.startswith("data:") or "," not in url:
                    continue
                try:
                    header, b64 = url.split(",", 1)
                    raw = base64.b64decode(b64)
                except Exception:  # noqa: BLE001
                    continue
                if len(raw) < 64:
                    continue
                ext = ".png"
                if "jpeg" in header:
                    ext = ".jpg"
                elif "webp" in header:
                    ext = ".webp"
                digest = hashlib.sha1(raw).hexdigest()[:20]
                path = os.path.join(CHAT_IMAGES, digest + ext)
                if not os.path.exists(path):
                    try:
                        with open(path, "wb") as fh:
                            fh.write(raw)
                    except OSError:
                        continue
                im["dataUrl"] = ""
                im["file"] = digest + ext
    return out


def _restore_images(data: dict) -> dict:
    """Turn stored image paths back into data URLs for the browser."""
    import base64

    out = json.loads(json.dumps(data))
    for c in out.get("chats", []):
        for m in c.get("messages", []):
            for im in m.get("images") or []:
                if im.get("dataUrl") or not im.get("file"):
                    continue
                path = os.path.join(CHAT_IMAGES, os.path.basename(str(im["file"])))
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                    ext = os.path.splitext(path)[1].lower()
                    mime = "image/png"
                    if ext == ".jpg":
                        mime = "image/jpeg"
                    elif ext == ".webp":
                        mime = "image/webp"
                    im["dataUrl"] = f"data:{mime};base64," + base64.b64encode(raw).decode()
                except OSError:
                    pass
    return out


def _read_chats_unlocked() -> dict:
    if not os.path.exists(CHATS_FILE):
        return {"current": None, "chats": []}
    try:
        with open(CHATS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("bad shape")
        data.setdefault("chats", [])
        data.setdefault("current", None)
        return data
    except Exception:  # noqa: BLE001 - a corrupt file must not kill the UI
        return {"current": None, "chats": []}


def read_chats() -> dict:
    with _chats_lock:
        return _restore_images(_read_chats_unlocked())


def merge_chats(incoming: dict) -> dict:
    """Save the client's conversations without destroying other windows' work.

    Two open windows each hold a full copy of the list and post it wholesale,
    so a plain overwrite silently drops whatever the other window added. Here
    conversations are unioned by id (newest updatedAt wins) and only the client's
    "current" pointer is taken over. Deletions are honoured through the
    client's tombstone list.
    """
    with _chats_lock:
        stored = _read_chats_unlocked()
        by_id: dict[str, dict] = {}
        for c in stored.get("chats", []):
            if isinstance(c, dict) and c.get("id"):
                by_id[c["id"]] = c

        for c in incoming.get("chats", []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            old = by_id.get(c["id"])
            if old is None or (c.get("updatedAt") or 0) >= (old.get("updatedAt") or 0):
                by_id[c["id"]] = c

        for dead in incoming.get("deleted") or []:
            by_id.pop(dead, None)

        merged = {
            "current": incoming.get("current") or stored.get("current"),
            "chats": list(by_id.values()),
        }
        merged["chats"].sort(key=lambda c: c.get("updatedAt") or 0, reverse=True)
        to_save = _extract_images(merged)
        with open(tmp := CHATS_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(to_save, fh, ensure_ascii=False)
        os.replace(tmp, CHATS_FILE)

        # remove image files that nothing references any more (deleted chats)
        try:
            used = {im.get("file") for c in to_save["chats"]
                    for m in c.get("messages", []) for im in (m.get("images") or [])}
            for name in os.listdir(CHAT_IMAGES):
                if name not in used and os.path.isfile(os.path.join(CHAT_IMAGES, name)):
                    os.remove(os.path.join(CHAT_IMAGES, name))
        except OSError:
            pass
        return merged


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
}

ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", f"127.0.0.1:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{APP_VERSION}"

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - base class signature
        # pythonw.exe leaves sys.stderr as None; a bare write() would raise
        # inside send_response() and kill the reply, so never assume it exists.
        stream = getattr(self.server, "log_stream", None) or sys.stderr
        if stream is None:
            return
        try:
            stream.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
            stream.flush()
        except Exception:  # noqa: BLE001 - logging must never break a response
            pass

    # -- helpers ---------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Only serves the loopback UI; a foreign Host header means DNS rebinding.
        # Port is deliberately not compared, so --port and friends still work.
        host = (self.headers.get("Host") or "").lower().strip()
        name = host.rsplit(":", 1)[0] if ":" in host else host
        if name.startswith("["):  # [::1]
            name = name[1:].split("]", 1)[0]
        return name in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - http.server naming
        if not self._host_ok():
            self._json({"error": "bad host"}, 403)
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            self._json({
                "models": [probe(c) for c in MODELS.values()],
                "sd": sd_probe(),
                "tts": tts_probe(),
                "stt": stt_probe(),
                "comfy": comfy_probe(),
                "hub": {"port": PORT, "name": APP_NAME,
                        "version": APP_VERSION,
                        # configuration mistakes worth showing in the UI
                        "problems": config_problems()},
            })
        elif path == "/api/log":
            self._serve_log()
        elif path == "/api/sd/log":
            self._serve_file_log(SD["log"])
        elif path == "/api/tts/log":
            self._serve_file_log(TTS["log"], label="tts")
        elif path == "/api/comfy/log":
            self._serve_file_log(os.path.join(LOG_DIR, "comfy.log"), label="comfy")
        elif path == "/api/progress":
            self._json({"sd": job_snapshot("sd"), "tts": job_snapshot("tts"),
                        "comfy": job_snapshot("comfy"),
                        "download": job_snapshot("download")})
        elif path == "/api/chats":
            self._json(read_chats())
        elif path.startswith("/audio/"):
            self._serve_audio(path[len("/audio/"):])
        elif path.startswith("/v1/"):
            self._proxy("GET", path)
        else:
            self._static(path)

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            self._json({"error": "bad host"}, 403)
            return
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/start":
            self._model_action(body, start_model)
        elif path == "/api/stop":
            self._model_action(body, stop_model)
        elif path == "/api/chats":
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict) or not isinstance(data.get("chats"), list):
                    raise ValueError("bad payload")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            merged = merge_chats(data)
            self._json({"ok": True, "revision": len(merged["chats"])})
        elif path == "/api/comfy/start":
            with _spawn_lock:
                self._json(comfy_start())
        elif path == "/api/comfy/stop":
            with _spawn_lock:
                self._json(comfy_stop())
        elif path == "/api/comfy/generate":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            self._json(comfy_generate(payload if isinstance(payload, dict) else {}))
        elif path == "/api/stt":
            # raw audio body, not JSON: the browser posts a Blob directly
            audio = body or b""
            self._json(stt_transcribe(audio))
        elif path == "/api/download":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            mid = str((payload or {}).get("model") or "")
            self._json(model_download(mid))
        elif path == "/api/tts/start":
            with _spawn_lock:
                self._json(tts_start())
        elif path == "/api/tts/stop":
            with _spawn_lock:
                self._json(tts_stop())
        elif path == "/api/tts/speak":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            self._json(tts_speak(payload if isinstance(payload, dict) else {}))
        elif path == "/api/sd/start":
            with _spawn_lock:
                self._json(sd_start())
        elif path == "/api/sd/select":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            self._json(sd_select(payload if isinstance(payload, dict) else {}))
        elif path == "/api/sd/stop":
            with _spawn_lock:
                self._json(sd_stop())
        elif path == "/api/sd/generate":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            self._json(sd_generate(payload if isinstance(payload, dict) else {}))
        elif path == "/api/sd/save":
            # save a generated picture next to the app, so it can be opened
            # and kept rather than living only inside the browser tab
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "message": "bad payload"}, 400)
                return
            self._json(sd_save(payload if isinstance(payload, dict) else {}))
        elif path == "/api/sd/interrupt":
            self._json(sd_interrupt())
        elif path == "/api/chats/export":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            chat_id = str((payload or {}).get("chat") or "")
            self._json(export_chat(chat_id))
        elif path == "/api/images/cleanup":
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            days = int((payload or {}).get("days") or 30)
            self._json(images_cleanup(max(1, min(3650, days))))
        elif path.startswith("/v1/"):
            self._proxy("POST", path, body)
        else:
            self._json({"error": "not found"}, 404)

    # -- handlers --------------------------------------------------------
    def _model_action(self, body: bytes, fn):
        try:
            payload = json.loads(body or b"{}") or {}
        except Exception:  # noqa: BLE001
            payload = {}
        mid = payload.get("model", "")
        replace = bool(payload.get("replace"))   # "yes, stop the other one"
        cfg = MODELS.get(mid)
        if not cfg:
            self._json({"ok": False, "message": "unknown model"}, 400)
            return

        # Only one model can be loaded at a time on this machine: a second
        # llama-server (or SD) would push the first out of VRAM and stall both.
        # So a start request is refused while something else is up, unless the
        # caller explicitly allows replacing it.
        with _spawn_lock:
            if fn is start_model and not replace:
                busy = [
                    o for o in MODELS.values()
                    if o["id"] != mid and (port_open(o["port"]) or spawn_in_progress(o["id"]))
                ]
                labels = [o["label"] for o in busy]
                # SD and IndexTTS hold the card too, so they block a model start
                if not labels and port_open(SD["port"]):
                    labels = [SD["label"]]
                if not labels and port_open(TTS["port"]):
                    labels = [TTS["label"]]
                if labels:
                    other = labels[0]
                    self._json({
                        "ok": False,
                        "needsReplace": True,
                        "running": other,
                        "message": f"{other} 正在运行。显卡同时只能跑一个模型，"
                                   f"要先停掉它才能启动 {cfg['label']}。",
                    })
                    return
            result = fn(cfg)
        _health_cache.pop(mid, None)
        for other in MODELS.values():          # names may have changed either way
            _health_cache.pop(other["id"], None)
        self._json(result)

    def _serve_log(self):
        mid = (self.path.split("?", 1)[1] if "?" in self.path else "")
        want = "9b" if "9b" in mid else "35b"
        self._serve_file_log(MODELS[want]["log"], label=want)

    def _serve_audio(self, name: str) -> None:
        """Serve a generated speech file so the browser can play it."""
        safe = os.path.basename(name)
        target = os.path.normpath(os.path.join(SPEECH_DIR, safe))
        if not target.startswith(SPEECH_DIR) or not os.path.isfile(target):
            self._json({"error": "not found", "name": safe}, 404)
            return
        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except OSError as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)
            return
        ext = os.path.splitext(target)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "audio/wav"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file_log(self, path: str, label: str = "sd") -> None:
        """Return the last chunk of a log file, for the log viewer."""
        text = ""
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 60000))
                    text = fh.read().decode("utf-8", "replace")
            except OSError as exc:
                text = f"<无法读取日志: {exc}>"
        else:
            text = f"<日志还没生成：{path}>"
        self._json({"model": label, "log": text})

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/").replace("/", os.sep)
        target = os.path.normpath(os.path.join(UI_DIR, rel))
        if not target.startswith(UI_DIR) or not os.path.isfile(target):
            self._json({"error": "not found", "path": path}, 404)
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            data = fh.read()
        # index.html gets content hashes injected into its script tags, so the
        # browser always fetches the current JS without a manual ?v=N counter
        if os.path.basename(target) == "index.html":
            data = self._inject_asset_versions(data)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _asset_version(self, filename: str) -> str:
        """A short content hash of a frontend file.

        The UI references app.js and friends without a version query, and the
        browser caches them hard. Rather than maintaining a ?v=N counter by hand
        (which gets forgotten), derive it from the file content: change the file
        and the URL changes with it.
        """
        try:
            with open(os.path.join(UI_DIR, filename), "rb") as fh:
                return hashlib.sha1(fh.read()).hexdigest()[:8]
        except OSError:
            return "0"

    def _inject_asset_versions(self, html: bytes) -> bytes:
        """Add ?v=<hash> to the frontend script tags.

        Hand-maintained version counters get forgotten, and a forgotten one
        means the browser keeps serving stale JavaScript - which looks exactly
        like "the feature I just added is not showing up". Hashing the file
        content ties the URL to the code automatically.
        """
        text = html.decode("utf-8", "replace")
        for name in ("app.js", "markdown-speech.js", "browser-tts.js"):
            ver = self._asset_version(name)
            text = text.replace(f'<script src="{name}">',
                                f'<script src="{name}?v={ver}">')
        return text.encode("utf-8")

    def _proxy(self, method: str, path: str, body: bytes | None = None):
        """Stream /v1/* to the llm server, flushing SSE chunks as they arrive."""
        try:
            payload = json.loads(body or b"{}")
            mid = payload.get("model") or DEFAULT_MODEL
        except Exception:
            mid = self.headers.get("X-Model") or DEFAULT_MODEL
        cfg = MODELS.get(mid) or MODELS[DEFAULT_MODEL]

        if not port_open(cfg["port"]):
            msg = (f"{cfg['label']} 没有在运行。先在左侧点“启动”，"
                   f"或双击桌面上的 {os.path.basename(cfg['shortcut'])}。")
            self._json({"error": {"message": msg, "type": "model_down",
                                  "code": "model_down"}}, 503)
            return

        conn = http.client.HTTPConnection(HOST, cfg["port"], timeout=HTTP_TIMEOUT_DOWNLOAD)
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if self.headers.get("Accept"):
            headers["Accept"] = self.headers["Accept"]
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            self._json({"error": {"message": f"连接模型失败：{exc}", "type": "proxy"}}, 502)
            return

        self.send_response(resp.status)
        for key in ("Content-Type", "Cache-Control"):
            value = resp.getheader(key)
            if value:
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()


class Hub(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------
# browser window (Edge --app gives a chrome-less native-looking window)
# --------------------------------------------------------------------------
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def screen_work_area() -> tuple[int, int, int, int] | None:
    """Usable desktop rectangle in pixels (excludes the taskbar).

    Uses the Win32 work area rather than the full screen so the window never
    opens underneath the taskbar or off the edge of a scaled display.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()          # report real pixels, not scaled ones

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        # the monitor under the cursor: matches where the user just clicked
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        w = info.rcWork
        return (w.left, w.top, w.right - w.left, w.bottom - w.top)
    except Exception:  # noqa: BLE001 - sizing must never block startup
        return None


GEOMETRY_FILE = os.path.join(HERE, "data", "window.json")


def load_saved_geometry() -> tuple[int, int, int, int] | None:
    """Remember the size and position the user last left the window at."""
    try:
        with open(GEOMETRY_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        g = (int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))
    except Exception:  # noqa: BLE001 - missing or corrupt file just means "auto"
        return None
    # reject anything that is not fully on the current work area (monitor
    # unplugged, resolution changed, window dragged off-screen)
    area = screen_work_area()
    if area:
        ax, ay, aw, ah = area
        x, y, w, h = g
        if w < 600 or h < 400 or x + w > ax + aw or y + h > ay + ah or x < ax - 50 or y < ay - 50:
            return None
    return g


def save_geometry(geometry: tuple[int, int, int, int]) -> None:
    try:
        os.makedirs(os.path.dirname(GEOMETRY_FILE), exist_ok=True)
        with open(GEOMETRY_FILE, "w", encoding="utf-8") as fh:
            json.dump({"x": geometry[0], "y": geometry[1],
                       "w": geometry[2], "h": geometry[3]}, fh)
    except OSError:
        pass


def window_geometry() -> tuple[int, int, int, int]:
    """Where the window should open: last size if there is one, else centred.

    Returns (x, y, w, h) in physical pixels.
    """
    saved = load_saved_geometry()
    if saved:
        return saved

    area = screen_work_area()
    if not area:
        return (120, 80, 1240, 840)
    x, y, sw, sh = area

    # First run: 85% of the work area, hard-capped so a big monitor does not
    # get a stretched-out window; floored so the two-pane layout never collapses.
    w = min(int(sw * 0.85), 1340)
    h = min(int(sh * 0.90), 900)
    w = max(min(w, sw - 40), 860)
    h = max(min(h, sh - 40), 560)

    px = x + max(0, (sw - w) // 2)
    py = y + max(0, (sh - h) // 2)
    return (px, py, w, h)


def _find_app_window(timeout: float = 15.0):
    """Wait for our app window and return its HWND.

    Matched by window title rather than PID: Chromium hands the real work to a
    separate browser process, so the PID from Popen exits almost immediately.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.strip() == APP_TITLE:
                    found.append(hwnd)
                    return False
                return True

            user32.EnumWindows(_enum, 0)
            if found:
                return found[0]
            time.sleep(0.25)
    except Exception:  # noqa: BLE001
        return None
    return None


def _dbg(msg: str) -> None:
    """Append a line to logs/window.log for diagnosing window placement."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "window.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def place_window(geometry: tuple[int, int, int, int], tries: int = 24) -> None:
    """Force the window to `geometry` in physical pixels and bring it forward.

    Edge restores the size it remembers for the profile and interprets
    --window-size in device-independent pixels, so on a scaled display the
    result comes out bigger than asked. Setting the rectangle directly fixes
    both problems, but Chromium applies its own remembered state for a second
    or two *after* the window first appears, so this keeps correcting for a
    while and gives up only once the window finally matches.
    """
    hwnd = _find_app_window()
    if not hwnd:
        _dbg("place_window: no window found, giving up")
        return
    _dbg(f"place_window: hwnd={hwnd} target={geometry}")
    try:
        import ctypes
        user32 = ctypes.windll.user32
        x, y, w, h = geometry
        SW_RESTORE = 9
        SW_SHOWNORMAL = 1
        SWP_NOZORDER = 0x0004
        SWP_SHOWWINDOW = 0x0040
        settled = 0
        deadline = time.time() + 20.0
        attempts = 0
        while attempts < tries and time.time() < deadline:
            attempts += 1
            # a maximized frame ignores SetWindowPos: drop out of that state
            if user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.3)
                user32.ShowWindow(hwnd, SW_SHOWNORMAL)
                time.sleep(0.3)
            user32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_SHOWWINDOW)
            time.sleep(0.4)
            # Chromium may repaint it maximized again a beat later, so require
            # the rectangle to hold for several consecutive checks before
            # trusting it. Single confirmations were what let it drift back.
            if not user32.IsZoomed(hwnd) and current_window_rect() == (x, y, w, h):
                settled += 1
                if settled >= 4:
                    break
            else:
                settled = 0
                # keep hunting: the frame may have been replaced at startup
                if not user32.IsWindow(hwnd):
                    hwnd = _find_app_window(timeout=3.0)
                    if not hwnd:
                        return
            if settled == 0:
                time.sleep(0.2)
        user32.SetForegroundWindow(hwnd)
        save_geometry(geometry)
        _dbg(f"place_window: settled={settled} attempts={attempts} final={current_window_rect()}")
    except Exception as exc:  # noqa: BLE001 - cosmetic only, never fatal
        _dbg(f"place_window error: {exc}")


def current_window_rect():
    """Read the live window rectangle, or None if the window is gone.

    A maximized window reports a rect that spills past the work area, which is
    useless to remember; its restored rectangle is what we want in that case.
    """
    hwnd = _find_app_window(timeout=0.5)
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT),
                        ("ptMinPosition", wintypes.POINT),
                        ("ptMaxPosition", wintypes.POINT),
                        ("rcNormalPosition", RECT)]

        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return None
        if user32.IsIconic(hwnd):
            return None                     # minimized: coordinates are junk
        if user32.IsZoomed(hwnd):
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                r = wp.rcNormalPosition
                return (r.left, r.top, r.right - r.left, r.bottom - r.top)
            return None
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:  # noqa: BLE001
        return None


def stop_all_models(keep: str = "") -> list[str]:
    """Unload every GPU user and free the VRAM. Returns labels stopped.

    `keep` names the service that is about to start: everything else goes,
    including a ComfyUI instance that happens to be running already.
    """
    global _sd_spawning, _tts_spawning  # noqa: PLW0603
    stopped = []
    for cfg in MODELS.values():
        killed = kill_port(cfg["port"])
        _spawning.pop(cfg["id"], None)
        _health_cache.pop(cfg["id"], None)
        if killed:
            stopped.append(cfg["label"])
    # SD and TTS share the same card, so they go too: keeping either would
    # leave no room to load a language model back in.
    if keep != "sd" and kill_port(SD["port"]):
        stopped.append(SD["label"])
        _sd_spawning = 0.0
    if keep != "tts" and kill_port(TTS["port"]):
        stopped.append(TTS["label"])
        _tts_spawning = 0.0
    # give the driver a moment to hand the memory back; SD/TTS starting right
    # after this would otherwise race the teardown and hit an OOM
    if stopped:
        for cfg in MODELS.values():
            wait_port_free(cfg["port"], timeout=10)
        wait_port_free(SD["port"], timeout=10)
        wait_port_free(TTS["port"], timeout=10)
    # ComfyUI holds the card as well
    if keep != "comfy":
        try:
            if comfy_backend.is_up():
                comfy_backend.stop()
                stopped.append("Qwen 图像")
        except Exception:  # noqa: BLE001
            pass
    return stopped


def watch_geometry(interval: float = 3.0, idle_after: float = 5.0,
                   on_close=None) -> None:
    """Persist the window size while it is being used, so the next launch matches.

    Chromium's own "remember window size" cannot be trusted here (it stores
    device-independent pixels and can come back larger than asked), so the hub
    records the real rectangle itself and window_geometry() replays it.

    Closing the window is also the "I am done" signal: `on_close` fires once the
    window has stayed gone for `idle_after` seconds, which is where the model is
    unloaded and the VRAM handed back.
    """
    last = None
    missing = 0.0
    while True:
        time.sleep(interval)
        rect = current_window_rect()
        if rect is None:
            missing += interval
            if missing >= idle_after:
                if on_close:
                    on_close()
                return          # window closed: stop watching
            continue
        missing = 0.0
        if rect != last:
            if last is not None:
                save_geometry(rect)
            last = rect


def focus_app_window() -> bool:
    """Bring an already-open chat window to the front. True if one existed.

    Two windows would share one hub and one chats.json, so edits in either
    could overwrite the other's history. There is only ever one window.
    """
    hwnd = _find_app_window(timeout=0.6)
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return True         # the window exists even if activation failed


_opening_lock = threading.Lock()
_last_open = 0.0


def open_window(url: str, profile_dir: str, block: bool = False) -> None:
    """Show the one chat window, or raise it if it is already open.

    Guarded twice: _opening_lock stops two concurrent launch threads from both
    spawning a window, and the time check covers a fast double-click, where the
    second call arrives before the first window has a title to find.

    `block` runs the placement inline. That matters for the "hub is already
    running" path: there the launcher process returns from main() right away,
    and any daemon thread it started would be killed before it could move the
    window, leaving it at whatever size Edge restored.
    """
    global _last_open  # noqa: PLW0603
    with _opening_lock:
        # Single instance: if the window is already up, just raise it.
        if focus_app_window():
            # it exists but may have come back at the wrong size, so enforce it
            geometry = window_geometry()
            _dbg(f"open_window: raising existing window, enforcing {geometry}")
            if block:
                place_window(geometry)
            else:
                threading.Thread(target=place_window, args=(geometry,),
                                 daemon=True).start()
                threading.Thread(target=watch_geometry, daemon=True).start()
            return
        if time.time() - _last_open < 8.0:
            # a launch is almost certainly still starting up; wait for it
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if focus_app_window():
                    return
                time.sleep(0.5)
        _last_open = time.time()

        geometry = window_geometry()
        x, y, w, h = geometry
        _dbg(f"open_window: spawning new window at {geometry}")
        for exe in EDGE_CANDIDATES:
            if os.path.exists(exe):
                subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={profile_dir}",
                     f"--window-size={w},{h}", f"--window-position={x},{y}",
                     "--no-first-run", "--no-default-browser-check",
                     "--disable-features=Translate"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                # The command line alone is not enough: this profile remembers
                # its own window size, and --window-size is read as
                # device-independent pixels. Correct the real rectangle once
                # the frame exists.
                if block:
                    place_window(geometry)
                else:
                    threading.Thread(target=place_window, args=(geometry,),
                                     daemon=True).start()
                    threading.Thread(target=watch_geometry, daemon=True).start()
                return
        os.startfile(url)  # noqa: S606 - fall back to the default browser


def port_in_use(port: int) -> bool:
    return port_open(port)


def shutdown_server(httpd: "Hub", stopped: list[str]) -> None:
    """Stop listening and end the process, so nothing is left holding VRAM."""
    _dbg(f"shutting down; stopped: {stopped or 'nothing was running'}")
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001
        pass


def on_window_closed(httpd: "Hub") -> None:
    """The user closed the chat window: free the GPU and quit the hub."""
    stopped = stop_all_models()
    _dbg(f"window closed -> unloading models {stopped}")
    shutdown_server(httpd, stopped)


# --------------------------------------------------------------------------
def main() -> int:
    global PORT, ALLOWED_HOSTS  # noqa: PLW0603 - overridden by --port
    ap = argparse.ArgumentParser(description="橙芯绘话 hub")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--keep-after-close", action="store_true",
                    help="keep the model loaded (and the hub alive) after the "
                         "chat window is closed; default is to unload it")
    ap.add_argument("--open", default="",
                    help="panel to open on start: tts or draw")
    args = ap.parse_args()

    PORT = args.port
    ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
    # a panel shortcut lands straight on the tool it is meant for
    panel = str(args.open or "").lower()
    suffix = ""
    if panel == "tts":
        suffix = "#tts"
    elif panel == "draw":
        suffix = "#draw"
    url = f"http://{HOST}:{PORT}/{suffix}"

    if port_in_use(PORT):
        print(f"hub already running on {url}")
        if not args.no_browser:
            # block: this launcher exits immediately after, so the placement
            # work must finish before that happens
            open_window(url, os.path.join(HERE, "data", "edge-profile"), block=True)
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    # warm up the local CPU translator so the first drawing does not wait for
    # the 6-second model load. It stays resident and uses no VRAM.
    _start_translator()
    _start_stt()
    httpd = Hub((HOST, PORT), Handler)
    # pythonw has no console: send our own request log to a file instead.
    try:
        httpd.log_stream = open(os.path.join(LOG_DIR, "hub.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        httpd.log_stream = None
    print(f"橙芯绘话 hub on {url}  (models: "
          + ", ".join(f"{c['short']}@:{c['port']}" for c in MODELS.values()) + ")")

    # Closing the window means "I am done" and hands the VRAM back, unless the
    # caller explicitly asked to keep the model resident.
    on_close = None if args.keep_after_close else (lambda: on_window_closed(httpd))

    if not args.no_browser:
        threading.Timer(0.6, open_window,
                        args=(url, os.path.join(HERE, "data", "edge-profile"))).start()
        if on_close:
            threading.Thread(target=watch_geometry,
                             kwargs={"on_close": on_close}, daemon=False).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if not args.keep_after_close:
            # a normal exit must not leave a model pinning the GPU
            stopped = stop_all_models()
            if stopped:
                _dbg(f"exit -> unloaded {stopped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
