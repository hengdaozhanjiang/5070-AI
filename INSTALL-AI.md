# 给 AI 的安装指引（INSTALL-AI.md）

> **读者是 AI 助手。** 用户可能把这份文件连同 README 一起丢给你，说「帮我装一下」。
> 这份文档把 README 里默认「读者能自己补全」的部分全部写死了：每一步的确切命令、
> 每个文件的确切内容、每个失败的确切含义。
>
> **做之前先读「必须问用户的问题」一节。** 有三件事我无法替你决定，猜错会让整套装不起来。

---

## 0. 你必须先知道的三件事

1. **这不是一个能「pip install 然后跑」的软件。** 它是个外壳，把用户机器上已有的
   llama.cpp、Stable Diffusion、IndexTTS 接到一个界面里。**它不自带任何模型或推理引擎。**
2. **只支持 Windows。** 代码里用了 Win32 API 摆窗口、`taskkill`、`nvidia-smi`，
   在 Linux/macOS 上跑不起来，不要尝试移植。
3. **只有一个 GPU 服务能同时运行。** 这不是 bug 也不是配置项，是设计前提。
   如果用户想「三个都用」，告诉他做不到，必须来回切。

---

## 1. 必须问用户的问题

在动手之前，逐条问清。**不要替用户假设。**

| # | 问题 | 为什么必须问 | 猜错的后果 |
|---|---|---|---|
| 1 | 显卡型号和显存多少？ | 决定能跑多大的模型，以及 `-ngl`、`-ncmoe` 这些参数 | 显存不够会 OOM，或者跑成共享内存慢十倍 |
| 2 | 想用哪个 GGUF 模型？有现成的吗？ | `config.json` 里的 `model` 必须指向真实存在的文件 | 启动时报「模型文件不存在」 |
| 3 | llama.cpp 装了吗？`llama-server.exe` 在哪？ | 仓库不带推理引擎 | 完全没有可执行文件，第一步就断 |
| 4 | 要用绘图吗？SD WebUI 装了吗？路径？ | 可选组件，留空即禁用 | 配错路径会导致启动 SD 失败 |
| 5 | 要用语音合成吗？IndexTTS 装了吗？路径？ | 可选组件，留空即禁用 | 同上 |
| 6 | Python 在哪？版本？ | 需要 3.10+，且要用 `pythonw.exe` 启动才不弹控制台 | 版本过低会语法错误 |

**如果用户只想「先跑起来看看」**：只配对话功能，SD 和 TTS 全部留空。这是最短路径。

---

## 2. 目录布局与 `.bat` 文件（最容易漏的一步）

**README 没有给出 `.bat` 的内容，但 `hub.py` 会执行 `cmd /c <bat>`。
没有这个文件，模型永远启动不了。**

`llama_dir` 下的推荐布局：

```
<llama_dir>\
├─ llama\
│  └─ llama-server.exe        ← llama.cpp 的推理服务
├─ bin\
│  ├─ ffmpeg.exe              ← 可选，解码 WebP 图片需要
│  └─ ffprobe.exe             ← 同上
├─ <模型文件>.gguf
├─ <mmproj 文件>.gguf         ← 多模态投影层，看图才需要
├─ start-llama-server-9b.bat
└─ start-llama-server.bat
```

### `.bat` 必须写的内容

**9B（`start-llama-server-9b.bat`）**，把 `%%` 内的路径换成用户的实际路径：

```bat
@echo off
cd /d "<LLAMA_DIR>"
set "PATH=<LLAMA_DIR>\bin;%PATH%"
"<LLAMA_DIR>\llama\llama-server.exe" -m "<LLAMA_DIR>\<模型>.gguf" --mmproj "<LLAMA_DIR>\<mmproj>.gguf" --host 127.0.0.1 --port 8082 -c 40960 -ngl 99 -fa on -t 6 --jinja -np 1 --no-cache-idle-slots --reasoning-budget 0
pause
```

