# 报 bug 与修 bug 指南

> 这份文档解决一个问题：**开新对话时，怎么让 AI 快速接手这个项目。**
>
> 新对话没有记忆。下面的模板就是给 AI 的「上下文快照」，贴进去它就知道
> 项目在哪、怎么查日志、改完怎么验证。

---

## 一、最省事的做法

新对话第一句直接贴这个：

```
项目在 G:\Strata\Chat，是「橙芯绘话 v1.7」——本地对话+绘图+语音工作台。
先读 README.md 的「实现上踩过的坑」和「显存互斥规则」两节再动手。

问题：<一句话说现象>
复现：<做什么操作会触发>
日志：<相关的日志尾部>
```

**为什么要先读那两节**：

- 「实现上踩过的坑」——里面每个坑都花过几小时，不知道就会重踩
- 「显存互斥规则」——动了这块会让服务抢显卡，表现是「卡死」

---

## 二、问题分类与对应模板

### 2.1 某个服务启动不了

```
服务：SD / Qwen / 9B / 35B / TTS / 语音识别（说哪个）
现象：点了「启动」，然后 <发生什么>
```

**AI 需要看的日志**（会自动去读，你只要说哪个服务）：

| 服务 | 日志 |
|---|---|
| hub（调度层） | `logs/hub.log` |
| SD | `logs/sd.log` |
| Qwen / ComfyUI | `logs/comfy.log` |
| 9B / 35B | `logs/server-9b.log` / `logs/server-35b.log` |
| TTS | `logs/tts.log` |
| 语音识别 | `logs/stt.log` |

另外一条**最有用的信息**：

```powershell
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
```

占比高说明显存没释放干净，这是「启动失败」最常见的原因。

---

### 2.2 出图/出语音有问题

```
服务：SD / Qwen / TTS
操作：<提示词/文字>，参数 <尺寸/步数>
现象：<报错 / 图不对 / 卡住 / 没有反应>
```

**额外信息**（有就贴，没有 AI 会自己查）：

- 界面上进度条走到哪一步停的
- 镜像里有没有图（`outputs/` 目录）

---

### 2.3 界面显示不对

```
位置：<左侧栏 / 绘图面板 / 设置 / 某个弹窗>
现象：<看到什么，期望看到什么>
```

**贴一下浏览器控制台的报错**（F12 → Console）。前端报错在日志里看不到，
必须从浏览器拿。

---

### 2.4 想把某个行为改掉

这类最容易出问题——因为**改动可能碰到互斥逻辑**。

```
现在：<当前行为>
希望：<期望行为>
原因：<为什么要改>
```

AI 会先判断这属于哪一类：

| 改动类型 | 风险 |
|---|---|
| 纯界面文字/样式 | 低 |
| 加新功能 | 中，要动 `_free_gpu` 就有风险 |
| 改互斥/启停逻辑 | **高**，会抢显卡 |
| 改配置格式 | 高，会破坏已有配置 |

---

## 三、本机环境的实际情况

新对话里的 AI 不知道这台机器的状态，容易给错建议。这几条是实测值：

| 项目 | 实际值 |
|---|---|
| 项目目录 | `G:\Strata\Chat` |
| Python（跑 hub） | `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe` |
| SD 整合包 | `D:\AI\sd-webui-aki-v4.2`（自带 Python **3.10**） |
| ComfyUI | `D:\AI\ComfyUI_windows_portable\ComfyUI`（用**系统 Python 3.12**，不是自带的 3.11） |
| GPU | RTX 5070 12GB，驱动支持 CUDA 13.4 |
| 系统 | Windows 11 |
| 代理 | `127.0.0.1:65532`（只有访问 GitHub / HuggingFace 时需要，本地请求不要走） |

**几个容易搞错的点**：

1. **SD 和 ComfyUI 用不同的 Python**。SD 是 3.10（整合包自带），
   ComfyUI 是系统的 3.12。装依赖时别装错地方。

2. **本地请求必须绕过代理**。`urllib` 默认读 `HTTPS_PROXY`，本地请求
   被转发出去会连接重置——表现是「hub 好像崩了」。`comfy_backend.py`
   里用 `build_opener(ProxyHandler({}))` 就是这个原因。

