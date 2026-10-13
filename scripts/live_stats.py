#!/usr/bin/env python3
"""实时战绩：从网关日志里读**上游自己报回来的**真实用量，算命中率与 token 量。

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

# 只读日志尾部这么多字节。日志按 5MB 轮转，尾部 4MB 足够覆盖相当长的使用；
# 再往前翻收益很小，而解析成本是线性的。
TAIL_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 20000  # 单次汇总最多统计的事件行数上限（防御异常大的日志）

# 这里**刻意不算钱**。
#
# 本项目支持任意上游，而每家的价目都不一样、还随时会调 —— 硬编码一张价目表，
# 用户看到的金额就是编的（他接的是另一家）。编一个价码比不给更糟：数字看着精确，
# 实际没有依据。所以只报 token 与命中率，那两个是上游自己数出来的。
#
# 要算钱的话，客户端本身带了官方机制：3P 配置里的 inferenceModelPricing（价目表）
# 与 inferenceModelPricingEnabled（显示估算费用）—— 由用户填自己那家的价，客户端算。
# 我们不去重复造一张会过时的表。


def log_path(home=None):
    base = home or os.environ.get("CLAUDE_TUNER_HOME") or os.path.expanduser("~/.claude-tuner")
    return os.path.join(base, "logs", "gateway.log")


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
    }
    return result


def read_recent_usage(home=None):
    """读日志尾部并汇总。日志不存在（还没跑过请求）时如实返回空战绩。"""
    path = log_path(home)
    if not os.path.isfile(path):
        return dict(summarize([]), log_missing=True)
    return summarize(tail_lines(path))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="claude-tuner 实时战绩")
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
