#!/usr/bin/env python3
"""客户端注入的剥除边界，以及哨兵判据与之配对（`gateway.py` ↔ `sentinel.py`）。

- **剥除侧**：注入块只认落在文本开头的（夹在正文里的是引用）；裸 marker 的截断只对
  system 注入面开启，对话正文里的裸句原样保留。
- **自检侧**：成对断言「网关剥得掉 **且** 哨兵认得出」，防止两侧正则静默脱节。

取舍来自实测：只扫 system 面时，注入全落在 messages 面，结论是假保险。
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# 家目录隔离必须在 import gateway 之前（gateway 在 import 时就定下 BASE_DIR）
import _isolated_home  # noqa: E402,F401

import gateway  # noqa: E402
import sentinel  # noqa: E402

MARKER = "This is the git status at the start of the conversation."
GIT_STATUS_BLOCK = (
    "<system-reminder>\nAs you answer the user's questions, you can use the following "
    "context:\n# gitStatus\n" + MARKER + "\nOn branch main\nnothing to commit\n</system-reminder>"
)
CURRENT_DATE_BLOCK = "<system-reminder>\n# currentDate\nToday's date is 2026-10-02.\n</system-reminder>"


def _payload(session_id, subagent=False, model="claude-opus-5-5"):
    """构一份最小报文，形态对齐真实主会话请求（会话 id 走 metadata.user_id 这条真实路径）。"""
    p = {
        "model": model,
        "max_tokens": 4096,
        "tools": [{"name": "Bash"}],
        "system": [{"type": "text", "text": "x"}],
    }
    if session_id is not None:
        p["metadata"] = {"user_id": json.dumps({"session_id": session_id})}
    if subagent:
        p["system"] = [{"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true;"}]
        p["messages"] = [{"role": "user", "content": [{"type": "text", "text": "审一下"}]}]
    return p


class TestUnknownToolDetection(unittest.TestCase):
    """哨兵自检侧：只报类别表外的工具，判为保留的不再每次跟版重报。"""

    def _cfg(self, **over):
        c = dict(gateway.DEFAULT_TOOL_PRUNING_CONFIG)
        c.update(over)
        return c

    def test_判为保留的命名空间不再报警(self):
        """`mcp__ccd_view__*` 开关存在但默认关（判为保留）—— 不该出现在待判定里。"""
        names = ["mcp__ccd_view__show_pane", "mcp__brand_new__do_it"]
        self.assertEqual(sentinel.unknown_tools(names, self._cfg()), ["mcp__brand_new__do_it"])

    def test_被剥掉的工具不算待判定(self):
        names = ["mcp__ccd_sidebar__set_view"]
        self.assertEqual(sentinel.unknown_tools(names, self._cfg()), [])

    def test_开关关掉的类别也照样认帐(self):
        names = ["mcp__ccd_sidebar__set_view"]
        self.assertEqual(sentinel.unknown_tools(names, self._cfg(strip_sidebar_tools=False)), [])

    def test_原生工具没有开关也要报(self):
        """回归：以前只挑 `mcp__` 前缀，掉出开关范围的原生工具完全没人管 ——
        `TaskStop` / `ReportFindings` 就是这么漏掉的（现已各给一个开关，见下一条）。"""
        names = ["BrandNewNativeTool", "Bash"]
        self.assertEqual(sentinel.unknown_tools(names, self._cfg()), ["BrandNewNativeTool"])

    def test_底线名单不算没有开关(self):
        """`Bash` / `Read` 这些是**刻意**不给开关的，不是漏了。"""
        self.assertEqual(sentinel.unknown_tools(["Bash", "Read", "Edit", "Agent"], self._cfg()), [])

    def test_有开关的原生工具算已知(self):
        """含那对曾经掉队的：给了开关就算判过，哨兵不再重复报。"""
        self.assertEqual(sentinel.unknown_tools(
            ["WebSearch", "WebFetch", "NotebookEdit", "TaskStop", "ReportFindings"],
            self._cfg()), [])


class TestCompareCaptures(unittest.TestCase):
    """`compare` 的差异算法（生产同一形态：喂真实报文形态的两份 payload）。"""

    def _payload(self, tools, sys_blocks):
        return {"tools": [{"name": n, "description": n + " 的说明"} for n in tools],
                "system": [{"type": "text", "text": t} for t in sys_blocks]}

    def test_工具增删与定义改动都报得出来(self):
        old = self._payload(["Read", "Foo"], ["同一段"])
        new = self._payload(["Read", "Bar"], ["同一段"])
        new["tools"][0]["description"] = "Read 改了说明"
        d = sentinel._diff_captures(old, new)
        self.assertEqual(d["tools"]["added"], ["Bar"])
        self.assertEqual(d["tools"]["removed"], ["Foo"])
        self.assertEqual(d["tools"]["changed"], ["Read"])

    def test_一字未动时没有差异(self):
        p = self._payload(["Read"], ["同一段", "另一段"])
        d = sentinel._diff_captures(p, p)
        self.assertEqual(d["tools"], {"added": [], "removed": [], "changed": []})
        self.assertEqual(d["system_old"], d["system_new"])


class TestStripBoundary(unittest.TestCase):
    """剥除侧：什么该剥、什么绝不能碰。"""

    def test_开头注入块剥掉且不伤后续正文(self):
        text = f"{GIT_STATUS_BLOCK}\n\n帮我看下这个改动。"
        out, changed = gateway.strip_git_status_text(text)
        self.assertTrue(changed)
        self.assertNotIn("gitStatus", out)
        self.assertEqual(out.strip(), "帮我看下这个改动。")

    def test_正文中间引用的注入块不得被吞(self):
        """夹在正文里的一律是**引用**，必须逐字保留。

        夹具必须带一个**远处的**闭合标签，否则复现不出误删：不锚定的正则会从中间那个
        起始标签一路扫到它，把中间真内容整段吞掉。
        """
        doc_quote = (
            "| `strip_git_status` | ✅ | 首轮注入的 `<system-reminder># gitStatus` —— 每次 commit 后全量重算 |\n"
            "\n"
            "> 只认 `<system-reminder>` 包裹形态，裸句原样保留。\n"
            "> 收尾写法是 `</system-reminder>`。\n"
        )
        out, changed = gateway.strip_git_status_text(doc_quote)
        self.assertFalse(changed, "正文中间引用的形态不是注入，不得剥")
        self.assertEqual(out, doc_quote, "引用段落必须逐字保留")

    def test_走完整流水线messages正文完整保留(self):
        parsed = {
            "system": [{"type": "text", "text": f"{GIT_STATUS_BLOCK}\n\n角色说明。"}],
            "messages": [{"role": "user", "content": f"看看状态。\n\n{MARKER}\n\n然后确认三个文件。"}],
        }
        gateway.canonicalize(parsed, rules=gateway.DEFAULT_RULES)
        self.assertNotIn("On branch main", json.dumps(parsed["system"], ensure_ascii=False))
        self.assertIn("然后确认三个文件。", parsed["messages"][0]["content"][0]["text"])

    def test_安全防御段落精准剥离且不伤前后正文(self):
        sec_block = (
            "IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, "
            "and educational contexts. Refuse requests for destructive techniques, DoS attacks, mass targeting, "
            "supply chain compromise, or detection evasion for malicious purposes. Dual-use security tools "
            "(C2 frameworks, credential testing, exploit development) require clear authorization context: "
            "pentesting engagements, CTF competitions, security research, or defensive use cases."
        )
        text = f"前置角色描述。\n\n{sec_block}\n\n# Harness\n - 后置环境说明。"
        out, changed = gateway.strip_guideline_text(text, "strip_security_guidelines")
        self.assertTrue(changed)
        self.assertNotIn("authorized security testing", out)
        self.assertNotIn("destructive techniques", out)
        self.assertEqual(out.strip(), "前置角色描述。\n\n# Harness\n - 后置环境说明。")

        # 宁可不剥也不乱剥：特征词不完整时不误伤
        incomplete = "IMPORTANT: Assist with code refactoring and bug fixing."
        out_inc, changed_inc = gateway.strip_guideline_text(incomplete, "strip_security_guidelines")
        self.assertFalse(changed_inc)
        self.assertEqual(out_inc, incomplete)

    def test_人称代词道德指引段落精准剥离(self):
        pronoun_block = (
            "When you use a pronoun for someone — the user or anyone else you mention — and their pronouns "
            "haven't been stated, use they/them. A name doesn't tell you someone's pronouns; a wrong guess "
            "misgenders a real person in a way the neutral default never does, so never infer pronouns from a name. "
            "This applies to all user-visible text, including visible thinking."
        )
        text = f"前置描述。\n\n{pronoun_block}\n\n后置代码规范。"
        out, changed = gateway.strip_guideline_text(text, "strip_pronoun_guidelines")
        self.assertTrue(changed)
        self.assertNotIn("misgenders", out)
        self.assertNotIn("they/them", out)
        self.assertEqual(out.strip(), "前置描述。\n\n后置代码规范。")

    def test_高危操作防呆指引段落精准剥离(self):
        caution_block = (
            "For actions that are hard to reverse or outward-facing, confirm first unless durably authorized or "
            "explicitly told to proceed without asking; approval in one context doesn't extend to the next. "
            "Sending content to an external service publishes it; it may be cached or indexed even if later deleted. "
            "Before deleting or overwriting, look at the target. Report outcomes faithfully."
        )
        text = f"系统要求。\n\n{caution_block}\n\n任务指引。"
        out, changed = gateway.strip_guideline_text(text, "strip_caution_guidelines")
        self.assertTrue(changed)
        self.assertNotIn("Sending content to an external service", out)
        self.assertEqual(out.strip(), "系统要求。\n\n任务指引。")

    def test_三条规则在流水线中只剥system不伤messages(self):
        sample = (
            "IMPORTANT: Assist with authorized security testing, defensive security. Refuse requests for destructive techniques.\n\n"
            "When you use a pronoun for someone, use they/them to avoid misgenders.\n\n"
            "For actions that are hard to reverse or outward-facing, confirm first unless durably authorized. Sending content to an external service requires care.\n\n"
            "真系统指令。"
        )
        user_discuss = "用户说：请帮我分析 IMPORTANT: Assist with authorized security testing 这段话的安全性。"
        parsed = {
            "system": [{"type": "text", "text": sample}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_discuss}]}],
        }
        custom_rules = dict(gateway.DEFAULT_RULES)
        custom_rules.update({
            "strip_security_guidelines": True,
            "strip_pronoun_guidelines": True,
            "strip_caution_guidelines": True,
        })
        gateway.canonicalize(parsed, rules=custom_rules)
        # system 面被干净剥离
        sys_res = parsed["system"][0]["text"]
        self.assertNotIn("destructive techniques", sys_res)
        self.assertNotIn("misgenders", sys_res)
        self.assertNotIn("external service", sys_res)
        self.assertEqual(sys_res.strip(), "真系统指令。")
        # messages 正文讨论必须逐字完好保留
        self.assertEqual(parsed["messages"][0]["content"][0]["text"], user_discuss)

    def test_模型推销段由既有规则覆盖不必另立规则(self):
        """**回归**：曾单加过一条 `strip_model_environment`，实测是重复劳动。

        那段 `# Environment` 早被 `strip_model_declarations`（默认开）整块删掉了 ——
        单独调用它时「省 582 字节」看着像回事，放进流水线净省 **0**。删掉那条规则之后，
        用这条测试钉住「那段确实有人管」，免得谁看它还在又加一遍。
        """
        env_block = (
            "# Environment\n"
            "- The most recent Claude models are the Claude 5 family: Claude 5 Opus, Claude 5 Sonnet, and Claude 5 Haiku.\n"
            "- Claude Code is available as a CLI, desktop app, web app, and plugins for VS Code and JetBrains IDEs.\n"
            "- When building AI applications, default to the latest and most capable Claude models:"
        )
        parsed = {"system": [{"type": "text", "text": f"前置说明。\n\n{env_block}\n\n后置要求。"}]}
        gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        out = parsed["system"][0]["text"]
        self.assertNotIn("Claude 5 family", out)
        self.assertNotIn("When building AI applications", out)
        self.assertIn("后置要求。", out, "只该剥那段推销块，不该连累邻居")

    def test_终端弹窗那段刻意不剥(self):
        """**别再把它加回来** —— 它删的不是说教，是压先验的事实说明。

        `/permissions` `/config` `/doctor` `/hooks` 是 Claude Code 的公开命令，模型本来就
        认识；那段的作用是**禁止**模型让用户去跑它们（写法本身就是一句禁止 —— 只有模型
        会去做的事才需要禁止），并给出替代说法。删掉它省 370 字节，代价是用户问「怎么改
        权限」时被指到一条在当前会话跑不通的命令上。

        与「`<ci-monitor-event>` 那段不随 ccd_pr 剥」（见 test_tool_rules.py）同一类取舍。
        """
        self.assertFalse(hasattr(gateway, "strip_terminal_dialog_guidelines_text"))
        term_block = (
            "Terminal-dialog slash commands such as `/permissions`, `/config`, `/doctor`, and "
            "`/hooks` open an interactive terminal panel and are not available in this session — "
            "do not tell the user to run them here."
        )
        parsed = {"system": [{"type": "text", "text": f"系统要求。\n\n{term_block}\n\n后置工作流。"}]}
        gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        out = parsed["system"][0]["text"]
        self.assertIn("/permissions", out)
        self.assertIn("not available in this session", out)

    def test_配额标签精准剥离(self):
        text_with_tag = "系统指令。\n\n<total_tokens>15000000 tokens left</total_tokens>\n\n任务要求。"
        out, changed = gateway.strip_total_tokens_tag_text(text_with_tag)
        self.assertTrue(changed)
        self.assertNotIn("<total_tokens>", out)
        self.assertEqual(out.strip(), "系统指令。\n\n任务要求。")

        # 嵌在行内的标签亦能干净消除
        inline_tag = "系统指令。<total_tokens>15000000 tokens left</total_tokens> 任务要求。"
        out_inline, changed_inline = gateway.strip_total_tokens_tag_text(inline_tag)
        self.assertTrue(changed_inline)
        self.assertNotIn("<total_tokens>", out_inline)
        self.assertEqual(out_inline.strip(), "系统指令。任务要求。")

    def test_防抖规则在流水线中只剥system不伤messages(self):
        sample = (
            "# Environment\n"
            "- The most recent Claude models are the Claude 5 family: Claude 5 Opus.\n"
            "- When building AI applications, default to Claude.\n\n"
            "Terminal-dialog slash commands such as /permissions are not available in this session.\n\n"
            "<total_tokens>15000000 tokens left</total_tokens>\n\n"
            "核心系统提示词。"
        )
        user_discuss = (
            "用户讨论：# Environment 下的 Claude 5 family 怎么样？"
            "另外 /permissions 为什么 not available in this session？"
            "还有 <total_tokens>15000000 tokens left</total_tokens> 是什么？"
        )
        parsed = {
            "system": [{"type": "text", "text": sample}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_discuss}]}],
        }
        gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        sys_res = parsed["system"][0]["text"]
        # 推销块由 strip_model_declarations 剥、配额行由 strip_total_tokens_tag 剥
        self.assertNotIn("Claude 5 family", sys_res)
        self.assertNotIn("<total_tokens>", sys_res)
        # 终端命令那段**刻意留着**：删了模型会按先验把用户指到跑不通的命令上
        self.assertIn("/permissions", sys_res)
        self.assertIn("not available in this session", sys_res)

        # messages 对话中的用户发言必须逐字完好保留
        self.assertEqual(parsed["messages"][0]["content"][0]["text"], user_discuss)



class TestSentinelMatchesGateway(unittest.TestCase):
    """自检侧：哨兵必须认得出网关负责剥的形态，且只扫注入真正落地的地方。"""

    def scan(self, messages):
        """该函数给人看报告用、会打表；测试只要计数，故吞掉输出。

        返回 (注入级命中数, 存疑级命中数) —— 注入级计入结论，存疑级是落在用户正文里的
        裸形态（引用），不计结论。
        """
        with contextlib.redirect_stdout(io.StringIO()):
            return sentinel._audit_message_injections({"messages": messages})

    def test_两种注入形态都认得出(self):
        for label, block in (("gitStatus 注入块", GIT_STATUS_BLOCK),
                             ("currentDate 注入块", CURRENT_DATE_BLOCK)):
            self.assertTrue(any(m == label and __import__("re").search(p, block)
                                for m, p in sentinel.INJECTION_MARKERS.items()),
                            f"哨兵认不出 {label} → 它会对着未剥除的注入报「0 残留」")
            self.assertTrue(__import__("re").search(sentinel.VOLATILE_PATTERNS["git 状态"],
                                                    GIT_STATUS_BLOCK))

    def test_currentDate网关确实剥得掉(self):
        parsed = {"system": [{"type": "text", "text": CURRENT_DATE_BLOCK}]}
        gateway.canonicalize(parsed, rules=gateway.DEFAULT_RULES)
        self.assertNotIn("currentDate", str(parsed["system"]))

    def test_只扫用户文本块工具输出与助手输出不报(self):
        """同一个字符串既是注入形态、也是我们讨论它时的引用，能分的只有它落在哪种块里。"""
        msg = {"role": "user", "content": [{"type": "text", "text": GIT_STATUS_BLOCK}]}
        self.assertEqual(self.scan([msg]), (1, 0), "这是唯一真的该报的位置")
        self.assertEqual(self.scan([{"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": GIT_STATUS_BLOCK}]}]}]), (0, 0),
            "工具输出是引用所在，扫它必然假命中")
        self.assertEqual(self.scan([{"role": "assistant", "content": [
            {"type": "text", "text": GIT_STATUS_BLOCK}]}]), (0, 0))

    def test_用户正文里的裸形态降级为存疑不计结论(self):
        """注入形态落在 user 消息的裸正文里 —— 那是我们在讨论它，不是客户端在注入。

        判据是「落在哪种块里」（承 DECISIONS #18/#27）：注入面 = role:system 消息，
        或 user 消息里带 `<system-reminder>` 容器的块。裸形态降级成存疑，但**照样打印**，
        免得客户端真改了注入形态时静悄悄给一个 ✅。
        """
        bare = {"role": "user", "content": [{"type": "text", "text":
            "这个仓库里 <total_tokens>14962621 tokens left</total_tokens> 是什么标签？"}]}
        self.assertEqual(self.scan([bare]), (0, 1), "user 正文里的裸形态必须降级")
        # 同一个形态落进 role:system 消息（客户端真正的注入面）必须计入结论
        injected = {"role": "system", "content": [{"type": "text", "text":
            "<total_tokens>14962621 tokens left</total_tokens>"}]}
        self.assertEqual(self.scan([injected]), (1, 0), "role:system 消息里的是注入，必须计入结论")
        # user 消息里带容器的（gitStatus 提醒走这条）同样计入结论
        self.assertEqual(self.scan([{"role": "user", "content": [
            {"type": "text", "text": GIT_STATUS_BLOCK}]}]), (1, 0),
            "容器内的形态即便在 user 消息里也是注入")

    def test_清洗过的请求报零(self):
        parsed = {"system": [], "messages": [
            {"role": "user", "content": [{"type": "text", "text": GIT_STATUS_BLOCK}]},
            {"role": "user", "content": [{"type": "text", "text": "帮我看下这个改动。"}]},
        ]}
        gateway.canonicalize(parsed, rules=gateway.DEFAULT_RULES)
        self.assertEqual(self.scan(parsed["messages"]), (0, 0))

    def test_幽灵工具孤立段落被精准剔除且不伤其余内容(self):
        text = (
            "You are an interactive agent.\n\n"
            "To show the user this session's diff, a file at a line, or its terminal, PR, tasks or plan pane, use `mcp__ccd_view__show_pane` instead of describing it.\n\n"
            "After opening a PR, use the ccd_pr tools: call `get_status` and, if it does not report that PR, bind it with `bind_pr`; then read its CI and offer Auto-fix, and never schedule or poll CI checks yourself (CronCreate, ScheduleWakeup, /loop, Monitor, `gh` polling); never enable auto-merge unless the user asked.\n\n"
            "To read or change the user's Code tab preferences in this app (auto-archive, branch prefix, notifications, keep-awake, the Remote Control default, output style), use the ccd_settings tools instead of sending them to Settings; the user approves each change, and security settings stay theirs.\n\n"
            "When this session runs in a worktree the app made for it, bring its branch up to date with the base branch (for merge conflicts, or because you need its latest commits) by calling the ccd_host `sync_with_base_branch` tool instead of running `git merge` or `git pull` yourself: the app fetches and merges on the host, where the repository's sandbox-protected files can be written; resolve any conflicts it reports, then commit and push. In any other checkout the app does not merge for you; merge the base branch yourself there.\n\n"
            "The desktop app may send this session `<ci-monitor-event>` messages about a pull request it is watching."
        )
        # 这三段对应的工具都被剥掉时，三段一起走。
        # 配置显式给出，不读环境里那份 —— 否则用例的结果取决于跑测试的机器上恰好勾了什么。
        prune = dict(gateway.DEFAULT_TOOL_PRUNING_CONFIG)
        prune.update({"enabled": True, "strip_pr_ci_tools": True,
                      "strip_settings_tools": True, "strip_host_tools": True})
        out, changed = gateway.strip_ghost_tool_instructions(text, tp_cfg=prune)
        self.assertTrue(changed)
        self.assertNotIn("ccd_pr", out)
        self.assertNotIn("ccd_settings", out)
        self.assertNotIn("sync_with_base_branch", out)
        # ccd_view 和最后的 ci-monitor-event 必须完好保留
        self.assertIn("mcp__ccd_view__show_pane", out)
        self.assertIn("<ci-monitor-event>", out)

        # 反向联动守卫：若用户保留 ccd_pr 工具 (strip_pr_ci_tools=False)，说明必须自动保留，绝不脱节
        keep_pr_cfg = {"enabled": True, "strip_pr_ci_tools": False,
                       "strip_settings_tools": True, "strip_host_tools": True,
                       "strip_window_tools": True}
        out_keep_pr, _ = gateway.strip_ghost_tool_instructions(text, tp_cfg=keep_pr_cfg)
        self.assertIn("ccd_pr", out_keep_pr, "保留了 PR 工具时，说明必须同步保留")
        self.assertNotIn("ccd_settings", out_keep_pr)

        # 反向联动守卫：若用户保留设置/主机工具，对应的说明段落必须自动保留
        keep_session_cfg = {"enabled": True, "strip_pr_ci_tools": True,
                            "strip_settings_tools": False, "strip_host_tools": False,
                            "strip_window_tools": False}
        out_keep_session, _ = gateway.strip_ghost_tool_instructions(text, tp_cfg=keep_session_cfg)
        self.assertNotIn("ccd_pr", out_keep_session)
        self.assertIn("ccd_settings", out_keep_session, "保留了设置工具时，说明必须同步保留")
        self.assertIn("sync_with_base_branch", out_keep_session)

    def test_配置可直接注入不依赖重复读盘(self):
        """注入 tool_pruning_cfg 后，prepare_request 与 canonicalize 都不再重读 config.json。"""
        req = {"tools": [{"name": "mcp__ccd_pr__list"}, {"name": "Read"}], "messages": []}
        custom_tp = {"enabled": True, "strip_pr_ci_tools": True}
        system_text = "After opening a PR, use the ccd_pr tools: call get_status and never enable auto-merge unless the user asked.\n\nHello"
        parsed = {"system": [{"type": "text", "text": system_text}]}

        reads = []
        real_loader = gateway.load_tool_pruning_config
        gateway.load_tool_pruning_config = lambda: (reads.append(1), real_loader())[1]
        self.addCleanup(setattr, gateway, "load_tool_pruning_config", real_loader)

        changed = gateway.prepare_request(req, session_id="test-inject", tool_pruning_cfg=custom_tp)
        c_changed = gateway.canonicalize(parsed, rules={"strip_date": False}, tool_pruning_cfg=custom_tp)

        self.assertEqual(reads, [], "注入配置后不得回退去读 config.json")
        self.assertIn("prune_tools_1", changed)
        self.assertEqual([t["name"] for t in req["tools"]], ["Read"])
        self.assertIn("strip_ghost_tool_instructions", c_changed)
        self.assertEqual(parsed["system"][0]["text"].strip(), "Hello")

    def test_孤儿探测认识前缀类工具(self):
        """前缀类工具（`mcp__ccd_session*` 等）剥掉后仍被提示词提到，也要报得出来。

        ghost 名单以前只取配置里的**精确名**，类别表里的前缀一条都不展开：27 个
        ccd_session 工具明明剥了，日志里却一条 orphan 都没有 —— 探测器对它们全瞎。
        现在按本次请求实际带来的工具名补齐，口径与真实剥离一致。
        """
        seen = []
        real_log = gateway.log_event
        gateway.log_event = lambda event, req_id=None: seen.append(dict(event))
        self.addCleanup(setattr, gateway, "log_event", real_log)
        gateway._orphan_memo[0] = None
        self.addCleanup(lambda: gateway._orphan_memo.__setitem__(0, None))

        tp = {"enabled": True, "strip_session_start_tools": True}
        parsed = {"system": [{"type": "text", "text": (
            "Work that should not happen here goes to its own session — "
            "`mcp__ccd_session__start_session`.")}],
            "tools": [{"name": "mcp__ccd_session__start_session"},
                      {"name": "mcp__ccd_session__read_widget_context"},
                      {"name": "Read"}],
            "messages": []}
        # **生产同一条路**：名单在剥离之前算。prepare_request 会把这些工具从
        # parsed["tools"] 里删掉，删完再算就只剩保留的那些，探测等于没做。
        ghost = gateway.ghost_tool_names(parsed["tools"], tp)
        gateway.prepare_request(parsed, session_id=None, tool_pruning_cfg=tp)
        self.assertEqual([t["name"] for t in parsed["tools"]], ["Read"],
                         "剥离后工具确实不在了 —— 所以名单只能事先算")
        gateway.canonicalize(parsed, rules={}, tool_pruning_cfg=tp, stripped_names=ghost)

        hits = [e for e in seen if e.get("event") == "orphan_tool_mention"]
        self.assertTrue(hits, "前缀类工具被剥后仍被提示词提到，必须报出来")
        self.assertIn("mcp__ccd_session__start_session", hits[-1]["tools"])

    def test_缺键时工具与说明剥离仍原子一致(self):
        """两侧必须读同一个声明默认，缺键也不能出现「工具在、说明没了」。"""
        ghost_pr = (
            "After opening a PR, use the ccd_pr tools: call get_status and, "
            "if it does not report that PR, bind it with `bind_pr`; then read its CI and offer "
            "Auto-fix, and never schedule or poll CI checks yourself (CronCreate, ScheduleWakeup, "
            "/loop, Monitor, `gh` polling); never enable auto-merge unless the user asked.\n"
        )

        def sides(cfg):
            """返回 (工具是否被剥, 说明是否被剥) —— 两者必须同进同退。"""
            names = gateway.get_stripped_tool_names(dict(cfg))
            tool_out = gateway.should_strip_tool("mcp__ccd_pr__list", names, dict(cfg))
            _, ghost_out = gateway.strip_ghost_tool_instructions(ghost_pr, tp_cfg=dict(cfg))
            return tool_out, ghost_out

        # 两个键都缺：回落声明默认（True）—— 两侧都剥
        self.assertEqual(sides({"enabled": True}), (True, True))
        # 显式保留：两侧都不剥
        self.assertEqual(
            sides({"enabled": True, "strip_pr_ci_tools": False}), (False, False)
        )
        # 工具精简总开关关掉：工具不剥，说明也必须留着
        self.assertEqual(sides({"enabled": False}), (False, False))

    # 客户端真实形态：system 拆成三块 —— 计费头 / 身份 / 正文。配对段落只可能落在第三块。
    _SMALL_BLOCKS = [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.295.1fa; cc_entrypoint=claude-desktop-3p;"},
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude, "
                                 "running within the Claude Agent SDK."},
    ]
    _PAIR_PARAS = (
        "After opening a PR, use the ccd_pr tools: call `get_status` and, if it does not report "
        "that PR, bind it with `bind_pr`; then read its CI and offer Auto-fix, and never enable "
        "auto-merge unless the user asked.",
        "To read or change the user's Code tab preferences in this app (auto-archive, branch "
        "prefix, notifications, keep-awake, the Remote Control default, output style), use the "
        "ccd_settings tools instead of sending them to Settings; the user approves each change, "
        "and security settings stay theirs.",
        "When this session runs in a worktree the app made for it, bring its branch up to date "
        "with the base branch by calling the ccd_host `sync_with_base_branch` tool instead of "
        "running `git merge` or `git pull` yourself.",
        "To show sessions side by side (split view), the user can drag a session from the sidebar "
        "to where they want it in the window or right-click it and choose Open in → Split view, "
        "or, for a session you started, you can do it for them with `mcp__ccd_window__open_session_in`.",
        "Link a Terminal panel tab whenever you point the user at one, so they can open it with a "
        "click: [the tab's title](#terminal/<tab_id>), using the tab_id a terminal tool or an "
        "attached terminal snippet gave you, never one you made up.",
    )
    _ORPHANED_IDS = {"ccd_pr", "ccd_settings", "ccd_host_sync", "ccd_window_split", "ccd_terminal_link"}

    def _capture_missing(self):
        """替换掉上报函数，截获 canonicalize 每次上报的失配清单（绕开进程级去重 memo）。"""
        calls = []
        real = gateway.report_missing_pair_paragraphs
        gateway.report_missing_pair_paragraphs = lambda missed: calls.append(list(missed))
        self.addCleanup(setattr, gateway, "report_missing_pair_paragraphs", real)
        return calls

    def test_多块system不产生配对失配假报警(self):
        """「段落没找到」必须**跨全部 system 块**判。

        客户端把 system 拆成计费头 / 身份 / 正文三块，配对段落只可能在第三块。旧实现按块判：
        前两块本就不含这些段落，于是**每份请求**都被判成「失配」，网关每次启动都发一次假 WARN
        （见 DECISIONS「配对段落失配的按块假报警」）。这里钉住真形态下零误报。
        """
        big = "\n\n".join(self._PAIR_PARAS)
        parsed = {"system": self._SMALL_BLOCKS + [{"type": "text", "text": big}]}
        calls = self._capture_missing()
        gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES),
                             tool_pruning_cfg={"enabled": True})
        self.assertEqual(calls, [[]], "段落都在正文里，一处失配都不该报（按块判会误报这 5 项）")

    def test_段落真被改写时如实报失配(self):
        """反向守卫：锚点全失配时必须报出来 —— 别把假报警修成哑巴。"""
        parsed = {"system": self._SMALL_BLOCKS + [{"type": "text", "text": "正文里没有这些配对段落。"}]}
        calls = self._capture_missing()
        # 显式剥掉 host/terminal：_ORPHANED_IDS 是「这五对全剥」时的集合，
        # 别依赖它们各自的默认值（默认保留时它们本来就不该报）
        gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES),
                             tool_pruning_cfg={"enabled": True,
                                               "strip_host_tools": True, "strip_terminal_tools": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), self._ORPHANED_IDS)

    def test_splitview段落随会话管理开关原子联动(self):
        """整段可剥的形态：`ccd_window` 的说明段落随 strip_window_tools 同进同退。

        这段话是句子中段耦合的**反例** —— 它自成一段、首尾清楚，所以按约定整段摘除；
        而它引用的 `mcp__ccd_window__open_session_in` 正是被同一个开关剥掉的。
        """
        para = ("To show sessions side by side (split view), the user can drag a session from "
                "the sidebar to where they want it in the window or right-click it and choose "
                "Open in → Split view, or, for a session you started, you can do it for them "
                "with `mcp__ccd_window__open_session_in`.\n\n")
        keep = {"enabled": True, "strip_window_tools": False}
        cut = {"enabled": True, "strip_window_tools": True}
        self.assertIn("side by side", gateway.strip_ghost_tool_instructions(para, tp_cfg=keep)[0])
        self.assertNotIn("side by side", gateway.strip_ghost_tool_instructions(para, tp_cfg=cut)[0])
        # 与工具侧同进同退：开关关掉时工具也必须留下
        names = gateway.get_stripped_tool_names(keep)
        self.assertFalse(gateway.should_strip_tool("mcp__ccd_window__open_session_in", names, keep))

    def test_cmd_list_includes_global_capture(self):
        """sentinel.cmd_list 必须列出默认抓包文件 latest_raw_inbound.json。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            global_file = Path(tmp) / sentinel.GLOBAL_NAME
            global_file.write_text("{}", encoding="utf-8")
            old_dir = sentinel.CAPTURE_DIR
            sentinel.CAPTURE_DIR = tmp
            self.addCleanup(setattr, sentinel, "CAPTURE_DIR", old_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sentinel.cmd_list()
            out = buf.getvalue()
            self.assertIn(sentinel.GLOBAL_NAME, out)
            self.assertNotIn("抓包目录为空", out)

    def test_会话归档按会话分开且子代理不顶掉主会话(self):
        """同一会话的子代理与主会话必须各归一份 —— 它们共用 session_id，只能靠后缀分开。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sid = "06aed897-3c5c-4241-b61b-0f2fe7bd9643"
            main = gateway.save_session_capture(tmp, _payload(sid))
            sub = gateway.save_session_capture(tmp, _payload(sid, subagent=True))
            other = gateway.save_session_capture(tmp, _payload("40177a9f-d5f7-4e16-a966-43235421827e"))
            self.assertEqual(Path(main).name, "06aed897.json")
            self.assertEqual(Path(sub).name, "06aed897_subagent.json")
            self.assertEqual(Path(other).name, "40177a9f.json")
            # 两份都得在：子代理那份把主会话顶掉，就是这次要修的毛病换了个位置复发
            self.assertTrue(Path(main).exists() and Path(sub).exists())

    def test_无会话id不建归档(self):
        """取不到会话 id 的请求不建会话归档 —— 键会是空的，等于把不相干的请求混进一份。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gateway.save_session_capture(tmp, _payload(None)))
            self.assertFalse((Path(tmp) / gateway.CAPTURE_SESSIONS_DIR).exists())

    def test_会话归档有条数闸门(self):
        """每开一个对话就多一份，没有闸门时该目录只增不减。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(gateway.MAX_SESSION_CAPTURES + 10):
                gateway.save_session_capture(tmp, _payload(f"{i:08d}-0000-0000-0000-000000000000"))
            left = sorted((Path(tmp) / gateway.CAPTURE_SESSIONS_DIR).glob("*.json"))
            self.assertEqual(len(left), gateway.MAX_SESSION_CAPTURES)

    def test_session_resolution_接受三种写法(self):
        """sentinel --session 得吃下短键、完整 uuid、以及子代理后缀（与 --model 同一套约定）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = sentinel.CAPTURE_DIR
            sentinel.CAPTURE_DIR = tmp
            self.addCleanup(setattr, sentinel, "CAPTURE_DIR", old_dir)
            sid = "06aed897-3c5c-4241-b61b-0f2fe7bd9643"
            gateway.save_session_capture(tmp, _payload(sid))
            gateway.save_session_capture(tmp, _payload(sid, subagent=True))
            for written, given in (("06aed897.json", "06aed897"),
                                   ("06aed897.json", sid),
                                   ("06aed897_subagent.json", "06aed897_subagent"),
                                   ("06aed897_subagent.json", sid + "_subagent")):
                self.assertEqual(
                    sentinel._resolve_current(session=given), str(Path(tmp) / "sessions" / written),
                    "写法 %r 没落到 %s" % (given, written))
            self.assertIsNone(sentinel._resolve_current(session="deadbeef"))

    def test_Token计数块两边都认得出(self):
        """`<total_tokens>` 网关剥得掉、**且**哨兵 system 面认得出。

        它同时出现在顶层 system 与 role:system 消息里；当初这条只写进了 messages 面那张表，
        于是「剥得到、system 面验不到」—— 规则一失效就是个看不见的漏洞。
        """
        import copy
        text = "sys\n<total_tokens>15000000 tokens left</total_tokens>\n仍在这里"
        payload = {"model": "m", "max_tokens": 4096, "tools": [{"name": "Bash"}],
                   "system": [{"type": "text", "text": text}], "messages": [{"role": "user", "content": "hi"}]}
        d = copy.deepcopy(payload)
        gateway.prepare_request(d, "s")
        gateway.canonicalize(d)
        self.assertNotIn("<total_tokens>", d["system"][0]["text"], "网关没剥掉")
        self.assertTrue(
            any(re.search(p, text) for p in sentinel.VOLATILE_PATTERNS.values()),
            "网关剥得掉，但哨兵 system 面认不出 —— 规则失效时会静默漏报")

    def test_裸token标签是引用不算注入(self):
        """裸 `<total_tokens>` 是引用、不是注入 —— 实测一次审计里它假命中 6 处
        （压缩摘要 4 处、客户端文件附件 2 处，全都是我们在正文里提到它）。

        判据要求标签后紧跟计数：既把引用排除掉，又仍认得出形态变了的注入。
        落在 user 正文里的命中进一步降级成**存疑**（不计结论）；判据本身仍要认得出，
        否则客户端改了形态就成了静默漏报。
        """
        def count(text):
            return self.scan([{"role": "user", "content": [{"type": "text", "text": text}]}])

        self.assertEqual(count("`<total_tokens>` 这一项两边都要认得出。"), (0, 0),
                         "裸标签是引用，报它等于把真告警训练成噪音")
        self.assertEqual(count('"Token 计数块": r"<total_tokens>",'), (0, 0),
                         "连源码里这行判据本身也算引用")
        self.assertEqual(count("<total_tokens>14962621 tokens left</total_tokens>"), (0, 1),
                         "落在用户正文里 —— 降级为存疑，不计入结论")
        self.assertEqual(count("<total_tokens>1.5M tokens remaining</total_tokens>"), (0, 1),
                         "形态变了也要认得出来，否则收窄判据就成了静默漏报")
        # 判据层面仍必须认得这两种形态（降级只发生在「落在哪种块」这一层）
        for text in ("<total_tokens>14962621 tokens left</total_tokens>",
                     "<total_tokens>1.5M tokens remaining</total_tokens>"):
            self.assertTrue(
                any(re.search(p, text) for p in sentinel.INJECTION_MARKERS.values()),
                "判据认不出 %r —— 客户端真注入这种形态时会静默漏报" % text)
        # 同一个形态落进 role:system 消息（客户端真正的注入面）必须计入结论
        self.assertEqual(self.scan([{"role": "system", "content": [
            {"type": "text", "text": "<total_tokens>14962621 tokens left</total_tokens>"}]}]), (1, 0),
            "role:system 消息里的是注入，必须计入结论")

    def test_提示词工具搭配检查认得出被剥工具的引用(self):
        """被剥掉的工具若仍被正文引用（“说明在、工具没了”），审计必须报出来；作为更长标识符子串时不得误报。"""
        tools = [{"name": "Bash"}, {"name": "mcp__ccd_session__get_usage"}]
        # 两种写法各来一份**只含其一**的样例：正文常常省掉命名空间前缀，
        # 只查全名会漏，而两种情况写在同一段里则互相掩护（去掉短名查找也照样绿）。
        full_only = {"tools": tools, "system": [{"type": "text", "text": "用量可以问 `mcp__ccd_session__get_usage`。"}]}
        short_only = {"tools": tools, "system": [{"type": "text", "text": "用量可以问 get_usage（省略前缀的写法）。"}]}
        substr_word = {"tools": tools, "system": [{"type": "text", "text": "内部函数名 get_usage_stats 不应被当成短名命中。"}]}
        ok = {"tools": tools, "system": [{"type": "text", "text": "这里一个字都没提那个工具。"}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # 返回 (孤句引用, 配对失配条数)；这里只看前者。
            got = [sentinel._audit_prompt_tool_pairing(p, p, {})[0] for p in (full_only, short_only)]
            got_sub = sentinel._audit_prompt_tool_pairing(substr_word, substr_word, {})[0]
            got_ok = sentinel._audit_prompt_tool_pairing(ok, ok, {})[0]
        for hits in got:
            self.assertEqual([n for n, _ in hits], ["mcp__ccd_session__get_usage"])
        self.assertEqual(got_sub, [], "短名落在更长标识符内部时不得误报")
        self.assertEqual(got_ok, [])

    def test_role_system消息注入在messages面可检出(self):
        """客户端注入走 messages 里的 role:system 消息时，③ messages 面必须能检出未剥除的宣称与计数。"""
        raw_sys_msg = {
            "role": "system",
            "content": (
                "# Environment\n - Primary working directory: /Users/test\n\n"
                "You are powered by the model named Opus 5.5.\n\n"
                "<total_tokens>15000000 tokens left</total_tokens>\n\n"
                "Today's date is 2026-10-07."
            ),
        }
        self.assertEqual(self.scan([raw_sys_msg]), (3, 0),
                         "role:system 消息里的模型宣称/token计数/日期必须被检出")
        payload = {"system": [], "messages": [dict(raw_sys_msg)]}
        gateway.canonicalize(payload, rules=gateway.DEFAULT_RULES)
        self.assertEqual(self.scan(payload["messages"]), (0, 0), "经网关清洗后应报 0 残留")

    def test_classify_kind_handles_whitespace_and_str_system(self):
        """classify_kind 与 canonicalize 一致支持空白前缀及字符串形态的计费头。"""
        self.assertEqual(
            gateway.classify_kind({"system": [{"type": "text", "text": "  x-anthropic-billing-header: cc_is_subagent=true"}]}),
            "subagent",
        )
        self.assertEqual(
            gateway.classify_kind({"system": "x-anthropic-billing-header: cc_is_subagent=true"}),
            "subagent",
        )
        # CoWork 主会话
        self.assertEqual(
            gateway.classify_kind({"system": "x-anthropic-billing-header: cc_entrypoint=local-agent"}),
            "cowork",
        )
        # CoWork 子代理 (Header 同时含两者)
        self.assertEqual(
            gateway.classify_kind({"system": "x-anthropic-billing-header: cc_entrypoint=local-agent; cc_is_subagent=true"}),
            "cowork_subagent",
        )
        # CoWork 子代理 (Header 缺失但 tools 含 mcp__cowork__ 且系统提示词含 cc_is_subagent=true)
        self.assertEqual(
            gateway.classify_kind({
                "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true"}],
                "tools": [{"name": "mcp__cowork__create_artifact"}]
            }),
            "cowork_subagent",
        )
        # 验证 session_capture_key
        payload_cw_sub = {
            "metadata": {"session_id": "12345678-abcd-ef01-2345-6789abcdef01"},
            "system": "x-anthropic-billing-header: cc_entrypoint=local-agent; cc_is_subagent=true"
        }
        self.assertEqual(gateway.session_capture_key(payload_cw_sub), "12345678_cowork_subagent")