**35B（`start-llama-server.bat`）** 的区别只有三处：端口 8081、上下文 65536、多一个 `-ncmoe 24`：

```bat
@echo off
cd /d "<LLAMA_DIR>"
set "PATH=<LLAMA_DIR>\bin;%PATH%"
"<LLAMA_DIR>\llama\llama-server.exe" -m "<LLAMA_DIR>\<模型>.gguf" --mmproj "<LLAMA_DIR>\<mmproj>.gguf" --host 127.0.0.1 --port 8081 -c 65536 -ngl 99 -ncmoe 24 -fa on -t 6 --jinja -np 1 --no-cache-idle-slots --reasoning-budget 0
pause
```

### 每个参数为什么存在（不要随意删）

| 参数 | 作用 | 删了会怎样 |
|---|---|---|
| `--host 127.0.0.1` | 只监听本机 | 默认监听 0.0.0.0，局域网能访问你的模型 |
| `--port 8082` / `8081` | **必须和 `config.json` 的 `port` 完全一致** | 端口对不上，hub 会一直显示「加载中」然后超时 |
| `-c 40960` | 上下文长度 | 和 `config.json` 的 `ctx` 不一致时，界面显示的上下文是错的 |
| `-ngl 99` | 所有层放 GPU | 减小会变慢（部分层跑到 CPU） |
| `-ncmoe 24` | 前 24 层 MoE 专家留在 CPU | 只对 35B 有意义；调大到 16/20 会撞 12GB 墙上，掉到 11-18 tok/s |
| `-fa on` | flash attention | 关掉更慢更吃显存 |
| `-t 6` | 线程数 | 通用建议：物理核数的一半左右 |
| `--jinja` | 启用模型的 chat template | 关掉多轮对话会错乱 |
| `-np 1` | 单并发槽 | 多槽会成倍吃显存 |
| `--no-cache-idle-slots` | 空闲不缓存 | 关掉会白占显存 |
| `--reasoning-budget 0` | 跳过思维链 | 不设的话 thinking 会吃掉输出预算，用户以为模型话少 |
| `pause` | 出错时窗口停住 | 删了会一闪而过，看不到报错 |

> ⚠️ **`-ncmoe` 只对 MoE 模型（如 35B-A3B）有意义**。9B 是稠密模型，没有 MoE 专家可分层，
> 加上它不会报错但也不起作用——不要给 9B 加，避免让人误以为那是个必要的参数。

---

## 3. 逐步安装流程

### 第 1 步：确认 Python

```powershell
python --version
```

- 需要 **3.10 或更高**
- 同时确认 `pythonw.exe` 存在（和 `python.exe` 同目录）：`where pythonw`
- 如果只有 `python` 没有 `pythonw`，用户会在启动时看到控制台黑框一闪而过

### 第 2 步：拿到代码

```powershell
cd <用户想放的目录>
git clone https://github.com/hengdaozhanjiang/5070-AI.git
cd 5070-AI
```

没有 git 的话，让用户在 GitHub 页面点 **Code → Download ZIP** 解压，效果一样。

### 第 3 步：创建配置

```powershell
copy config.example.json config.json
```

然后**用文本编辑器**改 `config.json`（不要用记事本，它可能加 BOM 导致 JSON 解析失败；
如果只能用记事本，改完用 `python -c "import json;json.load(open('config.json',encoding='utf-8'))"` 验证）。

### 第 4 步：填配置

照用户回答填。**最小可用配置**（只跑对话，9B）：

```jsonc
{
  "llama_dir": "D:\\llama",          // 换成实际路径
  "models": [
    {
      "id": "9b",
      "label": "Qwen 9B",
      "short": "9B",
      "port": 8082,                   // 必须与 .bat 一致
      "bat": "start-llama-server-9b.bat",   // 相对 llama_dir
      "model": "你的模型.gguf",        // 相对 llama_dir，文件名要一模一样
      "mmproj": "你的mmproj.gguf",     // 不看图就填空字符串
      "ctx": 40960,
      "args": ["-c", "40960", "-ngl", "99"]
    }
  ],
  "default_model": "9b",
  "sd":  { "enabled": false, "dir": "" },
  "tts": { "enabled": false, "dir": "" }
}
```

