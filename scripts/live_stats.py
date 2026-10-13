#!/usr/bin/env python3
"""实时战绩：从网关日志里读**上游自己报回来的**真实用量，算命中率与省下的量。

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

# 上游公开单价（美元 / 百万 token）。**这些数字会变**，所以：
#  1. 只登记查证过的，没查证的不猜；
#  2. 页面上必须连着「按什么价、什么时候查的」一起显示，不能只甩一个金额；
#  3. 查不到价的供应商就只报 token，不折算金额 —— 编一个价码比不给更糟。
# DeepSeek 官方（2026-10 查证，off-peak）：
PRICES = {
    "deepseek": {"label": "DeepSeek 官方", "currency": "USD", "per_million": {
        "cache_hit": 0.003,      # 缓存命中
        "cache_miss": 0.15,      # 缓存未命中（输入）
        "output": 0.60,          # 输出
    }, "as_of": "2026-10"},
}

# 上游 URL 片段 → 价目表键。认不出来的就只报 token。
PRICE_MATCH = (
    ("api.deepseek.com", "deepseek"),
)


def log_path(home=None):
    base = home or os.environ.get("CLAUDE_TUNER_HOME") or os.path.expanduser("~/.claude-tuner")
    return os.path.join(base, "logs", "gateway.log")


def price_for(upstream_url):
    """按上游地址找价目。找不到返回 None —— 调用方据此决定只报 token。"""
    url = (upstream_url or "").lower()
    for fragment, key in PRICE_MATCH:
        if fragment in url:
            entry = dict(PRICES[key])
            entry["key"] = key
            return entry
    return None


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


def summarize(lines, price=None):
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
        "price": None,
        "cost": None,
    }

    if price and billed_input:
        per = price["per_million"]
        cost = (total["cache_read"] / 1e6) * per["cache_hit"] \
            + (total["input"] + total["cache_creation"]) / 1e6 * per["cache_miss"] \
            + (total["output"] / 1e6) * per["output"]
        # 「全都当没命中」的反面假设：同样这些输入，一个都不命中要花多少
        worst = (billed_input / 1e6) * per["cache_miss"] + (total["output"] / 1e6) * per["output"]
        result["price"] = price
        result["cost"] = {"actual": cost, "if_no_cache": worst, "saved": worst - cost}
    return result


def read_recent_usage(upstream_url=None, home=None):
    """读日志尾部并汇总。日志不存在（还没跑过请求）时如实返回空战绩。"""
    path = log_path(home)
    if not os.path.isfile(path):
        return dict(summarize([]), log_missing=True)
    return summarize(tail_lines(path), price=price_for(upstream_url))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="claude-tuner 实时战绩")
    parser.add_argument("--upstream", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    data = read_recent_usage(args.upstream)
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
    if data["cost"]:
        c = data["cost"]
        print("  按 %s 单价：实付 ~$%.4f；若全不命中 ~$%.4f" % (
            data["price"]["label"], c["actual"], c["if_no_cache"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
