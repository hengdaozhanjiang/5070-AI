"""Local, offline Chinese→English translation service for SD prompts.

Runs Helsinki-NLP/opus-mt-zh-en on the CPU. Loads once (~6 s) and then answers
each request in under a second. Kept as a separate process so the 300 MB model
stays resident without touching the GPU or the chat hub.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

_TR = config.CFG.get("translator") or {}
# Where the opus-mt-zh-en weights live. Empty means "let transformers fetch it
# from the Hugging Face hub on first run".
MODEL_DIR = _TR.get("model_dir") or "Helsinki-NLP/opus-mt-zh-en"
PORT = int(_TR.get("port") or 8091)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Needs transformers + torch; launched with an interpreter that has them (SD's,
# by default - see _start_translator in hub.py).
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM  # noqa: E402

_local = os.path.isdir(MODEL_DIR)
print("loading opus-mt-zh-en from", MODEL_DIR, flush=True)
t0 = time.time()
TOKENIZER = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=_local)
MODEL = AutoModelForSeq2SeqLM.from_pretrained(MODEL_DIR, local_files_only=_local)
MODEL.eval()
print(f"loaded in {time.time() - t0:.1f}s on CPU", flush=True)


def has_cjk(text):
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


# Only terms the small opus model reliably mistranslates. Common words are left
# in Chinese: feeding it English words it has never seen makes it hallucinate
# (e.g. "photorealistic" -> "photoealistic, no no no").
TERMS = {
    "赛博朋克": "cyber punk",     # mbart/opus mangles this as "Sybon punk"
    "水母": "jellyfish",         # otherwise "mother of water"
}


def preprocess(text: str) -> str:
    """Replace known prompt terms with English before machine translation."""
    for zh, en in TERMS.items():
        text = text.replace(zh, en)
    return text


def translate(text: str) -> str:
    """Translate Chinese to English; split long text into sentence chunks."""
    if not text or not has_cjk(text):
        return text
    text = preprocess(text)
    # if preprocessing removed all CJK, the terms already carry the meaning
    if not has_cjk(text):
        return text

    # split on Chinese punctuation so each chunk fits the model's 512-token limit
    import re
    parts = [p.strip() for p in re.split(r'[，。！？；、,\n]', text) if p.strip()]
    if not parts:
        parts = [text]

    outputs = []
    for part in parts:
        batch = TOKENIZER([part], return_tensors="pt", padding=True,
                          truncation=True, max_length=256)
        gen = MODEL.generate(**batch, max_new_tokens=128)
        out = TOKENIZER.batch_decode(gen, skip_special_tokens=True)[0].strip()
        # the model often appends a period to each chunk; strip it so joining
        # with commas does not produce "sea., Cyberpunk."
        out = out.rstrip(' .。')
        if out:
            outputs.append(out)
    result = ", ".join(outputs)
    # guard against the model degenerating into "no, no, no..." loops
    words = result.lower().split()
    if len(words) >= 8:
        from collections import Counter
        top, count = Counter(words).most_common(1)[0]
        if count >= 8 and len(result) > 20:
            return text
    return result or text


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._json({"ok": True, "model": "opus-mt-zh-en", "device": "cpu"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/translate":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "message": "bad payload"}, 400)
            return
        text = str(data.get("text") or "")
        t0 = time.time()
        try:
            result = translate(text)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "message": str(exc)}, 500)
            return
        print(f"[{time.time() - t0:.2f}s] {text!r} -> {result!r}", flush=True)
        self._json({"ok": True, "text": text, "translated": result,
                    "seconds": round(time.time() - t0, 2)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"translation service on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
