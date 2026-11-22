"""Configuration loader.

Everything machine-specific lives in `config.json` next to this file, so the
published code carries no personal paths. `config.example.json` is the template
that ships with the repo; copy it to `config.json` and edit the paths.

Environment variables override any value, which is handy for a one-off run:

    CHENGXIN_LLAMA_DIR=D:\\models python hub.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
EXAMPLE_FILE = os.path.join(HERE, "config.example.json")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8090,
    # where llama-server .bat files and .gguf weights live
    "llama_dir": "",
    # chat models, in sidebar order. `id` is used everywhere internally.
    "models": [
        {
            "id": "9b",
            "label": "Qwen 9B 快速",
            "short": "9B",
            "blurb": "默认模型，反应最快",
            "color": "#8ce99a",
            "port": 8082,
            "bat": "start-llama-server-9b.bat",
            "model": "model-9b.gguf",
            "mmproj": "",
            "ctx": 40960,
            "args": ["-c", "40960", "-ngl", "99"],
            # where to fetch the weights from if they are missing (see get-models)
            "url": "",
            "hf_repo": "",
            "hf_file": "",
            "about": "9B 稠密模型，40K 上下文，首字延迟最低，日常默认用它。",
        },
        {
            "id": "35b",
            "label": "Qwen 35B 深度",
            "short": "35B",
            "blurb": "MoE 35B，会看图，质量优先",
            "color": "#7cc4ff",
            "port": 8081,
            "bat": "start-llama-server.bat",
            "model": "model-35b.gguf",
            "mmproj": "",
            "ctx": 65536,
            "args": ["-c", "65536", "-ngl", "99", "-ncmoe", "24"],
            "url": "",
            "hf_repo": "",
            "hf_file": "",
            "about": "MoE 结构，体积大但每 token 只激活部分专家，速度接近小模型，"
                     "知识面更广，支持图片输入。",
        },
    ],
    "default_model": "9b",
    # Stable Diffusion WebUI (AUTOMATIC1111 / 秋葉整合包)
    "sd": {
        "enabled": True,
        "label": "SD 绘图",
        "dir": "",
        "python": "",
        "port": 7860,
        "args": ["launch.py", "--api", "--no-hashing", "--skip-python-version-check"],
        # extra text appended to every prompt to steer the default look
        "style_guard": "detailed skin texture, natural lighting, film grain, sharp focus",
        # per-checkpoint display name / note shown in the drawing panel
        "checkpoints": {},
    },
    # IndexTTS2 text-to-speech
    "tts": {
        "enabled": True,
        "label": "语音合成",
        "dir": "",
        "python": "",
        "port": 7862,
        "args": ["--port", "7862", "--host", "127.0.0.1"],
        "ref_dir": "",
        "default_voice": "",
        "allow_bundled": [],
        "hide": [],
    },
    # base endpoints used by the model downloader
    "download": {
        # Hugging Face endpoint; set this to https://hf-mirror.com in mainland China
        "hf_endpoint": "https://huggingface.co",
        # how many parallel connections for a single file (0 = let the server decide)
        "connections": 4,
    },
    # local CPU translator used to turn Chinese prompts into English
    "translator": {
        "enabled": True,
        "port": 8091,
        "model_dir": "",
    },
}


def _merge(base: dict, override: dict) -> dict:
    """Shallow-merge but keep nested dicts (so partial configs still work)."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_overrides() -> dict:
    env = os.environ
    out: dict = {}
    if env.get("CHENGXIN_HOST"):
        out["host"] = env["CHENGXIN_HOST"]
    if env.get("CHENGXIN_PORT"):
        out["port"] = int(env["CHENGXIN_PORT"])
    if env.get("CHENGXIN_LLAMA_DIR"):
        out["llama_dir"] = env["CHENGXIN_LLAMA_DIR"]
    sd_dir = env.get("CHENGXIN_SD_DIR")
    if sd_dir:
        out.setdefault("sd", {})["dir"] = sd_dir
    tts_dir = env.get("CHENGXIN_TTS_DIR")
    if tts_dir:
        out.setdefault("tts", {})["dir"] = tts_dir
    return out


def load() -> dict:
    """Read config.json (if present) on top of the defaults."""
    raw: dict = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"config.json 读取失败：{exc}")
    return _merge(_merge(DEFAULTS, raw), _env_overrides())


def ensure_exists() -> None:
    """Create config.json from the example on first run, so paths are obvious."""
    if os.path.exists(CONFIG_FILE):
        return
    if os.path.exists(EXAMPLE_FILE):
        with open(EXAMPLE_FILE, "r", encoding="utf-8") as src:
            data = src.read()
    else:
        data = json.dumps(DEFAULTS, ensure_ascii=False, indent=2)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(data)


CFG = load()
