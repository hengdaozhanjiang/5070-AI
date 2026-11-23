"""Local speech-to-text service for the chat box.

Runs faster-whisper on the CPU. Kept as a separate process for the same reason
as the translator: the model stays resident between requests, and a crash here
cannot take the hub down with it. It uses no VRAM, so it can run alongside any
GPU service.

Endpoints:
    GET  /health          -> {"ok": true, "model": "...", "loaded": bool}
    POST /transcribe      <- raw audio bytes (wav/mp3/m4a/webm)
                          -> {"ok": true, "text": "...", "language": "zh"}

The browser records with MediaRecorder, which produces WebM/Opus; faster-whisper
decodes it through `av`, so no external ffmpeg is needed.
"""
import io
import json
import os
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

_STT = config.CFG.get("stt") or {}
PORT = int(_STT.get("port") or 8092)
MODEL_SIZE = _STT.get("model") or "small"
DEVICE = "cpu"
COMPUTE = _STT.get("compute_type") or "int8"

MODEL = None
LOAD_ERROR = None


def load_model() -> None:
    """Load once at startup. Downloads on first run, then cached on disk."""
    global MODEL, LOAD_ERROR
    model_dir = _STT.get("model_dir") or ""
    try:
        from faster_whisper import WhisperModel
        t0 = time.time()
        print(f"loading faster-whisper {MODEL_SIZE} ({COMPUTE}, {DEVICE})…", flush=True)
        if model_dir and os.path.isdir(model_dir):
            MODEL = WhisperModel(model_dir, device=DEVICE, compute_type=COMPUTE)
        else:
            # no local copy: let it fetch and cache
            MODEL = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE)
        print(f"model ready in {time.time() - t0:.1f}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"model failed to load: {LOAD_ERROR}", flush=True)


def transcribe(raw: bytes) -> dict:
    """Decode audio bytes and return the text."""
    if MODEL is None:
        return {"ok": False, "message": f"语音模型没加载成功：{LOAD_ERROR or '未知原因'}"}
    if not raw:
        return {"ok": False, "message": "没有收到音频数据"}

    tmp = os.path.join(HERE, "data", "_stt_tmp.bin")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        # VAD stays OFF by default. Its onnxruntime backend segfaults
        # (0xC0000005) on this machine - the crash is silent and kills the whole
        # process, so it cannot be caught. Without VAD transcription works fine
        # and is fast enough for short voice input; the only cost is that long
        # silences are still fed to the model.
        use_vad = bool(_STT.get("vad", False))
        segments, info = MODEL.transcribe(
            tmp,
            beam_size=_STT.get("beam_size") or 5,
            vad_filter=use_vad,
            **({"vad_parameters": {"min_silence_duration_ms": 500}}
               if use_vad else {}),
            condition_on_previous_text=False,
        )
        parts = [seg.text for seg in segments]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"识别失败：{type(exc).__name__}: {exc}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    text = "".join(parts).strip()
    # Whisper likes to add a trailing period to Chinese; harmless, leave it
    return {"ok": True, "text": text,
            "language": getattr(info, "language", None),
            "seconds": round(getattr(info, "duration", 0.0) or 0.0, 2)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._json({"ok": True, "model": MODEL_SIZE,
                        "loaded": MODEL is not None,
                        "error": LOAD_ERROR, "port": PORT})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/transcribe":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json({"ok": False, "message": "空请求"}, 400)
            return
        if length > 60 * 1024 * 1024:
            self._json({"ok": False, "message": "音频太大（上限 60MB）"}, 400)
            return
        raw = self.rfile.read(length)
        t0 = time.time()
        result = transcribe(raw)
        result["elapsed"] = round(time.time() - t0, 2)
        self._json(result)

    def log_message(self, fmt, *args):
        # keep the console quiet; the hub tails this file only on demand
        pass


def main() -> int:
    load_model()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"speech-to-text listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
