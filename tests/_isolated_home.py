"""测试的家目录隔离：两件事，一份键集。

1. **`import` 本模块**（必须在 `import gateway` 之前）把当前进程的 `CLAUDE_TUNER_HOME`
   指到临时目录。gateway 在 import 时就把 BASE_DIR 定下来，而日志、抓包、配置都写在那里。
   不隔离的话，测试造出来的 `canonicalized` / `rule_failed` 会混进用户真实日志 ——
   那份日志是缓存归因的唯一判据，混进来的事件会把人引向错误结论。
2. **`subprocess_env()`** 给「真起子进程」的测试用：一次把平台层会读的每个变量都指到临时目录。

为什么键集只能有一处
--------------------
早先每个测试文件各拼一份这个 env，结果 `XDG_CONFIG_HOME` 只漏在了其中一份里：
Ubuntu 上 bootstrap 把配置写进了 runner 真实的 `~/.config`，而断言盯着临时目录 ——
**既是假红，又真的把机器写脏了**。Windows 上同理：`expanduser("~")` 读的是
`USERPROFILE` 而不是 `HOME`，只设 `HOME` 等于没隔离。
"""
import atexit
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="claude-tuner-test-home-")
os.environ["CLAUDE_TUNER_HOME"] = HOME
atexit.register(shutil.rmtree, HOME, True)


def subprocess_env(home, **extra):
    """子进程测试的隔离环境：平台层会读的每个变量都指到 `home`。

    `extra` 用来叠加本次测试特有的项（CLAUDE_TUNER_3P_DIR / CLAUDE_TUNER_PORT …）。
    """
    home = str(home)
    env = dict(os.environ)
    # 先摘掉继承来的本插件变量：开发者的 shell 里要是恰好 export 过一个，子进程就会
    # 悄悄跑在他的真实目录上，而断言盯着临时目录 —— 正是这条链上最贵的那种假绿。
    for stale in ("CLAUDE_TUNER_HOME", "CLAUDE_TUNER_3P_DIR", "CLAUDE_TUNER_PORT"):
        env.pop(stale, None)
    env.update({
        "HOME": home,                                    # POSIX 的 expanduser
        "USERPROFILE": home,                             # Windows 的 expanduser
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "CLAUDE_TUNER_HOME": os.path.join(home, ".claude-tuner"),
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env
