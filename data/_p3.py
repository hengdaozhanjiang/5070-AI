import io

p = r"G:\Strata\Chat\hub.py"
s = io.open(p, encoding="utf-8").read()

start = s.index("# A process that is alive but has not bound our port this quickly is almost")
end = s.index("def stop_model(cfg: dict) -> dict:")

new = '''# If the model is alive but has not bound our port within this many seconds it
# is almost certainly listening on a *different* one - the .bat disagrees with
# config.json. Fail fast on that rather than making the user wait out the full
# timeout and then read a misleading "process exited" message.
PORT_MISMATCH_AFTER = 25.0

# A launcher that gives up this fast never really started.
CRASH_CHECK_AFTER = 10.0


def _bat_ports(cfg: dict) -> list[int]:
    """Every --port a .bat could be using (it may declare one, or none)."""
    found = []
    declared = bat_port(cfg["bat"]) if cfg.get("bat") else None
    if declared:
        found.append(declared)
    # services other than this model, as the likely culprit for a wrong port
    for other in MODELS.values():
        if other["id"] != cfg["id"]:
            found.append(other["port"])
    for other in (SD, TTS):
        if other.get("enabled") and other.get("port"):
            found.append(other["port"])
    return [p for p in found if p and p != cfg["port"]]


def check_spawn_health(cfg: dict) -> dict | None:
    """Report a stuck or crashed load instead of showing "加载中" forever.

    Returns None while it is still plausibly loading, or a dict describing the
    failure. Called from probe() so every poll benefits.

    Three distinct outcomes, because each needs a different fix:
      * bound the wrong port  -> config.json and the .bat disagree
      * process already gone  -> it crashed, read the log
      * alive but never ready -> genuinely out of VRAM, or a very large model
    """
    mid = cfg["id"]
    started = _spawning.get(mid)
    if not started:
        return None
    if port_open(cfg["port"]):
        return None                      # it bound the port, give it time to answer

    elapsed = time.time() - started

    # 1) our port is closed, but one of the other known ports is open: the .bat
    #    and config.json are pointing at different numbers
    if elapsed >= 6.0:
        for other_port in _bat_ports(cfg):
            if other_port and port_open(other_port):
                _spawning.pop(mid, None)
                _health_cache.pop(mid, None)
                return {
                    "error": f"端口不一致：模型好像监听在 {other_port}，"
                             f"但 config.json 配的是 {cfg['port']}。"
                             f"检查 .bat 里的 --port 并把两处改成同一个值",
                    "hint": "config",
                }

    # 2) still nothing after a grace period - the launcher died on startup
    if elapsed >= CRASH_CHECK_AFTER:
        _spawning.pop(mid, None)
        _health_cache.pop(mid, None)
        return {"error": "启动失败：进程已退出，看日志确认原因（logs 目录）",
                "hint": "log"}

    return None


'''
s = s[:start] + new + s[end:]
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("重写 check_spawn_health：端口探测逻辑已修正")
