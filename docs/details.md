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

## Windows

**Windows 还没有真机验证。** 核心逻辑是纯 Python，`platform_support.py` 收口了全部系统差异，
CI 在真实的 Windows 上跑全部单测与安装演练；但我们手上没有 Windows 机器，所以**安装过程、
客户端拉起网关、3P 配置目录这几件只有真机才能确认的事，都没有验过**。请按实验性使用。

装完如果不对，把下面的 `doctor` 输出贴到 [issue](https://github.com/ls-open/claude-tuner/issues)
里——有这一段才可能定位问题。

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

