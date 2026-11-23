"""Download a Stable Diffusion model with automatic resume.

The HF mirror drops the connection every few minutes on some networks, so this
wraps the project's downloader in a retry loop: each attempt continues from the
.part file instead of starting over.

Usage:
    python get-sd-model.py                                  # the default SDXL base
    python get-sd-model.py --repo <org/name> --file <name>  # anything else
    python get-sd-model.py --dest <dir>                     # somewhere else
    python get-sd-model.py --refiner                        # the refiner too

Models land in the SD models folder by default, which is where AUTOMATIC1111
looks for them.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
import download  # noqa: E402

# A couple of models worth having. Each entry: repo, filename, note.
PRESETS = {
    "sdxl": {
        "repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "file": "sd_xl_base_1.0.safetensors",
        "note": "SDXL 基础模型，约 6.9GB，原生 1024×1024",
    },
    "sdxl-refiner": {
        "repo": "stabilityai/stable-diffusion-xl-refiner-1.0",
        "file": "sd_xl_refiner_1.0.safetensors",
        "note": "SDXL 精修模型（可选），约 6GB",
    },
    "sd15": {
        "repo": "stable-diffusion-v1-5/stable-diffusion-v1-5",
        "file": "v1-5-pruned-emaonly.safetensors",
        "note": "SD1.5 基础模型，约 4GB",
    },
    "realvisxl": {
        "repo": "SG161222/RealVisXL_V5.0",
        "file": "RealVisXL_V5.0_fp16.safetensors",
        "note": "RealVisXL V5.0 写实微调，约 6.5GB。"
                "通用写实最好用的一档：人像、风景都能出，"
                "openrail++ 许可，可商用。",
    },
    "juggernaut-xl": {
        "repo": "RunDiffusion/Juggernaut-XL-v9",
        "file": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
        "note": "Juggernaut XL v9 写实微调，约 6.6GB。"
                "偏人像，肤质和光影比 RealVis 更有摄影感。",
    },
}


def safe(text) -> str:
    """Printable on a GBK console. A stray symbol must not kill the retry loop."""
    s = str(text)
    return s.encode("gbk", errors="replace").decode("gbk")


def default_dest() -> str:
    """The SD models folder, taken from config so it stays in one place."""
    sd = config.CFG.get("sd") or {}
    base = sd.get("dir") or ""
    if base:
        return os.path.join(base, "models", "Stable-diffusion")
    return os.path.join(HERE, "models", "Stable-diffusion")


def fetch(repo: str, name: str, dest_dir: str, endpoint: str,
          attempts: int = 12) -> bool:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    url = download.resolve_url({"hf_repo": repo, "hf_file": name}, endpoint)

    print(f"来源: {url}")
    print(f"目标: {dest}\n")

    for attempt in range(1, attempts + 1):
        if os.path.exists(dest):
            size = os.path.getsize(dest)
            print(f"  已存在，{download.human(size)}，跳过")
            return True

        part = dest + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        print(f"  第 {attempt}/{attempts} 次尝试（已有 {download.human(have)}）")

        last = [0.0]

        def progress(done: int, total: int, speed: float) -> None:
            now = time.time()
            if now - last[0] < 5 and done != total:
                return
            last[0] = now
            pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
            print(f"    {pct}  {download.human(done)}"
                  f"{' / ' + download.human(total) if total else ''}"
                  f"  {download.human(speed)}/s", flush=True)

        res = download.download(url, dest, on_progress=progress)
        if res.get("ok"):
            print(f"  [OK] 完成 {download.human(res.get('bytes') or 0)}")
            break

        msg = res.get("message", "")
        # the Windows console is GBK: a stray symbol here would raise and kill
        # the retry loop before it gets a chance to resume
        print(f"  [FAIL] {safe(msg)}")
        if "不完整" in msg or "中断" in msg:
            time.sleep(2)          # the .part file is kept, just resume
            continue
        if "HTTP 5" in msg:
            time.sleep(10)
            continue
        print("  无法续传，停止")
        return False
    else:
        print("  重试次数用尽")
        return False

    check = download.verify(dest)
    if not check.get("ok"):
        print(f"  [FAIL] 完整性检查失败：{safe(check.get('message'))}")
        return False
    print(f"  [OK] 完整性检查通过")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 SD 模型")
    ap.add_argument("preset", nargs="?", default="sdxl",
                    choices=list(PRESETS) + [], help="预设名称")
    ap.add_argument("--repo", help="Hugging Face 仓库，如 org/name")
    ap.add_argument("--file", help="仓库里的文件名")
    ap.add_argument("--dest", help="存放目录，默认 SD 的 models/Stable-diffusion")
    ap.add_argument("--refiner", action="store_true", help="同时下载精修模型")
    ap.add_argument("--endpoint", help="镜像地址，默认用配置里的")
    args = ap.parse_args()

    cfg_dl = config.CFG.get("download") or {}
    endpoint = args.endpoint or cfg_dl.get("hf_endpoint") or "https://huggingface.co"

    todo = []
    if args.repo and args.file:
        todo.append((args.repo, args.file, "自定义"))
    elif args.preset in PRESETS:
        p = PRESETS[args.preset]
        todo.append((p["repo"], p["file"], p["note"]))
    else:
        print(f"未知预设：{args.preset}")
        return 2

    if args.refiner:
        p = PRESETS["sdxl-refiner"]
        todo.append((p["repo"], p["file"], p["note"]))

    dest_dir = args.dest or default_dest()
    print(f"存放目录：{dest_dir}")
    print(f"下载源：{endpoint}\n")

    ok = True
    for repo, name, note in todo:
        print(f"=== {name}")
        print(f"    {note}")
        if not fetch(repo, name, dest_dir, endpoint):
            ok = False
        print()

    if ok:
        print("全部完成。重启 SD 或在界面里点刷新就能看到新模型。")
        return 0
    print("有文件没下成功，重跑本命令可以续传。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