**关键一致性检查**（这三处对不上是最常见的失败原因）：

| 必须一致 | 位置 A | 位置 B |
|---|---|---|
| 端口 | `config.json` 的 `port` | `.bat` 里的 `--port` |
| 模型文件名 | `config.json` 的 `model` | 磁盘上真实文件名 |
| 模型文件名 | `config.json` 的 `model` | `hf_file`（如果配了下载） |

### 第 5 步：下载模型（可选）

如果用户没现成模型，用自带的下载器（支持断点续传）：

```powershell
python get-models.py --list    # 先看缺什么
python get-models.py           # 下载全部缺失的
```

或者界面里点模型卡片上的「下载」。

### 第 6 步：验证配置能加载

```powershell
python -c "import config; import json; print(json.dumps(config.CFG, ensure_ascii=False, indent=2))"
```

能看到完整 JSON 说明配置格式没问题。如果报错，看错误信息里的字段名。

### 第 7 步：启动

```powershell
python hub.py
```

**第一次启动会发生什么：**
1. 后台起一个翻译服务（约 6 秒，只占 CPU）
2. 浏览器（Edge）以应用窗口模式打开 `http://127.0.0.1:8090`
3. 界面里**所有服务都是未启动状态**——这是正常的，不会自动加载模型

### 第 8 步：让用户点「启动」

界面左侧「模型与服务」是**折叠**的。点开它，点模型卡片上的「启动」。
等 30 秒到 3 分钟（看模型大小），状态变成绿点「就绪」。

---

## 4. 验证安装成功的标准

按顺序检查，任何一步不过就停下排查：

| # | 检查 | 命令 / 现象 | 通过标准 |
|---|---|---|---|
| 1 | 配置能解析 | `python -c "import config; print(config.CFG['llama_dir'])"` | 输出用户设的路径 |
| 2 | hub 能起 | `python hub.py` | 浏览器窗口打开，不报错 |
| 3 | 状态接口通 | 浏览器访问 `http://127.0.0.1:8090/api/state` | 返回 JSON，含 `models` 数组 |
| 4 | 配置无问题 | 上面 JSON 里 `hub.problems` | 是空数组 `[]` |
| 5 | 模型能加载 | 界面点启动 | 绿点「就绪」，顶部标签显示 `9B · :8082` |
| 6 | 能真的对话 | 输入「你好」回车 | 有回复流式出现，底部显示 tok/s |

**第 6 步是最关键的**：前五步都过但第 6 步失败，通常是模型本身的问题（文件损坏、
量化格式不支持），不是配置问题。

---

## 5. 常见故障与确切原因

### 「启动脚本不存在：<路径>」

`config.json` 里 `bat` 字段指向的文件不在。
- 检查 `llama_dir` + `bat` 拼起来的完整路径是否存在
- 注意 `.bat` 是相对 `llama_dir` 的，不要写成绝对路径（虽然也支持）

### 「模型文件不存在：<路径>」

`model` 字段指向的 gguf 不在。最常见的原因是**下载下来是 `xxx-Q4_K_M.gguf`
但配置里写的是 `xxx.gguf`**。用 `dir` 确认真实文件名。

### 一直显示「加载中」，最后变「加载超时」

按可能性排序：
1. **端口对不上**——`.bat` 里 `--port 8082`，`config.json` 里 `port: 8082`，必须一致
2. **显存不够**——看 `nvidia-smi`，9B 约需 7GB，35B 约需 20GB
3. **模型文件损坏**——用 `python get-models.py` 会做文件头校验
4. **`.bat` 里路径有中文或空格且没加引号**——所有路径都要用双引号包住

