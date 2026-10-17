#!/usr/bin/env python3
"""插件自更新：查有没有新版、一键换掉自己。

自更新最容易把人坑在「更新到一半坏了」上，所以这里的规矩是：
**先在临时目录里下全、验完，再动正在用的那份**；动之前先整份备份，失败回滚。

下不下来的情况比想象中多（国内直连 GitHub 常不通），所以每条路都按
同一套通道依次试：GitHub raw → jsdelivr → gh-proxy → ghfast。
用了哪条通道会如实报出来，不假装「更新成功」而实际一个字节没换。

用法：
    updater.py check [--json]
    updater.py apply [--json]
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from common import BACKUP_KEEP_TREES, prune_backups
import urllib.request

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from common import client_version, plugin_version, release_info, version_tuple  # noqa: E402

PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 180


def _sources(repo, ref, path):
    """同一个文件的几条取用通道，按「国内直连优先」排。"""
    urls = []
    urls.append("https://raw.githubusercontent.com/%s/%s/%s" % (repo, ref, path))
    urls.append("https://cdn.jsdelivr.net/gh/%s@%s/%s" % (repo, ref, path))
    urls.append("https://gh-proxy.com/https://raw.githubusercontent.com/%s/%s/%s"
                % (repo, ref, path))
    urls.append("https://ghfast.top/https://raw.githubusercontent.com/%s/%s/%s"
                % (repo, ref, path))
    return urls


def _archive_sources(repo, ref):
    urls = []
    urls.append("https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (repo, ref))
    urls.append("https://gh-proxy.com/https://github.com/%s/archive/refs/heads/%s.tar.gz"
                % (repo, ref))
    urls.append("https://ghfast.top/https://github.com/%s/archive/refs/heads/%s.tar.gz"
                % (repo, ref))
    return urls


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "claude-tuner-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(path, repo=None, ref="main"):
    """取远端一个小文件，返回 (内容, 实际用的 URL)；全不通返回 (None, 试过的最后一条)。"""
    info = release_info()
    repo = repo or info["repo"]
    if not repo:
        return None, ""
    tried = ""
    for url in _sources(repo, ref, path):
        tried = url
        try:
            return _get(url).decode("utf-8", "replace").strip(), url
        except Exception:
            continue
    return None, tried


def check():
    """查有没有新版。返回给页面用的字典，全部字段都是如实报出的。"""
    info = release_info()
    local = plugin_version()
    client = client_version()
    result = {
        "local": local,
        "client": client,
        "tested_client": info["tested_client"],
        "repo": info["repo"],
        "remote": None,
        "update_available": False,
        "source": "",
        "url": "",
        "error": "",
    }
    if not info["repo"] or info["repo"].startswith("OWNER"):
        result["error"] = "这份安装没带来源信息（release.json 未填），无法检查更新"
        return result
    text, url = fetch_text("release.json")
    result["url"] = url
    if not text:
        result["error"] = "取不到远端版本信息：所有下载通道都不通"
        return result
    try:
        remote = json.loads(text)
        result["remote"] = str(remote.get("version") or "")
    except Exception:
        result["error"] = "远端版本信息格式不对"
        return result
    result["source"] = url
    lv, rv = version_tuple(local), version_tuple(result["remote"])
    result["update_available"] = bool(rv) and (not lv or rv > lv)
    if client and info["tested_client"] and version_tuple(client) > version_tuple(info["tested_client"]):
        result["client_newer_than_tested"] = True
    return result


def _extract(blob, workdir):
    """解开 tarball，返回插件根目录（tarball 里通常有一层同名目录）。"""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            # 只允许相对路径、且不越出工作目录 —— 远端内容不可信
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise ValueError("压缩包里有不安全的路径：%s" % m.name)
        tar.extractall(workdir)
    entries = [os.path.join(workdir, n) for n in os.listdir(workdir)]
    for entry in entries:
        if os.path.isdir(entry) and os.path.isfile(os.path.join(entry, "release.json")):
            return entry
    return workdir


def _validate(root):
    """换上去之前先验：清单能解析、每个 .py 能编译。
    坏包换上去 = 用户点一下「更新」把自己的安装搞坏，宁可当场失败。"""
    import py_compile
    problems = []
    manifest = os.path.join(root, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        if not data.get("name"):
            problems.append("plugin.json 里没有 name")
    except Exception as exc:
        problems.append("plugin.json 读不了：%s" % exc)
    for name in sorted(os.listdir(os.path.join(root, "scripts"))):
        if not name.endswith(".py"):
            continue
        try:
            py_compile.compile(os.path.join(root, "scripts", name), doraise=True)
        except Exception as exc:
            problems.append("%s 编译不过：%s" % (name, str(exc).splitlines()[0]))
    return problems


def apply_update():
    """下载 → 校验 → 备份现有的 → 换掉 → 重启网关。

    返回字典；失败时 ``ok=False``，且**正在用的那份保持原样**。
    """
    info = release_info()
    if not info["repo"] or info["repo"].startswith("OWNER"):
        return {"ok": False, "error": "这份安装没带来源信息（release.json 未填）"}
    workdir = tempfile.mkdtemp(prefix="claude-tuner-update-")
    try:
        blob, used, errors = None, "", []
        for url in _archive_sources(info["repo"], "main"):
            try:
                blob = _get(url, timeout=DOWNLOAD_TIMEOUT)
                used = url
                break
            except Exception as exc:
                errors.append("%s → %s" % (url.split("/")[2], exc))
        if not blob:
            return {"ok": False, "error": "所有下载通道都不通",
                    "detail": errors[:4]}
        root = _extract(blob, workdir)
        problems = _validate(root)
        if problems:
            return {"ok": False, "error": "下载到的包没通过校验，保持原样", "detail": problems}

        stamp = time.strftime("%Y%m%dT%H%M%S")
        # 备份放在运行目录里（~/.claude-tuner/backups/），不放进插件目录 ——
        # 免得同步时被当成多出来的文件
        backup_dir = os.path.join(os.path.expanduser("~"), ".claude-tuner", "backups")
        backup = os.path.join(backup_dir, "plugin-%s" % stamp)
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copytree(PLUGIN_ROOT, backup, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 整树备份每次几百 KB，只留最近几份：够回滚，不会把磁盘堆满
        prune_backups(backup_dir, BACKUP_KEEP_TREES, prefix="plugin-")
        for name in os.listdir(root):
            src = os.path.join(root, name)
            dst = os.path.join(PLUGIN_ROOT, name)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        return {"ok": True, "source": used, "backup": backup,
                "version": json.load(open(os.path.join(root, "release.json"), encoding="utf-8")
                                     ).get("version", "")}
    except Exception as exc:
        return {"ok": False, "error": "更新失败：%s" % exc}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def restart_gateway():
    """换完代码要重启网关 —— 它自己不会重启自己。"""
    manager = os.path.join(SCRIPT_DIR, "manager.py")
    subprocess.Popen([sys.executable, manager, "restart"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="claude-tuner 自更新")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("check", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    cmd = args.cmd or "check"
    data = check() if cmd == "check" else apply_update()
    if getattr(args, "as_json", False):
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0 if data.get("ok", True) else 1
    if cmd == "check":
        print("本版：%s" % (data["local"] or "未知"))
        print("远端：%s" % (data["remote"] or "取不到"))
        print("客户端：%s（按 %s 验过）" % (data["client"] or "未知",
                                          data["tested_client"] or "未知"))
        if data.get("error"):
            print("⚠️ %s" % data["error"])
        else:
            print("有新版可用" if data["update_available"] else "已是最新")
        return 0
    if data.get("ok"):
        print("✅ 已更新到 %s（来源 %s）" % (data.get("version"), data.get("source")))
        print("   原文件备份在 %s" % data.get("backup"))
        return 0
    print("❌ %s" % data.get("error"))
    for line in data.get("detail") or []:
        print("   %s" % line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
