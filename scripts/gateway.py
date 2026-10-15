#!/usr/bin/env python3
"""Claude Desktop 3P gateway -> third-party model rewrite proxy."""
import copy
import glob
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 不在插件运行目录留 __pycache__：代码以仓库为准且改动频繁，缓存无收益（省几毫秒），
# 只带来「跑的是不是新代码」的疑问。所有入口脚本（manager/gateway/stats/sentinel）同此。
sys.dont_write_bytecode = True

from common import (
    DEFAULT_RULES,
    DEFAULT_TOOL_PRUNING_CONFIG,
    atomic_write_json,
    ensure_config_initialized,
    client_api_key,
    extract_session_id,
    estimate_tokens,
    is_opencode_upstream,
    upstream_client_headers,
    get_3p_config_dir,
    find_active_3p_config_path,
    read_3p_models,
    normalize_upstream,
)
import webui  # noqa: E402  配置台挂在同一个端口上，见 webui.handle_get / handle_post

HOST = "127.0.0.1"
DEFAULT_PORT = 8789
DEFAULT_CLAUDE_TUNER_UPSTREAM_TIMEOUT = 45  # 默认 45 秒上游超时，杜绝网络假死导致客户端被动干等 5 分钟
BASE_DIR = os.environ.get("CLAUDE_TUNER_HOME", os.path.expanduser("~/.claude-tuner"))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PROJECTS_DIR = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
COWORK_SESSIONS_DIRS = [
    os.path.expanduser("~/Library/Application Support/Claude-3p/local-agent-mode-sessions"),
    os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions"),
]
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_OLD_MAX_AGE_DAYS = 7  # .old 轮转文件保留天数
CAPTURE_SESSIONS_DIR = "sessions"  # 按会话归档的子目录，见 save_session_capture()
SESSION_KEY_LEN = 8  # 会话键取 session_id 前若干位，够区分同时开着的几个对话即可
MAX_SESSION_CAPTURES = 20  # 会话归档保留份数上限（每份是一整段对话报文，不设闸门只增不减）
MAX_BODY_BYTES = 50 * 1024 * 1024  # 50MB 保护上限，防止恶意或意外超大包引发 OOM
ERROR_BODY_LOG_CHARS = 2000  # 上游错误体只记前 2000 字符：够定位原因，又不至于把日志撑爆

# 事件名 → 日志级别映射（供 log_event 自动注入 level 字段，便于生产按级别过滤）
EVENT_LEVELS = {
    "unsupported_model_id": "WARN",
    "probe_local": "INFO",
    "invalid_json": "WARN",
    "stabilize_error": "ERROR",
    "canonicalized": "DEBUG",
    "orphan_tool_mention": "WARN",
    "pair_text_not_found": "WARN",
    "client_disconnected": "WARN",
    "upstream_ok": "INFO",
    "upstream_truncated": "ERROR",
    "upstream_stream_error": "ERROR",
    "upstream_http_error": "ERROR",
    "upstream_error": "ERROR",
    "models_config_empty": "WARN",
    "stabilize_rollback_failed": "ERROR",
}

_cached_models = None
_cached_mtime_state = None  # (meta_mtime, active_path, active_mtime)
_models_lock = threading.Lock()


def _safe_mtime(path):
    if path and os.path.isfile(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    return 0.0


def read_models_config():
    """读取模型映射字典（单一真理源：Claude 3P configLibrary）。

    带线程安全锁与 mtime 热加载检测：当 3P 配置文件被修改时，下一轮请求秒级自动重载。
    """
    global _cached_models, _cached_mtime_state
    with _models_lock:
        cfg_dir = get_3p_config_dir()
        meta_path = os.path.join(cfg_dir, "_meta.json") if (cfg_dir and os.path.isdir(cfg_dir)) else None
        meta_mtime = _safe_mtime(meta_path)
        active_path = find_active_3p_config_path()
        active_mtime = _safe_mtime(active_path)

        current_state = (meta_mtime, active_path, active_mtime)
        if _cached_models is not None and current_state == _cached_mtime_state:
            return dict(_cached_models)

        _cached_mtime_state = current_state
        _cached_models = read_3p_models()
        return dict(_cached_models)


# 启动时加载一次，运行中按 mtime 自动热加载
_warned_models_empty = False  # 「读不到 3P 模型列表」只在进程内提示一次


_config_cache = None
_config_mtime = 0.0
_config_lock = threading.Lock()


def _read_config():
    """读取 config.json（带线程安全锁与 mtime 热加载检测）。"""
    global _config_cache, _config_mtime
    with _config_lock:
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
            if _config_cache is not None and mtime == _config_mtime:
                return _config_cache
            _config_mtime = mtime
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            _config_cache = cfg if isinstance(cfg, dict) else {}
            return _config_cache
        except Exception:
            _config_mtime = 0.0
            _config_cache = {}
            return {}


def load_upstream():
    cfg = _read_config()
    url = (cfg.get("upstream_url") or "").strip()
    return normalize_upstream(url) if url else None


def load_tool_pruning_config():
    """读取 tool_pruning 配置，带线程安全锁与 mtime 热加载检测。"""
    cfg = _read_config()
    user_pruning = cfg.get("tool_pruning")
    if isinstance(user_pruning, dict):
        merged = dict(DEFAULT_TOOL_PRUNING_CONFIG)
        merged.update(user_pruning)
        return merged
    return dict(DEFAULT_TOOL_PRUNING_CONFIG)


def load_rules_config():
    """读取 rules 清洗与防抖规则配置，带线程安全锁与 mtime 热加载检测。"""
    cfg = _read_config()
    user_rules = cfg.get("rules")
    if isinstance(user_rules, dict):
        merged = dict(DEFAULT_RULES)
        merged.update(user_rules)
        return merged
    return dict(DEFAULT_RULES)



# 工具剥离规则本身住在 tool_rules.py（纯判断层）：网关与配置页共用同一份，
# 页面显示的「按现在的配置会不会剥」与实际发生的事必然是同一个判断。
# 这里 import 进来再导出，sentinel.py 等既有调用方无需改动。
from tool_rules import (  # noqa: E402
    PROMPT_PAIRS,
    active_pairs,
    category_for,
    get_stripped_tool_names,
    orphan_mentions,
    pair_is_orphaned,
    pair_paragraph_predicate,
    should_strip_tool,
    strip_reason,
    tp_flag as _tp_flag,
)



_INVENTORY_MAX_PROFILES = 8


# 上一次报出来的孤句集合。逐轮重复报同一件事只会把日志刷满，
# 但**变化时一定报** —— 客户端新增交叉引用时它就会变，那正是我们要知道的那一刻。
_orphan_memo = [None]
_pair_miss_memo = [None]


def _system_text(parsed):
    """只拼 system 提示词。

    **刻意不扫对话正文**，两个理由：
    1. 工具说明住在 system 里。对话正文提到某个工具名是正常的（用户自己说的话、
       模型之前的输出），拿它报警只会变成噪声；
    2. 成本。messages 动辄 1 MB 且随会话线性增长，system 只有几 KB 且基本恒定 ——
       这个函数在**每个请求**上跑，只扫 system 是 0.2 ms，扫全部是它的几十倍。
    """
    sysv = parsed.get("system")
    if isinstance(sysv, str):
        return sysv
    if not isinstance(sysv, list):
        return ""
    parts = []
    for b in sysv:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts)


def report_orphan_mentions(parsed, stripped_names):
    """工具剥了、提示词里还在提它 —— 记进日志，不去自动改写。

    **必须在配对段落删除之后调用**。配对段落正是被我们连带删掉的，在删之前查，
    会把「已经正确处理掉的那段」误报成孤句。

    配对表（tool_rules.PROMPT_PAIRS）只登记了「整段只为解释某个工具」的段落，那些会被
    连带删掉。但提示词里还有**句中被提一句**的情况（例如通用指引里那句「invoke it via
    Skill」），整段删会连累无关内容、句中手术又容易切坏，都不该自动做。

    于是这里退一步：**发现并报出来**。客户端哪天新增工具或交叉引用，日志先知道，
    页面据此提示 —— 而不是等用户遇到「模型在用一个不存在的工具」再回头查。
    """
    try:
        text = _system_text(parsed)
        if not text:
            return
        hits = orphan_mentions(text, stripped_names)
        if hits == _orphan_memo[0]:
            return   # 和上一轮一样就闭嘴：这个检查每个请求都跑，重复报会把日志刷满
        _orphan_memo[0] = hits
        if hits:
            log_event({"event": "orphan_tool_mention",
                       "tools": hits[:20],
                       "note": "这些工具已被剥离，但提示词里仍提到它们"})
    except Exception:
        pass