class TestRuleIsolation(unittest.TestCase):
    """一条规则炸掉，不能连累这一轮的其他规则。

    反例是曾经的实现：整个 canonicalize 包在一个 try 里，任何一条规则抛异常就把这一轮
    全部规则一起作废 —— 症状是「缓存突然掉一大截」，日志只有一行 stabilize_error。
    """

    def test_failing_rule_does_not_disable_the_others(self):
        payload = {"system": [{"type": "text",
                               "text": CURRENT_DATE_BLOCK + "\nYou are powered by the model named X."}]}
        original = gateway.strip_model_declaration_text
        gateway.strip_model_declaration_text = lambda text: 1 / 0
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                changed = gateway.canonicalize(
                    payload, rules={"strip_date": True, "strip_model_declarations": True})
        finally:
            gateway.strip_model_declaration_text = original

        self.assertIn("strip_date", changed, "另一条规则必须照常生效")
        self.assertNotIn("currentDate", payload["system"][0]["text"], "日期规则必须真的剥掉了")
        log = buf.getvalue()
        self.assertIn("rule_failed", log, "坏掉的规则必须留下可定位的一条日志")
        self.assertIn("strip_model_declarations", log)


class TestGuidelineTable(unittest.TestCase):
    """三条准则住在同一张表里，行为由一个函数统一执行。

    加一类新准则只需往 GUIDELINE_PARAGRAPHS 加一行 —— 这条用例保证「加了行就真的能剥」，
    不会出现「登记了但锚点/关键词写错、于是永远不命中」的静默失效。
    """

    def test_every_row_strips_its_own_paragraph(self):
        for name, spec in gateway.GUIDELINE_PARAGRAPHS.items():
            with self.subTest(rule=name):
                para = "%s %s %s" % (spec["anchor"], spec["requires"][0], spec["any_of"][0])
                text = "正文第一段。\n\n" + para + "\n\n正文最后一段。"
                out, changed = gateway.strip_guideline_text(text, name)
                self.assertTrue(changed, "表里这一行的锚点或关键词永远命不中")
                self.assertNotIn(para, out)
                self.assertIn("正文第一段。", out, "只该剥命中的那段")
                self.assertIn("正文最后一段。", out)

    def test_rows_are_independent(self):
        """别的准则的段落不该被这一条误剥。"""
        sec = gateway.GUIDELINE_PARAGRAPHS["strip_security_guidelines"]
        para = "%s %s %s" % (sec["anchor"], sec["requires"][0], sec["any_of"][0])
        out, changed = gateway.strip_guideline_text(para, "strip_pronoun_guidelines")
        self.assertFalse(changed)
        self.assertIn(para, out)


