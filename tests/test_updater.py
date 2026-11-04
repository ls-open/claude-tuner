#!/usr/bin/env python3
"""claude-tuner/scripts/updater.py 的单测：自更新这条路上最怕的那几件事。

为什么需要这个文件
------------------
「点一下更新」是用户会主动按的按钮，而它做的事是**把正在运行的插件换掉**。
判错一次的后果比不能更新严重得多：更新到一半、换上半个包、或者被一个
恶意压缩包写到插件目录外面去。

所以这里钉住三件事：
1. 压缩包里的路径不许越界（远端内容不可信）；
2. 换上去之前必须校验（坏包宁可当场失败，也不能装上去）；
3. 任何一步失败，**正在用的那份保持原样**，且失败原因如实报出。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 tools/test_updater.py
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def load_updater():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("updater_under_test",
                                                  SCRIPTS_DIR / "updater.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["updater_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_tarball(entries, top="claude-tuner-main"):
    """造一个 tarball。entries 是 {相对路径: 内容}，都放在顶层目录下。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in entries.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo("%s/%s" % (top, rel))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def good_entries():
    return {
        "release.json": json.dumps({"repo": "a/b", "version": "9.9.9"}),
        ".claude-plugin/plugin.json": json.dumps({"name": "claude-tuner"}),
        "scripts/common.py": "X = 1\n",
        "scripts/gateway.py": "def main():\n    pass\n",
    }


class UpdaterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = load_updater()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class TestVersionCompare(UpdaterTestCase):
    def test_tuple(self):
        t = self.up.version_tuple
        self.assertEqual(t("2.31226.0"), (2, 31226, 0))
        self.assertEqual(t("0.1.70"), (0, 1, 70))
        self.assertEqual(t(""), ())
        self.assertEqual(t("v"), ())   # 一个数字都没有 = 读不出

    def test_newer_detection(self):
        t = self.up.version_tuple
        self.assertGreater(t("0.1.71"), t("0.1.70"))
        self.assertLess(t("0.1.9"), t("0.1.10"))
        self.assertGreater(t("2.31227.0"), t("2.31226.0"))


