#!/usr/bin/env python3
"""claude-tuner/scripts/bootstrap.py 的端到端测试。

为什么需要这个文件
------------------
`bootstrap.py` 会去改用户的三样东西：客户端的 3P 配置、`~/.claude/settings.json`、
网关自己的 config.json。判错一次的后果不是报错而是**静默改坏用户环境**，而且
只有用户重启客户端之后才会发现。所以这里的断言全部落在「文件到底变成了什么样」上：

- `--dry-run` 必须零落盘（它是用户唯一能先看一眼的手段）；
- 就地合并必须保留客户端自己写的字段与用户自有的设置键；
- 连跑两次，第二次必须零写入；
- 改已有的文件前必须留下备份。

**在子进程里真跑**（本仓铁律：命令行入口必须在子进程验）：bootstrap 的入口就是命令行，import 后调函数
够不到 `main()` 与 `sys.argv`，而 `--dry-run` / `--no-restart` 恰恰只在那里成立。
HOME / CLAUDE_TUNER_3P_DIR / CLAUDE_TUNER_HOME 全部指向临时目录，不碰真实的 ~/Library 与 ~/.claude。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 tools/test_bootstrap.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import common  # noqa: E402
import config_ops  # noqa: E402


class BootstrapTestCase(unittest.TestCase):
    """把 bootstrap 关进一个假的 HOME 里跑，断言它写了/没写什么。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # 客户端目录由**生产函数**算（同一份 platform_paths），测试不写死 macOS 的
        # `~/Library/Application Support` —— 写死就等于把断言绑在一个平台上，
        # 换到 Windows 就跑不过，而那种红是「测试写得不对」不是「代码有问题」。
        # APPDATA 一并指到临时目录，Windows 上的分支也在这套隔离里跑。
        # Windows 的 expanduser 读 USERPROFILE 而不是 HOME：两个都指到临时目录，
        # 否则测试会去写 runner 的真实用户目录，而断言盯着临时目录（假红且危险）。
        self._env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
        }
        with mock.patch.dict(os.environ, self._env):
            self.paths = config_ops.platform_paths()
        self.config_lib = Path(self.paths["config_library"])
        self.config_lib.mkdir(parents=True, exist_ok=True)
        self.claude_dir = Path(self.paths["claude_dir"])
        self.bridge_home = self.home / ".claude-tuner"
        self.claude_home = self.home / ".claude"

    # ── 跑生产入口 ──

    def run_bootstrap(self, *argv):
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "CLAUDE_TUNER_3P_DIR": str(self.config_lib),
            "CLAUDE_TUNER_HOME": str(self.bridge_home),
            "CLAUDE_TUNER_API_KEY": "",
        })
        return subprocess.run([sys.executable, str(BOOTSTRAP)] + list(argv),
                              capture_output=True, text=True, env=env, timeout=60)

    def install(self, *extra):
        """跑一次安装。**在这里就断言退出码**：否则 bootstrap 一失败，表现只是
        「某个文件不存在」，等于把「为什么失败」这条信息丢掉了（CI 上尤其致命）。"""
        # --no-restart：测试不该去碰真实的网关进程
        res = self.run_bootstrap("install", "--no-restart", *extra)
        self.assertEqual(res.returncode, 0,
                         "bootstrap 退出码 %d\nstdout:\n%s\nstderr:\n%s"
                         % (res.returncode, (res.stdout or "")[-1200:], (res.stderr or "")[-1200:]))
        return res

    # ── 夹料 ──

    def seed_3p(self, extra=None, applied="existing"):
        """造一份客户端已经存在的 3P 配置（含客户端自己写的字段）。"""
        cfg = {"coworkTabEnabled": True, "someFutureClientField": {"nested": True}}
        cfg.update(extra or {})
        (self.config_lib / f"{applied}.json").write_text(json.dumps(cfg), encoding="utf-8")
        (self.config_lib / "_meta.json").write_text(
            json.dumps({"appliedId": applied, "entries": [{"id": applied, "name": "Default"}]}),
            encoding="utf-8")
        return cfg

    def seed_settings(self):
        (self.claude_home).mkdir(parents=True, exist_ok=True)
        (self.claude_home / "settings.json").write_text(json.dumps({
            "enabledPlugins": {}, "sandbox": {"enabled": False},
            "env": {"MY_OWN_VAR": "keep-me"},
            "attribution": {"commit": "", "pr": ""},
        }), encoding="utf-8")

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def active_3p(self):
        applied = self.read(self.config_lib / "_meta.json")["appliedId"]
        return self.config_lib / f"{applied}.json"

    def all_files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*") if p.is_file())


class TestDryRun(BootstrapTestCase):
    def test_dry_run_writes_nothing(self):
        res = self.install("--dry-run", "--key", "sk-test-key-123456")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.all_files(), [])
        self.assertIn("未执行", res.stdout)

    def test_dry_run_lists_what_it_would_do(self):
        res = self.install("--dry-run", "--key", "sk-test-key-123456")
        for token in ("developer_settings.json", "claude_desktop_config.json",
                      "settings.json", "configLibrary"):
            self.assertIn(token, res.stdout)

    def test_dry_run_on_existing_install_also_writes_nothing(self):
        self.seed_3p()
        self.seed_settings()
        before = self.all_files()
        res = self.install("--dry-run")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.all_files(), before)


