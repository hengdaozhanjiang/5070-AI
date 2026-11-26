"""ComfyUI backend: the only practical way to run Qwen-Image here.

AUTOMATIC1111 cannot load MMDiT models such as Qwen-Image, so the SD WebUI in
this project stays on SD1.5/SDXL while ComfyUI handles the newer ones. Both
share the GPU and therefore the same mutual-exclusion rule.

ComfyUI exposes a JSON API: POST /prompt queues a workflow, GET /history/<id>
reports the result, and /view serves the finished picture.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# --- configuration ----------------------------------------------------------

# Values come from config.json's "comfy" section when present. The literals
# below are only a fallback for a machine that has not been configured yet -
# hard-coding them meant the config could not actually move ComfyUI.
from config import CFG  # noqa: E402  (imported late to avoid a cycle)

_CC = CFG.get("comfy") or {}

COMFY_DIR = _CC.get("dir") or r"D:\AI\ComfyUI_windows_portable\ComfyUI"
COMFY_PYTHON = _CC.get("python") or r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
COMFY_PORT = int(_CC.get("port") or 8188)

# the three pieces Qwen-Image needs
QWEN_UNET = _CC.get("unet") or "qwen-image-2.1-turbo-Q4_K_M.gguf"
QWEN_CLIP = _CC.get("clip") or r"text_encoders\qwen3vl_8b_w4a8.safetensors"
QWEN_VAE = _CC.get("vae") or "qwen_image_2.1_vae_bf16.safetensors"

# Generation presets, one per resolution tier.
#
# Qwen-Image Turbo is distilled for few steps, but the right number depends on
# how many pixels there are: a 720p frame is fully resolved in 8 passes, while
# 2K keeps gaining detail up to about 12. cfg stays at 1.0 everywhere - raising
# it flattens texture (guidance skips the negative branch at 1.0, which is what
# Turbo wants).
#
# Measured on a 5070, edge density / high-frequency energy after normalising to
# the same size, so the numbers compare like for like:
#
#   1280x720    8 steps 15.59/6.46   12 steps 16.68/7.28   16 steps 16.40/7.18
#   1920x1088   8 steps 19.42/6.67   12 steps 22.29/7.94   16 steps 22.91/8.30
#   2048x1152   8 steps 20.01/6.87   12 steps 23.24/8.17   16 steps 24.08/8.61
#   1088x1920   8 steps 29.58/10.74  12 steps 32.63/11.97  16 steps 31.15/11.67
#   1328x1328   8 steps 23.44/8.26   12 steps 26.62/9.69   16 steps 29.09/10.50
#
# 12 steps wins almost everywhere; 8 is fine for small frames and saves time,
# 16 only pays off on square crops that are already dense.
QWEN_PRESETS = [
    # key,        width, height, steps, cfg, label
    ("2k",        2048, 1152, 12, 1.0, "2K 宽屏"),
    ("1080p",     1920, 1088, 12, 1.0, "1080p 宽屏"),
    ("720p",      1280, 720,   8, 1.0, "720p 宽屏"),
    ("portrait",  1088, 1920, 12, 1.0, "竖屏"),
    ("square",    1328, 1328, 14, 1.0, "方形"),
    ("square-sm", 1024, 1024, 10, 1.0, "方形小图"),
    ("half",       896, 1152,  8, 1.0, "竖版小图"),
]

QWEN_PRESET_MAP = {p[0]: p for p in QWEN_PRESETS}
QWEN_PRESET_BY_SIZE = {(p[1], p[2]): p for p in QWEN_PRESETS}

# Default tier: high resolution with its matching step count.
QWEN_WIDTH = QWEN_PRESETS[0][1]
QWEN_HEIGHT = QWEN_PRESETS[0][2]
QWEN_STEPS = QWEN_PRESETS[0][3]
QWEN_CFG = QWEN_PRESETS[0][4]


def preset_for(width: int, height: int):
    """The preset whose size matches, or the closest one by pixel count."""
    hit = QWEN_PRESET_BY_SIZE.get((width, height))
    if hit:
        return hit
    area = width * height
    return min(QWEN_PRESETS, key=lambda p: abs(p[1] * p[2] - area))


def recommended_for(width: int, height: int) -> dict:
    """Steps and cfg that suit a given size.

    Used when the caller changes the resolution but leaves steps/cfg at their
    defaults: a 720p frame at 12 steps is wasted time, a 2K frame at 8 is soft.
    """
    p = preset_for(width, height)
    return {"steps": p[3], "cfg": p[4], "preset": p[0], "label": p[5]}

# Qwen-Image is natively high resolution; 1024 was a needless downgrade.
#
# Both values must be divisible by 16: the latent is half the target and must
# itself be a multiple of 8, so 1920x1080 would come out 1920x1088.
QWEN_WIDTH = 2048
QWEN_HEIGHT = 1152

# Largest edge any backend accepts, applied as a single scale factor so the
# aspect ratio survives being clamped.
QWEN_MAX_EDGE = 4096

# Applied when the caller sends no negative prompt.
#
# NOTE: with cfg 1.0 the sampler skips the negative branch entirely, so this
# list is inert in the default configuration. It is kept for people who raise
# cfg above 1.
#
# Asking for "detailed skin pores" or similar at 1024 px makes Qwen-Image draw
# texture it cannot resolve, which reads as dirty/noisy skin. These words push
# back on that without touching the positive prompt the user wrote.
QWEN_NEGATIVE = (
    "noisy, grainy, dirty, blotchy, jpeg artifacts, oversharpened, "
    "plastic skin, deformed, low quality, blurry"
)

_state = {"up": False, "loading": False, "pid": 0, "error": None}

# whether the weights are already in VRAM; reset when the service stops
_warm = [False]


def base() -> str:
    return f"http://127.0.0.1:{COMFY_PORT}"


# Everything here is loopback. urllib honours HTTPS_PROXY / HTTP_PROXY from the
# environment, and with a proxy set the local requests get forwarded outward and
# the connection is dropped - so build an opener that never proxies.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout: int):
    return _OPENER.open(req, timeout=timeout)


def _get(path: str, timeout: int = 20):
    req = urllib.request.Request(base() + path, headers={"Accept": "application/json"})
    with _open(req, timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post(path: str, obj, timeout: int = 300):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base() + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with _open(req, timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def is_up() -> bool:
    try:
        _get("/system_stats", timeout=6)
        return True
    except Exception:
        return False


def start(log_path: str | None = None) -> dict:
    """Launch ComfyUI if it is not already running."""
    if is_up():
        return {"ok": True, "message": "ComfyUI 已经在运行"}
    if not os.path.exists(COMFY_PYTHON):
        return {"ok": False, "message": f"找不到 Python：{COMFY_PYTHON}"}
    if not os.path.exists(os.path.join(COMFY_DIR, "main.py")):
        return {"ok": False, "message": f"找不到 ComfyUI：{COMFY_DIR}"}

    env = os.environ.copy()
    env["PYTHONPATH"] = COMFY_DIR
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    log = open(log_path, "ab", buffering=0) if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [COMFY_PYTHON, "main.py", "--port", str(COMFY_PORT),
             "--listen", "127.0.0.1", "--disable-auto-launch"],
            cwd=COMFY_DIR, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"启动失败：{exc}"}
    finally:
        if log_path:
            log.close()

    _state["pid"] = proc.pid
    _state["loading"] = True
    _warm[0] = False
    return {"ok": True, "message": f"ComfyUI 开始启动（PID {proc.pid}），首次约 10-20 秒"}


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _kill_port(port: int) -> bool:
    """Kill whatever is listening on a port. True if something was stopped."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return False
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    killed = False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           capture_output=True, text=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            killed = True
        except Exception:  # noqa: BLE001
            pass
    return killed