### 「显存没释放干净：当前空闲 X MB」

切换模型时，上一个卸载后显存没回到阈值。原因和处置：
- 有其他程序占着显卡（浏览器硬件加速、游戏、其他 AI 工具）→ 关掉
- 驱动还没释放 → 等 10 秒重试
- 真的卡住了 → 重启一次

### 界面能开，但顶部标签是「—」

`/api/state` 里 `models` 为空，说明配置里 `models` 数组没填或 `id` 为空。

### 语音合成启动就崩

看 `logs/tts.log`。最常见的是 `kaldifst` 的 DLL 初始化失败——需要 `tts_launch.py`
存在（仓库自带），它会先导入 kaldifst 再导入 torch。如果这个文件被删了就修不好。

### 中文提示词翻译不生效

`translator.py` 需要 `transformers` / `torch` / `sentencepiece`。
hub 默认用 SD 的 Python 跑它。没有 SD 的话，让用户：
- 自己准备一个带这些包的解释器，在 `config.json` 的 `translator` 段里指定 `python` 路径
- 或者干脆不用翻译，直接写英文提示词

---

## 6. 绝对不要做的事

| 不要 | 原因 |
|---|---|
| 不要 `pip install -r requirements.txt` | 没有这个文件，主程序纯标准库 |
| 不要用 `webui.bat` 启动 SD | 它会创建空 venv 并试图下载 2.4GB torch，失败后一闪即关。必须用 `<SD目录>\python\python.exe` 直接跑 `launch.py` |
| 不要同时启动两个 GPU 服务 | 会 OOM 或卡死，这是硬件限制 |
| 不要改 `python -m http.server` 之类的替代方案 | hub 不是静态服务器，它要代理和进程管理 |
| 不要把 `config.json` 提交进 git | 里面有用户的绝对路径 |
| 不要建议用 Linux/macOS | 代码是 Windows 专用的 |
| 不要给 9B 加 `-ncmoe` | 9B 是稠密模型，没有 MoE 专家可分层，加了不报错但没作用，容易误导 |
| 不要改 `config.py` 的 `DEFAULTS` | 那是兜底默认值，改了会在更新代码时被覆盖。所有用户配置都放 `config.json` |

---

## 7. 给用户的最小可用清单

如果用户只想「先看看长什么样」，把步骤压到最短：

```powershell
# 1. 确认 Python
python --version          # 需要 3.10+

# 2. 拿代码
git clone https://github.com/hengdaozhanjiang/5070-AI.git
cd 5070-AI

# 3. 建配置
copy config.example.json config.json

# 4. 编辑 config.json，只改 llama_dir 和 models 里的路径
#    sd.enabled 和 tts.enabled 都设 false

# 5. 下载模型
python get-models.py

# 6. 启动
python hub.py
```

然后界面里点「模型与服务」→「启动」。

---

## 8. 问用户「为什么跑不起来」时的排查顺序

按这个顺序问，能最快定位：

1. **「界面打开了吗？」** — 没打开就是 hub 没起来，看控制台报错
2. **「左侧卡片显示什么状态？」** — 未启动 / 加载中 / 就绪 / 报错信息
3. **「点启动后多久变的？」** — 立刻失败是路径问题，过了 2 分钟才失败是显存或加载问题
4. **「`logs/server-9b.log` 最后五行是什么？」** — 这是最直接的证据
5. **「`nvidia-smi` 显示多少空闲显存？」** — 少于 7GB 就跑不了 9B

`logs/` 目录下各文件的用途：

| 文件 | 内容 |
|---|---|
| `hub.log` | hub 自己的运行日志 |
| `server-9b.log` | 9B 模型的启动和推理日志，**排查启动失败看这个** |
| `server-35b.log` | 同上 |
| `sd.log` | Stable Diffusion 的日志 |
| `tts.log` | IndexTTS 的日志 |
| `translator.log` | 翻译服务的日志 |