class TestInstall(BootstrapTestCase):
    def test_writes_the_handful_of_files_that_open_3p(self):
        self.assertEqual(self.install("--key", "sk-test-key-123456").returncode, 0)
        expected = [
            self.home / ".claude" / "settings.json",
            Path(self.paths["claude_dir"]) / "developer_settings.json",
            Path(self.paths["claude_dir"]) / "claude_desktop_config.json",
            Path(self.paths["claude_3p_dir"]) / "developer_settings.json",
            Path(self.paths["claude_3p_dir"]) / "claude_desktop_config.json",
            self.home / ".claude-tuner" / "config.json",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), str(path))
        # 两个 deploymentMode 都写：app.asar 里读它的路径是 userData + "-3p"，
        # 但 1P 目录那份同样要写，免得换个版本改了读法就失效
        for path in (Path(self.paths["claude_dir"]) / "claude_desktop_config.json",
                     Path(self.paths["claude_3p_dir"]) / "claude_desktop_config.json"):
            self.assertEqual(self.read(path)["deploymentMode"], "3p")
        self.assertIs(self.read(Path(self.paths["claude_dir"]) / "developer_settings.json")["allowDevTools"], True)

    def test_3p_config_points_at_local_gateway(self):
        self.install("--key", "sk-test-key-123456")
        cfg = self.read(self.active_3p())
        self.assertEqual(cfg["inferenceProvider"], "gateway")
        self.assertEqual(cfg["inferenceGatewayBaseUrl"], "http://127.0.0.1:8789")
        self.assertEqual(cfg["inferenceGatewayApiKey"], "sk-test-key-123456")
        self.assertEqual([m["name"] for m in cfg["inferenceModels"]],
                         ["claude-opus-5-5", "claude-sonnet-5-5"])

    def test_bridge_config_learns_upstream(self):
        """上游写进网关配置，而不是等着 manager 从 3P 里反推 —— 两段式接管那个坑就不发生。"""
        self.install("--key", "sk-test-key-123456")
        cfg = self.read(self.bridge_home / "config.json")
        self.assertEqual(cfg["upstream_url"], "https://api.deepseek.com/anthropic/v1/messages")

    def test_upstream_override(self):
        self.install("--key", "k", "--upstream", "https://opencode.ai/zen/go")
        cfg = self.read(self.bridge_home / "config.json")
        self.assertEqual(cfg["upstream_url"], "https://opencode.ai/zen/go/v1/messages")

    def test_no_cli_skips_env(self):
        self.install("--key", "k", "--no-cli")
        self.assertNotIn("env", self.read(self.claude_home / "settings.json"))


class TestDefaultMapping(BootstrapTestCase):
    """默认映射只有一条规则：认识这家上游就映射过去，不认识就让代号同名直通。"""

    def test_known_preset_maps_two_recommended_codenames(self):
        self.install("--key", "k", "--upstream", "https://opencode.ai/zen/go")
        cfg = self.read(self.active_3p())
        self.assertEqual([(m["name"], m["labelOverride"]) for m in cfg["inferenceModels"]],
                         [("claude-opus-5-5", "deepseek-flash"),
                          ("claude-sonnet-5-5", "deepseek-flash")])

    def test_unknown_upstream_keeps_codenames_identical(self):
        """自填 URL（多半是官方中转站）：代号同名直通，且必须列全 ——
        漏一个就会走兜底，把用户的 Opus 悄悄换成别的模型。"""
        self.install("--key", "k", "--upstream", "https://my-relay.example.com")
        cfg = self.read(self.active_3p())
        pairs = [(m["name"], m["labelOverride"]) for m in cfg["inferenceModels"]]
        self.assertTrue(pairs)
        self.assertTrue(all(n == t for n, t in pairs), pairs)
        self.assertGreaterEqual(len(pairs), 13)

    def test_default_model_is_left_to_the_gateway(self):
        """不在安装时钉 default_model：它留空时网关的兜底取映射表第一个目标，
        也就是用户自己配的那个模型。钉一个常量反而会在用户换上游后说错话。"""
        self.install("--key", "k")
        self.assertEqual(self.read(self.bridge_home / "config.json")["default_model"], "")

    def test_explicit_model_overrides_preset(self):
        self.install("--key", "k", "--model", "deepseek-v4-pro")
        cfg = self.read(self.active_3p())
        self.assertTrue(all(m["labelOverride"] == "deepseek-v4-pro"
                            for m in cfg["inferenceModels"]))