def stop() -> dict:
    """Stop ComfyUI - whether this process started it or the user did.

    Only tracking our own PID missed the case where ComfyUI was already up, so
    the port is checked as well.
    """
    pid = _state.get("pid")
    stopped = False
    if pid:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            stopped = True
        except Exception:  # noqa: BLE001
            pass
    if _kill_port(COMFY_PORT):
        stopped = True
    _state.update({"up": False, "loading": False, "pid": 0})
    _warm[0] = False
    return {"ok": True,
            "message": "ComfyUI 已停止" if stopped else "ComfyUI 本来就没在运行"}


def probe() -> dict:
    """State for the sidebar card."""
    info = {"label": "Qwen 图像", "up": is_up(), "loading": False,
            "enabled": bool(_CC.get("enabled", True)),
            "port": COMFY_PORT, "dir": COMFY_DIR, "error": None,
            "models": [], "ready": False}
    if not info["up"]:
        return info
    try:
        stats = _get("/system_stats", timeout=10)
        for dev in stats.get("devices") or []:
            info["vram_total"] = (dev.get("vram_total") or 0) // 1024 ** 2
            info["vram_free"] = (dev.get("vram_free") or 0) // 1024 ** 2
            info["gpu"] = dev.get("name")
    except Exception:
        pass
    # which models are actually present
    unet_dir = os.path.join(COMFY_DIR, "models", "unet")
    clip_dir = os.path.join(COMFY_DIR, "models", "clip")
    vae_dir = os.path.join(COMFY_DIR, "models", "vae")
    for label, folder, name in (
        ("unet", unet_dir, QWEN_UNET),
        # ComfyUI lists text encoders under clip\text_encoders\
        ("text encoder", clip_dir, os.path.join("text_encoders",
                                               "qwen3vl_8b_w4a8.safetensors")),
        ("vae", vae_dir, QWEN_VAE),
    ):
        path = os.path.join(folder, name)
        ok = os.path.exists(path)
        info["models"].append({"role": label, "file": name, "ok": ok,
                               "bytes": os.path.getsize(path) if ok else 0})
    info["ready"] = all(m["ok"] for m in info["models"])
    return info


