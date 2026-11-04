#!/usr/bin/env python3
"""claude-tuner/scripts/config_ops.py 的单测：把「写用户配置」的判定钉死。

为什么需要这个文件
------------------
配置台与安装脚本会去改用户的三样东西：3P 配置、`~/.claude/settings.json`、
网关自己的 config.json。这条路径一旦判错，后果不是报错而是**静默改坏用户环境**
（删掉客户端要求的字段、把别的设置覆盖掉、重复安装每次都重写）。

所以这里不测「能不能跑」，只测判定本身：合并是否保留未知键、幂等时是否零改动、
门禁判据是否只放行本机回环。断言对象是生产同一份函数（webui.py / bootstrap.py
引用的就是它们），不是复刻件。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 tools/test_config_ops.py
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def load_config_ops():
    """按文件路径加载 config_ops.py；它内部 `from common import ...`，故先把 scripts/ 入 path。"""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("config_ops_under_test",
                                                  SCRIPTS_DIR / "config_ops.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["config_ops_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class ConfigOpsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ops = load_config_ops()


class TestMaskApiKey(ConfigOpsTestCase):
    def test_empty_and_short_are_fully_hidden(self):
        self.assertEqual(self.ops.mask_api_key(""), "")
        self.assertEqual(self.ops.mask_api_key("   "), "")
        self.assertEqual(self.ops.mask_api_key("sk-abc"), "****")

    def test_middle_never_leaks(self):
        key = "sk-1234567890abcdefghij"
        masked = self.ops.mask_api_key(key)
        self.assertTrue(masked.startswith("sk-1"))
        self.assertTrue(masked.endswith("ghij"))
        self.assertNotIn(key[4:-4], masked)
        self.assertIn("****", masked)

    def test_unchanged_detection(self):
        key = "sk-1234567890abcdefghij"
        for submitted in ("", "   ", self.ops.MASK_SENTINEL, self.ops.mask_api_key(key)):
            self.assertTrue(self.ops.key_unchanged(submitted, key), submitted)
        self.assertFalse(self.ops.key_unchanged("sk-a-brand-new-key-value", key))


class TestGate(ConfigOpsTestCase):
    def test_constant_time_eq(self):
        self.assertTrue(self.ops.constant_time_eq("abc", "abc"))
        self.assertFalse(self.ops.constant_time_eq("abc", "abd"))
        self.assertFalse(self.ops.constant_time_eq("", "abc"))

    def test_host_must_be_loopback(self):
        ok = ("127.0.0.1:8789", "localhost:8789", "127.0.0.1", "localhost",
              "LOCALHOST:8789")
        bad = ("evil.com:8789", "127.0.0.1:9999", "", "127.0.0.1.evil.com:8789",
               "127.0.0.1.attacker.net")
        for h in ok:
            self.assertTrue(self.ops.host_ok(h, 8789), h)
        for h in bad:
            self.assertFalse(self.ops.host_ok(h, 8789), h)

    def test_origin_rules(self):
        self.assertTrue(self.ops.origin_ok("", 8789))               # curl 等无 Origin
        self.assertTrue(self.ops.origin_ok("http://127.0.0.1:8789", 8789))
        self.assertFalse(self.ops.origin_ok("http://evil.com", 8789))
        self.assertFalse(self.ops.origin_ok("null", 8789))
        self.assertFalse(self.ops.origin_ok("http://127.0.0.1:9999", 8789))


class TestPatchMerge(ConfigOpsTestCase):
    def test_rules_patch_drops_unknown_keys(self):
        new, changed = self.ops.apply_rules_patch({}, {"strip_date": False,
                                                        "not_a_rule": True})
        self.assertFalse(new["rules"]["strip_date"])
        self.assertNotIn("not_a_rule", new["rules"])
        self.assertEqual(changed, ["strip_date"])

    def test_rules_patch_is_idempotent(self):
        first, _ = self.ops.apply_rules_patch({}, {"strip_date": True})
        _, changed = self.ops.apply_rules_patch(first, {"strip_date": True})
        self.assertEqual(changed, [])

    def test_rules_patch_coerces_to_bool(self):
        new, _ = self.ops.apply_rules_patch({}, {"strip_date": 0})
        self.assertIs(new["rules"]["strip_date"], False)

    def test_pruning_patch_replaces_list(self):
        new, changed = self.ops.apply_pruning_patch(
            {}, {"custom_strip_tools": ["Foo", "Bar"]})
        self.assertEqual(new["tool_pruning"]["custom_strip_tools"], ["Foo", "Bar"])
        self.assertIn("custom_strip_tools", changed)

    def test_pruning_patch_keeps_source_config_untouched(self):
        original = {"rules": {"strip_date": True}, "upstream_url": "http://x"}
        self.ops.apply_rules_patch(original, {"strip_date": False})
        self.assertTrue(original["rules"]["strip_date"])


class TestMergeSettings(ConfigOpsTestCase):
    def test_preserves_unrelated_keys_and_other_env(self):
        existing = {
            "enabledPlugins": {"a": True},
            "sandbox": {"enabled": False},
            "env": {"MY_OWN_VAR": "1", "ANTHROPIC_BASE_URL": "https://old"},
        }
        new, changed = self.ops.merge_settings(
            existing, {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8789"},
                       "promptCacheTtl": "1h"})
        self.assertEqual(new["enabledPlugins"], {"a": True})
        self.assertEqual(new["sandbox"], {"enabled": False})
        self.assertEqual(new["env"]["MY_OWN_VAR"], "1")
        self.assertEqual(new["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8789")
        self.assertEqual(new["promptCacheTtl"], "1h")
        self.assertEqual(changed, ["ANTHROPIC_BASE_URL", "promptCacheTtl"])

    def test_unknown_keys_are_not_writable(self):
        new, changed = self.ops.merge_settings({}, {"env": {"PATH": "/evil"},
                                                    "sandbox": {"enabled": True}})
        self.assertEqual(changed, [])
        self.assertNotIn("env", new)
        self.assertNotIn("sandbox", new)

    def test_noop_reports_no_change(self):
        existing = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8789"}}
        _, changed = self.ops.merge_settings(
            existing, {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8789"}})
        self.assertEqual(changed, [])

    def test_unset_removes_only_our_keys(self):
        existing = {"env": {"MY_OWN_VAR": "1", "ANTHROPIC_AUTH_TOKEN": "sk-x"},
                    "includeCoAuthoredBy": False, "attribution": {"commit": ""}}
        new, changed = self.ops.merge_settings(
            existing, {}, unset_env=["ANTHROPIC_AUTH_TOKEN"],
            unset_top=["includeCoAuthoredBy"])
        self.assertEqual(new["env"], {"MY_OWN_VAR": "1"})
        self.assertNotIn("includeCoAuthoredBy", new)
        self.assertEqual(new["attribution"], {"commit": ""})   # 不在 unset 里的保留
        self.assertEqual(sorted(changed), ["ANTHROPIC_AUTH_TOKEN", "includeCoAuthoredBy"])

    def test_unset_drops_empty_env_object(self):
        new, _ = self.ops.merge_settings(
            {"env": {"ANTHROPIC_BASE_URL": "http://x"}}, {},
            unset_env=["ANTHROPIC_BASE_URL"])
        self.assertNotIn("env", new)

    def test_managed_settings_present(self):
        self.assertTrue(self.ops.managed_settings_present({"promptCacheTtl": "1h"}))
        self.assertTrue(self.ops.managed_settings_present({"env": {"ANTHROPIC_MODEL": "x"}}))
        self.assertFalse(self.ops.managed_settings_present({"enabledPlugins": {}}))
        self.assertFalse(self.ops.managed_settings_present(None))

    def test_cli_env_matches_official_shape(self):
        env = self.ops.cli_env("http://127.0.0.1:8789", "sk-test", "deepseek-flash")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8789")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "deepseek-flash")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "deepseek-flash[1m]")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "deepseek-flash")
        # 官方文档里的九个变量一个不少
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
            self.assertIn(k, env)


class Test3PBundle(ConfigOpsTestCase):
    def base(self):
        return {
            "inferenceGatewayBaseUrl": "http://127.0.0.1:8789",
            "inferenceGatewayApiKey": "sk-old-key-1234",
            "inferenceProvider": "gateway",
            "inferenceCredentialKind": "static",
            "inferenceGatewayAuthScheme": "x-api-key",
            "chatTabEnabled": True,
            "inferenceModels": [{"name": "claude-opus-5-5", "labelOverride": "old"}],
            "someFutureClientField": {"nested": True},
        }

    def test_unknown_keys_survive(self):
        new, _ = self.ops.build_3p_bundle(self.base(), gateway_url="http://127.0.0.1:8789")
        self.assertEqual(new["someFutureClientField"], {"nested": True})
        self.assertTrue(new["chatTabEnabled"])

    def test_user_set_defaults_are_not_overwritten(self):
        existing = dict(self.base(), scheduledTasksEnabled=True,
                        coworkEgressAllowedHosts=["api.deepseek.com"])
        new, changed = self.ops.build_3p_bundle(existing)
        self.assertTrue(new["scheduledTasksEnabled"])
        self.assertEqual(new["coworkEgressAllowedHosts"], ["api.deepseek.com"])
        self.assertNotIn("scheduledTasksEnabled", changed)

    def test_key_only_replaced_when_provided(self):
        new, _ = self.ops.build_3p_bundle(self.base())
        self.assertEqual(new["inferenceGatewayApiKey"], "sk-old-key-1234")
        new2, changed = self.ops.build_3p_bundle(self.base(), api_key="sk-new-key-5678")
        self.assertEqual(new2["inferenceGatewayApiKey"], "sk-new-key-5678")
        self.assertIn("inferenceGatewayApiKey", changed)

    def test_idempotent(self):
        first, _ = self.ops.build_3p_bundle(
            self.base(), gateway_url="http://127.0.0.1:8789",
            models=self.ops.default_model_mapping())
        _, changed = self.ops.build_3p_bundle(
            first, gateway_url="http://127.0.0.1:8789",
            models=self.ops.default_model_mapping())
        self.assertEqual(changed, [])

    def test_source_dict_not_mutated(self):
        original = self.base()
        self.ops.build_3p_bundle(original, api_key="sk-new-key-5678")
        self.assertEqual(original["inferenceGatewayApiKey"], "sk-old-key-1234")

    def test_bundled_skills_只传值时才写(self):
        """`disableBundledSkills` 是 **3P 配置**的键（客户端自己的设置页也写那儿）。

        它曾被我方放进 ~/.claude/settings.json 的写白名单 —— 那一半从来没生效过，
        而且页面读我们那份、客户端读 3P 配置，同一次改动两边显示的消息还不一样。
        """
        new, _ = self.ops.build_3p_bundle(self.base())
        self.assertNotIn("disableBundledSkills", new, "不传值就不写，留给客户端默认的「保留」")

        new2, changed = self.ops.build_3p_bundle(self.base(), bundled_skills=True)
        self.assertTrue(new2["disableBundledSkills"])
        self.assertIn("disableBundledSkills", changed)

        new3, changed = self.ops.build_3p_bundle(new2, bundled_skills=False)
        self.assertFalse(new3["disableBundledSkills"])
        self.assertIn("disableBundledSkills", changed)

    def test_两个键各住各的地方(self):
        """`disableBundledSkills` 归 3P 配置，`enableWorkflows` 归用户设置 —— 别混。"""
        self.assertNotIn("disableBundledSkills", self.ops.SETTINGS_TOP_KEYS)
        self.assertIn("enableWorkflows", self.ops.SETTINGS_TOP_KEYS)


class TestValidate3P(ConfigOpsTestCase):
    def valid(self):
        return {
            "inferenceProvider": "gateway",
            "inferenceCredentialKind": "static",
            "inferenceGatewayBaseUrl": "http://127.0.0.1:8789",
            "inferenceGatewayApiKey": "sk-key-123456",
            "inferenceModels": [{"name": "claude-opus-5-5", "labelOverride": "deepseek-flash"}],
        }

    def test_valid_config_passes(self):
        self.assertEqual(self.ops.validate_3p_config(self.valid()), [])

    def test_missing_pieces_are_reported(self):
        cases = [
            ("inferenceGatewayApiKey", ""),
            ("inferenceGatewayBaseUrl", ""),
            ("inferenceModels", []),
            ("inferenceModels", [{"labelOverride": "x"}]),
            ("inferenceProvider", "anthropic"),
            ("inferenceCredentialKind", None),
        ]
        for key, value in cases:
            cfg = self.valid()
            cfg[key] = value
            self.assertTrue(self.ops.validate_3p_config(cfg), key)

    def test_not_a_dict(self):
        self.assertEqual(self.ops.validate_3p_config(None), ["配置不是对象"])


class TestInstalledDetection(ConfigOpsTestCase):
    def test_detects_installed(self):
        cfg = {"inferenceProvider": "gateway",
               "inferenceGatewayApiKey": "sk-key-123456",
               "inferenceGatewayBaseUrl": "http://127.0.0.1:8789"}
        self.assertTrue(self.ops.is_already_installed(cfg, {}))
        self.assertTrue(self.ops.is_already_installed(
            dict(cfg, inferenceGatewayBaseUrl="https://api.deepseek.com/anthropic"),
            {"upstream_url": "https://api.deepseek.com/anthropic/v1/messages"}))

    def test_not_installed(self):
        self.assertFalse(self.ops.is_already_installed(None, {}))
        self.assertFalse(self.ops.is_already_installed({}, {}))
        self.assertFalse(self.ops.is_already_installed(
            {"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": "http://127.0.0.1:8789"}, {}))
        self.assertFalse(self.ops.is_already_installed(
            {"inferenceProvider": "anthropic", "inferenceGatewayApiKey": "sk-key-123456"}, {}))


class TestModelMapping(ConfigOpsTestCase):
    # 这份清单抄自客户端自己的模型目录
    # （~/Library/Application Support/Claude-3p/model-catalog/published.json 的
    #   cc / ccd / chat / cowork 四个面的 model_selector_config）。
    # 它变 = 客户端加了新代号，默认映射必须跟着补，否则用户选中新模型就原样透传给上游。
    EXPECTED_CLIENT_MODELS = [
        "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
        "claude-opus-4-6", "claude-opus-4-5-20251101", "claude-opus-4-1-20250805",
        "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
        "claude-haiku-5-5", "claude-haiku-4-5-20251001",
        "claude-fable-5-1", "claude-fable-5",
    ]

    def test_client_model_list_is_complete(self):
        self.assertEqual(sorted(self.ops.CLIENT_MODEL_IDS),
                         sorted(self.EXPECTED_CLIENT_MODELS))

    def test_default_mapping_is_only_the_recommended_pair(self):
        """默认只配最新代的两个代号：客户端选择器直接读这份映射，列全量只会变脏。"""
        mapping = self.ops.default_model_mapping()
        self.assertEqual([m["name"] for m in mapping],
                         list(self.ops.RECOMMENDED_MODEL_IDS))
        for item in mapping:
            self.assertEqual(item["labelOverride"], "deepseek-flash")
            self.assertTrue(item["prefer1m"])

    def test_recommended_ids_are_real_codenames(self):
        for mid in self.ops.RECOMMENDED_MODEL_IDS:
            self.assertIn(mid, self.EXPECTED_CLIENT_MODELS, mid)

    def test_find_preset(self):
        self.assertEqual(self.ops.find_preset("https://api.deepseek.com/anthropic/"), "deepseek")
        self.assertEqual(self.ops.find_preset("https://api.deepseek.com/anthropic"), "deepseek")
        self.assertEqual(self.ops.find_preset("https://my-proxy.example.com"), "custom")
        self.assertEqual(self.ops.find_preset(""), "custom")

    def test_unknown_upstream_gets_identity_mapping(self):
        """不认识的上游只能同名直通：既不会把上一家的模型名发过去，也不会写错大小写。"""
        for pid in ("custom",):
            mapping = self.ops.preset_mapping(pid)
            self.assertTrue(all(m["name"] == m["labelOverride"] for m in mapping), pid)
            self.assertEqual({m["name"] for m in mapping},
                             set(self.EXPECTED_CLIENT_MODELS), pid)

    def test_known_upstream_gets_its_model(self):
        for pid in ("deepseek", "opencode-go"):
            mapping = self.ops.preset_mapping(pid)
            self.assertEqual([m["name"] for m in mapping],
                             list(self.ops.RECOMMENDED_MODEL_IDS), pid)
            self.assertTrue(all(m["labelOverride"] == "deepseek-flash" for m in mapping), pid)

    def test_opencode_zen_gets_identity_mapping(self):
        """Zen 提供的是真正的 claude-* 官方模型（84 个里含全套），
        所以它的默认映射是同名直通，而不是指向某个第三方模型。"""
        preset = self.ops.get_preset("opencode-zen")
        self.assertEqual(preset["model"], "")
        mapping = self.ops.preset_mapping("opencode-zen")
        self.assertTrue(all(m["name"] == m["labelOverride"] for m in mapping))

    def test_opencode_zen_and_go_are_different_endpoints(self):
        """两家不同的服务：Go 是在 Zen 后面加 /go，填错就调不通。"""
        self.assertEqual(self.ops.get_preset("opencode-zen")["url"], "https://opencode.ai/zen")
        self.assertEqual(self.ops.get_preset("opencode-go")["url"], "https://opencode.ai/zen/go")
        self.assertEqual(self.ops.find_preset("https://opencode.ai/zen"), "opencode-zen")
        self.assertEqual(self.ops.find_preset("https://opencode.ai/zen/go"), "opencode-go")

    def test_explicit_target_beats_preset_default(self):
        mapping = self.ops.preset_mapping("custom", "my-own-model")
        self.assertEqual([m["name"] for m in mapping],
                         list(self.ops.RECOMMENDED_MODEL_IDS))
        self.assertTrue(all(m["labelOverride"] == "my-own-model" for m in mapping))

    def test_identity_mapping_is_complete(self):
        """恒等映射必须列全：漏一个代号就会走兜底，等于悄悄换模型。"""
        self.assertEqual({m["name"] for m in self.ops.identity_model_mapping()},
                         set(self.EXPECTED_CLIENT_MODELS))


class TestPaths(ConfigOpsTestCase):
    def test_backup_path_is_stable_and_sanitized(self):
        with mock.patch.dict(os.environ, {"CLAUDE_TUNER_HOME": "/tmp/ccds-test"}, clear=False):
            p = self.ops.backup_path("/x/configLibrary/abc.json", "2026-10-13T12:00:00+08:00")
        self.assertTrue(p.endswith("abc.json.20261013T1200000800.bak"), p)
        self.assertIn("/backups/3p/", p)

    def test_platform_paths_honours_overrides(self):
        env = {"HOME": "/tmp/fake-home", "USERPROFILE": "/tmp/fake-home",
               "XDG_CONFIG_HOME": "/tmp/fake-config", "CLAUDE_TUNER_HOME": "/tmp/fake-bridge",
               "CLAUDE_TUNER_3P_DIR": "/tmp/fake-3p"}
        with mock.patch.dict(os.environ, env, clear=False):
            paths = self.ops.platform_paths()
        self.assertEqual(paths["config_library"], "/tmp/fake-3p")
        self.assertEqual(paths["bridge_home"], "/tmp/fake-bridge")
        # 用 os.path.join 拼期望值：Windows 上分隔符是反斜杠，写死 `/tmp/fake-home/...`
        # 会让这条用例在那边必红 —— 而那种红是「测试写得不对」。
        self.assertEqual(paths["claude_settings"],
                         os.path.join("/tmp/fake-home", ".claude", "settings.json"))

    def test_platform_paths_linux_uses_xdg(self):
        """Linux 上绝不能落到 ~/Library —— 那是 macOS 的布局，写了没人读。"""
        with mock.patch("sys.platform", "linux"):
            env = {"XDG_CONFIG_HOME": "/fake/config", "HOME": "/fake/home"}
            with mock.patch.dict(os.environ, env, clear=False):
                paths = self.ops.platform_paths()
                self.assertEqual(paths["claude_dir"], os.path.join("/fake/config", "Claude"))
                self.assertEqual(paths["config_library"],
                                 os.path.join("/fake/config", "Claude-3p", "configLibrary"))

    def test_platform_paths_windows(self):
        with mock.patch("sys.platform", "win32"):
            env = {"APPDATA": "/fake/appdata", "HOME": "/fake/home"}
            with mock.patch.dict(os.environ, env, clear=False):
                paths = self.ops.platform_paths()
                self.assertEqual(paths["claude_dir"], os.path.join("/fake/appdata", "Claude"))
                self.assertEqual(paths["claude_3p_dir"], os.path.join("/fake/appdata", "Claude-3p"))
                self.assertEqual(paths["config_library"], os.path.join("/fake/appdata", "Claude-3p", "configLibrary"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOneMContextSwitch(ConfigOpsTestCase):
    """「1M 上下文」开关：它决定的是**客户端**怎么算窗口，不是上游能不能吃下。

    实测反馈：上游模型不加这个标记照样给 1M，但客户端不加就按小窗口算
    （约 200K）并自动提前压缩。所以这个开关必须在，且默认要与「现在能用的那份
    配置」一致 —— 默认关掉等于改变一切现有安装的行为。
    """

    def test_defaults_to_on(self):
        from common import DEFAULT_CONFIG
        self.assertTrue(DEFAULT_CONFIG["one_m_context"])

    def test_mapping_flags_follow_switch(self):
        on = self.ops.preset_mapping("deepseek", "deepseek-flash", True)
        off = self.ops.preset_mapping("deepseek", "deepseek-flash", False)
        self.assertTrue(all(m["supports1m"] and m["prefer1m"] for m in on))
        self.assertTrue(all(not m["supports1m"] and not m["prefer1m"] for m in off))

    def test_identity_mapping_also_follows(self):
        off = self.ops.identity_model_mapping(False)
        self.assertTrue(off)
        self.assertTrue(all(not m["prefer1m"] for m in off))

    def test_cli_env_suffix_only_when_on(self):
        on = self.ops.cli_env("http://127.0.0.1:1", "k", "deepseek-flash", True)
        off = self.ops.cli_env("http://127.0.0.1:1", "k", "deepseek-flash", False)
        self.assertEqual(on["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        self.assertEqual(off["ANTHROPIC_MODEL"], "deepseek-flash")
        # 小模型那档本来就不带后缀，别被这个开关牵连
        self.assertEqual(off["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "deepseek-flash")

    def test_bundle_writes_flag_not_half_state(self):
        bundle, _ = self.ops.build_3p_bundle({}, models=[], one_m=False)
        self.assertFalse(bundle["modelPrefer1mContext"])
        bundle, _ = self.ops.build_3p_bundle({}, models=[], one_m=True)
        self.assertTrue(bundle["modelPrefer1mContext"])

    def test_bundle_leaves_it_alone_when_unspecified(self):
        """不传就是不表态，不该凭空空写一个值。"""
        bundle, changed = self.ops.build_3p_bundle({"modelPrefer1mContext": False}, models=[])
        self.assertFalse(bundle["modelPrefer1mContext"])
        self.assertNotIn("modelPrefer1mContext", changed)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEnsureConfigInitialized(unittest.TestCase):
    """config.json 的补全与**退役开关键剔除**。

    默认值表就是 rules / tool_pruning 的唯一 schema：表里没有的键在代码里不可能有读取点，
    留着只会让配置显示「某项开着」而报文一个字节没变 —— 同一形态踩过两次
    （`strip_plugin_mgmt_tools`、`strip_design_sync_tools`）。这里把两个方向都钉住：
    退役键被剔除、其余键与值一字不动。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tuner-cfg-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "config.json")

    def _ensure(self):
        load_config_ops()          # 先把 scripts/ 放进 sys.path，common 才 import 得到
        from common import ensure_config_initialized
        return ensure_config_initialized(self.path)

    def test_退役开关键被剔除_用户取值不动(self):
        import json
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "tool_pruning": {"strip_design_sync_tools": True,
                                 "strip_notebook_tools": False,   # 用户关掉的，必须保住
                                 "custom_strip_tools": ["Foo"]},
                "rules": {"strip_date": False, "strip_old_gone_rule": True},
                "upstream_url": "https://example.test/v1/messages",
            }, fh)
        self._ensure()
        with open(self.path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertNotIn("strip_design_sync_tools", cfg["tool_pruning"])
        self.assertNotIn("strip_old_gone_rule", cfg["rules"])
        self.assertFalse(cfg["tool_pruning"]["strip_notebook_tools"], "用户关掉的开关不能被改回默认")
        self.assertEqual(cfg["tool_pruning"]["custom_strip_tools"], ["Foo"])
        self.assertFalse(cfg["rules"]["strip_date"])
        self.assertEqual(cfg["upstream_url"], "https://example.test/v1/messages")
        # 缺的键照旧补齐
        self.assertIn("strip_pr_ci_tools", cfg["tool_pruning"])
        self.assertIn("probe_interception", cfg["rules"])