class TestMerging(BootstrapTestCase):
    def test_keeps_client_fields_and_user_settings(self):
        self.seed_3p()
        self.seed_settings()
        self.install("--key", "sk-test-key-123456")

        cfg = self.read(self.active_3p())
        self.assertEqual(cfg["someFutureClientField"], {"nested": True})
        self.assertIs(cfg["coworkTabEnabled"], True)

        settings = self.read(self.claude_home / "settings.json")
        self.assertEqual(settings["env"]["MY_OWN_VAR"], "keep-me")
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})
        self.assertEqual(settings["sandbox"], {"enabled": False})
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8789")

    def test_meta_is_never_rewritten(self):
        """_meta.json 记录客户端当前生效的是哪一份，已有内容一律不动。"""
        self.seed_3p()
        self.install("--key", "k")
        meta = self.read(self.config_lib / "_meta.json")
        self.assertEqual(meta["appliedId"], "existing")
        self.assertEqual(meta["entries"], [{"id": "existing", "name": "Default"}])

    def test_existing_files_are_backed_up(self):
        self.seed_3p()
        self.seed_settings()
        self.install("--key", "k")
        backups = list((self.bridge_home / "backups" / "3p").glob("*.bak"))
        names = {b.name.split(".")[0] for b in backups}
        self.assertIn("existing", names)
        self.assertIn("settings", names)


class TestIdempotency(BootstrapTestCase):
    def test_second_install_writes_nothing(self):
        self.seed_settings()
        self.install("--key", "sk-test-key-123456")
        watched = [self.claude_home / "settings.json",
                   Path(self.paths["claude_3p_dir"]) / "claude_desktop_config.json",
                   self.bridge_home / "config.json"]
        before = {str(p): p.stat().st_mtime_ns for p in watched}

        res = self.install("--key", "sk-test-key-123456")
        self.assertEqual(res.returncode, 0)
        self.assertIn("已经装过", res.stdout)
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in watched}, before)

    def test_force_rewrites(self):
        self.install("--key", "sk-test-key-123456")
        res = self.install("--force", "--key", "sk-new-key-999999")
        self.assertNotIn("已经装过", res.stdout)
        self.assertEqual(self.read(self.active_3p())["inferenceGatewayApiKey"],
                         "sk-new-key-999999")

    def test_repeated_install_does_not_spawn_more_backups(self):
        self.seed_settings()
        self.install("--key", "k")
        first = len(list((self.bridge_home / "backups" / "3p").glob("*.bak")))
        self.install("--key", "k")
        self.assertEqual(len(list((self.bridge_home / "backups" / "3p").glob("*.bak"))), first)


class TestUninstall(BootstrapTestCase):
    def test_removes_only_our_keys(self):
        self.seed_settings()
        self.install("--key", "sk-test-key-123456")
        res = self.run_bootstrap("uninstall")
        self.assertEqual(res.returncode, 0, res.stderr)
        settings = self.read(self.claude_home / "settings.json")
        self.assertEqual(settings["env"], {"MY_OWN_VAR": "keep-me"})
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})
        self.assertNotIn("promptCacheTtl", settings)
        self.assertNotIn("autoCompactEnabled", settings)

    def test_3p_config_survives_by_default(self):
        self.seed_3p()
        self.install("--key", "k")
        self.run_bootstrap("uninstall")
        self.assertTrue(self.active_3p().is_file())

    def test_purge_3p_removes_it(self):
        self.seed_3p()
        self.install("--key", "k")
        self.run_bootstrap("uninstall", "--purge-3p")
        self.assertFalse(self.active_3p().exists())


class TestStatus(BootstrapTestCase):
    def test_reports_not_installed_on_clean_home(self):
        res = self.run_bootstrap("status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        info = json.loads(res.stdout)
        self.assertFalse(info["installed"])
        self.assertFalse(info["api_key_set"])

    def test_reports_installed_after_install(self):
        self.install("--key", "sk-test-key-123456")
        info = json.loads(self.run_bootstrap("status", "--json").stdout)
        self.assertTrue(info["installed"])
        self.assertTrue(info["api_key_set"])
        self.assertEqual(info["three_p_problems"], [])


class TestBackupPruning(unittest.TestCase):
    """备份只留最近 N 份：够回滚，不会只增不减地堆满磁盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _make(self, name, mtime):
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "f"), "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_keeps_newest_and_deletes_the_rest(self):
        paths = [self._make("plugin-%02d" % i, 1000 + i) for i in range(8)]
        removed = common.prune_backups(self.dir, 3, prefix="plugin-")
        self.assertEqual(sorted(os.path.basename(p) for p in removed),
                         ["plugin-00", "plugin-01", "plugin-02", "plugin-03", "plugin-04"])
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, ["plugin-05", "plugin-06", "plugin-07"])

    def test_prefix_scopes_the_pruning(self):
        """3p 单文件备份和整树备份同住一个父目录，互相不能删。"""
        self._make("plugin-00", 1000)
        keep = os.path.join(self.dir, "3p")
        os.makedirs(keep)
        common.prune_backups(self.dir, 1, prefix="plugin-")
        self.assertTrue(os.path.isdir(keep), "别的类型备份不该被这一刀带走")

    def test_no_op_when_under_limit(self):
        self._make("plugin-00", 1000)
        self.assertEqual(common.prune_backups(self.dir, 5, prefix="plugin-"), [])
        self.assertEqual(os.listdir(self.dir), ["plugin-00"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