def report_missing_pair_paragraphs(missed):
    """该删的配对段落一处都没找到 —— 记进日志。

    与 report_orphan_mentions 是两件事，别混：那边是「工具剥了、正文里还提一句」，
    段落边界不清楚，属**已知取舍**；这边是「我们承诺过整段删掉，结果一处都没命中」，
    属**故障** —— 工具已经没了，那段却还留着，模型被引到一个不存在的工具上。

    最可能的成因：客户端改了那段的措辞，锚点失配。改一行 PROMPT_PAIRS 的 anchor 即可。
    """
    if not missed:
        return
    if missed == _pair_miss_memo[0]:
        return   # 每个请求都跑，和上一轮一样就闭嘴，免得把日志刷满
    _pair_miss_memo[0] = list(missed)
    log_event({"event": "pair_text_not_found",
               "pairs": missed[:20],
               "note": "这些配对的工具已被全部剥离，但它们的说明段落没找到 —— 客户端可能改了措辞"})


def cleanup_runtime():
    """清理超过保留期的日志轮转文件 (.old)，防止长期积累。

    日志轮转本身由 check_log_rotation() 在写入侧事件驱动完成，此处只负责老化清理。
    """
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    try:
        cutoff = time.time() - LOG_OLD_MAX_AGE_DAYS * 86400
        for name in os.listdir(log_dir):
            if name.endswith(".old"):
                full = os.path.join(log_dir, name)
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
    except OSError:
        pass


def load_default_model():
    """未命中映射表时使用的兜底上游模型名（空 = 不兜底）。

    显式配的 default_model 优先；没配就取当前映射表里第一个目标 ——
    这样「用户换了上游但没改 default_model」也能落回他自己配的那个模型。
    对官方模型中转站这类用户尤其要紧：他们的映射目标就是 claude-* 本身，
    兜底若写死成某个第三方模型名，就会把用户的 Opus 悄悄换成别的。
    """
    explicit = (_read_config().get("default_model") or "").strip()
    if explicit:
        return explicit
    for target in read_models_config().values():
        target = (target or "").strip()
        if target:
            return target
    return ""


def map_model(model):
    """直接字典查找：自动剥离 [1m] 等上下文后缀并从 3P 配置获取真实映射。

    未命中映射表的客户端代号（`claude-*`）默认落到 `default_model`，而不是原样透传 ——
    客户端的选择器由 3P 的 inferenceModels 决定，用户换了代号、映射表没跟上时，
    透传会让上游以 404 打回，表现成「刚装好就报错」。
    """
    global _models, _warned_models_empty
    _models = read_models_config()
    raw = (model or "").strip()
    # 后缀是客户端自己的上下文标记（[1m] 等），任何情况下都不该发给上游
    stripped = re.sub(r"\[.*?\]$", "", raw).strip()
    clean = stripped.lower()
    mapped = _models.get(clean) or _models.get(raw.lower()) or ""
    if not mapped:
        fallback = load_default_model()
        if (clean.startswith("claude-") and fallback
                and _read_config().get("unknown_model_fallback", True) is not False):
            log_event({
                "event": "unknown_model_fallback",
                "requested": raw,
                "mapped_to": fallback,
                "hint": "该客户端代号不在 3P 模型映射里，已落到 default_model",
            })
            return fallback
        mapped = stripped
    # 白名单校验：用户在 3P 配置中声明的有效别名均属于合法支持模型；
    # 仅当模型完全未知且未在 3P 配置中声明时才记录警告，彻底避免自定义别名刷屏 WARN。
    valid_ids = frozenset(k.lower() for k in _models.keys())
    if not valid_ids:
        # 读不到 3P 配置时没有任何可比对的名单，逐请求告警会刷爆日志，进程内只提示一次。
        if not _warned_models_empty:
            _warned_models_empty = True
            log_event({
                "event": "models_config_empty",
                "hint": "未读到 3P 配置的模型列表，模型名将原样透传给上游",
            })
    elif clean not in valid_ids and raw.lower() not in valid_ids:
        log_event({
            "event": "unsupported_model_id",
            "requested": raw,
            "mapped_to": mapped,
        })
    return mapped


_LOG_EVENT_COUNT = 0
_LOG_LOCK = threading.Lock()


def check_log_rotation():
    """事件驱动型日志轮转：每写入 50 条日志抽样检查一次大小，超 5MB 自动复制为 .old 并截断清空。
    完全由事件驱动，零后台定时器、零常驻轮询线程、空闲时零系统开销。"""
    global _LOG_EVENT_COUNT
    _LOG_EVENT_COUNT += 1
    if _LOG_EVENT_COUNT % 50 != 0:
        return
    log_dir = os.path.join(BASE_DIR, "logs")
    log_path = os.path.join(log_dir, "gateway.log")
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > LOG_MAX_BYTES:
            old_path = log_path + ".old"
            import shutil
            shutil.copyfile(log_path, old_path)
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.truncate(0)
    except Exception:
        pass


def log_event(event, req_id=None):
    """向 stdout 输出单行 JSON 结构化日志，统一注入 ts、level（默认 INFO）与可选 request_id。"""
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if req_id and "request_id" not in event:
        event["request_id"] = req_id
    if "level" not in event:
        event["level"] = EVENT_LEVELS.get(event.get("event", ""), "INFO")
    line = json.dumps(event, ensure_ascii=False)
    with _LOG_LOCK:
        print(line, flush=True)
        check_log_rotation()


def is_probe_request(parsed):
    """Claude Code/Desktop capability, token-counting, and preflight probes."""
    max_tok = parsed.get("max_tokens")
    if max_tok is not None and max_tok <= 1 and not parsed.get("stream"):
        return True
    return False

