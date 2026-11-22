#!/usr/bin/env python3
"""Fetch the model weights this project needs, without starting the app.

    python get-models.py              # download everything that is missing
    python get-models.py 9b           # only the model with id "9b"
    python get-models.py --list       # show what is configured and what is present

Reads the same config.json as the app, so there is one place to describe where a
model comes from. A model entry can carry either:

    "url": "https://example.com/model.gguf"          direct link
    "hf_repo": "org/repo", "hf_file": "model.gguf"   Hugging Face resolve path

The mmproj (vision projector) uses "mmproj_url" / "mmproj_hf_file" the same way.

Run this if the in-app downloader is not available (for example on a headless
box), or to pre-fetch before first launch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Windows consoles default to GBK; force UTF-8 so Chinese labels print cleanly
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config     # noqa: E402
import download   # noqa: E402


def plan_for(spec: dict, endpoint: str) -> list[tuple[str, str, str]]:
    """[(label, url, dest)] for every file this model is missing."""
    out = []
    for field, path in download.missing(spec):
        probe = dict(spec)
        if field == "mmproj":
            probe["url"] = spec.get("mmproj_url") or ""
            probe["hf_file"] = spec.get("mmproj_hf_file") or ""
        url = download.resolve_url(probe, endpoint)
        out.append((field, url, path))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="下载模型权重")
    ap.add_argument("model", nargs="?", help="只处理这个 id，默认全部")
    ap.add_argument("--list", action="store_true", help="只列出状态，不下载")
    args = ap.parse_args()

    cfg = config.CFG
    endpoint = ((cfg.get("download") or {}).get("hf_endpoint")
                or "https://huggingface.co")

    # rebuild the resolved model table the same way hub.py does
    llama_dir = cfg.get("llama_dir") or ""
    selected = []
    for raw in cfg.get("models") or []:
        mid = str(raw.get("id") or "")
        if args.model and mid != args.model:
            continue
        spec = dict(raw)
        for field in ("model", "mmproj"):
            path = spec.get(field) or ""
            if path and not os.path.isabs(path):
                spec[field] = os.path.join(llama_dir, path)
        selected.append((mid, spec))

    if not selected:
        print(f"没有匹配的模型：{args.model or '(全部)'}")
        return 2

    total_planned = 0
    for mid, spec in selected:
        print(f"\n[{mid}] {spec.get('label') or mid}")
        todo = plan_for(spec, endpoint)
        present = [f for f in ("model", "mmproj")
                   if spec.get(f) and os.path.exists(spec[f])]
        for f in present:
            size = os.path.getsize(spec[f])
            print(f"  [OK] {os.path.basename(spec[f])}  {download.human(size)}")
        if not todo:
            print("  全部就绪")
            continue
        for field, url, dest in todo:
            if url:
                print(f"  [DL] {os.path.basename(dest)}")
                print(f"      {url}")
            else:
                print(f"  [--] {os.path.basename(dest)} 没有配置下载地址")
        total_planned += len(todo)

    if args.list or not total_planned:
        return 0

    print("\n开始下载（Ctrl+C 可中断，下次会续传）")
    failures = 0
    for mid, spec in selected:
        for field, url, dest in plan_for(spec, endpoint):
            if not url:
                failures += 1
                continue
            name = os.path.basename(dest)
            last = [0.0]

            def progress(done: int, total: int, speed: float) -> None:
                # redraw at most 10x/second so the console stays readable
                import time as _t
                now = _t.time()
                if now - last[0] < 0.1 and done != total:
                    return
                last[0] = now
                pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
                line = (f"  {name}  {pct}  {download.human(done)}"
                        f"{' / ' + download.human(total) if total else ''}"
                        f"  {download.human(speed)}/s")
                print("\r" + line.ljust(78), end="", flush=True)

            result = download.download(url, dest, on_progress=progress)
            print()
            if result.get("ok"):
                print(f"  [OK] {name}  {download.human(result.get('bytes') or 0)}")
            else:
                failures += 1
                print(f"  [!!] {name}  {result.get('message')}")

    print()
    if failures:
        print(f"{failures} 个文件没下成，重跑本命令可续传")
        return 1
    print("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