class TestSentinelClientSurface(unittest.TestCase):
    """compare 跨客户端面必须告警：面差异不是版本差异。"""

    def _capture(self, tmp, name, entrypoint, tools, system_text="同一段"):
        p = Path(tmp) / name
        p.write_text(json.dumps({
            "system": [{"type": "text", "text":
                        "x-anthropic-billing-header: cc_version=2.1.299.aaa; cc_entrypoint=%s;"
                        % entrypoint}],
            "tools": [{"name": t} for t in tools],
            "messages": [],
        }, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def _compare(self, a, b, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sentinel.cmd_compare(a, b, **kw)
        return buf.getvalue()

    def test_跨面比较必须告警(self):
        """终端面与桌面面的工具集本就不一样 —— 不告警就会把面差异读成版本差异。"""
        with tempfile.TemporaryDirectory() as tmp:
            a = self._capture(tmp, "old.json", "local-agent", ["Read"])
            b = self._capture(tmp, "new.json", "claude-desktop-3p", ["Read", "Bash"])
            out = self._compare(a, b)
            self.assertIn("客户端面不同", out)
            self.assertIn("local-agent", out)
            self.assertIn("claude-desktop-3p", out)

    def test_承认跨面后告警降级(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._capture(tmp, "old.json", "local-agent", ["Read"])
            b = self._capture(tmp, "new.json", "claude-desktop-3p", ["Read"])
            out = self._compare(a, b, allow_cross_surface=True)
            self.assertIn("--allow-cross-surface", out)
            self.assertNotIn("⚠️", out, "承认跨面后不该再打告警")

    def test_同面比较不告警(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._capture(tmp, "old.json", "claude-desktop-3p", ["Read"])
            b = self._capture(tmp, "new.json", "claude-desktop-3p", ["Read"])
            self.assertNotIn("客户端面不同", self._compare(a, b))

    def test_入口面取自计费头(self):
        payload = {"system": [{"type": "text", "text":
                               "x-anthropic-billing-header: cc_version=2.1.299.bcc; cc_entrypoint=claude-desktop-3p;"}]}
        self.assertEqual(sentinel.extract_client_entrypoint(payload), "claude-desktop-3p")
        self.assertEqual(sentinel.extract_client_entrypoint({"system": []}), "(未标注)")

    def test_工具定义差异逐行打出来(self):
        """只报一个工具名等于没答「客户端动了什么」。"""
        with tempfile.TemporaryDirectory() as tmp:
            a = self._capture(tmp, "old.json", "claude-desktop-3p", ["Read"])
            b = self._capture(tmp, "new.json", "claude-desktop-3p", ["Read"])
            old = json.loads(Path(a).read_text(encoding="utf-8"))
            new = json.loads(Path(b).read_text(encoding="utf-8"))
            old["tools"][0]["description"] = "旧说明"
            new["tools"][0]["description"] = "新说明"
            Path(a).write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            Path(b).write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
            out = self._compare(a, b)
            self.assertIn("定义有变的工具", out)
            self.assertIn("旧说明", out)
            self.assertIn("新说明", out)


class TestSentinelCaptureSwitch(unittest.TestCase):
    """抓包开关走 CLI：手改 JSON 是 SOP 第一步最容易写坏的一步。"""

    def test_开关写配置且保留权限位(self):
        path = Path(sentinel.BASE_DIR) / "config.json"
        path.write_text(json.dumps({"capture_raw_inbound": False, "rules": {}}), encoding="utf-8")
        os.chmod(path, 0o600)
        with contextlib.redirect_stdout(io.StringIO()):
            sentinel.cmd_capture("on")
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["capture_raw_inbound"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, "写配置必须保留权限位")
        with contextlib.redirect_stdout(io.StringIO()):
            sentinel.cmd_capture("off")
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["capture_raw_inbound"])
        # 别的键不能被顺手抹掉
        self.assertIn("rules", json.loads(path.read_text(encoding="utf-8")))

    def test_status只报状态不改文件(self):
        path = Path(sentinel.BASE_DIR) / "config.json"
        path.write_text(json.dumps({"capture_raw_inbound": True}), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sentinel.cmd_capture("status")
        self.assertIn("开着", buf.getvalue())
        self.assertEqual(path.read_text(encoding="utf-8"), before)


class TestSentinelLastCapture(unittest.TestCase):
    """`--last` 取最近改动的会话归档：多对话并存时省掉认会话键这一步。"""

    def test_取mtime最新的会话归档(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = sentinel.CAPTURE_DIR
            sentinel.CAPTURE_DIR = tmp
            self.addCleanup(setattr, sentinel, "CAPTURE_DIR", old_dir)
            sessions = Path(tmp) / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "aaaa1111.json").write_text("{}", encoding="utf-8")
            (sessions / "bbbb2222.json").write_text("{}", encoding="utf-8")
            os.utime(sessions / "aaaa1111.json", (1000, 1000))
            os.utime(sessions / "bbbb2222.json", (2000, 2000))
            self.assertEqual(sentinel._resolve_current(last=True),
                             str(sessions / "bbbb2222.json"))
            # 没有会话归档时回落到全局快照
            (Path(tmp) / sentinel.GLOBAL_NAME).write_text("{}", encoding="utf-8")
            os.remove(sessions / "bbbb2222.json")
            os.remove(sessions / "aaaa1111.json")
            self.assertEqual(sentinel._resolve_current(last=True),
                             str(Path(tmp) / sentinel.GLOBAL_NAME))


if __name__ == "__main__":
    unittest.main()


class TestEveryRuleSwitch(unittest.TestCase):
    """每条清洗开关都得真的能剥：开着要动它、关着要原样留住。

    只钉「配置里有这个键、页面有这只勾」是不够的 —— 开关真正的功能是**对报文有效**。
    客户端的注入形态一变（措辞改写、容器改名、段落重排），规则就会静默失配：开关照旧
    在页面上亮着，报文却一个字节都没变，日志里也没有任何一行会报出来。这条用例把
    「开 → 剥掉」与「关 → 一个字节不动」两个方向都钉住。

    样本一律照抓包原文形态写，并且准则那三条直接从 `GUIDELINE_PARAGRAPHS` 的锚点/特征词
    拼出来 —— 表里加一行，这里自动跟着测新的一条。
    """

    def _samples(self):
        samples = {
            "strip_billing_header":
                "x-anthropic-billing-header: cc_version=9.9.9; cc_entrypoint=x;",
            "strip_date": CURRENT_DATE_BLOCK,
            "strip_git_status": GIT_STATUS_BLOCK,
            "strip_model_declarations":
                "You are powered by the model named Claude X. The exact model ID is x.",
            "strip_total_tokens_tag": "<total_tokens>64000 tokens left</total_tokens>",
        }
        for name, spec in gateway.GUIDELINE_PARAGRAPHS.items():
            samples[name] = "%s %s %s" % (spec["anchor"], spec["requires"][0], spec["any_of"][0])
        return samples

    def _system_text(self, text, only_rule=None):
        """只开 `only_rule` 这一条（其余全关）跑一遍 canonicalize，返回剩下的 system 文本。"""
        rules = {k: False for k in gateway.DEFAULT_RULES}
        if only_rule:
            rules[only_rule] = True
        parsed = {"system": [{"type": "text", "text": text}],
                  "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
        gateway.canonicalize(parsed, rules=rules)
        return "\n".join(b.get("text", "") for b in parsed.get("system") or []
                         if isinstance(b, dict))

    def test_每条规则开了就剥_关了必留(self):
        for rule, text in self._samples().items():
            with self.subTest(rule=rule):
                self.assertIn(text, self._system_text(text),
                              "全关时这条规则不该动任何字节")
                self.assertNotIn(text, self._system_text(text, rule),
                                 "这条开关开着却没剥掉它 —— 锚点或关键词已与客户端脱节")
