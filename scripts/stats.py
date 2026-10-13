#!/usr/bin/env python3
"""claude-tuner 统计面板：以官方 .jsonl 底稿为唯一真理源，零中间状态文件展示当前会话核心指标。"""
import datetime
import glob
import json
import os
import sys
import time

sys.dont_write_bytecode = True  # 不在插件运行目录留 __pycache__（代码以仓库为准）

BASE = os.environ.get("CLAUDE_TUNER_HOME", os.path.expanduser("~/.claude-tuner"))

# 「重算」判定的回落门槛（回落不足 DROP_MIN_DELTA 视为记录抖动，不构成重算）
DROP_MIN_DELTA = 1000


def parse_ts(ts):
    """把底稿时间戳解析成带本地时区的 datetime；不可用时返回 None。"""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def fmt_num(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return "%.0f" % n


def hit_rate_label(pct):
    """缓存命中率的直观描述。"""
    if pct >= 90:
        return "优秀"
    if pct >= 70:
        return "良好"
    if pct >= 40:
        return "一般"
    if pct > 0:
        return "偏低"
    return "无缓存"


def find_project_dir_for_cwd(cwd=None):
    """定位 CWD 所属的 Claude 项目目录（slug 直配 ➔ 大小写不敏感扫描 ➔ 路径段匹配兜底）。"""
    try:
        if not cwd:
            cwd = os.getcwd()
        cwd = os.path.abspath(cwd)
        projects_dir = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
        if not os.path.isdir(projects_dir):
            return None

        # 1. 按 Claude 的 slug 规则拼目录名：非 ASCII（如中文路径）一律折成 -
        slug = "".join(c if (c.isascii() and (c.isalnum() or c in ("-", "_"))) else "-" for c in cwd)
        exact = os.path.join(projects_dir, slug)
        if os.path.isdir(exact):
            return exact

        cand_dirs = glob.glob(os.path.join(projects_dir, "*"))

        # 2. 大小写不敏感的同名扫描
        for cand in cand_dirs:
            if os.path.basename(cand).lower() == slug.lower():
                return cand

        # 3. 中文等多字节段在 slug 里退化成连续 -，改用 ASCII 段序列匹配兜底
        parts = [p.lower() for p in cwd.replace("\\", "/").split("/")
                 if p and p.isascii() and len(p) > 1]
        if len(parts) >= 2:
            best, best_score = None, 0
            for cand in cand_dirs:
                name = os.path.basename(cand).lower()
                matched = sum(1 for p in parts if p in name)
                if matched >= len(parts) - 1 and matched > best_score:
                    best, best_score = cand, matched
            if best:
                return best
    except Exception:
        pass
    return None


def find_target_transcript(sid=None):
    """定位目标会话的官方 .jsonl 文件。"""
    projects_dir = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))

    # 1. 用户显式指定 session_id
    if sid:
        matches = glob.glob(os.path.join(projects_dir, "*", f"{sid}*.jsonl"))
        if matches:
            return matches[0]

    # 2. 优先在当前工作区 (CWD) 所属项目中查找最新 .jsonl
    try:
        pdir = find_project_dir_for_cwd()
        if pdir and os.path.isdir(pdir):
            jsonls = glob.glob(os.path.join(pdir, "*.jsonl"))
            if jsonls:
                return max(jsonls, key=os.path.getmtime)
    except Exception:
        pass

    # 3. 全局查找最近 24 小时内活跃的最新 .jsonl
    try:
        all_jsonls = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
        if all_jsonls:
            newest = max(all_jsonls, key=os.path.getmtime)
            if time.time() - os.path.getmtime(newest) < 86400:
                return newest
    except Exception:
        pass

    return None


def parse_session_transcript(filepath):
    """直接解析官方 .jsonl 底稿，提取会话的全部核心指标。"""
    sid = os.path.basename(filepath)[:-6]
    main_in = 0
    main_cr = 0
    main_upstream = 0
    recent_turns = []
    drops = []
    compact_count = 0
    # 同一 API 响应按 content block 拆成多行且重复携带相同 usage，须按 message.id 去重
    seen_response_ids = set()
    compacted = False
    agent_invs = 0
    first_title = ""
    app_title = ""
    first_date = ""
    last_cr = 0

    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                if not first_date:
                    dt = parse_ts(d.get("timestamp"))
                    if dt is not None:
                        first_date = dt.strftime("%Y-%m-%d")

                if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    compact_count += 1
                    compacted = True

                # 应用自己生成的会话名（取首行、超 32 字符截断加 ...），优先于首条消息截断
                if d.get("type") == "custom-title" and d.get("customTitle"):
                    raw_title = str(d["customTitle"]).split("\n")[0].strip()
                    if raw_title:
                        app_title = raw_title[:32] + ("..." if len(raw_title) > 32 else "")

                msg = d.get("message") or {}
                role = msg.get("role") or d.get("type")
                c = msg.get("content") or d.get("content")

                if not first_title and role == "user":
                    if isinstance(c, list):
                        txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                    else:
                        txt = str(c or "")
                    if txt and "<command-name>" not in txt and "<system-reminder>" not in txt:
                        first_line = txt.split("\n")[0].strip()
                        if first_line:
                            first_title = first_line[:24] + ("..." if len(first_line) > 24 else "")

                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                            agent_invs += 1

                usage = msg.get("usage")
                if usage and isinstance(usage, dict):
                    # 客户端本地合成的消息（上下文提示、打断占位等）usage 恒为 0，且没有发生过
                    # API 调用；拿它去比上一轮会凭空造出一次「重算」，必须先排除。
                    if msg.get("model") == "<synthetic>":
                        continue
                    resp_id = msg.get("id") or d.get("uuid")
                    if resp_id in seen_response_ids:
                        continue                      # 同一响应的第二个及以后的块
                    seen_response_ids.add(resp_id)
                    inp = usage.get("input_tokens") or 0
                    cr = usage.get("cache_read_input_tokens") or 0
                    if not inp and not cr:
                        continue
                    main_upstream += 1
                    main_in += inp
                    main_cr += cr
                    if cr > 0:
                        last_cr = cr
                    prev_turn = recent_turns[-1] if recent_turns else None
                    # 上下文压缩后的首轮 cache_read 自然回落已计入 compact_count，不重复计为偶发重算
                    if (prev_turn is not None and not compacted
                            and prev_turn["cr"] - cr > DROP_MIN_DELTA):
                        drops.append({"delta": prev_turn["cr"] - cr})
                    compacted = False
                    recent_turns.append({"in": inp, "cr": cr})
    except Exception:
        pass

    return {
        "sid": sid,
        "title": app_title or first_title,
        "first_date": first_date,
        "main_in": main_in,
        "main_cr": main_cr,
        "main_upstream": main_upstream,
        "last_cr": last_cr,
        "recent_turns": recent_turns,
        "drops": drops,
        "compact_count": compact_count,
        "agent_invs": agent_invs,
    }


