import io

msg = """推送前复查，修掉五个 bug

1. ComfyUI 启停必然崩溃
   comfy_backend.py 用了 subprocess 却没 import，start() 和 stop()
   一调就 NameError。之前测试走的是手动启动的实例，绕过了这条路径。

2. 手工启动的 ComfyUI 停不掉
   stop() 只杀自己记下的 PID。改成同时按端口查，谁监听 8188 就杀谁。

3. 启动 SD/TTS/聊天模型时不会停 ComfyUI
   _free_gpu() 只处理 MODELS/SD/TTS，压根不知道 ComfyUI 的存在。
   结果是点「启动 SD」时 Qwen 还占着显存——正是会导致卡死的那种状态。
   现在 _free_gpu 会一并卸掉 ComfyUI（除非 keep_id 就是它）。

4. 本地请求被代理劫持
   urllib 默认读 HTTPS_PROXY，请求被转发出去后连接被重置。测试时踩过，
   当时误判成 hub 崩了。改成不走代理的 opener。

5. Qwen 模式下 SD 的模型下拉没隐藏
   选择器写成 #draw-checkpoints，实际 id 是 #draw-ckpts，null 保护让它
   静默失效。另外补了 ready 检查：缺模型时提示具体缺哪个，而不是打开
   面板后才在出图时失败。

顺带补上 /api/progress 的 comfy 字段（进度条要读它）。

验证：
- 三向互斥都过（ComfyUI→SD、SD→ComfyUI、重复启动），每轮 GPU 服务数 ≤1
- stop() 在 pid=0（模拟手工启动）时仍能停掉实例
- 浏览器 18/18：两个后端来回切、控件显隐恢复、真实出图、seed 回传
- 35 个单元测试通过
"""

with io.open(r"G:\Strata\Chat\data\_msg.txt", "w", encoding="utf-8") as fh:
    fh.write(msg)
print("ok")
