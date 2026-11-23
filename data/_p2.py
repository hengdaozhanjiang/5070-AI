import io

p = r"G:\Strata\Chat\hub.py"
s = io.open(p, encoding="utf-8").read()

old = '''def check_spawn_health(cfg: dict) -> dict | None:
    """Report a stuck or crashed load instead of showing "加载中" forever.

    Returns None while it is still plausibly loading, or a dict describing the
    failure. Called from probe() so every poll benefits.
    """
    mid = cfg["id"]
    started = _spawning.get(mid)
    if not started:
        return None
    if port_open(cfg["port"]):
        return None                      # it bound the port, give it time to answer
    elapsed = time.time() - started
    if elapsed < spawn_timeout(cfg):
        return None

    # too long: see whether the process is even alive
    alive = bool(server_pids(cfg["port"]))
    _spawning.pop(mid, None)
    _health_cache.pop(mid, None)
    if not alive:
        return {"error": "启动失败：进程已退出，看日志确认原因",
                "hint": "log"}
    return {"error": f"加载超时（{int(elapsed)} 秒仍未就绪），可能是显存不足",
            "hint": "log"}
'''

new = '''# A process that is alive but has not bound our port this quickly is almost
# always listening on a *different* port - the .bat disagrees with config.json.
# Fail fast on that instead of making the user wait out the full timeout.
PORT_MISMATCH_AFTER = 25.0


def check_spawn_health(cfg: dict) -> dict | None:
    """Report a stuck or crashed load instead of showing "加载中" forever.

    Returns None while it is still plausibly loading, or a dict describing the
    failure. Called from probe() so every poll benefits.

    Three distinct outcomes, because they need different fixes:
      * process gone            -> crashed, read the log
      * alive but wrong port    -> config/bat port mismatch (checked early)
      * alive, right port, slow -> genuinely out of VRAM or a huge model
    """
    mid = cfg["id"]
    started = _spawning.get(mid)
    if not started:
        return None
    if port_open(cfg["port"]):
        return None                      # it bound the port, give it time to answer
    elapsed = time.time() - started

    alive = bool(server_pids(cfg["port"]))

    # an early, decisive check: the process exists but our port stays closed
    if not alive and elapsed >= 8.0:
        # not bound to *our* port either - look for the port it actually took
        declared = bat_port(cfg["bat"]) if cfg.get("bat") else None
        if declared and declared != cfg["port"] and port_open(declared):
            _spawning.pop(mid, None)
            _health_cache.pop(mid, None)
            return {
                "error": f"端口不一致：模型实际监听 {declared}，"
                         f"但 config.json 写的是 {cfg['port']}。"
                         f"把两边改成同一个值即可",
                "hint": "config",
            }
        if elapsed >= PORT_MISMATCH_AFTER:
            _spawning.pop(mid, None)
            _health_cache.pop(mid, None)
            return {"error": "启动失败：进程已退出，看日志确认原因",
                    "hint": "log"}
        return None

    if elapsed < spawn_timeout(cfg):
        return None

    _spawning.pop(mid, None)
    _health_cache.pop(mid, None)
    if not alive:
        return {"error": "启动失败：进程已退出，看日志确认原因",
                "hint": "log"}
    return {"error": f"加载超时（{int(elapsed)} 秒仍未就绪），可能是显存不足",
            "hint": "log"}
'''

if old not in s:
    raise SystemExit("找不到 check_spawn_health 原文")

s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("check_spawn_health 已增强：能识别端口不一致")
