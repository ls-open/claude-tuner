"""测试进程的家目录隔离：**必须在 import gateway 之前 import 本模块**。

gateway 在 import 时就把 BASE_DIR 定下来，而日志、抓包、配置都写在那里。不隔离的话，
测试造出来的 `canonicalized` / `rule_failed` / `pair_text_not_found` 会混进用户真实日志 ——
那份日志是缓存归因的唯一判据，混进来的事件会把人引向错误结论。
"""
import atexit
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="claude-tuner-test-home-")
os.environ["CLAUDE_TUNER_HOME"] = HOME
atexit.register(shutil.rmtree, HOME, True)
