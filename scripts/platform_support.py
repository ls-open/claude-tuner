#!/usr/bin/env python3
"""平台差异只在这里写一份：进程存活 / 终止 / 客户端是否在跑 / 脱离父进程地起子进程。

为什么单独一个文件
------------------
同一个判断原先是分散的：`manager.py` 用 `ps` 探活、`webui.py` 用 `pgrep` 查客户端、
`install.py` 又抄了一份同样的 `tasklist`/`pgrep`，`_spawn` 里各自拼 `creationflags`。
抄件一多，就会出现「Windows 上页面说客户端没开、manager 却说有」这类自相矛盾 ——
和 `tool_rules.py` 当年的教训是同一条：判断只写一份，消费者 import 它。

**这里不碰配置、不碰日志、不读磁盘路径**（那些在 `common.py` / `config_ops.py`）：
本模块只回答「这个 pid 活着吗」「怎么让它退出」「哪个客户端在跑」这几个操作系统问题。
"""
import ctypes
import os
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"

# ---- 进程存活 ----------------------------------------------------------------

_WAIT_TIMEOUT = 0x00000102
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001


def lock_file(fd):
    """对已打开的 fd 加独占的非阻塞锁；拿不到返回 False。"""
    try:
        if IS_WINDOWS:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock_file(fd):
    try:
        if IS_WINDOWS:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def lock_is_held(path):
    """这个锁文件现在有没有被别人持有 —— 用来判断「持有它的那个进程还活着吗」。

    这是跨平台的**进程归属证明**：锁由目标进程在启动时拿到、进程一死由内核释放，
    所以我们不需要读命令行、不需要遍历进程表，也不会被 pid 复用骗到。
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        if lock_file(fd):
            unlock_file(fd)
            return False
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _win_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    return kernel32


def is_alive(pid):
    """这个 pid 现在还有没有一个进程在跑。

    **绝不能用 `os.kill(pid, 0)` 判活**：Windows 上 `os.kill` 对任何非 CTRL_* 信号都是
    `TerminateProcess` —— 那是「杀死」不是「探测」，探活会把进程探死（真踩过）。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        kernel32 = _win_kernel32()
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            # 打不开有两种：没这个 pid（ERROR_INVALID_PARAMETER）或没权限（ACCESS_DENIED）。
            # 后者说明进程存在，只是不归我们管 —— 按「活着」处理才安全。
            return ctypes.get_last_error() == 5
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 活着，但不是本用户的进程
    except OSError:
        return False


def terminate(pid):
    """请它退出：POSIX 发 SIGTERM（进程可拦、可清理），Windows 直接 TerminateProcess。

    Windows 没有「可拦的退出信号」这种东西，所以那边的优雅退出**不靠信号**，走
    `manager.stop()` 的停止标记（网关自己轮询到就退）。这里只是最后的手段。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            kernel32 = _win_kernel32()
            handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
            if not handle:
                return False
            try:
                return bool(kernel32.TerminateProcess(handle, 1))
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 15)      # SIGTERM
        return True
    except Exception:
        return False


def kill(pid):
    """强杀：POSIX 用 SIGKILL，Windows 用 TerminateProcess。"""
    try:
        if IS_WINDOWS:
            return terminate(pid)
        os.kill(pid, 9)
        return True
    except Exception:
        return False


def wait_gone(pid, timeout):
    """等进程消失；返回是否真的没了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)


# ---- 起子进程 ----------------------------------------------------------------

def detached_kwargs():
    """让子进程脱离父进程的 kwargs（替代 POSIX 专有的 `start_new_session`）。

    Windows 上用 `CREATE_NO_WINDOW`（不弹黑框）+ `CREATE_NEW_PROCESS_GROUP`
    （自成一组，将来要发 CTRL_BREAK 也有地方发）。
    **不用 `DETACHED_PROCESS`**：它与 `CREATE_NO_WINDOW` 语义冲突（前者就不建控制台），
    两个一起传的行为未定义，留一个明确的即可。
    """
    if IS_WINDOWS:
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


# ---- 客户端是否在跑 ----------------------------------------------------------

def _windows_process_names():
    """枚举当前所有进程的可执行名（ctypes Toolhelp，纯标准库、不起子进程）。"""
    names = []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint),
                    ("cntUsage", ctypes.c_uint),
                    ("th32ProcessID", ctypes.c_uint),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_uint),
                    ("cntThreads", ctypes.c_uint),
                    ("th32ParentProcessID", ctypes.c_uint),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_uint),
                    ("szExeFile", ctypes.c_char * 260)]

    TH32CS_SNAPPROCESS = 0x00000002
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return names
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        kernel32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("utf-8", "replace")
            if name:
                names.append(name)
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return names


# 桌面端在各平台的进程特征。macOS 用可执行文件路径片段（客户端主进程就在 .app 里），
# Windows 用可执行名（安装包可能是 Claude.exe，也可能是带版本后缀的变体，按前缀认）。
MAC_CLIENT_PATTERN = "Claude.app/Contents/MacOS/Claude"
WIN_CLIENT_NAMES = ("claude.exe",)


def client_running():
    """Claude 桌面端在不在跑。偏好类改动只在它没跑时写得进去。

    读不到一律返回 False（宁可让用户自己判断，也不要凭空拦一道）。
    """
    try:
        if IS_WINDOWS:
            return any(name.lower() in WIN_CLIENT_NAMES for name in _windows_process_names())
        if sys.platform == "darwin":
            # 不能用 `pgrep -f` 匹配客户端绝对路径：实测在这台机器上它匹配不到主进程
            # （只匹配到 Helper），于是「客户端在跑吗」永远答否，界面上那句
            # 「请先退出客户端」的提醒从来没出现过。
            # 改成自己扫 ps 的第 1 个字段做**精确**比较 —— Helper 与随包的另一条
            # 产物线（小写的 claude）都排除在外。
            res = subprocess.run(["ps", "-ax", "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
            for line in (res.stdout or "").splitlines():
                exe = line.strip().split(" ")[0] if line.strip() else ""
                if exe.endswith("/Claude.app/Contents/MacOS/Claude"):
                    return True
            return False
        return False      # Linux 上没有该桌面端
    except Exception:
        return False