def make_probe_response(parsed):
    return {
        "id": "msg_probe_" + os.urandom(8).hex(),
        "type": "message",
        "role": "assistant",
        "model": map_model(parsed.get("model", "")),
        "content": [{"type": "thinking", "thinking": "ok", "signature": "probe"}],
        "stop_reason": "max_tokens",
        "stop_sequence": None,
        "usage": {
            "input_tokens": estimate_tokens(parsed),
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def find_session_transcript(session_id):
    if not session_id:
        return None
    # 1. 优先从标准 Code 模式 projects 目录查找
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    if matches:
        return matches[0]
    # 2. 从 CoWork 协作模式 (local-agent-mode-sessions) 目录查找
    for cw_dir in COWORK_SESSIONS_DIRS:
        if os.path.isdir(cw_dir):
            pattern = os.path.join(cw_dir, "*", "*", "*", ".claude", "projects", "session", f"{session_id}.jsonl")
            cw_matches = glob.glob(pattern)
            if cw_matches:
                return cw_matches[0]
    return None


def strip_model_declaration_text(text):
    """精准连根拔除 Claude 专属模型代号、Mythos/Fable 宣称与环境模型绑定行。"""
    if not isinstance(text, str):
        return text, False
    orig = text
    # 1. 彻底抹除整块 # Environment 下关于模型代号与 CLI 的宣称（整块拔除标题与列表项）
    text = re.sub(
        r'(?m)^# Environment\s*\n(?:\s*-\s*.*(?:Claude|models|Fast mode).*\n?)+',
        '',
        text
    )
    # 2. 抹除 Fable / Mythos 等自主迭代神化宣称段落 (如 This iteration of Claude is Claude Fable 5.1...)
    text = re.sub(r'(?m)^This iteration of Claude is Claude[^\n]+(?:\n|\Z)', '', text)
    # 3. 抹除正文中零散的 You are powered by the model named... 及其 knowledge cutoff
    text = re.sub(r'(?m)^You are powered by the model named[^\n]+(?:\n|\Z)', '', text)
    # 4. 剥离零散的模型代号声明行（兜底）
    pattern = r'^\s*-\s*(?:You are powered by the model named|The most recent Claude models are|Fast mode for Claude Code uses|Claude Code is available as a CLI).*$\n?'
    text = re.sub(pattern, '', text, flags=re.MULTILINE)
    return text, (text != orig)


def strip_ghost_tool_instructions(text, tp_cfg=None, stripped_names=None, found=None):
    """剥离「工具已被剥、说明段落却还留着」的孤句。

    配对关系登记在 tool_rules.PROMPT_PAIRS 里（数据，不是散在各处的 if），
    判断用 strip_reason 的同一份逻辑 —— 所以**无论是类别开关剥的还是用户自己勾的**，
    段落都会跟着走。此前只认两个类别开关，用户自定义剥掉 ccd_pr 就会留下孤句。

    **只认整段**：配对表里登记的都是「整段存在的意义就是解释这个工具」的段落，
    按空行切段、段首命中锚点即摘除整段。段中顺带提一句工具名的不在这里处理（整段删
    会连累无关指引，句中手术又容易切坏），交给 orphan_mentions 去发现并报警。

    `found` 传入 set 时，把**本段里真被摘掉**的配对 id 记进去。判定「该剥却没摘到」
    是**跨全部 system 块**的事（一个块没有不代表整份请求没有），所以只记命中、由调用方
    收官：见 canonicalize 里 `active_pairs() - found`（`missed` 曾按块判定，把计费头/
    身份那两块误报成失配，见 DECISIONS「配对段落失配的按块假报警」）。
    """
    if not isinstance(text, str):
        return text, False
    cfg = tp_cfg if tp_cfg is not None else load_tool_pruning_config()
    if not _tp_flag(cfg, "enabled"):
        return text, False
    names = stripped_names if stripped_names is not None else get_stripped_tool_names(cfg)

    orig = text
    for pair in PROMPT_PAIRS:
        if not pair_is_orphaned(pair, names, cfg):
            continue
        text, hit = _strip_paragraphs_by_predicate(
            text, pair_paragraph_predicate(pair), trim=False)
        if hit and found is not None:
            found.add(pair["id"])
    return text, (text != orig)


def _strip_paragraphs_by_predicate(text, predicate, trim=True):
    """按自然段落(\\n\\n)精准拆解与识别，若命中谓词则安全剔除整段并压实空白。

    核心防御哲学：
    1. 绝不使用跨越全局的贪婪正则，杜绝吞噬临近段落；
    2. 必须同时命中段首特征与段内核心关键字；
    3. 特征不全则宁可不剥，绝不误伤任何正常文本。

    trim=False 保留原文首尾空白。配对段落删除走这条：那段常出现在 system 正中间，
    整体 strip 会顺手动到头尾，没必要 —— 只摘掉该摘的那一段。
    """
    if not isinstance(text, str) or not text:
        return text, False
    paras = text.split("\n\n")
    new_paras = []
    changed = False
    for p in paras:
        clean_p = p.strip()
        if clean_p and predicate(clean_p):
            changed = True
        else:
            new_paras.append(p)
    if not changed:
        return text, False
    result = "\n\n".join(new_paras)
    return (result.strip() if trim else result), True


def strip_security_guidelines_text(text):
    """剥离安全攻防与拒绝服务限制指令段落。

    特征：以 IMPORTANT: 开头，提及 security testing，且包含拒绝破坏性技术或双重用途工具授权。
    """
    def _is_security_para(p):
        if not p.startswith("IMPORTANT:"):
            return False
        p_lower = p.lower()
        if "security testing" not in p_lower:
            return False
        return ("destructive techniques" in p_lower or 
                "dos attacks" in p_lower or 
                "dual-use security tools" in p_lower or
                "supply chain compromise" in p_lower)
    return _strip_paragraphs_by_predicate(text, _is_security_para)


def strip_pronoun_guidelines_text(text):
    """剥离人称代词道德约束与性别中立指令段落。

    特征：以 When you use a pronoun for someone 开头，提及 they/them，且说明不从名字推测性别。
    """
    def _is_pronoun_para(p):
        if not p.startswith("When you use a pronoun for someone"):
            return False
        p_lower = p.lower()
        if "they/them" not in p_lower:
            return False
        return ("misgender" in p_lower or 
                "infer pronouns" in p_lower or
                "doesn't tell you someone's pronouns" in p_lower)
    return _strip_paragraphs_by_predicate(text, _is_pronoun_para)


def strip_caution_guidelines_text(text):
    """剥离向外发送与不可逆操作防呆指引段落。

    特征：以 For actions that are hard to reverse or outward-facing 开头，要求先确认或注意外部服务外发。
    """
    def _is_caution_para(p):
        if not p.startswith("For actions that are hard to reverse or outward-facing"):
            return False
        p_lower = p.lower()
        if "confirm first" not in p_lower:
            return False
        return ("external service" in p_lower or 
                "hard to reverse" in p_lower or
                "durably authorized" in p_lower)
    return _strip_paragraphs_by_predicate(text, _is_caution_para)


def strip_total_tokens_tag_text(text):
    """剥离虚假硬编码的 <total_tokens> 配额标签。

    特征：匹配 <total_tokens>...tokens left</total_tokens> 虚假配额标签。
    防范静态虚假数字变动击穿 Prompt Cache。
    """
    if not isinstance(text, str) or "<total_tokens>" not in text:
        return text, False
    orig = text
    # 1. 若 <total_tokens> 自成一个独立段落（前后空行），直接段落剔除，保持剩余段落间双换行
    def _is_tokens_para(p):
        clean = p.strip()
        return bool(re.match(r"(?i)^<total_tokens>.*?tokens left</total_tokens>$", clean))

    text, _ = _strip_paragraphs_by_predicate(text, _is_tokens_para)

    # 2. 兜底整行剔除或行内嵌入形式
    if "<total_tokens>" in text:
        text = re.sub(r"(?mi)^\s*<total_tokens>.*?tokens left</total_tokens>\s*$\n?", "", text)
        text = re.sub(r"(?i)<total_tokens>.*?tokens left</total_tokens>\s*", "", text)
    return text, (text != orig)


def prefix_hashes(parsed):
    """返回 (system, tools) 的 12 位 SHA-256 摘要，用于检测逐轮前缀漂移（messages 自然增长，不参与）。"""
    def digest(value):
        try:
            return hashlib.sha256(
                json.dumps(value or [], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:12]
        except Exception:
            return ""
    return digest(parsed.get("system")), digest(parsed.get("tools"))


def map_request_texts(parsed, fn, messages=True):
    """对请求中「system / messages」×「裸字符串 / 内容块数组」的全部文本槽位施加 fn(text, legacy) -> (new_text, hit)。

    返回 `(system 命中数, messages 命中数)`：
    - `legacy`：按角色判定（`system` 字段或 `role == "system"` 的消息为 True），防止对用户/助手正文误执行截断兜底；
    - `messages=False`：仅扫描 `system` 字段（用于只出现在 system 尾部的幽灵工具说明）。
    """
    def apply(value, legacy):
        """返回 (新值, 命中数)。list 形态就地改块、原样返回同一个 list。"""
        if isinstance(value, str):
            new, hit = fn(value, legacy)
            return (new, 1) if hit else (value, 0)
        if isinstance(value, list):
            hits = 0
            for blk in value:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    new, hit = fn(blk["text"], legacy)
                    if hit:
                        blk["text"] = new
                        hits += 1
            return value, hits
        return value, 0

    # system 是注入面：这里的裸句就是注入
    sys_value, sys_hits = apply(parsed.get("system"), True)
    if sys_hits:
        parsed["system"] = sys_value

    msg_hits = 0
    if messages:
        for m in parsed.get("messages") or []:
            if not isinstance(m, dict):
                continue
            value, hits = apply(m.get("content"), m.get("role") == "system")
            if hits:
                m["content"] = value
                msg_hits += hits
    return sys_hits, msg_hits


def canonicalize(parsed, rules=None, tool_pruning_cfg=None):
    """Stabilize cache-relevant bytes without touching semantic content."""
    changed = []
    r = rules or load_rules_config()
    if tool_pruning_cfg is not None:
        tp = dict(DEFAULT_TOOL_PRUNING_CONFIG)
        tp.update(tool_pruning_cfg)
    else:
        tp = load_tool_pruning_config()

    # 1. 彻底物理剥离计费与入口头 (x-anthropic-billing-header)
    if r.get("strip_billing_header", True):
        sysv = parsed.get("system")
        if isinstance(sysv, list):
            clean_sys = [
                b for b in sysv
                if not (isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip().startswith("x-anthropic-billing-header:"))
                and not (isinstance(b, str) and b.strip().startswith("x-anthropic-billing-header:"))
            ]
            if len(clean_sys) != len(sysv):
                parsed["system"] = clean_sys
                changed.append("strip_billing_header")
        elif isinstance(sysv, str) and "x-anthropic-billing-header:" in sysv:
            clean_lines = [l for l in sysv.splitlines() if not l.strip().startswith("x-anthropic-billing-header:")]
            parsed["system"] = "\n".join(clean_lines).strip()
            changed.append("strip_billing_header")

    for m in parsed.get("messages", []) or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = [{"type": "text", "text": c}]
            changed.append("user_content_shape")

    # 2. 从 System 与所有 Messages 中清洗动态注入的 <system-reminder> 与日期、Token 计数
    if r.get("strip_date", True):
        def _clean_reminder_text(text, legacy=False):
            """legacy 在此规则下不用：日期与 Token 计数在对话正文里也是纯噪声。"""
            if not isinstance(text, str):
                return text, 0
            orig = text
            # 彻底剥离整块 # currentDate 日期提醒（与 gitStatus 共用同一个定义）
            text = _strip_leading_reminder(text, "currentDate")
            # 彻底按整行抹除动态日期 (无论带不带 - 列表符，无论是否独立成行)
            text = re.sub(r"(?m)^\s*(?:-\s*)?Today's date is \d{4}-\d{2}-\d{2}\.\s*$\n?", "", text)
            # 剥离散落的动态变动项 (Today's date is..., currentDate)
            text = re.sub(r"Today's date is \d{4}-\d{2}-\d{2}\.\s*", "", text)
            text = re.sub(r"currentDate:\s*[^\n]+\s*", "", text)
            return text, (1 if text != orig else 0)

        sys_n, msg_n = map_request_texts(parsed, _clean_reminder_text)
        if sys_n + msg_n:
            changed.append("strip_date")

    # 3. 彻底物理剥离模型代号声明与版本宣称（全面覆盖 system 与 messages）
    if r.get("strip_model_declarations", True):
        sys_n, msg_n = map_request_texts(
            parsed, lambda t, legacy: strip_model_declaration_text(t))
        changed.extend(["strip_model_declarations"] * sys_n)
        changed.extend(["strip_model_declarations_msg"] * msg_n)

    # 4. 彻底物理剥离动态注入的 gitStatus 区块（全面整块抹除）
    if r.get("strip_git_status", True):
        # legacy 按角色判定（见 map_request_texts）：system 与 role=system 的消息放行截断，
        # user/assistant 的裸句更可能是正常讨论，不截。
        sys_n, msg_n = map_request_texts(
            parsed, lambda t, legacy: strip_git_status_text(t, legacy_truncate=legacy))
        changed.extend(["strip_git_status_system"] * sys_n)
        changed.extend(["strip_git_status_msg"] * msg_n)

    # 4.5. 精准剥离已剔除冗余工具在 System 尾部的孤立引导段落（与 tool_pruning 自动原子联动，杜绝反向脱节）
    # messages=False：剥的是 system 尾部那份引导，不该去对话正文里找同样的段落。
    _ghost_names = get_stripped_tool_names(tp)
    _pair_found = set()
    sys_n, _ = map_request_texts(
        parsed, lambda t, legacy: strip_ghost_tool_instructions(
            t, tp_cfg=tp, stripped_names=_ghost_names, found=_pair_found), messages=False)
    changed.extend(["strip_ghost_tool_instructions"] * sys_n)
    # 「该剥却整份请求都没摘到」才是失配：found 跨 system 各块收集，这里收官。按块判会把
    # 计费头、身份那两块（本就不含这些段落）每个请求都误报一遍 —— 客户端改了措辞的信号，
    # 别让它静默过去，但也别拿假报警淹掉它。
    report_missing_pair_paragraphs(
        [p["id"] for p in active_pairs(tp, _ghost_names) if p["id"] not in _pair_found])

    # 4.6. 精准剥离安全防御、代词约束与高危操作防呆指令（仅作用于 system 注入面，绝不误伤正文）
    if r.get("strip_security_guidelines", False):
        sys_n, _ = map_request_texts(
            parsed, lambda t, legacy: strip_security_guidelines_text(t), messages=False)
        if sys_n:
            changed.extend(["strip_security_guidelines"] * sys_n)

    if r.get("strip_pronoun_guidelines", False):
        sys_n, _ = map_request_texts(
            parsed, lambda t, legacy: strip_pronoun_guidelines_text(t), messages=False)
        if sys_n:
            changed.extend(["strip_pronoun_guidelines"] * sys_n)

    if r.get("strip_caution_guidelines", False):
        sys_n, _ = map_request_texts(
            parsed, lambda t, legacy: strip_caution_guidelines_text(t), messages=False)
        if sys_n:
            changed.extend(["strip_caution_guidelines"] * sys_n)

    if r.get("strip_total_tokens_tag", True):
        # 仅在注入面生效（顶层 system 或 messages 中 role: system 的注入消息），严禁误伤正文真实讨论
        def _strip_tokens_fn(text, legacy):
            if not legacy:
                return text, False
            return strip_total_tokens_tag_text(text)
        sys_n, msg_n = map_request_texts(parsed, _strip_tokens_fn, messages=True)
        if sys_n + msg_n:
            changed.extend(["strip_total_tokens_tag"] * (sys_n + msg_n))

    # 统一剔除清洗后变为空的残余消息与空 text 块 (例如被抽空的 reminder 块)
    msgs = parsed.get("messages")
    if isinstance(msgs, list):
        clean_msgs = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, list):
                c = [b for b in c if not (isinstance(b, dict) and b.get("type") == "text" and not (b.get("text") or "").strip())]
                m["content"] = c
            # 剔除空 system 消息或全空消息（保留纯输出 assistant 占位）
            if m.get("role") == "system" and not m.get("content"):
                continue
            if not m.get("content") and m.get("role") != "assistant":
                continue
            # 保留原生 role: system 语义，避免转换造成连续相邻 user 消息
            clean_msgs.append(m)
        if len(clean_msgs) != len(msgs):
            parsed["messages"] = clean_msgs
            changed.append("prune_empty_messages")

    # 5. 拆分混合 user 消息（text 与 tool_result 共存同一条消息，防 OpenAI 400）
    if r.get("split_mixed_user_messages", True):
        if split_mixed_user_messages(parsed):
            changed.append("split_mixed_user_msg")

    # 6. 收尾：查一遍「已经被剥掉、提示词里却还在提」的工具名。
    #    放在最后（配对段落已删完），所以报出来的都是我们**没能**处理干净的 —— 即
    #    客户端新增的交叉引用。这是探测器不是改写器，只记日志，见 report_orphan_mentions。
    report_orphan_mentions(parsed, _ghost_names)

    return changed


def _strip_leading_reminder(text, keyword):
    """剥离锚定在文本开头（允许前置空白）的 `# <keyword>` `<system-reminder>` 块。

    必须锚定开头（客户端注入只出现在消息首部；正文中间出现的同名标签属于引用，不可跨越吞噬）。
    """
    head = r"^\s*<system-reminder>\s*"
    tail = r"[\s\S]*?</system-reminder>\s*"
    mark = r"#\s*" + keyword
    text = re.sub(head + r"As you answer the user's questions, you can use the following context:\s*" + mark + tail, "", text)
    text = re.sub(head + mark + tail, "", text)
    return re.sub(r"<system-reminder>\s*</system-reminder>\s*", "", text)


def strip_git_status_text(text, legacy_truncate=False):
    """剥离动态注入的 gitStatus 块；legacy_truncate=True 时额外对旧版非标签形态自 marker 起截断（仅用于 system 角色）。"""
    if not isinstance(text, str):
        return text, False
    orig = text
    # 1. 彻底整块抹除以 <system-reminder> 包装的 # gitStatus
    text = _strip_leading_reminder(text, "gitStatus")
    # 2. 兼容旧版 marker 截断（若存在非 system-reminder 包装形式）
    if legacy_truncate:
        for marker in (
            "gitStatus: This is the git status at the start of the conversation.",
            "This is the git status at the start of the conversation.",
        ):
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx].rstrip()
                break
    return text, (text != orig)


def split_mixed_user_messages(parsed):
    """拆分混合 user 消息（text 与 tool_result 共存于同一条消息）。

    Claude Code 启动 Skill 时会生成这种合法 Anthropic 格式消息，但部分上游
    （如 opencode/DeepSeek）严格要求紧跟 assistant tool_use 的消息为纯
    tool_result，混合块会触发 HTTP 400。统一拆为 tool_result 消息在前、
    其余块独立成 user 消息在后。
    纯函数确定性变换，不破坏字节级前缀缓存。返回是否发生了拆分。
    """
    msgs = parsed.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    out = []
    changed = False
    for m in msgs:
        if (isinstance(m, dict) and m.get("role") == "user"
                and isinstance(m.get("content"), list)):
            content = m["content"]
            has_tr = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            has_other = any(isinstance(b, dict) and b.get("type") != "tool_result" for b in content)
            if has_tr and has_other:
                out.append({"role": "user", "content": [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]})
                out.append({"role": "user", "content": [b for b in content if isinstance(b, dict) and b.get("type") != "tool_result"]})
                changed = True
                continue
        out.append(m)
    if changed:
        parsed["messages"] = out
    return changed






def classify_kind(parsed):
    sysv = parsed.get("system")
    if isinstance(sysv, list):
        for b in sysv:
            t = b.get("text") if isinstance(b, dict) else (b if isinstance(b, str) else None)
            if isinstance(t, str) and t.strip().startswith("x-anthropic-billing-header:"):
                return "subagent" if "cc_is_subagent=true" in t else "main"
    elif isinstance(sysv, str) and sysv.strip().startswith("x-anthropic-billing-header:"):
        return "subagent" if "cc_is_subagent=true" in sysv else "main"
    return "main"


def extract_subagent_fingerprint(parsed):
    """从 parsed 请求体中提取子代理唯一标识 (sub_id, task_title)。"""
    msgs = parsed.get("messages") or []
    if not msgs or not isinstance(msgs, list):
        return "sub-empty", "子代理任务"
    msg0 = msgs[0]
    content = msg0.get("content") or ""
    text_content = ""
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text") or ""
                t_clean = clean_text_for_fp(t)
                if t_clean:
                    text_content += t_clean + "\n"
    elif isinstance(content, str):
        text_content = clean_text_for_fp(content)
    text_content = text_content.strip()
    if not text_content:
        text_content = json.dumps(msg0, sort_keys=True)
    fp = hashlib.md5(text_content.encode("utf-8")).hexdigest()[:8]
    sub_id = f"sub-{fp}"
    lines = [l.strip() for l in text_content.split("\n") if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("<")]
    task_title = (lines[0][:30] + "...") if lines else "子代理任务"
    return sub_id, task_title


def clean_text_for_fp(text):
    if not isinstance(text, str):
        return ""
    # 过滤客户端动态注入的 <system-reminder> 块，确保与转录本底稿精准对齐
    return re.sub(r'<system-reminder>[\s\S]*?</system-reminder>\s*', '', text).strip()


def session_capture_key(parsed, kind=None):
    """本次请求在会话归档里的键；取不到会话 id 时返回 None。

    子代理与主会话**共用同一个 session_id**（客户端如此），所以键上必须再带 `_subagent`
    后缀，否则一份审计用的子代理报文会把同会话的主会话归档顶掉。
    """
    sid = extract_session_id(parsed)
    if not sid:
        return None
    suffix = "_subagent" if (kind or classify_kind(parsed)) == "subagent" else ""
    return sid[:SESSION_KEY_LEN] + suffix


def prune_session_captures(sess_dir, keep=MAX_SESSION_CAPTURES):
    """按 mtime 只保留最近 keep 份会话归档。

    每开一个新对话就多一份（每份是一整段对话的原始报文），没有这道闸门时该目录只增不减。
    """
    try:
        paths = [os.path.join(sess_dir, n) for n in os.listdir(sess_dir) if n.endswith(".json")]
        if len(paths) <= keep:
            return
        # mtime 打平时用文件名兜底，保证每次删的是同一批（不靠 listdir 的顺序）
        paths.sort(key=lambda p: (_safe_mtime(p), os.path.basename(p)), reverse=True)
        for p in paths[keep:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def save_session_capture(capture_dir, parsed, kind=None):
    """把报文归档到 captures/sessions/<会话键>.json，返回归档路径（取不到会话 id 时返回 None）。

    这是**唯一**按会话隔离的一份：`latest_raw_inbound.json` 是全局最后写者赢，按模型那份
    也只在模型维度分开 —— 两个对话同模型同时开着时，那两份都会互相覆盖，互不覆盖的只有这里。
    """
    key = session_capture_key(parsed, kind)
    if not key:
        return None
    sess_dir = os.path.join(capture_dir, CAPTURE_SESSIONS_DIR)
    os.makedirs(sess_dir, exist_ok=True)
    path = os.path.join(sess_dir, f"{key}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(parsed, fh, ensure_ascii=False, indent=2)
    prune_session_captures(sess_dir)
    return path


_transcript_cache = {}
_TRANSCRIPT_CACHE_MAX = 10    # 按活跃会话数限制缓存上限，防止长会话 thinking 索引累积占用内存
_TRANSCRIPT_CACHE_BATCH = 2   # 超限时按 LRU 每次淘汰的条目数
_tpath_locks = {}
_tpath_locks_lock = threading.Lock()


def _get_tpath_lock(tpath):
    """获取指定转录文件的细粒度并发锁，消除跨会话/子代理之间的全局锁竞争。"""
    with _tpath_locks_lock:
        lock = _tpath_locks.get(tpath)
        if lock is None:
            if len(_tpath_locks) > 200:
                # 仅淘汰未持锁的闲置锁，绝不触碰任何正在持锁中的活跃锁对象（彻底消除并发重构竞态）
                for k, lk in list(_tpath_locks.items()):
                    if not lk.locked():
                        _tpath_locks.pop(k, None)
            lock = threading.Lock()
            _tpath_locks[tpath] = lock
        return lock


def parse_transcript_data(tpath):
    """基于文件偏移量（offset seek）与 per-tpath 细粒度锁增量解析 .jsonl 底稿，提取 thinking 索引。"""
    try:
        cur_size = os.path.getsize(tpath)
        cur_mtime = os.path.getmtime(tpath)
    except OSError:
        return {}

    file_lock = _get_tpath_lock(tpath)
    with file_lock:
        entry = _transcript_cache.get(tpath)
        # 大小与 mtime 均未变化时直接命中并刷新 LRU 顺序（同时比对 mtime 以防等长重写）
        if entry and entry["size"] == cur_size and entry.get("mtime") == cur_mtime:
            _transcript_cache[tpath] = _transcript_cache.pop(tpath)
            return entry["index"]

        # 首次读取 / 被截断 / 等长重写时归零重读；正常追加则从原 offset 增量续读
        truncated = bool(entry) and cur_size < entry["size"]
        rewritten = bool(entry) and cur_size == entry["size"] and entry.get("mtime") != cur_mtime
        if not entry or truncated or rewritten:
            entry = {
                "offset": 0,
                "size": 0,
                "index": {},
                "current_turn_thinking": [],
                "current_turn_tools": [],
                "current_turn_texts": [],
            }

        offset = entry["offset"]
        index = entry["index"]
        current_turn_thinking = entry["current_turn_thinking"]
        current_turn_tools = entry["current_turn_tools"]
        current_turn_texts = entry["current_turn_texts"]

        def flush_turn():
            nonlocal current_turn_thinking, current_turn_tools, current_turn_texts
            if current_turn_thinking:
                for tid in current_turn_tools:
                    index["tool:" + tid] = list(current_turn_thinking)
                if not current_turn_tools:
                    for txt in current_turn_texts:
                        if txt:
                            index["text:" + txt] = list(current_turn_thinking)
            current_turn_thinking.clear()
            current_turn_tools.clear()
            current_turn_texts.clear()

        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                if offset > 0:
                    f.seek(offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    # 仅当读到完整行换行符时才推进有效 offset（防半截脏读）
                    if line.endswith("\n"):
                        offset = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    dtype = d.get("type")
                    # 遇到压缩分界线 (compact_boundary)：仅 flush 正在累积的 turn，不清空 index
                    # 保留历史 thinking 供 boundary 后的保留消息（preserved messages）匹配，杜绝压缩后首轮掉 thinking
                    if dtype == "system" and d.get("subtype") == "compact_boundary":
                        flush_turn()
                        continue

                    msg = d.get("message") or {}
                    role = msg.get("role") or ("assistant" if dtype == "assistant" else ("user" if dtype in ("user", "tool_result") else None))
                    if not role:
                        continue

                    if role in ("user", "tool_result") or dtype in ("user", "tool_result"):
                        flush_turn()
                    elif role == "assistant" or dtype == "assistant":
                        c = msg.get("content") or []
                        if isinstance(c, list):
                            for b in c:
                                if not isinstance(b, dict):
                                    continue
                                btype = b.get("type")
                                if btype == "thinking":
                                    current_turn_thinking.append(dict(b))
                                elif btype == "tool_use" and b.get("id"):
                                    current_turn_tools.append(b["id"])
                                elif btype == "text" and b.get("text"):
                                    current_turn_texts.append(b["text"].strip()[:100])
                        elif isinstance(c, str):
                            current_turn_texts.append(c.strip()[:100])

            flush_turn()
        except OSError:
            return {}

        # 注意判据是 >=：检查发生在「插入本条之前」，用 > 会让实际上界变成 MAX+1
        if len(_transcript_cache) >= _TRANSCRIPT_CACHE_MAX:
            for k in list(_transcript_cache.keys())[:_TRANSCRIPT_CACHE_BATCH]:
                _transcript_cache.pop(k, None)
        entry["offset"] = offset
        entry["size"] = cur_size
        entry["mtime"] = cur_mtime
        entry["index"] = index
        _transcript_cache[tpath] = entry

        return index


def prepare_request(parsed, session_id, rules=None, tool_pruning_cfg=None):
    """主请求前置稳定化预处理：
    1. 稳定工具顺序 + 模块化工具裁剪 (Tool Pruning)
    2. 稳定 tool_result 消息顺序 (消除并发工具调用返回乱序)
    3. 回填被客户端剥离的 thinking blocks (Thinking Restoration)
    """
    changed = []
    r = rules or load_rules_config()

    # 1. 模块化动态工具精简（剥掉桌面端强制注入、多数场景用不到的那几类）
    #    完全保留客户端原生注册的工具物理顺序与 Schema 参数顺序，不做任何人为重排
    tools = parsed.get("tools")
    if isinstance(tools, list):
        if tool_pruning_cfg is not None:
            pruning_cfg = dict(DEFAULT_TOOL_PRUNING_CONFIG)
            pruning_cfg.update(tool_pruning_cfg)
        else:
            pruning_cfg = load_tool_pruning_config()
        stripped_names = get_stripped_tool_names(pruning_cfg)
        filtered_tools = [
            t for t in tools
            if isinstance(t, dict) and not should_strip_tool(t.get("name", ""), stripped_names, pruning_cfg)
        ]
        if len(filtered_tools) != len(tools):
            pruned_count = len(tools) - len(filtered_tools)
            parsed["tools"] = filtered_tools
            tools = filtered_tools
            changed.append(f"prune_tools_{pruned_count}")

    # 2. 稳定 tool_result 与 assistant tool_use 顺序 (消除并发工具调用/返回乱序导致的前缀变化)
    #    Claude Code 使用 Anthropic 协议：并发工具返回位于单条消息内，严格按 id 排序保证对称稳定
    #    这是一条独立规则（可关）：它不删内容、只重排，但重排本身会让这一轮的字节与写进缓存的
    #    那份不一致 —— 关掉它缓存会掉，只是有些上游对块顺序敏感，留一个逃生口。
    incoming_msgs = parsed.get("messages")
    if r.get("tool_block_ordering", True) and isinstance(incoming_msgs, list):
        for m in incoming_msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if not isinstance(content, list) or len(content) <= 1:
                continue
            if role == "user":
                has_tool_results = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                if has_tool_results:
                    orig_ids = [b.get("tool_use_id", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                    # 稳定排序：保持非 tool_result 块在前，tool_result 块严格按 tool_use_id 排序
                    content.sort(key=lambda b: (0 if (isinstance(b, dict) and b.get("type") != "tool_result") else 1, b.get("tool_use_id", "") if isinstance(b, dict) else ""))
                    new_ids = [b.get("tool_use_id", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                    if orig_ids != new_ids:
                        changed.append("tool_result_blocks_sorted_%d" % len(orig_ids))
            elif role == "assistant":
                has_tool_uses = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
                if has_tool_uses:
                    orig_ids = [b.get("id", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                    # 稳定排序：保持 thinking/text 块在前，tool_use 块严格按 id 排序
                    content.sort(key=lambda b: (0 if (isinstance(b, dict) and b.get("type") != "tool_use") else 1, b.get("id", "") if isinstance(b, dict) else ""))
                    new_ids = [b.get("id", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                    if orig_ids != new_ids:
                        changed.append("tool_use_blocks_sorted_%d" % len(orig_ids))

    if not r.get("restore_thinking", True) or not session_id or not isinstance(incoming_msgs, list) or not incoming_msgs:
        return changed

    thinking_index = None

    # 统一读取 transcript 获取 thinking 索引（只调一次 glob + parse，带 mtime 内存缓存）
    tpath = find_session_transcript(session_id)
    if tpath:
        thinking_index = parse_transcript_data(tpath)

    # 先数一遍「历史里有几条 assistant 消息是没带 thinking 的」——不管索引建没建起来。
    #
    # 为什么这一步必须独立于后面的回填：以前只有回填成功才留痕，于是日志里
    # 「一条回填记录都没有」既可能是「客户端没剥」（正常），也可能是「剥了、但我们的
    # 索引没匹配上」（功能失效）—— 这两种情况在日志里长得一模一样，谁也没法判断
    # 这条规则到底有没有在工作。分不清，就没法回答「它多长时间剥一次」这个问题。
    bare_assistant = 0
    for m in incoming_msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            bare_assistant += 1
            continue
        if not isinstance(c, list):
            continue
        has_body = any(isinstance(b, dict) and b.get("type") in ("text", "tool_use") for b in c)
        has_think = any(isinstance(b, dict) and b.get("type") == "thinking" for b in c)
        if has_body and not has_think:
            bare_assistant += 1

    if bare_assistant:
        changed.append("bare_thinking_%d" % bare_assistant)

    if not thinking_index:
        if bare_assistant:
            # 有该补的、索引却没建起来 —— 这是真故障，不是「没发生」
            changed.append("thinking_index_missing")
        return changed

    # 4. Thinking 确定性归一化与智能全量补齐
    #    必须与索引字典做整数组比对：跳过式粗筛（例如「已有 thinking 就 continue」）会漏判，导致缓存掉落
    restored_count = 0
    unmatched = 0
    if thinking_index:
        for m in incoming_msgs:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str):
                c = [{"type": "text", "text": c}]
                m["content"] = c
            if not isinstance(c, list):
                continue
            current_thinking = [b for b in c if isinstance(b, dict) and b.get("type") == "thinking"]
            thinking_to_inject = None
            # 1. 优先按 tool_use 的 UUID 匹配（绝对唯一主键）
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    key = "tool:" + b["id"]
                    if key in thinking_index:
                        thinking_to_inject = thinking_index[key]
                        break
            # 2. 仅当本消息完全没有匹配到 tool_use 时，才 fallback 到纯文本查找
            if not thinking_to_inject:
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        key = "text:" + b["text"].strip()[:100]
                        if key in thinking_index:
                            thinking_to_inject = thinking_index[key]
                            break
            if thinking_to_inject:
                if current_thinking != thinking_to_inject:
                    m["content"] = [dict(tb) for tb in thinking_to_inject] + [
                        b for b in c if not (isinstance(b, dict) and b.get("type") == "thinking")
                    ]
                    restored_count += 1
            elif current_thinking:
                pass
            else:
                # 这条历史没有 thinking、索引里也找不到它的原件 —— 补不回来。
                # 它不是「没发生」，是「发生了但我们没接住」，必须和成功分开记，
                # 否则永远看不出这条规则的失效。
                unmatched += 1

    if restored_count:
        changed.append(f"restore_thinking_{restored_count}")
    if unmatched:
        changed.append(f"restore_thinking_miss_{unmatched}")

    return changed


class UsageCapture:
    """流式与非流式 usage 提取器：SSE 用增量+尾部窗口；非流式 JSON 完整累积（带上限）。"""
    WINDOW_SIZE = 8192  # SSE 尾部滑动窗口大小（字节）
    BODY_CAP = 32 * 1024 * 1024  # 非流式完整累积上限，超过后退化为尾部窗口

    def __init__(self):
        self._usage = {}
        self._head = b""
        self._tail = b""
        self._tail_len = 0
        self._is_sse = None
        self._total_bytes = 0
        self._expected_length = None

    def set_expected_length(self, length: int):
        """设置预期的响应体字节数（来自 Content-Length 响应头）。"""
        self._expected_length = length

    def feed(self, chunk: bytes):
        """增量接收字节流并提取 usage。"""
        self._total_bytes += len(chunk)
        if self._is_sse is None and chunk:
            stripped = chunk.lstrip()
            if stripped:
                self._is_sse = stripped.startswith(b"event:") or stripped.startswith(b"data:")

        # SSE 流式：头部提取 message_start
        if self._is_sse and not self._usage:
            self._head += chunk
            text = self._head.decode("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].lstrip())
                    except Exception:
                        continue
                    if d.get("type") == "message_start":
                        m = d.get("message", {})
                        if m.get("usage") and isinstance(m["usage"], dict):
                            self._usage.update(m["usage"])
                            self._head = b""
                            break
            if len(self._head) > self.WINDOW_SIZE:
                # 保留最后半个窗口（或未完结行），防止 message_start 跨 8KB 分块边界被整块清空
                self._head = self._head[-self.WINDOW_SIZE // 2:]

        # 非流式 JSON：完整累积，保证超过尾部窗口的大响应也能解析出 usage
        if self._is_sse is False:
            self._tail += chunk
            self._tail_len += len(chunk)
            if self._tail_len > self.BODY_CAP:
                self._tail = self._tail[-self.WINDOW_SIZE * 2:]
                self._tail_len = len(self._tail)
            return

        # 尾部滑动窗口：保留最后 WINDOW_SIZE 字节
        self._tail += chunk
        self._tail_len += len(chunk)
        if self._tail_len > self.WINDOW_SIZE * 2:
            self._tail = self._tail[-self.WINDOW_SIZE:]
            self._tail_len = len(self._tail)

    def get_usage(self):
        """解析并返回最终聚合的 usage 字典。"""
        # 1. 非流式纯 JSON 响应
        if self._is_sse is False:
            try:
                data = json.loads(self._tail.decode("utf-8", "replace"))
                if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                    return data["usage"]
            except Exception:
                pass

        # 2. SSE 流式响应：从尾部窗口提取 message_delta / usage
        usage = dict(self._usage)
        text = self._tail.decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:].lstrip())
                except Exception:
                    continue
                if d.get("type") == "message_delta":
                    if d.get("usage") and isinstance(d["usage"], dict):
                        usage.update(d["usage"])
                elif d.get("type") == "ping" and isinstance(d.get("usage"), dict):
                    usage.update(d["usage"])
        return usage if usage else None

    def stream_complete(self):
        """流是否正常收尾（读到 EOF 不等于收到完整回答）。

        - 非流式 JSON：http.client.HTTPResponse.read1 在非 chunked 且有 Content-Length 时，
          对端提前断开直接静默返回空字节，不做长度校验、不抛 IncompleteRead。因此必须显式
          校验收到的字节数达到 Content-Length，且完整报文必须能被 JSON 解析成功。
        - SSE：必须见到终态事件（Anthropic 的 message_stop / OpenAI 的 [DONE]）。
          上游中途被切断时 read 会正常返回 EOF，不报任何异常，故只能靠在尾部
          滑动窗口里找终态事件来区分。窗口始终保留流末尾，终态事件必然落在其中。
        - 一个字节都没收到（_is_sse 仍为 None 或 _total_bytes == 0）：空响应，不算完整。
        """
        if self._is_sse is None or self._total_bytes == 0:
            return False
        if self._is_sse is False:
            if self._expected_length is not None and self._total_bytes < self._expected_length:
                return False
            try:
                json.loads(self._tail.decode("utf-8", "replace"))
                return True
            except Exception:
                return False
        return b"message_stop" in self._tail or b"[DONE]" in self._tail


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_html(self, status, html, extra_headers=None):
        """配置台页面。与 _send_json 同构，另加几条只对本机页面有意义的防护头。"""
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # 页面自包含（无 CDN、无外链），所以只放行内联样式与脚本
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; img-src data:")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _new_req_id(self):
        """每个请求独立生成 12 字符 request_id，串联同一次请求的多条日志。"""
        return "req_" + uuid.uuid4().hex[:12]

    def do_GET(self):
        self._req_id = self._new_req_id()
        if self.path.split("?")[0] == "/v1/models":
            cur_models = read_models_config()
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m, "object": "model"} for m in sorted(cur_models.keys())],
            })
            ua = self.headers.get("User-Agent", "")
            if "Python-urllib" not in ua:
                self._log(
                    ua=ua,
                )
            return
        # 配置台：走本地页面那几条路由（自己带 Host/token 门禁），未命中再 404
        if webui.handle_get(self, parse_qs(urlparse(self.path).query)):
            return
        self._send_json(404, {"error": "not found"})

    def _save_raw_inbound(self, parsed, kind=None):
        """保存客户端入口原始报文（未清洗、未转译模型、未裁剪工具的 100% 原版）。
        受 ~/.claude-tuner/config.json 中 'capture_raw_inbound' 开关控制（开着就抓，关了不抓）。
        """
        try:
            if not isinstance(parsed, dict) or is_probe_request(parsed):
                return
            # 过滤会话标题生成等非主交互的轻量后台辅助请求（杜绝冲刷覆盖主会话的完整开发快照）
            if parsed.get("max_tokens", 0) <= 200 and not parsed.get("tools"):
                return
            # 复用带 mtime 缓存的 _read_config()，保证默认关闭时保持零磁盘读取
            if not _read_config().get("capture_raw_inbound", False):
                return

            if kind is None:
                kind = classify_kind(parsed)
            capture_dir = os.path.join(BASE_DIR, "captures")
            os.makedirs(capture_dir, exist_ok=True)
            model_raw = str(parsed.get("model") or "unknown").replace("/", "_").replace(":", "_")
            suffix = "_subagent" if kind == "subagent" else ""
            # 1. 统一保存该模型的最新专属快照 (子代理保存为 _subagent.json，避免覆盖主会话快照)
            model_path = os.path.join(capture_dir, f"latest_raw_inbound_{model_raw}{suffix}.json")
            with open(model_path, "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, ensure_ascii=False, indent=2)
            # 2. 同时更新一份全局主会话最新快照 (仅主会话更新)
            if kind != "subagent":
                latest_path = os.path.join(capture_dir, "latest_raw_inbound.json")
                with open(latest_path, "w", encoding="utf-8") as fh:
                    json.dump(parsed, fh, ensure_ascii=False, indent=2)
            # 3. 按会话归档一份 (唯一不会被别的对话顶掉的那份，见 save_session_capture 注释)
            save_session_capture(capture_dir, parsed, kind)
        except Exception:
            pass

    def do_POST(self):
        """POST /v1/messages 主入口：解析 → 探测拦截 → 缓存稳定化 → 上游转发。"""
        self._req_id = self._new_req_id()
        started = time.time()
        if self.path.split("?")[0] != "/v1/messages":
            # 配置台的写接口：不参与稳定化，也不进消息日志
            parsed, err = self._parse_body()
            if err:
                return
            if webui.handle_post(self, parse_qs(urlparse(self.path).query), parsed):
                return
            self._send_json(404, {"error": "not found"})
            return
        parsed, err = self._parse_body()
        if err:
            return

        original = parsed.get("model", "")
        session_id = extract_session_id(parsed)
        x_opencode_session = "claude-" + (session_id or "unknown")
        mapped = map_model(original)
        kind = classify_kind(parsed)
        # 捕获 100% 原始入口报文快照（未经任何模型改写、日期清洗与工具精简）
        self._save_raw_inbound(parsed, kind=kind)
        self._log_inbound(parsed, original, session_id, x_opencode_session, kind, mapped)
        rules = load_rules_config()
        if rules.get("probe_interception", True) and is_probe_request(parsed):
            self._handle_probe(parsed, mapped, session_id, kind)
            return
        self._stabilize(parsed, session_id, kind, mapped)
        self._forward(parsed, mapped, started, session_id, kind)

    def _parse_body(self):
        """读取并解析请求体；50MB 上限保护 + 失败时回 4xx。

        返回 (parsed, err_flag)：err_flag=True 时已就地回写错误响应，调用方直接 return。
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY_BYTES:
                self._send_json(413, {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "Request entity too large (max 50MB)"},
                })
                return None, True
            body = self.rfile.read(length) if length else b"{}"
            # 留存原始字节：稳定化中途失败时据此回滚到未改动的请求（见 _stabilize）
            self._raw_body = body
            parsed = json.loads(body.decode("utf-8"))
            # 解析成功不等于形状正确：[] / null / "abc" / 123 都是合法 JSON，
            # 但 do_POST 之后一律按 dict 取值，非 dict 会抛 AttributeError 且无处兜底。
            if not isinstance(parsed, dict):
                self._log(
                    event="invalid_json",
                    reason="top_level_not_object",
                )
                self._send_json(400, {"type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": "Request body must be a JSON object"}})
                return None, True
            return parsed, False
        except Exception:
            self._log(
                event="invalid_json",
            )
            self._send_json(400, {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "invalid JSON body"},
            })
            return None, True

    def _log(self, **fields):
        """记录当前请求的结构化日志，统一注入 method / path / client / request_id。"""
        log_event(dict(method="POST", path=self.path,
                       client=self.client_address[0], **fields),
                  req_id=self._req_id)

    def _log_inbound(self, parsed, original, session_id, x_opencode_session, kind, mapped):
        """写一条结构化入口日志（每次请求一行 JSON）。"""
        entry = {
            # ts / method / path / client 由 _log() 与 log_event() 统一注入
            "model_requested": original,
            "model_mapped": mapped,
            "session_id": session_id,
            "x_opencode_session": x_opencode_session,
            "messages": len(parsed.get("messages", []) or []),
            "max_tokens": parsed.get("max_tokens"),
            "stream": parsed.get("stream"),
            "tools": len(parsed.get("tools", []) or []),
            "kind": kind,
            "ua": self.headers.get("User-Agent", ""),
        }
        if kind == "subagent":
            sub_id, sub_task = extract_subagent_fingerprint(parsed)
            entry["subagent_id"] = sub_id
            entry["subagent_task"] = sub_task
        self._log(**entry)

    def _handle_probe(self, parsed, mapped, session_id, kind):
        """本地秒回探测请求：max_tokens=1 且非流式且单条 user 消息 → 零成本应答。"""
        resp = make_probe_response(parsed)
        self._log(
            model_requested=parsed.get("model", ""),
            model_mapped=mapped,
            session_id=session_id,
            kind=kind,
            event="probe_local",
            estimated_input_tokens=resp["usage"]["input_tokens"],
        )
        self._send_json(200, resp)

    def _stabilize(self, parsed, session_id, kind, mapped):
        """缓存稳定化预处理（失败回滚并降级放行，绝不阻断主请求）。"""
        rules = load_rules_config()
        tp_cfg = load_tool_pruning_config()
        try:
            stabilize_changed = prepare_request(parsed, session_id, rules=rules, tool_pruning_cfg=tp_cfg)
            changed = canonicalize(parsed, rules=rules, tool_pruning_cfg=tp_cfg)
            changed = stabilize_changed + changed
        except Exception as exc:
            # 两个稳定化函数都原地修改 parsed，失败必须回滚到原始报文：
            # 转发半成品会让同一段对话在不同轮次发出不同字节。
            changed = []
            try:
                parsed.clear()
                parsed.update(json.loads(self._raw_body.decode("utf-8")))
            except Exception:
                self._log(
                    event="stabilize_rollback_failed",
                )
            self._log(
                model_mapped=mapped,
                session_id=session_id,
                kind=kind,
                event="stabilize_error",
                error=str(exc),
            )
        sys_hash, tools_hash = prefix_hashes(parsed)
        # 每轮均记录，以便在 changes 为空时仍能通过 hash 发现客户端侧的前缀漂移
        self._log(
            model_mapped=mapped,
            session_id=session_id,
            kind=kind,
            event="canonicalized",
            changes=changed,
            sys_hash=sys_hash,
            tools_hash=tools_hash,
        )

    def _get_upstream_timeout(self):
        """解析上游请求超时时间（优先 env CLAUDE_TUNER_UPSTREAM_TIMEOUT，次优先 config.json，兜底 DEFAULT_CLAUDE_TUNER_UPSTREAM_TIMEOUT）。"""
        if "CLAUDE_TUNER_UPSTREAM_TIMEOUT" in os.environ:
            try:
                return max(5, int(os.environ["CLAUDE_TUNER_UPSTREAM_TIMEOUT"]))
            except ValueError:
                pass
        cfg = _read_config()
        try:
            return max(5, int(cfg.get("upstream_timeout", DEFAULT_CLAUDE_TUNER_UPSTREAM_TIMEOUT)))
        except (ValueError, TypeError):
            return DEFAULT_CLAUDE_TUNER_UPSTREAM_TIMEOUT

    def _forward(self, parsed, mapped, started, session_id, kind):
        """上游转发总入口：解析 URL → 构造请求 → 流式回写 → 错误兜底。"""
        parsed["model"] = mapped
        upstream_url = self._resolve_upstream_url()
        if not upstream_url:
            return
        req, payload = self._build_upstream_request(parsed, upstream_url, session_id=session_id)
        timeout = self._get_upstream_timeout()
        self._headers_sent = False
        try:
            with urllib.request.urlopen(req, timeout=timeout) as upstream:
                self._stream_response(upstream, parsed, started, session_id, kind)
        except urllib.error.HTTPError as exc:
            self._handle_http_error(exc, parsed, started, session_id, kind)
        except Exception as exc:
            self._handle_unknown_error(exc, parsed, started, session_id, kind)

    def _resolve_upstream_url(self):
        """解析最终上游 URL（env 优先，config.json 兜底）；未配置时本地 503 兜底并返回 None。"""
        upstream_url = normalize_upstream(os.environ.get("CLAUDE_TUNER_UPSTREAM_URL") or load_upstream())
        if not upstream_url:
            self._send_json(503, {
                "type": "error",
                "error": {
                    "type": "configuration_error",
                    "message": "upstream_url is empty. Re-save the 3P endpoint or edit ~/.claude-tuner/config.json",
                },
            })
            return None
        return upstream_url

    def _build_upstream_request(self, parsed, upstream_url, session_id=None):
        """序列化请求体并组装转发 Headers。

        请求头分两档：
          1. 基础头       —— 上游通用协议头（白名单重建，客户端指纹类头一律不转发）；
          2. 客户端标识头 —— 恒发自有 User-Agent；OpenCode 系上游另带其会话标识头。

        刻意不给上游发 anthropic-version：各家上游对它的接受度不一致，发了反而可能被拒。
        """
        payload = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        if session_id is None:
            session_id = extract_session_id(parsed)
        headers = {
            "Content-Type": "application/json",
            "Connection": "close",
            "x-api-key": client_api_key(self.headers),
            "Accept": self.headers.get("Accept", "application/json"),
        }

        # 第 2 档：恒发自有 UA；仅 OpenCode 系上游额外附带会话标识头（见 common.py CLIENT_UA）
        headers.update(upstream_client_headers(
            session_id, opencode=is_opencode_upstream(upstream_url)))

        req = urllib.request.Request(upstream_url, data=payload, headers=headers, method="POST")
        return req, payload

    def _stream_response(self, upstream, parsed, started, session_id, kind):
        """流式回写客户端 + UsageCapture 聚合 usage + 成功/断连日志。"""
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        rid = upstream.headers.get("request-id")
        if rid:
            self.send_header("request-id", rid)
        self.end_headers()
        self._headers_sent = True
        cap = UsageCapture()
        cl = upstream.headers.get("Content-Length")
        if cl and cl.isdigit():
            cap.set_expected_length(int(cl))
        client_gone = False
        upstream_read_error = None
        while True:
            try:
                chunk = upstream.read1(65536)
            except (BrokenPipeError, ConnectionResetError, OSError,
                    http.client.HTTPException) as exc:
                # 读上游失败（含 socket.timeout）属上游/链路异常，须与写客户端断连分开记账
                upstream_read_error = exc
                break
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                client_gone = True
                break
            cap.feed(chunk)
        elapsed_ms = int((time.time() - started) * 1000)
        if client_gone:
            self._log(
                model_mapped=parsed["model"],
                session_id=session_id,
                kind=kind,
                event="client_disconnected",
                elapsed_ms=elapsed_ms,
            )
            return
        if upstream_read_error is not None:
            self._log(
                model_mapped=parsed["model"],
                session_id=session_id,
                kind=kind,
                event="upstream_stream_error",
                error=str(upstream_read_error),
                headers_sent=self._headers_sent_flag(),
                elapsed_ms=elapsed_ms,
            )
            return
        usage = cap.get_usage()
        complete = cap.stream_complete()
        if not complete:
            # 读到 EOF 不等于回答完整：上游中途切断时既无异常也无终态事件。
            # 单独记一条，避免把截断的流混进「成功轮次」参与归因。
            self._log(
                model_mapped=parsed["model"],
                session_id=session_id,
                kind=kind,
                event="upstream_truncated",
                status=upstream.status,
                stream_complete=False,
                headers_sent=self._headers_sent_flag(),
                elapsed_ms=elapsed_ms,
                usage=usage or {},
            )
            return

        ok_event = {
            # ts / method / path / client 由 _log() 与 log_event() 统一注入
            "model_mapped": parsed["model"],
            "session_id": session_id,
            "kind": kind,
            "event": "upstream_ok",
            "status": upstream.status,
            "stream_complete": True,
            "elapsed_ms": elapsed_ms,
            "usage": usage or {},
        }
        # 上游 request-id 以 upstream_request_id 独立记录，避免覆盖内部串联用的 request_id
        upstream_rid = upstream.headers.get("request-id")
        if upstream_rid:
            ok_event["upstream_request_id"] = upstream_rid
        self._log(**ok_event)

    def _headers_sent_flag(self):
        """返回响应头是否已发往客户端（用于区分异常发生时是否仍有重试余地）。"""
        return bool(getattr(self, "_headers_sent", False))

    def _handle_http_error(self, exc, parsed, started, session_id, kind):
        """上游 4xx/5xx 错误透传 + 结构化日志。"""
        detail = exc.read().decode("utf-8", "replace")
        self._log(
            model_mapped=parsed["model"],
            session_id=session_id,
            kind=kind,
            event="upstream_http_error",
            status=exc.code,
            # 上游的报错原话：没有它，4xx 只有状态码，归因只能靠猜
            error_body=detail[:ERROR_BODY_LOG_CHARS],
            headers_sent=self._headers_sent_flag(),
            elapsed_ms=int((time.time() - started) * 1000),
        )
        if getattr(self, "_headers_sent", False):
            return
        self.send_response(exc.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self._headers_sent = True
        try:
            self.wfile.write(detail.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_unknown_error(self, exc, parsed, started, session_id, kind):
        """网络断流/连接重置等非 HTTP 异常的统一兜底 → 502 + 日志。"""
        self._log(
            model_mapped=parsed["model"],
            session_id=session_id,
            kind=kind,
            event="upstream_error",
            error=str(exc),
            headers_sent=self._headers_sent_flag(),
            elapsed_ms=int((time.time() - started) * 1000),
        )
        if not getattr(self, "_headers_sent", False):
            try:
                self._send_json(502, {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"Upstream service error or network timeout: {exc}",
                    },
                })
            except (BrokenPipeError, ConnectionResetError):
                pass


if __name__ == "__main__":
    ensure_config_initialized(CONFIG_FILE)
    cleanup_runtime()

    # 端口由 manager 独占决定（它负责顺延扫描，并把地址回写 config.json 与全部 3P 配置），
    # 这里只认它传来的值。网关不再自己另选端口 —— 否则选出的端口可能与
    # manager 已写进各处配置的地址不一致，客户端就会打到空端口上。
    raw_port = os.environ.get("CLAUDE_TUNER_PORT") or _read_config().get("gateway_port") or DEFAULT_PORT
    try:
        PORT = int(raw_port)
    except (TypeError, ValueError):
        PORT = DEFAULT_PORT

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print("gateway 启动失败（端口 %d 不可用）: %s" % (PORT, exc), flush=True)
        raise SystemExit(1)

    # 写入自身真实 PID（确保 manager 启停与状态完全对齐）
    pid_file = os.path.join(BASE_DIR, "gateway.pid")
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    print("claude-gateway listening on http://%s:%d" % (HOST, PORT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