def get_session_hydration_details(sid):
    """扫描本地网关日志，按 session_id 过滤并统计本会话的 Thinking 补全次数。"""
    thinking_hydrations = 0
    try:
        log_files = glob.glob(os.path.join(BASE, "logs", "gateway.log*"))
        for lf in log_files:
            with open(lf, "r", errors="ignore") as fh:
                for line in fh:
                    if "canonicalized" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("session_id") != sid:
                        continue
                    for c in obj.get("changes", []):
                        if c.startswith("restore_thinking_"):
                            thinking_hydrations += 1
    except Exception:
        pass
    return {"thinking_hydrations": thinking_hydrations}


def main():
    target_sid = None
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        target_sid = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target_sid = sys.argv[1]

    tpath = find_target_transcript(target_sid)
    if not tpath or not os.path.exists(tpath):
        print("未检测到当前会话（当前工作区尚无活跃对话底稿）")
        return

    s = parse_session_transcript(tpath)
    sid = s["sid"]

    # 核心缓存命中率计算
    main_tot = s["main_in"] + s["main_cr"]
    main_pct = (s["main_cr"] / main_tot * 100) if main_tot > 0 else 0

    recent_5 = s["recent_turns"][-5:] if len(s["recent_turns"]) >= 5 else s["recent_turns"]
    rec5_in = sum(r["in"] + r["cr"] for r in recent_5)
    rec5_cr = sum(r["cr"] for r in recent_5)
    rec5_pct = (rec5_cr / rec5_in * 100) if rec5_in > 0 else 0

    # 趋势描述
    if rec5_pct > main_pct + 5:
        main_trend = "[上升]"
    elif rec5_pct < main_pct - 5:
        main_trend = "[下降]"
    else:
        main_trend = "[持平]"

    # ── 标题 ──
    print("======================================================")
    title = s["title"]
    if title:
        print(" 会话: %s · %s" % (sid[:12] + "...", title))
    else:
        print(" 会话: %s" % (sid[:20] + "..." if len(sid) > 20 else sid))
    print("------------------------------------------------------")

    # ── 核心缓存指标 ──
    if main_pct > 0:
        label = hit_rate_label(main_pct)
        if rec5_in > 0:
            print(" 缓存命中: %.0f%% (%s) · 近5轮: %.0f%% %s"
                  % (main_pct, label, rec5_pct, main_trend))
        else:
            print(" 缓存命中: %.0f%% (%s)" % (main_pct, label))
    else:
        print(" 缓存命中: 暂无数据")

    # ── 会话规模与并发 ──
    main_ctx = s["last_cr"] or (s["main_in"] if s["main_upstream"] == 1 else 0)
    agent_invs = s["agent_invs"]
    comp_count = s["compact_count"]

    scale_parts = ["上下文: %s" % fmt_num(main_ctx), "主对话: %d 次" % s["main_upstream"]]
    if agent_invs > 0:
        scale_parts.append("子代理: %d 个任务" % agent_invs)
    print(" " + " · ".join(scale_parts))

    # ── 生命周期与运行状态 ──
    h_details = get_session_hydration_details(sid)
    state_parts = []
    if comp_count > 0:
        state_parts.append("压缩 %d 次" % comp_count)
    if h_details["thinking_hydrations"] > 0:
        state_parts.append("已自动恢复思维链")

    drops = s["drops"]
    if drops:
        extra = " · " + " · ".join(state_parts) if state_parts else ""
        print(" 状态: [正常]（偶发重算 %d 次: 最近回落 %s tokens%s）"
              % (len(drops), fmt_num(drops[-1].get("delta")), extra))
    else:
        extra = " · " + " · ".join(state_parts) if state_parts else " · 零掉落"
        print(" 状态: [正常]（极速命中%s）" % extra)

    print("======================================================")


if __name__ == "__main__":
    main()