3. **`git push` 需要手动设代理**：

   ```powershell
   $env:HTTPS_PROXY = "http://127.0.0.1:65532"
   $env:GIT_TERMINAL_PROMPT = "0"
   git push origin main
   ```

4. **PowerShell 的编码坑**。控制台是 GBK，Python 脚本里 print 特殊符号
   （`✓` `✗`）会抛 `UnicodeEncodeError` 把脚本打死。脚本里统一用
   `[OK]` / `[FAIL]`。

---

## 四、AI 修完应该做什么

如果你用上面的模板，AI 的标准流程应该是：

1. **先读代码再改**，不要凭印象
2. **改完跑测试**：

   ```powershell
   cd G:\Strata\Chat
   python -m unittest discover -s tests
   ```

   35 项，全过才算没改坏。

3. **语法检查**（Python 和前端都要）：

   ```powershell
   python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) for f in ['hub.py', 'comfy_backend.py']]"
   ```

4. **必要时重启 hub 验证**：

   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*hub.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   Start-Process -FilePath "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe" -ArgumentList '"G:\Strata\Chat\hub.py"', '--no-browser' -WorkingDirectory 'G:\Strata\Chat' -WindowStyle Hidden
   ```

5. **说清改了什么、为什么**——不要只说「修好了」

---

## 五、AI 容易踩的三个坑

这几条是经验，写下来免得重犯：

### 4.1 别用脚本批量改前端，会吞掉代码

用「找下一个 `function`」来定位结尾时，中间夹着的 `const XXX = [...]`
常量块会被一起删掉。上回就这么把 `SUGGESTIONS` 删了，整个 `app.js` 抛
`ReferenceError`，界面直接白屏。

**改完必须检查常量完整性**：

```powershell
python -c "s=open('ui/app.js',encoding='utf-8').read(); [print(n, ('const '+n) in s) for n in ('SUGGESTIONS','DRAW_IDEAS','SPEAK_IDEAS','QWEN_IDEAS','SHORTCUTS')]"
```

### 4.2 前端改了要确认浏览器拿到新代码

`index.html` 里的脚本 URL 带内容哈希，hub 会自动注入。
如果看到「改了但没生效」，先确认 hub 重启过（它每次启动重新算哈希）。

### 4.3 测试进度条时要用随机提示词

ComfyUI **会缓存完全相同的 prompt**，第二次只要 2 秒。用固定提示词测
会以为「功能坏了」——其实是缓存命中，来不及显示进度。

---

## 六、常见现象的快速对照

| 现象 | 大概率原因 | 看哪 |
|---|---|---|
| 点启动没反应，服务一直「未启动」 | 端口被占 / 模型路径错 | `config_problems()` 的红条、`logs/hub.log` |
| 启动后马上变回「未启动」 | 进程崩溃 | 对应服务的日志尾部 |
| 两个服务同时显示「就绪」 | 互斥逻辑被改坏了 | `_free_gpu` 和 `stop_all_models` |
| 出图很慢（几十秒一步） | ComfyUI 用的 torch 没 DynamicVRAM | `logs/comfy.log` 搜 `DynamicVRAM` |
| 界面白屏 | JS 抛错 | 浏览器 F12 Console |
| 朗读念出「星号星号」 | markdown 没过滤 | `ui/markdown-speech.js` |
| 版本号不显示或不对 | `APP_VERSION` 没改 | `hub.py` 顶部 |

---

## 七、如果 AI 说「这个文档没覆盖」

那是真的没覆盖，让它：

1. 基于日志分析，不要编
2. 分析完把结论补进 README 的「实现上踩过的坑」
3. 这样下次就不用重新分析

这份文档和 README 是活的——每次修完 bug 都应该变长一点。

---

## 八、想让我看完整历史？

这一版（v1.3）的开发对话存档在仓库外（v1.7 的修改没有单独存档，结论都已写进 README）：

```
G:\Strata\对话存档\2026-11-23-v1.3-开发对话.md
```

1.1MB，128 轮，带主题索引。

**但日常修 bug 用不上它**——里面是未加工的过程（同一个问题试过四五种方法），
结论已经提炼进 README 了。只在想追溯「当初为什么这么设计」时才需要。
