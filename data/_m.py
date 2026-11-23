import io
msg = """把这一轮调试的知识全部补进文档

判断：对话本身不值得长期保存（太长、满是试错噪音），
但里面的结论必须沉淀到仓库，否则以后重踩一遍要花几小时。
查了一下，有两个真缺口：

1. TextEncodeQwenImage21 自带 latent —— 完全没记录，这个坑必重踩
2. faster-whisper 只写了 pip install，没钉版本 —— 照做会段错误

「实现上踩过的坑」从 1347 字符扩到 3784，新增三个分组：

### ComfyUI / Qwen-Image
- TextEncodeQwenImage21 自带 latent 输出，再接 EmptyLatent 会报
  too many values to unpack
- CLIPLoaderGGUF 的 type 列表里没有 qwen_image，要用普通 CLIPLoader
- KSampler 不接受负 seed（SD 习惯的 -1 会被拒）
- resolution 是整数不是节点连接
- torch 2.7 没有 DynamicVRAM，出图慢 30 倍（79 秒/步 vs 2 秒）
- ComfyUI 会缓存相同 prompt，调试进度条时会被骗
- 三个模型文件缺一不可

### 依赖安装的坑
- faster-whisper 必须钉四件套（1.2.1 / 4.4.0 / 12.3.0 / 70.3.0），
  否则在部分 AMD CPU 上无声段错误
- VAD 开关同样会崩，默认关掉
- ComfyUI 的 requirements 会把 torch 换成 CPU 版，装完要重装 cu128
- 便携版自带的 Python 3.11 跑不起来，要用系统 3.12
- c10.dll 初始化失败 = MSVCP140 太旧，改 Python 目录别动 System32
- 7zr/py7zr 解不了 BCJ2，要用真 7-Zip 或官方 7zr.exe

### 大文件下载
- 代理截断长连接（2.7GB 到 1.1GB 断）
- PyTorch 索引和 wheel 不在同一域名，两个 CDN 可能不一致
- 解法：8 路分块 + SHA256 校验
- HF 的 raw CDN 会缓存旧文件

另外：
- faster-whisper 的安装命令改成钉死版本，并说明失败形式（无声退出）
- 新增「以后改代码怎么查」章节：问题类型 → 该看哪节，
  以及改代码前必须先读的两节（互斥规则、踩过的坑）
- 目录补上这一节，24 项锚点验证全部对上
"""
with io.open(r"G:\Strata\Chat\data\_msg.txt", "w", encoding="utf-8") as fh:
    fh.write(msg)
print("ok")