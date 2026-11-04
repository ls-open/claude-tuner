#!/usr/bin/env python3
"""claude-tuner 跨平台一键安装（支持 Windows / macOS / Linux）

用法：
    python install.py [选项]
    或远程执行：
    python -c "import urllib.request; exec(urllib.request.urlopen('https://raw.githubusercontent.com/ls-open/claude-tuner/main/install.py').read())"

它做四件事：
  1. 把插件放到 ~/.claude/skills/claude-tuner/
  2. 写好让客户端进 3P 的配置与客户端设置（由 bootstrap.py 保证幂等）
  3. hooks 的解释器名由 bootstrap 在文件落地后对齐到本机（Windows 上通常是 python）
  4. 拉起本地网关并打开可视化配置台
"""
import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import webbrowser

REPO_SLUG = os.environ.get("CLAUDE_TUNER_REPO", "ls-open/claude-tuner")
REF = os.environ.get("CLAUDE_TUNER_REF", "main")
PLUGIN_NAME = "claude-tuner"

# 本脚本有两种运行方式：`python install.py`（有 __file__）与官方一行命令
# `python -c "exec(urlopen(...))"`（**没有 __file__**，`python -` 也只有 <stdin>）。
# 早先直接 `os.path.abspath(__file__)` 会让第二种方式一进来就 NameError —— 那正是
# 我们在 README 里主推的安装方式。这里统一求出「本地源码目录，取不到就是 None」。
_LOCAL_FILE = globals().get("__file__")
SRC_DIR = os.path.dirname(os.path.abspath(_LOCAL_FILE)) if _LOCAL_FILE else None
SRC_SCRIPTS = SRC_DIR or ""


