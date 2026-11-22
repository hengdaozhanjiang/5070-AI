"""Launch the IndexTTS2 WebUI with the kaldifst/torch DLL conflict worked around.

On this machine `import torch` (which pulls in its own bundled MKL/OpenMP DLLs)
breaks a later `import kaldifst` inside wetext: the DLL initialisation then
fails and the WebUI dies before it can serve anything. Importing kaldifst first
loads its DLLs against the plain system runtime, which works.

The IndexTTS project directory is passed as the first argument (the hub knows
where it lives), so this file can sit anywhere.

    python tts_launch.py <index-tts-dir> [webui args...]
"""
import os
import runpy
import sys

if len(sys.argv) < 2:
    print("usage: tts_launch.py <index-tts-dir> [args...]", flush=True)
    raise SystemExit(2)

PROJECT = os.path.abspath(sys.argv[1])
WEBUI = os.path.join(PROJECT, "webui.py")
if not os.path.exists(WEBUI):
    print(f"[tts-wrapper] webui.py not found under {PROJECT}", flush=True)
    raise SystemExit(2)

# 1. load kaldifst before anything pulls in torch's DLL set
try:
    import kaldifst  # noqa: F401
    print("[tts-wrapper] kaldifst preloaded", flush=True)
except Exception as exc:  # noqa: BLE001
    print(f"[tts-wrapper] kaldifst preload failed: {exc}", flush=True)

# 2. hand over to the real WebUI from inside its own directory
os.chdir(PROJECT)
sys.path.insert(0, PROJECT)
sys.argv = ["webui.py", *sys.argv[2:]]
runpy.run_path(WEBUI, run_name="__main__")
