"""Model downloader with resumable transfers and live progress.

The chat models are far too large for git (28 GB for the pair used during
development), so the repo only carries the recipe: each model in config.json can
declare a direct `url` or a Hugging Face `hf_repo`/`hf_file` pair. This module
fetches whatever is missing.

Two reasons this is a separate file rather than a few lines in hub.py:

* the transfer has to be resumable - a 19 GB download over a flaky link must not
  restart from zero, so it writes to `<name>.part` and sends a Range header;
* progress has to be visible, which means counting bytes as they stream rather
  than blocking on urlopen().read().
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

CHUNK = 1 << 20          # 1 MiB per read: big enough to keep the loop cheap
TIMEOUT = 60             # seconds of socket silence before giving up


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def resolve_url(spec: dict, endpoint: str = "https://huggingface.co") -> str:
    """Turn a model's config entry into a concrete download URL.

    Accepts either a plain `url`, or `hf_repo` + `hf_file` which is expanded to
    the standard resolve path. The endpoint is configurable so mainland users can
    point at hf-mirror.com without editing code.
    """
    url = str(spec.get("url") or "").strip()
    if url:
        return url
    repo = str(spec.get("hf_repo") or "").strip()
    name = str(spec.get("hf_file") or "").strip()
    if repo and name:
        base = (endpoint or "https://huggingface.co").rstrip("/")
        return f"{base}/{repo}/resolve/main/{name}"
    return ""


def _existing_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def download(url: str, dest: str, on_progress=None, on_log=None) -> dict:
    """Download `url` to `dest`, resuming a partial `.part` file if present.

    `on_progress(done_bytes, total_bytes, speed_bytes_per_sec)` is called
    roughly every chunk. Returns {"ok": bool, "path": str, "bytes": int,
    "message": str}.
    """
    if not url:
        return {"ok": False, "message": "没有下载地址"}

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    have = _existing_size(part)

    # already finished?
    if os.path.exists(dest) and not os.path.exists(part):
        return {"ok": True, "path": dest, "bytes": _existing_size(dest),
                "message": "文件已存在"}

    headers = {"User-Agent": "chengxin-huhua/1.0"}
    if have:
        headers["Range"] = f"bytes={have}-"
        log(f"从 {human(have)} 处续传")

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            # server says our partial file is already the whole thing
            os.replace(part, dest)
            return {"ok": True, "path": dest, "bytes": have, "message": "续传完成"}
        return {"ok": False, "message": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败：{exc}"}

    # a 206 means the range was honoured; anything else means start over
    resumed = resp.status == 206
    if have and not resumed:
        have = 0
        log("服务器不支持续传，从头下载")

    total = have
    length = resp.headers.get("Content-Length")
    if length and length.isdigit():
        total = have + int(length)
    else:
        total = 0

    started = time.time()
    written = have
    mode = "ab" if resumed and have else "wb"
    try:
        with open(part, mode) as fh:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                if on_progress:
                    elapsed = max(0.001, time.time() - started)
                    speed = (written - have) / elapsed
                    on_progress(written, total, speed)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"下载中断：{exc}（已保留，可续传）",
                "bytes": written}
    finally:
        resp.close()

    if total and written < total:
        return {"ok": False, "message": f"不完整：{human(written)} / {human(total)}",
                "bytes": written}

    try:
        os.replace(part, dest)
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "message": f"重命名失败：{exc}"}

    elapsed = max(0.001, time.time() - started)
    avg = (written - have) / elapsed
    return {"ok": True, "path": dest, "bytes": written,
            "message": f"完成：{human(written)}，平均 {human(avg)}/s"}


def missing(spec: dict) -> list[tuple[str, str]]:
    """Files this model needs but does not have yet: [(field, path), ...]."""
    out = []
    for field in ("model", "mmproj"):
        path = str(spec.get(field) or "")
        if path and not os.path.exists(path):
            out.append((field, path))
    return out