class TestExtractSafety(UpdaterTestCase):
    def test_rejects_path_traversal(self):
        """压缩包来自网络，不能信 —— 里面的路径不许写到工作目录之外。"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../evil.sh")   # noqa: foreign-path 压缩包内的恶意条目
            data = b"rm -rf /"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValueError):
            self.up._extract(buf.getvalue(), str(self.tmp))

    def test_rejects_absolute_path(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("/tmp/evil.sh")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            self.up._extract(buf.getvalue(), str(self.tmp))

    def test_finds_plugin_root(self):
        blob = make_tarball(good_entries())
        root = self.up._extract(blob, str(self.tmp))
        self.assertTrue(os.path.isfile(os.path.join(root, "release.json")))


class TestValidate(UpdaterTestCase):
    def write_tree(self, entries):
        root = self.tmp / "pkg"
        for rel, content in entries.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        return root

    def test_good_package_passes(self):
        root = self.write_tree(good_entries())
        self.assertEqual(self.up._validate(str(root)), [])

    def test_broken_python_is_caught(self):
        entries = good_entries()
        entries["scripts/gateway.py"] = "def broken(:\n"
        root = self.write_tree(entries)
        problems = self.up._validate(str(root))
        self.assertTrue(any("编译不过" in p for p in problems), problems)

    def test_manifest_without_name_is_caught(self):
        entries = good_entries()
        entries[".claude-plugin/plugin.json"] = json.dumps({"description": "无名字"})
        root = self.write_tree(entries)
        self.assertTrue(any("没有 name" in p for p in self.up._validate(str(root))))

    def test_unreadable_manifest_is_caught(self):
        entries = good_entries()
        entries[".claude-plugin/plugin.json"] = "{ not json"
        root = self.write_tree(entries)
        self.assertTrue(any("读不了" in p for p in self.up._validate(str(root))))


class TestCheck(UpdaterTestCase):
    SOURCE = {"repo": "a/b", "mirror": "", "version": "0.1.70", "tested_client": ""}

    def test_without_source_reports_honestly(self):
        """没填 repo 时不能装作「已是最新」—— 那会让用户以为检查过了。

        这里**构造**「没填来源」这个条件，不依赖仓库里 release.json 恰好是占位符：
        发布前填上真地址之后，原来的写法会变成真去联网，用例随之失效 ——
        而它要钉的是「缺来源时的行为」，不是「仓库当前的文件内容」。
        """
        with mock.patch.object(self.up, "release_info",
                               return_value=dict(self.SOURCE, repo="OWNER/claude-tuner")):
            result = self.up.check()
        self.assertFalse(result["update_available"])
        self.assertTrue(result["error"])
        self.assertIn("来源", result["error"])

    def test_reports_newer_remote(self):
        with mock.patch.object(self.up, "release_info", return_value=self.SOURCE), \
             mock.patch.object(self.up, "fetch_text",
                               return_value=(json.dumps({"version": "0.2.0"}), "http://x")), \
             mock.patch.object(self.up, "plugin_version", return_value="0.1.70"):
            result = self.up.check()
        self.assertEqual(result["remote"], "0.2.0")
        self.assertTrue(result["update_available"])
        self.assertEqual(result["source"], "http://x")

    def test_same_version_is_not_an_update(self):
        with mock.patch.object(self.up, "release_info", return_value=self.SOURCE), \
             mock.patch.object(self.up, "fetch_text",
                               return_value=(json.dumps({"version": "0.1.70"}), "http://x")), \
             mock.patch.object(self.up, "plugin_version", return_value="0.1.70"):
            self.assertFalse(self.up.check()["update_available"])

    def test_unreachable_reports_error_not_false_success(self):
        """所有通道都不通时必须说「不通」，不能说「已是最新」。"""
        with mock.patch.object(self.up, "release_info", return_value=self.SOURCE), \
             mock.patch.object(self.up, "fetch_text", return_value=(None, "http://last")):
            result = self.up.check()
        self.assertFalse(result["update_available"])
        self.assertIn("不通", result["error"])

    def test_broken_remote_json_reported(self):
        with mock.patch.object(self.up, "release_info", return_value=self.SOURCE), \
             mock.patch.object(self.up, "fetch_text", return_value=("{ not json", "http://x")):
            self.assertIn("格式", self.up.check()["error"])


class TestApplyKeepsThingsIntact(UpdaterTestCase):
    """失败路径最重要：宁可没更新，也不能把自己搞坏。"""

    def setUp(self):
        super().setUp()
        self.plugin = self.tmp / "installed"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "scripts" / "gateway.py").write_text("VERSION = 'old'\n", encoding="utf-8")
        (self.plugin / "release.json").write_text(json.dumps({"version": "0.1.70"}), encoding="utf-8")
        self.up.PLUGIN_ROOT = str(self.plugin)
        self.addCleanup(lambda: setattr(self.up, "PLUGIN_ROOT",
                                        str(PLUGIN_ROOT / "claude-tuner")))

    def read_gateway(self):
        return (self.plugin / "scripts" / "gateway.py").read_text(encoding="utf-8")

    def test_all_sources_down_leaves_install_untouched(self):
        boom = mock.Mock(side_effect=OSError("网络不通"))
        with mock.patch.object(self.up, "release_info", return_value=TestCheck.SOURCE), \
             mock.patch.object(self.up, "_get", boom):
            result = self.up.apply_update()
        self.assertFalse(result["ok"])
        self.assertIn("不通", result["error"])
        self.assertEqual(self.read_gateway(), "VERSION = 'old'\n")

    def test_bad_package_leaves_install_untouched(self):
        entries = good_entries()
        entries["scripts/gateway.py"] = "def broken(:\n"
        blob = make_tarball(entries)
        with mock.patch.object(self.up, "release_info", return_value=TestCheck.SOURCE), \
             mock.patch.object(self.up, "_get", return_value=blob):
            result = self.up.apply_update()
        self.assertFalse(result["ok"])
        self.assertIn("校验", result["error"])
        self.assertEqual(self.read_gateway(), "VERSION = 'old'\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