def warm() -> bool:
    """True when ComfyUI already holds the model in VRAM.

    Used to pick between the "loading 10 GB" progress curve and the fast one.
    The render happens on model 1's node; ComfyUI's stats do not expose what is
    loaded, so track it locally: a successful render means the next one is warm
    until the service restarts.
    """
    if not is_up():
        _warm[0] = False
        return False
    return _warm[0]


def mark_warm(value: bool = True) -> None:
    _warm[0] = bool(value)


def build_workflow(prompt: str, negative: str = "", seed: int = -1,
                   steps: int = QWEN_STEPS, cfg: float = QWEN_CFG,
                   width: int = QWEN_WIDTH, height: int = QWEN_HEIGHT) -> dict:
    """The node graph Qwen-Image 2.1 expects.

    TextEncodeQwenImage21 also emits the latent, so there is no separate
    EmptyLatent node: feeding one in makes the sampler choke.
    """
    # ComfyUI rejects a negative seed (the SD WebUI treats -1 as "random"),
    # so resolve it here instead of passing -1 through. Track the resolved
    # value separately so the caller can show which one was used.
    if seed is None or seed < 0:
        seed = int(time.time() * 1000) % (2 ** 32)
    used_seed = seed

    # The latent is half the target and must be a multiple of 8, so the target
    # has to be a multiple of 16. Snapping to 8 would let 1080 through and the
    # result would silently come back as 1088.
    #
    # Clamp with a single scale factor. Clamping width and height against
    # separate limits (4096 / 2304) squashed a 1:1 request into 16:9.
    width = max(1, int(width))
    height = max(1, int(height))

    # Scale by a single factor so the aspect ratio survives. Only two things
    # trigger it: an edge above the cap, or an edge below the floor. Clamping
    # each edge separately would turn 9999x1000 into 4096x2304 (10:1 -> 16:9).
    scale = 1.0
    if max(width, height) > QWEN_MAX_EDGE:
        scale = QWEN_MAX_EDGE / max(width, height)
    if min(width * scale, height * scale) < 256:
        # shrink-to-fit already applied; raise the whole pair instead so the
        # short edge clears the floor without stretching the long one
        scale = max(scale, 256 / min(width, height))
    if scale != 1.0:
        width = int(width * scale)
        height = int(height * scale)

    width = max(256, (width // 16) * 16)
    height = max(256, (height // 16) * 16)

    # The graph follows ComfyUI's own Qwen-Image template: a plain
    # EmptySD3LatentImage carries the requested width and height directly.
    #
    # An earlier version fed TextEncodeQwenImage21's latent into LatentUpscale
    # because that encoder always emits a square. It produced the right pixel
    # size (its VAE decodes at 2x) but the stretched latent made the picture
    # look dirty - blocky texture, muddy detail. Building the latent at the
    # right size from the start avoids the problem entirely and matches what
    # the official workflow does.
    workflow = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": QWEN_UNET}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": negative}},
        "6": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        # Qwen-Image is an AuraFlow-style model: the scheduler needs its shift
        # parameter or the result comes out washed and noisy. ComfyUI's own
        # template uses 3.1; leaving it out was part of why pictures looked dirty.
        "10": {"class_type": "ModelSamplingAuraFlow",
               "inputs": {"model": ["1", 0], "shift": 3.1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["10", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "latent_image": ["6", 0],
                         "seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "qwen", "images": ["8", 0]}},
    }
    return workflow, used_seed


def generate(prompt: str, negative: str = "", seed: int = -1,
              steps: int = QWEN_STEPS, cfg: float = QWEN_CFG,
              width: int = 1024, height: int = 1024,
              on_progress=None) -> dict:
    """Queue the workflow and wait for the picture."""
    # ComfyUI refuses a negative seed (the SD WebUI treats -1 as "random"), so
    # resolve it here and report back the value that was actually used
    if seed is None or seed < 0:
        seed = int(time.time() * 1000) % (2 ** 32)
    if not is_up():
        return {"ok": False, "message": "ComfyUI 没有在运行"}
    wf, used_seed = build_workflow(prompt, negative, seed, steps, cfg,
                                  width, height)
    try:
        res = _post("/prompt", {"prompt": wf, "client_id": "5070-ai"}, timeout=120)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {"ok": False, "message": f"工作流被拒绝：{body[:300]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"提交失败：{exc}"}

    p_id = res.get("prompt_id")
    if not p_id:
        return {"ok": False, "message": f"没有拿到 prompt_id：{res}"}

    started = time.time()
    deadline = started + 900
    while time.time() < deadline:
        try:
            hist = _get(f"/history/{p_id}", timeout=20)
        except Exception:
            time.sleep(2)
            continue
        if p_id in hist:
            entry = hist[p_id]
            status = entry.get("status", {})
            images = []
            for node_out in (entry.get("outputs") or {}).values():
                images.extend(node_out.get("images", []) or [])
            if images:
                im = images[0]
                url = (f"{base()}/view?filename={im.get('filename')}"
                       f"&subfolder={im.get('subfolder', '')}"
                       f"&type={im.get('type', 'output')}")
                req = urllib.request.Request(url)
                with _open(req, 120) as r:
                    raw = r.read()
                data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
                _warm[0] = True
                return {"ok": True, "images": [data_url],
                        "seed": used_seed, "elapsed": round(time.time() - started, 1),
                        "prompt": prompt}
            if status.get("status_str") == "error":
                for m in status.get("messages", []):
                    if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict):
                        return {"ok": False,
                                "message": str(m[1].get("exception_message", ""))[:300]}
                return {"ok": False, "message": "生成失败"}
        if on_progress:
            on_progress(time.time() - started)
        time.sleep(2)
    return {"ok": False, "message": "生成超时"}