def configure_stdio():
    """Windows 控制台默认 cp1252/cp936：不打这一下，安装器一 print emoji 就崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def log(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("unicode_escape").decode("ascii"), flush=True)


def step(title):
    print(f"\n\033[1m{title}\033[0m", flush=True)


def ok(msg):
    print(f"  ✅ {msg}", flush=True)


def warn(msg):
    print(f"  ⚠️  {msg}", flush=True)


def die(msg):
    print(f"\n❌ {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def dest_dir():
    """安装器自己要用的两个目录。

    这里**不推导客户端的配置目录**：那份口径在 `scripts/config_ops.py:platform_paths()`
    与 `scripts/common.py:get_3p_config_dir()` 里各有一份权威（一个管路径、一个管 3P），
    由 bootstrap 在插件落地之后去算。安装器只关心「插件放哪」和「网关的家在哪」，
    两者都与平台无关（都在家目录下）。

    之前这里还顺手推了一份 `claude_dir` / `claude_3p_dir`，既没人用，Linux 分支又是错的
    （非 win32 一律落到 macOS 的 `~/Library`）—— 一份没人读的错误口径，比没有更危险。
    """
    home = os.path.expanduser("~")
    return os.environ.get("CLAUDE_TUNER_DIR",
                          os.path.join(home, ".claude", "skills", PLUGIN_NAME))


def is_client_running():
    """客户端在不在跑。平台差异在插件的 platform_support 里写一份，这里不另抄。"""
    sys.path.insert(0, os.path.join(SRC_SCRIPTS, ""))
    try:
        import platform_support
        return platform_support.client_running()
    except Exception:
        return False


def download_plugin_package():
    candidates = [
        f"https://codeload.github.com/{REPO_SLUG}/tar.gz/refs/heads/{REF}",
        f"https://gh-proxy.com/https://github.com/{REPO_SLUG}/archive/refs/heads/{REF}.tar.gz",
        f"https://ghfast.top/https://github.com/{REPO_SLUG}/archive/refs/heads/{REF}.tar.gz",
    ]
    tmp_dir = tempfile.mkdtemp(prefix="claude-tuner-install-")
    for url in candidates:
        pkg_tar = os.path.join(tmp_dir, "pkg.tar.gz")
        try:
            log(f"  尝试下载源：{url}")
            req = urllib.request.Request(url, headers={"User-Agent": "claude-tuner-installer/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp, open(pkg_tar, "wb") as out:
                shutil.copyfileobj(resp, out)
            with tarfile.open(pkg_tar, "r:gz") as tar:
                # 与 common.safe_extractall 同一判据：filter="data" 是 3.12 起才有的参数
                # （3.11.4+ 也有）。install.py 必须能在插件文件落地前独立运行，所以这里
                # 不能 import common，只能自带这份四行判断。
                kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tar.extractall(tmp_dir, **kwargs)
            # 找到解压后的项目根目录
            for entry in os.listdir(tmp_dir):
                full = os.path.join(tmp_dir, entry)
                if os.path.isdir(full) and os.path.exists(os.path.join(full, "scripts", "gateway.py")):
                    ok(f"下载成功（{url}）")
                    return tmp_dir, full
                sub_tuner = os.path.join(full, "claude-tuner")
                if os.path.isdir(sub_tuner) and os.path.exists(os.path.join(sub_tuner, "scripts", "gateway.py")):
                    ok(f"下载成功（{url}）")
                    return tmp_dir, sub_tuner
        except Exception as exc:
            warn(f"当前通道失败 ({exc})，尝试备用线路…")
            if os.path.exists(pkg_tar):
                try:
                    os.remove(pkg_tar)
                except OSError:
                    pass

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


def build_parser():
    p = argparse.ArgumentParser(description="claude-tuner 跨平台安装脚本")
    p.add_argument("--key", default="", help="API Key（也可用环境变量 CLAUDE_TUNER_API_KEY）")
    p.add_argument("--upstream", default="", help="上游地址，默认 DeepSeek 官方")
    p.add_argument("--preset", default="", help="上游预设：deepseek / opencode-zen / opencode-go / custom")
    p.add_argument("--model", default="", help="上游模型名")
    p.add_argument("--no-cli", action="store_true", help="不写 ~/.claude/settings.json（终端 Claude Code 不走本网关）")
    p.add_argument("--no-open", action="store_true", help="装完不自动打开配置页")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    p.add_argument("--force", action="store_true", help="重写已经装过的配置")
    p.add_argument("--uninstall", action="store_true", help="卸载收回本插件写的客户端状态")
    return p


def main(argv=None):
    if sys.version_info < (3, 11):
        die(f"本插件要求 Python 3.11+，当前版本为 {sys.version.split()[0]}。请先升级 Python。")

    args = build_parser().parse_args(argv)
    dest = dest_dir()

    # 卸载逻辑
    if args.uninstall:
        step("① 卸载 claude-tuner")
        bootstrap_py = os.path.join(dest, "scripts", "bootstrap.py")
        if os.path.isfile(bootstrap_py):
            cmd = [sys.executable, bootstrap_py, "uninstall"]
            if args.dry_run:
                cmd.append("--dry-run")
            subprocess.run(cmd)
        if not args.dry_run and os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
            ok(f"已清理插件目录：{dest}")
        ok("卸载完成")
        return 0

    log(f"claude-tuner 跨平台安装程序 (平台: {sys.platform})")
    log("=" * 60)

    # 运行检查
    step("① 环境检查")
    ok(f"Python 环境：{sys.executable} ({sys.version.split()[0]})")
    if is_client_running() and not args.dry_run:
        warn("检测到 Claude 正在运行！运行中的客户端会覆盖写回的配置文件。")
        warn("建议先在菜单中完全退出 Claude 桌面端，然后再执行安装。")

    # 确定源码位置
    step("② 准备插件文件")
    self_dir = SRC_DIR
    tmp_clean = None
    if self_dir and os.path.isfile(os.path.join(self_dir, "scripts", "gateway.py")):
        src = self_dir
        ok(f"就地安装（源码在 {src}）")
    else:
        if args.dry_run:
            src = dest
            ok("[dry-run] 演练模式：跳过网络下载")
        else:
            tmp_clean, src = download_plugin_package()
            if not src:
                die("所有下载通道均不可达，请尝试手动下载 GitHub 仓库 Zip 后运行本脚本。")

    # 安装文件
    step(f"③ 安装到 {dest}")
    if args.dry_run:
        log(f"  [dry-run] 会将源码复制至 {dest}")
    else:
        os.makedirs(dest, exist_ok=True)
        # 复制核心代码
        for item in ["scripts", "webui", "hooks", ".claude-plugin", "release.json", "README.md", "LICENSE"]:
            s = os.path.join(src, item)
            d = os.path.join(dest, item)
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            elif os.path.isfile(s):
                shutil.copy2(s, d)
        ok(f"核心文件已就绪至 {dest}")

    if tmp_clean:
        shutil.rmtree(tmp_clean, ignore_errors=True)

    # 执行 bootstrap 配置
    step("④ 配置客户端状态")
    bootstrap_py = os.path.join(dest if not args.dry_run else src, "scripts", "bootstrap.py")
    boot_args = [sys.executable, bootstrap_py, "install"]
    if args.dry_run:
        boot_args.append("--dry-run")
    if args.force:
        boot_args.append("--force")
    if args.no_cli:
        boot_args.append("--no-cli")
    key = args.key or os.environ.get("CLAUDE_TUNER_API_KEY", "")
    if key:
        boot_args.extend(["--key", key])
    if args.upstream:
        boot_args.extend(["--upstream", args.upstream])
    if args.preset:
        boot_args.extend(["--preset", args.preset])
    if args.model:
        boot_args.extend(["--model", args.model])

    res = subprocess.run(boot_args)
    if res.returncode != 0:
        die("客户端配置初始化失败。")

    if args.dry_run:
        step("演练结束")
        log("所有步骤已模拟完成，未产生任何实际修改。")
        return 0

    # 拉起网关
    step("⑤ 拉起本地网关")
    manager_py = os.path.join(dest, "scripts", "manager.py")
    subprocess.run([sys.executable, manager_py, "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等待网关就绪
    time.sleep(1.0)
    webui_py = os.path.join(dest, "scripts", "webui.py")
    page_url = None
    for _ in range(15):
        try:
            p_res = subprocess.run([sys.executable, webui_py, "--url"], capture_output=True, text=True, timeout=3)
            out_url = p_res.stdout.strip()
            if out_url.startswith("http://"):
                page_url = out_url
                break
        except Exception:
            pass
        time.sleep(0.5)

    if page_url:
        ok("网关已就绪并在后台常驻。")
        step("⑥ 打开配置页")
        log(f"  配置页地址：{page_url}")
        if not args.no_open:
            webbrowser.open(page_url)
            ok("已在默认浏览器打开配置页。")
    else:
        warn("网关可能尚未就绪，在 Claude 中发送任意消息会自动唤醒，或手动执行：")
        log(f"  {sys.executable} {manager_py} restart")

    log("\n" + "=" * 60)
    ok("🎉 claude-tuner 安装完成！")
    if sys.platform == "win32":
        # 提前说清楚，比事后让人猜强：Windows 上「客户端会不会真的执行 hooks.json」
        # 只能在真机上验，我们验不到。装完就把话说在前面，也顺手给一条能贴给我们的命令。
        log("  ⚠️  Windows 目前是**实验性**：安装器与网关逻辑已在 CI 上验过，")
        log("     但「客户端会不会把网关拉起来」这一环我们验不到（没有 Windows 机器）。")
        log("     若在客户端里发消息没反应，请运行下面这条，把整段输出贴到 issue（不含密钥）：")
        log("     %s %s doctor" % (sys.executable, os.path.join(dest, "scripts", "bootstrap.py")))
        log("     https://github.com/ls-open/claude-tuner/issues")
    return 0


if __name__ == "__main__":
    configure_stdio()
    sys.exit(main())
