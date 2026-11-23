import io
msg = """对话存档不进仓库，改成教用户自己导出

试过导出，结论是：放仓库里不合适。

导出的东西
- 128 轮、279 条用户消息、1841 条助手回复、2900 次工具调用
- 1.1MB，3.2 万行

不放进仓库的原因
1. 含本机路径和端口（扫出 62 处 127.0.0.1:xxxx，虽然多数是项目自己的端口）
2. 含一个已吊销的 GitHub token（扫出来时吓一跳，确认已失效才继续）
3. 对排查问题没帮助 —— 同一个坑试了四五种方法，噪音远多于结论
4. 1MB 的 markdown 会让 clone 变慢，而 99% 的人用不到

所以 README 里改成写清导出方法：
~/.dsh/sessions/<项目>/<会话ID>/session.v4.jsonl.zstd
zstd 分帧压缩，用 zstandard 逐帧解压后是 JSONL。

存档本身移到仓库外：G:\\Strata\\对话存档\\2026-11-23-v1.3-开发对话.md
带主题索引（8 个阶段）、思考过程折叠、日期时间戳。

如果以后真需要追溯「当初为什么这么设计」，读那个文件；
日常查问题还是用 README 的「实现上踩过的坑」，那是提炼过的。
"""
with io.open(r"G:\Strata\Chat\data\_msg.txt", "w", encoding="utf-8") as fh:
    fh.write(msg)
print("ok")