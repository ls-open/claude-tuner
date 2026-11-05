# 细节

README 只留「是什么、怎么装、怎么用」。这些是只在此刻成立的细节与边角情况，
改动频繁，所以单独放一页 —— 正文不塞。

## 🔍 跟版自检（抓包分析）

客户端一升级，注入的提示词与工具表都可能变。升级后按这三步自查一次即可：

```bash
python3 ~/.claude/skills/claude-tuner/scripts/sentinel.py capture on   # 打开抓包
#   去客户端发一条消息（探针请求不会被抓）
python3 ~/.claude/skills/claude-tuner/scripts/sentinel.py audit --last # 四面自检
python3 ~/.claude/skills/claude-tuner/scripts/sentinel.py capture off  # 收工关掉
```

结论全绿即表示这次升级已被现有规则完全吸收，无需动作；有命中会直接指出该补哪条规则。
想跟版对比，`sentinel.py compare <旧抓包> <新抓包>` 会列出工具定义与 System 提示词的具体差异。

抓的是**客户端发来的原始报文**，平时开关是关的；打开后归档到 `~/.claude-tuner/captures/`
（最多保留最近 20 个会话），用完记得关掉。

---

## 💻 终端 Claude Code 联动

**默认开启，这是一个有明显副作用的动作，先看清楚再装。**

安装会往 `~/.claude/settings.json` 的 `env` 里写 9 个变量：

| 变量 | 值 | 效果 |
| :--- | :--- | :--- |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:<端口>` | 终端 `claude` 的请求改走本地网关 |
| `ANTHROPIC_AUTH_TOKEN` | 你填的网关 Key | 终端用这把 Key 认证，**不再用官方订阅** |
| `ANTHROPIC_MODEL` 等 4 个 | 上游模型名（如 `deepseek-…[1m]`） | 终端选的 Opus/Sonnet/Haiku 全部换成上游模型 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 上游模型名 | 子代理同样如此 |
| `CLAUDE_CODE_EFFORT_LEVEL` | `max` | 思考档位被固定 |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `786432` | 自动压缩窗口被改写 |

也就是说：**装完之后，你在终端敲的 `claude` 不再走 Anthropic 订阅，而是走这台机器上的网关、
打到你在配置里填的那个上游。** 网关没起来或上游挂了，终端也用不了。

不想这样，两条路：

- 安装时加 `--no-cli`：一个变量都不写；
- 配置台取消勾选「终端 Claude Code 同样走本网关」：会把这 9 个键从 `settings.json` 里摘掉
  （卸载同理）。

留着的话，验证链路：

```bash
claude -p "只回复两个字：成功"
```

---

## 配置生效机制速查

配置台清晰标注了每一项配置的生效边界：

| 标记 | 写入位置 | 涵盖内容 |
| :--- | :--- | :--- |
| **保存后即生效** | 网关自身配置 | 上游地址、请求稳定化规则、工具精简规则、日志保留天数 |
| **保存后新会话生效** | 客户端设置 / 3P 配置 | 输入联想、自动压缩、高级压缩 / 思考开关、Co-Authored-By、终端联动 |
| **保存后需重启客户端** | 3P 配置 / 桌面偏好 | API Key、上游模型映射、1M 上下文窗口、缓存保留 1 小时、随附技能、定时任务 |

> 若因清除浏览器缓存需要带 Token 的免密登录链接，可在终端运行：
> ```bash
> python3 ~/.claude/skills/claude-tuner/scripts/webui.py --url
> ```

## 上游卡住 / 断流：怎么判读日志

`upstream_timeout`（默认 45 秒）是**空闲**读超时：socket 级、按每次读算的 ——
**连续 N 秒一个字节都没到**才抛 `read operation timed out`，不是单轮总时长。
上游只要一直在吐字节，一轮跑几分钟也不会被掐；反过来，长推理时上游真哑了 45 秒，也会被掐。

一轮的日志里看这四样：

| 字段 | 出现在 | 含义 |
| :--- | :--- | :--- |
| `headers_ms` | `upstream_ok` / `upstream_stream_error` / `upstream_truncated` | 发出请求到**响应头**到达 |
| `ttfb_ms` | 同上 | 发出请求到**第一个正文字节**（响应头不算） |
| `max_gap_ms` | 同上 | 相邻两块正文之间最长静默，**含触发超时的最后一次** |
| `stage` | 失败事件 | `awaiting_headers`＝连响应头都没拿到；`body`＝已经流出去了，哑在正文里 |

判读：

| 现象 | 结论 | 处置 |
| :--- | :--- | :--- |
| `upstream_error` + `stage=awaiting_headers`，`max_gap_ms` ≈ `upstream_timeout` | 上游在**开口前**就空等超时（排队 / 大上下文预填充） | 这不是本地的问题，重试或换时段；把 `max_gap_ms` 和 `upstream_ok` 的基线比，看是不是只在高峰出现 |
| `upstream_stream_error` + `stage=body`，`last_event` 是 `thinking_delta` / `content_block_delta` | **说到一半**哑了（上游卡住，或中间链路缓冲） | 看 `bytes_received` 判断已经吐了多少；`error` 是 `read operation timed out` 就说明是本地空闲超时掐的 |
| `upstream_truncated`（读到 EOF 却没有 `message_stop`） | 上游自己断的流，**不是**被超时掐的 | 与上面两类分开计，别混进「成功轮次」 |
| `upstream_ok` 里 `max_gap_ms` 就很大（十几秒） | 长静默是这类模型的常态，不是故障 | **没有成功轮次这条基线，长静默是异常还是常态永远判不出来** |

`stage=body` 的失败事件还带 `usage`（已生成部分的 token 与费用）——被掐断的那轮照样计费，
没有这条账上看不见。

## Windows

**实验性。** 代码在 Windows CI 上跑通（单测 + 安装演练），但还没有真机验证——安装过程、
客户端拉起网关、3P 配置目录这几件只有真机能确认。

遇到问题请把这行输出贴到 [issue](https://github.com/ls-open/claude-tuner/issues)：
`bootstrap.py doctor`（在下面「网关起不来时」里有完整命令）。

## 网关起不来时

请求本身走本地网关：网关挂了，你连这条消息都发不出去。所以记住这两条：

请求本身走本地网关：网关挂了，你连这条消息都发不出去。所以记住这两条：

```bash
python3 ~/.claude/skills/claude-tuner/scripts/manager.py ensure
python3 ~/.claude/skills/claude-tuner/scripts/bootstrap.py doctor
```

第一条把网关拉起来（Windows：`python "%USERPROFILE%\.claude\skills\claude-tuner\scripts\manager.py" ensure`），
第二条打印一段可直接贴进 issue 的诊断信息（版本、系统、Python、解释器、插件目录、hooks 命令、
3P 配置路径、网关 pid 与日志尾部）。**不含任何密钥**，也不依赖客户端。

## 反馈

**issue 与 PR 都欢迎。** 报障请附上 `doctor` 的输出，写清系统版本与客户端版本就够了。

提 PR 有两条约定：平台相关的改动只往 `scripts/platform_support.py` 一处放，业务代码里不再写
`if win32`；提交前跑 `python3 -m unittest discover -s tests -p "test_*.py"`，全绿再提。

> Linux：没有该桌面端，插件逻辑（网关 / 配置台）可用，配置走 XDG。

---

