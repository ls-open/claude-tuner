#!/usr/bin/env python3
"""claude-tuner 本地进程管理器：gateway（中转）。保活由 hooks UserPromptSubmit 自动处理。"""
import json
import glob
import os
import socket
import subprocess
import sys
import time
import urllib.request

# 本脚本由客户端 Hook 高频调用，若写字节码缓存会在插件运行目录留下 __pycache__。
# 代码以仓库为准且改动频繁，缓存无收益（省几毫秒），只带来「跑的是不是新代码」的疑问。
sys.dont_write_bytecode = True

import platform_support  # 进程存活 / 锁 / 脱离父进程 —— 平台差异只在那里写一份

from common import (
    DEFAULT_LOG_RETENTION_DAYS,
    configure_stdio,
    STOP_FILE_NAME,
    LIFETIME_LOCK_NAME,
    RUNTIME_INFO_NAME,
    STOP_GRACE_SECONDS,
    DEFAULT_PORT,
    THREEP_CONFIG_GLOB,
    atomic_write_json,
    canonical_local_url,
    clamp_retention_days,
    ensure_config_initialized,
    find_active_3p_config_path,
    normalize_upstream,
    purge_old_logs,
)

BASE = os.environ.get("CLAUDE_TUNER_HOME", os.path.expanduser("~/.claude-tuner"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 客户端的插件加载位置：只有 skills/<name>/ 这一种。
# 它才算「安装副本」，有资格接管运行时；仓库工作副本不在其中（见 is_installed_copy）。
INSTALL_ROOTS = (os.path.expanduser("~/.claude/skills"),)
GATEWAY = os.path.join(SCRIPT_DIR, "gateway.py")
GATEWAY_PID = os.path.join(BASE, "gateway.pid")
CONFIG = os.path.join(BASE, "config.json")
PORT_PREFERRED = int(os.environ.get("CLAUDE_TUNER_PORT", str(DEFAULT_PORT)))
PORT_SCAN_LIMIT = 20   # 最多向上扫描 20 个端口（8789~8808）
QUIET = False          # main() 按 --quiet 覆盖；模块级留默认值，否则 log_msg 在 main 之外一调就 NameError
MAX_STDOUT_LOG_BYTES = 32 * 1024 * 1024   # stdout 副本（gateway.out.log）单份上限，启动时轮转


def find_free_port(preferred=PORT_PREFERRED, limit=PORT_SCAN_LIMIT):
    """从 preferred 开始向上扫描，返回第一个可绑定的端口号。"""
    for port in range(preferred, preferred + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # 兜底：让 OS 分配一个随机端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_config():
    """读取 config.json；文件不存在或损坏时返回 {}（启动路径禁止因配置损坏抛异常）。"""
    try:
        if os.path.exists(CONFIG):
            with open(CONFIG, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def save_config(cfg):
    """原子写入 config.json；写入失败静默忽略，避免阻塞网关启动与同步路径。"""
    os.makedirs(BASE, exist_ok=True)
    try:
        atomic_write_json(CONFIG, cfg, indent=1)
    except OSError:
        pass


def load_port():
    """从 config.json 读取上次选定的端口；不存在则返回 None。"""
    p = load_config().get("gateway_port")
    return p if isinstance(p, int) and p > 0 else None


def save_port(port):
    """把端口号写入 config.json（与其他字段合并，原子写）。"""
    cfg = load_config()
    cfg["gateway_port"] = port
    save_config(cfg)


def effective_port():
    """返回当前网关配置端口（优先 config.json 记录值，否则回退默认 8789）。"""
    return load_port() or PORT_PREFERRED


def resolve_port():
    """决定本次要用的端口：网关已在跑就沿用它记录的端口，否则从 8789 起扫描空闲端口并持久化。

    端口是 manager 独占的决策权：它选完会回写 config.json 与全部 3P 配置地址，
    再通过 CLAUDE_TUNER_PORT 传给网关；网关自己不再另选，避免两者选出不同端口。
    """
    port = load_port()
    if port and gateway_alive():
        return port
    port = find_free_port()
    save_port(port)
    return port


def ensure_dirs():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    ensure_config_initialized(CONFIG)


def acquire_lock():
    """跨进程互斥（flock）：多个会话同时 SessionStart 时，sync/spawn 串行执行。
    持有者进程退出时内核自动释放，不存在陈旧锁；最多等待 3 秒，
    超时则放弃（返回 None），由各操作自身的幂等守卫兜底，绝不无锁抢跑删他人锁。"""
    ensure_dirs()
    lock_path = os.path.join(BASE, "spawn.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    deadline = time.time() + 3.0
    while True:
        try:
            if platform_support.lock_file(fd):
                break
            raise OSError("lock busy")
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                log_msg("警告：等待 spawn 锁超时（其他实例正在执行），本次跳过")
                return None
            time.sleep(0.01)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd


def release_lock(fd):
    if fd is None:
        return
    platform_support.unlock_file(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def read_pid(pid_file):
    """读出 pid 文件里的进程号；文件缺失或内容坏掉时返回 None。"""
    try:
        with open(pid_file, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def write_runtime_info(pid, script, port=None):
    """记下「谁在跑、跑的是哪个脚本」。由 manager 在拉起时写 —— 这样**老版本的网关**
    也有记录（它自己不写），升级时才知道端口上那个进程该不该动。"""
    try:
        atomic_write_json(os.path.join(BASE, RUNTIME_INFO_NAME), {
            "pid": pid,
            "port": port or effective_port(),
            "script": os.path.abspath(script),
            "started_at": int(time.time()),
        })
    except Exception:
        pass


def _runtime_info():
    try:
        with open(os.path.join(BASE, RUNTIME_INFO_NAME), "r", encoding="utf-8") as fh:
            info = json.load(fh)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def pid_is_ours(pid):
    """该 pid 现在是否仍是我们自己的网关进程。

    判据是**归属锁**：网关启动时拿住 `gateway.lock` 直到进程结束，进程一死内核就释放。
    所以「锁还被人握着 + 这个 pid 还活着」= 它就是我们的网关；锁没被握 = 那个进程早没了，
    现在这个 pid 是系统回收后复用的别人 —— 绝不能杀。

    为什么不读命令行：`ps`/PowerShell 各写一套、路径带空格会被切碎、每次还要起子进程，
    而这三件事在 Windows 上全都会咬人（真机上真踩过路径被切碎）。
    """
    if not platform_support.is_alive(pid):
        return False
    if platform_support.lock_is_held(os.path.join(BASE, LIFETIME_LOCK_NAME)):
        return True
    # 退化路径：0.8.0 及更早的网关不拿这把锁，升级那一刻如果只认锁，
    # 就会得出「端口上那个进程不是我们的」—— 于是永远停不掉、也永远升不了级。
    # 这时退回「账本对得上」：pid 与脚本路径都与 manager 当初写的一致才算。
    info = _runtime_info()
    if info.get("pid") == pid and os.path.basename(str(info.get("script") or "")) == os.path.basename(GATEWAY):
        return True
    # 一次性升级过渡：0.8.0 及更早的网关既不持锁、也没有运行时记录（记录是 manager 新写的）。
    # 这时只剩一个判据 —— 端口在应答我们的网关协议。pid 又来自我们自己写的 pid 文件，
    # 两者都对得上就按「是我们的」处理；否则老版本永远停不掉，也就永远升不上来。
    # 下一个大版本可以把这条删掉（届时所有在跑的实例都会持锁）。
    return gateway_alive()


def gateway_alive():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open("http://127.0.0.1:%d/v1/models" % effective_port(), timeout=2)
        return True
    except Exception:
        return False


def is_installed_copy():
    """判断当前 manager 是否运行在客户端安装目录（skills/<name>）下。

    仅安装副本允许接管运行时并写入 runtime_script_dir，防止在源码目录直接执行时污染线上网关与 sync.py 推送目标。
    """
    try:
        here = os.path.abspath(SCRIPT_DIR)
        return any(here.startswith(os.path.abspath(root) + os.sep) for root in INSTALL_ROOTS)
    except Exception:
        return False


def check_runtime_dir():
    """当安装副本的代码路径变化（如插件升级）时，自动重启本地网关并更新 runtime_script_dir。"""
    if not is_installed_copy():
        log_msg("源码副本（%s）：跳过运行时接管，不影响正在运行的安装副本" % SCRIPT_DIR)
        return
    ensure_dirs()
    lock = acquire_lock()
    try:
        cfg = load_config()
        old = cfg.get("runtime_script_dir")
        if old and old != SCRIPT_DIR:
            log_msg("插件代码路径变化：%s -> %s，重启本地服务" % (old, SCRIPT_DIR))
            stop(GATEWAY_PID, "gateway")
        cfg["runtime_script_dir"] = SCRIPT_DIR
        save_config(cfg)
    finally:
        release_lock(lock)


def find_3p_configs():
    """桌面版 3P 配置文件：包含 inferenceGatewayBaseUrl 的 configLibrary JSON。
    按路径排序以保证遍历顺序确定性；若存在 _meta.json 指定的当前生效配置，将其排在最后，
    确保多配置文件并存时 cfg["upstream_url"] 优先取自当前生效配置。
    """
    active_path = find_active_3p_config_path()
    found = []
    for path in sorted(glob.glob(THREEP_CONFIG_GLOB)):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("inferenceGatewayBaseUrl"):
            found.append((path, data))
    if active_path and len(found) > 1:
        found.sort(key=lambda item: 1 if item[0] == active_path else 0)
    return found


def sync():
    """3P 填真实供应商地址，插件自动改写成本地网关并记住上游。
    只在 3P 配置文件变更（mtime 更新）时才处理，其余会话直接跳过。"""
    ensure_dirs()
    lock = acquire_lock()
    try:
        return _sync_locked()
    finally:
        release_lock(lock)


def _sync_locked():
    cfg = load_config()

    port = resolve_port()
    cfg["gateway_port"] = port  # 同步更新内存中的 cfg
    cur_local_url = "http://127.0.0.1:%d" % port

    # 检查 3P 配置文件是否有外部修改或端口错位
    last_sync = cfg.get("last_sync_ts") or 0
    newest = 0.0
    meta_path = os.path.join(os.path.dirname(THREEP_CONFIG_GLOB), "_meta.json")
    try:
        if os.path.isfile(meta_path):
            newest = max(newest, os.path.getmtime(meta_path))
    except OSError:
        pass
    configs_3p = find_3p_configs()
    port_mismatch = False
    for path, data in configs_3p:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            pass
        norm_url = canonical_local_url(data.get("inferenceGatewayBaseUrl"))
        if norm_url != cur_local_url:
            port_mismatch = True

    # 只有当：上游已存在、且 3P 文件无外部修改、且所有 3P 配置的端口与当前网关端口完全一致时，才跳过
    if last_sync and newest <= last_sync and not port_mismatch:
        upstream = cfg.get("upstream_url")
        if upstream:
            return upstream

    changed = []

    # 桌面版 3P 配置写入
    for path, data in configs_3p:
        url = (data.get("inferenceGatewayBaseUrl") or "").strip().rstrip("/")
        if not url:
            continue
        is_local = url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")
        if not is_local:
            cfg["upstream_url"] = normalize_upstream(url)

        norm_url = canonical_local_url(url)
        if norm_url != cur_local_url:
            try:
                data["inferenceGatewayBaseUrl"] = cur_local_url
                atomic_write_json(path, data)
                changed.append("3P: %s -> %s (upstream %s)" % (url, cur_local_url, cfg.get("upstream_url", "")))
            except OSError as exc:
                log_msg("3P 配置改写失败:", path, exc)

    cfg["last_sync_ts"] = time.time()
    save_config(cfg)
    for line in changed:
        log_msg("synced:", line)
    if changed and gateway_alive():
        stop(GATEWAY_PID, "gateway")
        if _spawn_unlocked(GATEWAY, "gateway", GATEWAY_PID, port):
            log_msg("gateway 已重启以应用新上游")
    upstream = cfg.get("upstream_url")
    if not upstream:
        log_msg("未配置上游：请在 3P 设置里把网关地址填成你要访问的真实地址"
                "（保存后应用会重启），插件下次会自动接管。")
        return None
    log_msg("upstream_url:", upstream)
    return upstream


def rotate_stdout_log(log_path, limit=MAX_STDOUT_LOG_BYTES):
    """返回 stdout 副本的路径，超过上限就先把旧的那份挪成 `.1`。

    这份文件只在进程活着时增长（每行日志都在里面抄一份），按自然日滚动的日文件
    管不到它 —— 不轮转就是永久增长。留一代 `.1` 够接住上一次崩溃的 traceback，
    它也被保留期清理覆盖（文件名以 gateway 开头）。
    """
    try:
        if os.path.getsize(log_path) > limit:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass   # 不存在／没权限就照常追加，这里不该拦住启动
    return log_path


def _spawn_unlocked(script, name, pid_file, port=None):
    # 存活与否只认 HTTP 探活：pid 文件在这里没有发言权（pid 会被系统回收复用，
    # 而「端口上有没有人能应答」才是客户端真实的体验）。
    if gateway_alive():
        log_msg("%s already running" % name)
        return False
    env = dict(os.environ)
    if port:
        env["CLAUDE_TUNER_PORT"] = str(port)
    log_path = os.path.join(BASE, "logs", name + ".out.log")
    with open(rotate_stdout_log(log_path), "a", encoding="utf-8") as log:
        kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                  "env": env}
        kwargs.update(platform_support.detached_kwargs())
        proc = subprocess.Popen(
            [sys.executable, script],
            **kwargs
        )
    # pid_file 原子写（与 save_port 一致）
    try:
        tmp = pid_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        os.replace(tmp, pid_file)
    except OSError as exc:
        log_msg("pid_file write failed: %s" % exc)
    # 记录启动时刻：外部据此判断「进程跑的是不是最新代码」（代码改动一定晚于 mtime）
    write_started_at(name)
    write_runtime_info(proc.pid, script, port)
    log_msg("%s started (pid %d)" % (name, proc.pid))
    return True


def spawn(script, name, pid_file, port=None):
    ensure_dirs()
    lock = acquire_lock()
    try:
        _spawn_unlocked(script, name, pid_file, port)
    finally:
        release_lock(lock)


def stop(pid_file, name):
    pid = read_pid(pid_file)
    if pid is None or not pid_is_ours(pid):
        # 没有 pid 文件，或那个 pid 已经不是我们的进程：只清账本，绝不 kill
        try:
            os.remove(pid_file)
        except OSError:
            pass
        log_msg("%s not running" % name)
        return
    # 1. 先请它自己退：写停止标记，网关的主循环轮询到就 shutdown。
    #    这条路两个平台一样 —— Windows 上没有可拦的退出信号，信号那套语义搬不过去。
    try:
        with open(os.path.join(BASE, STOP_FILE_NAME), "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except OSError as exc:
        log_msg("stop marker write failed: %s" % exc)
    if not platform_support.wait_gone(pid, STOP_GRACE_SECONDS):
        # 2. 不听话才强杀。POSIX 先 SIGTERM 再 SIGKILL；Windows 两者都是 TerminateProcess。
        platform_support.terminate(pid)
        if not platform_support.wait_gone(pid, 2.0):
            platform_support.kill(pid)
            platform_support.wait_gone(pid, 1.0)
    for leftover in (pid_file, os.path.join(BASE, RUNTIME_INFO_NAME)):
        try:
            os.remove(leftover)
        except OSError:
            pass
    log_msg("%s stopped" % name)


def start():
    check_runtime_dir()
    spawn(GATEWAY, "gateway", GATEWAY_PID, resolve_port())


def stop_all():
    stop(GATEWAY_PID, "gateway")


def live_script_path():
    """正在跑的那个网关，执行的是哪一份脚本。

    读网关自己在启动时写下的 `gateway.runtime.json` —— 不查进程表。
    记录里的 pid 必须还活着，否则那份记录是上一次崩溃留下的，不能当真。
    """
    try:
        info = _runtime_info()
        pid = info.get("pid")
        script = info.get("script")
        if isinstance(pid, int) and isinstance(script, str) and platform_support.is_alive(pid):
            return os.path.abspath(script)
    except (OSError, ValueError):
        pass
    return None


def started_at_path(name):
    return os.path.join(BASE, name + ".started")


def write_started_at(name):
    """记下进程启动时刻（epoch 秒），供外部判断「是否在跑旧代码」。"""
    try:
        with open(started_at_path(name), "w", encoding="utf-8") as fh:
            fh.write("%.3f" % time.time())
    except OSError:
        pass


def read_started_at(name):
    try:
        with open(started_at_path(name), "r", encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:
        return None


def status():
    gw = gateway_alive()
    print("gateway:   %s (http://127.0.0.1:%d)" % ("running" if gw else "stopped", effective_port()))
    if gw:
        live = live_script_path()
        if live:
            print("script:    %s" % live)
        started = read_started_at("gateway")
        if started:
            print("started:   %.3f" % started)


def log_msg(*args):
    """统一日志记录：在 --quiet 模式下完全静默 stdout 并自然写入当天日志文件。"""
    msg = " ".join(str(a) for a in args)
    if not QUIET:
        print(msg)
    try:
        log_dir = os.path.join(BASE, "logs")
        os.makedirs(log_dir, exist_ok=True)
        purge_old_logs(log_dir, clamp_retention_days(
            load_config().get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)))
        today_str = time.strftime("%Y-%m-%d")
        day_log = os.path.join(log_dir, f"manager.{today_str}.log")
        with open(day_log, "a", encoding="utf-8") as fh:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            fh.write("[%s] %s\n" % (ts, msg))
        symlink = os.path.join(log_dir, "manager.log")
        target = f"manager.{today_str}.log"
        if not os.path.islink(symlink) or os.readlink(symlink) != target:
            if os.path.exists(symlink) or os.path.islink(symlink):
                try:
                    os.remove(symlink)
                except OSError:
                    pass
            try:
                os.symlink(target, symlink)
            except OSError:
                pass
    except Exception:
        pass


def main():
    global QUIET
    args = [a for a in sys.argv[1:] if a != "--quiet"]
    QUIET = "--quiet" in sys.argv[1:]
    cmd = args[0] if args else "status"
    try:
        if cmd == "start":
            start()
        elif cmd == "stop":
            stop_all()
        elif cmd == "restart":
            stop_all()
            start()
        elif cmd == "ensure":
            check_runtime_dir()
            sync()
            if not gateway_alive():
                spawn(GATEWAY, "gateway", GATEWAY_PID, resolve_port())
        elif cmd == "sync":
            sync()
        else:
            status()
    except Exception as exc:
        if QUIET or cmd == "ensure":
            log_msg("manager error (suppressed):", exc)
            sys.exit(0)
        raise


if __name__ == "__main__":
    configure_stdio()
    main()
