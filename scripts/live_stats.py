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

# 历史日志也一起读：日志按 LOG_MAX_BYTES(5MB) 轮转，只读当前那份的话，
# 页面上那个数字只是最近一份日志的，而且**没人知道它截止到什么时候**。
# 轮转文件（.old / .old.1 …）保留 LOG_OLD_MAX_AGE_DAYS(7) 天。
TAIL_BYTES = 6 * 1024 * 1024      # 单份最多读这么多（当前那份可能到 5MB）
MAX_EVENTS = 20000                # 单次汇总最多统计的事件行数上限（防异常大的日志）

# 按「文件 + mtime + 大小」缓存汇总结果。读十兆日志要一两百毫秒，
# 而页面每次打开都会拉这个接口 —— 缓存让重复打开瞬时完成。
_usage_cache = (None, None)


def log_path(home=None):
    base = home or os.environ.get("CLAUDE_TUNER_HOME") or os.path.expanduser("~/.claude-tuner")
    return os.path.join(base, "logs", "gateway.log")


def log_paths(home=None):
    """按时间从旧到新列出所有日志文件（含轮转的 .old / .old.1 …）。

    **必须连轮转文件一起读**：只读当前那份，统计区间就只是「最近一份日志」，
    而这份日志什么时候开始，页面上看不出来 —— 那等于给了个没有截止日期的数字。
    """
    base = log_path(home)
    paths = []
    try:
        import glob
        for p in glob.glob(base + "*"):
            if os.path.isfile(p):
                paths.append(p)
    except Exception:
        pass
    if os.path.isfile(base) and base not in paths:
        paths.append(base)
    # 时间序：轮转文件里序号越大越旧（.old 比 .old.1 新），当前那份最新
    def rank(path):
        if path == base:
            return (1, 0)
        suffix = path[len(base):]
        try:
            return (0, -int(suffix.rsplit(".", 1)[-1]))
        except ValueError:
            return (0, 0)
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
    """把日志行汇总成战绩。纯函数（喂行数组，返回结果），便于单测直接调。"""
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

    # 命中率的分母是「上游这一轮读进去的全部输入」：命中的 + 新写入缓存的 + 完全没走缓存的。
    # 只拿 cache_read / input 会把命中率算得虚高 —— 那等于把分母里的命中部分扔掉。
    billed_input = total["input"] + total["cache_read"] + total["cache_creation"]
    hit_rate = (total["cache_read"] / billed_input) if billed_input else 0.0

    result = {
        "seen": requests > 0,
        "requests": requests,
        "tokens": total,
        "billed_input": billed_input,
        "hit_rate": hit_rate,
        "stripped_events": stripped_events,
        "stripped_tools": stripped_tools,
        "last": last,
        # 统计区间。**必须报出来** —— 日志会轮转、轮转文件只留几天，不写区间的话
        # 读到的人无法判断这个数字覆盖了多久，等于给了一个没有截止日期的数。
        "window": {"from": first_ts, "to": last_ts},
    }
    return result


def read_recent_usage(home=None):
    """读全部日志（含轮转文件）并汇总。

    读轮转文件而不只是当前那份，是因为日志按 5MB 轮转 —— 只读当前那份，覆盖区间
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


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="claude-tuner 用量统计")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    data = read_recent_usage()
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if not data["seen"]:
        print("还没有可统计的请求。")
        return 0
    t = data["tokens"]
    print("已统计 %d 次上游请求" % data["requests"])
    print("  命中缓存 %.0f  (%.1f%%)" % (t["cache_read"], data["hit_rate"] * 100))
    print("  未命中   %d" % (t["input"] + t["cache_creation"]))
    print("  输出     %d" % t["output"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
