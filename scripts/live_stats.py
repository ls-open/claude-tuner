#!/usr/bin/env python3
"""用量统计：从网关日志里读**上游自己报回来的**真实用量，算命中率与 token 量。

这个文件存在的理由
------------------
别的地方我们只能给「估算」「按体积算」—— 那都是我们的推算。而网关日志里躺着
上游每次返回的 `usage`：哪些 token 命中缓存、哪些没有，是**上游自己数的**。
把这份数字摆到页面上，用户看到的是事实，不是我们的宣传。

因此本模块只做一件事：读日志、做加法。**不推算、不补全、缺就说缺**。
没跑过请求时如实报空，而不是拿一个好看的默认值充数。

用法：
    from live_stats import read_recent_usage
    read_recent_usage()   # -> dict
"""
import json
import os
import sys

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 历史日志也一起读：日志按自然日滚动归档，活跃日志连同各归档文件一起汇总，
# 确保统计区间覆盖配置的全部保留天数（默认 7 天）。
TAIL_BYTES = 6 * 1024 * 1024      # 单份最多读这么多
MAX_EVENTS = 20000                # 单次汇总最多统计的事件行数上限（防异常大的日志）

# 按「文件 + mtime + 大小」缓存汇总结果。读十兆日志要一两百毫秒，
# 而页面每次打开都会拉这个接口 —— 缓存让重复打开瞬时完成。
_usage_cache = (None, None)


def log_path(home=None):
    base = home or os.environ.get("CLAUDE_TUNER_HOME") or os.path.expanduser("~/.claude-tuner")
    return os.path.join(base, "logs", "gateway.log")


def log_paths(home=None):
    """按时间从旧到新列出全部网关日志：各归档日文件 + 当前活跃的那一份。

    三条必须的取舍：
    - **必须读归档**：只读当前那份，统计区间就只是「最近一份日志」，而这份从什么时候开始
      页面上看不出来 —— 等于给了一个没有截止日期的数字。
    - **排除 `gateway.out.log`**：它是 manager 把 stdout 重定向出来的副本，同一行事件在里面
      有第二份，读进来就双倍计数。
    - **按真实路径去重**：`gateway.log` 是指向当天日文件的软链，两者是同一份，只能算一次。
    """
    base = log_path(home)
    logs_dir = os.path.dirname(base)
    by_real = {}
    try:
        names = os.listdir(logs_dir)
    except OSError:
        return []
    for name in names:
        if not (name.startswith("gateway.") and name.endswith(".log")):
            continue
        if name == "gateway.out.log":
            continue
        path = os.path.join(logs_dir, name)
        if not os.path.isfile(path):
            continue
        real = os.path.realpath(path)
        prev = by_real.get(real)
        # 软链与它指向的日文件是同一份：优先留真实文件那个名字
        if prev is None or (os.path.islink(prev) and not os.path.islink(path)):
            by_real[real] = path
    paths = list(by_real.values())

    def rank(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    return sorted(paths, key=rank)


def tail_lines(path, max_bytes=TAIL_BYTES):
    """读文件尾部若干字节并按行切分。首行可能是半截，丢弃。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢掉被截断的那半行
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _usage_of(event):
    u = event.get("usage")
    if not isinstance(u, dict):
        return None
    def num(key):
        v = u.get(key)
        return v if isinstance(v, (int, float)) and v >= 0 else 0
    return {
        "input": num("input_tokens"),
        "output": num("output_tokens"),
        "cache_read": num("cache_read_input_tokens"),
        "cache_creation": num("cache_creation_input_tokens"),
    }


def summarize(lines):
    """把日志行汇总为大盘真实用量。纯函数，不推算、不造假、基于上游原生 usage。"""
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    requests = 0
    first_ts = last_ts = ""
    stripped_events = 0
    stripped_tools = 0
    last = None
    seen = 0
    for line in lines:
        if seen >= MAX_EVENTS:
            break
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        seen += 1
        ts = event.get("ts") or ""
        if ts:
            if not first_ts or ts < first_ts:
                first_ts = ts
            if ts > last_ts:
                last_ts = ts
        if event.get("event") == "canonicalized":
            stripped_events += 1
            for change in (event.get("changes") or []):
                if isinstance(change, str) and change.startswith("prune_tools_"):
                    try:
                        stripped_tools += int(change.rsplit("_", 1)[1])
                    except ValueError:
                        pass
            continue
        if event.get("event") != "upstream_ok":
            continue
        usage = _usage_of(event)
        if not usage:
            continue
        requests += 1
        for k in total:
            total[k] += usage[k]
        last = {"usage": usage, "ts": event.get("ts", ""), "model": event.get("model_mapped", "")}

    billed_input = total["input"] + total["cache_read"] + total["cache_creation"]
    hit_rate = (total["cache_read"] / billed_input) if billed_input else 0.0

    return {
        "seen": requests > 0,
        "requests": requests,
        "tokens": total,
        "billed_input": billed_input,
        "hit_rate": hit_rate,
        "stripped_events": stripped_events,
        "stripped_tools": stripped_tools,
        "last": last,
        "window": {"from": first_ts, "to": last_ts},
    }


def read_recent_usage(home=None):
    """读全部日志（含历史归档文件）并汇总。

    读归档文件而不只是当前那份，是因为单份日志有体积上限 —— 只读当前那份，覆盖区间
    可能只有一两天，而页面上的数字看起来像是「一直以来的总计」。结果里带 window
    区间与文件数，让读的人知道这个数覆盖了多久。
    """
    global _usage_cache
    paths = log_paths(home)
    if not paths:
        return dict(summarize([]), log_missing=True)
    try:
        key = tuple((p, os.path.getmtime(p), os.path.getsize(p)) for p in paths)
    except OSError:
        key = None
    if key and _usage_cache[0] == key:
        return _usage_cache[1]
    lines = []
    for p in paths:
        lines.extend(tail_lines(p))
    result = summarize(lines)
    result["log_files"] = len(paths)
    if key:
        _usage_cache = (key, result)
    return result


def last_pruning(home=None, max_bytes=1024 * 1024):
    """最近一次真实请求的工具面：客户端发了几个、我们剥了几个。

    两个数字落在**同一次请求的两行**里，用 request_id 串起来 —— 请求行的 `tools`，
    与 canonicalized 行的 `prune_tools_<N>`。只读日志尾部，倒着找第一对就返回。

    **读不到就返回 None**：页面据此不显示这一项，而不是拿「打开了几个开关」冒充
    「剥掉了几个工具」—— 那正是口径混乱的来源（65 个开关数与 88 个工具数没法比）。
    """
    lines = tail_lines(log_path(home), max_bytes)
    incoming = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        rid = event.get("request_id")
        n = event.get("tools")
        if rid and isinstance(n, int) and n >= 0:
            incoming[rid] = n

    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("event") != "canonicalized":
            continue
        rid = event.get("request_id")
        if not rid or rid not in incoming:
            continue
        stripped = 0
        for change in event.get("changes") or []:
            if isinstance(change, str) and change.startswith("prune_tools_"):
                try:
                    stripped += int(change.rsplit("_", 1)[1])
                except ValueError:
                    pass
        total = incoming[rid]
        return {"incoming": total, "stripped": stripped,
                "kept": max(total - stripped, 0),
                "ts": event.get("ts", ""), "request_id": rid}
    return None
