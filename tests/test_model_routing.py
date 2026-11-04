"""模型路由与 3P 配置单真理源单元测试。

验证：
1. find_active_3p_config_path 能准确根据 _meta.json 或最新修改时间定位生效配置；
2. read_3p_models 准确提取 name -> labelOverride 映射并去除空白字符；
3. read_models_config 以 3P 配置为唯一真理源，支持 mtime 驱动的秒级自动热重载；
4. map_model 准确映射模型代号并剥离 [1m] 等后缀；
5. atomic_write_json 原子落盘且保留原文件权限位（3P 配置里有 API Key，默认 0600）；
6. THREEP_CONFIG_GLOB 由 get_3p_config_dir() 派生，全仓只有一份 3P 目录定义。

**用 unittest 而不是 pytest 写**：本仓的契约门禁是 `tools/contract_check.py` 里的
`unittest discover -s tools`。pytest 风格在这个门禁下会**收集到 0 个用例且不报错** ——
一套绿油油的假保险，比没有测试更糟。要跑单文件：`python3 tools/test_model_routing.py`。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "claude-tuner", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common
# 家目录隔离必须在 import gateway 之前（gateway 在 import 时就定下 BASE_DIR）
import _isolated_home  # noqa: E402,F401

import gateway


class ModelRoutingTestCase(unittest.TestCase):
    """隔离夹具：把 3P configLibrary 与 claude-tuner 运行目录都指到临时目录。

    进程内缓存（`gateway._cached_models` / `_cached_mtime_state` / `_warned_models_empty`、
    `common._meta_cache`）必须逐条重置 —— 它们正是本组用例要验的对象，
    带着上一个用例的值进来会让断言假绿。
    """

    #: 夹具会临时改写、tearDown 时逐条还原的环境变量
    _ENV_KEYS = ("CLAUDE_TUNER_3P_DIR", "CLAUDE_TUNER_HOME")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = tempfile.mkdtemp(dir=self._tmp.name)
        from pathlib import Path
        config_lib = Path(root) / "configLibrary"
        config_lib.mkdir(parents=True)
        bridge_home = Path(root) / ".claude-tuner"
        bridge_home.mkdir(parents=True)

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        self._saved_base_dir = gateway.BASE_DIR
        self._saved_config_file = gateway.CONFIG_FILE
        self.addCleanup(self._restore)

        os.environ["CLAUDE_TUNER_3P_DIR"] = str(config_lib)
        os.environ["CLAUDE_TUNER_HOME"] = str(bridge_home)
        gateway.BASE_DIR = str(bridge_home)
        # CONFIG_FILE 是导入时按 BASE_DIR 算出来的常量，只改 BASE_DIR 不够 ——
        # 不改它，_read_config() 会去读真实的 ~/.claude-tuner/config.json，
        # 用例的结论就变成「这台机器上碰巧配了什么」。
        gateway.CONFIG_FILE = str(bridge_home / "config.json")
        gateway._config_cache = None
        gateway._config_mtime = 0.0

        gateway._cached_models = None
        gateway._cached_mtime_state = None
        gateway._warned_models_empty = False
        common._meta_cache = (None, 0.0, "")

        self.config_lib = config_lib
        self.bridge_home = bridge_home

    def _restore(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        gateway.BASE_DIR = self._saved_base_dir
        gateway.CONFIG_FILE = self._saved_config_file
        gateway._config_cache = None
        gateway._config_mtime = 0.0

    def write_bridge_config(self, cfg):
        """写网关自己的 config.json（default_model / 各开关都从这里读）。"""
        (self.bridge_home / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def write_profile(self, applied_id, models):
        """写一份被 `_meta.json` 指为生效的 3P 配置，返回其路径。"""
        (self.config_lib / "_meta.json").write_text(
            json.dumps({"appliedId": applied_id}), encoding="utf-8")
        path = self.config_lib / f"{applied_id}.json"
        path.write_text(json.dumps({"inferenceModels": models}), encoding="utf-8")
        return path

    # ── find_active_3p_config_path ──

    def test_find_active_3p_config_via_meta(self):
        """当 _meta.json 存在时，应根据 appliedId 定位配置文件。"""
        applied_id = "test-uuid-1234"
        target_json = self.config_lib / f"{applied_id}.json"
        target_json.write_text(json.dumps({"name": "Profile 1"}), encoding="utf-8")
        (self.config_lib / "other-uuid-5678.json").write_text(
            json.dumps({"name": "Profile 2"}), encoding="utf-8")
        (self.config_lib / "_meta.json").write_text(
            json.dumps({"appliedId": applied_id}), encoding="utf-8")

        self.assertEqual(common.find_active_3p_config_path(), str(target_json))

    def test_find_active_3p_config_fallback_newest(self):
        """当 _meta.json 缺失或无效时，应回退选择最新修改的 JSON 文件。"""
        f1 = self.config_lib / "profile-old.json"
        f1.write_text(json.dumps({"name": "Old"}), encoding="utf-8")
        os.utime(str(f1), (time.time() - 100, time.time() - 100))

        f2 = self.config_lib / "profile-new.json"
        f2.write_text(json.dumps({"name": "New"}), encoding="utf-8")
        os.utime(str(f2), (time.time(), time.time()))

        self.assertEqual(common.find_active_3p_config_path(), str(f2))

    # ── read_3p_models ──

    def test_read_3p_models_parsing(self):
        """验证从 3P 配置中解析模型：大小写、空白修剪与无 labelOverride 时的处理。"""
        self.write_profile("test-models-uuid", [
            {"name": "claude-sonnet-5", "labelOverride": "  space-bunny-free  ", "supports1m": True},
            {"name": "Claude-Opus-5", "labelOverride": "deepseek-flash"},
            {"name": "claude-haiku-4-5", "labelOverride": ""},   # 空白时应退回 name
            {"name": "invalid-entry"},
            "not-a-dict",                                        # 非 dict 项必须被跳过而不是炸
        ])

        models = common.read_3p_models()
        self.assertEqual(models["claude-sonnet-5"], "space-bunny-free")
        self.assertEqual(models["claude-opus-5"], "deepseek-flash")
        self.assertEqual(models["claude-haiku-4-5"], "claude-haiku-4-5")
        self.assertEqual(models["invalid-entry"], "invalid-entry")

    # ── read_models_config 热重载 ──

    def test_meta_applied_id_switch_invalidates_cache(self):
        """切换 _meta.json 的 appliedId 后，缓存与 _meta 解析缓存都必须失效。"""
        for applied_id, target in (("profile-a", "model-a"), ("profile-b", "model-b")):
            (self.config_lib / f"{applied_id}.json").write_text(json.dumps({
                "inferenceModels": [{"name": "claude-sonnet-5", "labelOverride": target}]
            }), encoding="utf-8")

        meta = self.config_lib / "_meta.json"
        meta.write_text(json.dumps({"appliedId": "profile-a"}), encoding="utf-8")
        self.assertEqual(gateway.read_models_config()["claude-sonnet-5"], "model-a")

        # 用户在 3P 面板里切换到另一个 profile
        meta.write_text(json.dumps({"appliedId": "profile-b"}), encoding="utf-8")
        new_mtime = time.time() + 2
        os.utime(str(meta), (new_mtime, new_mtime))

        self.assertEqual(gateway.read_models_config()["claude-sonnet-5"], "model-b")

    def test_read_models_config_hot_reload_on_3p_change(self):
        """当 3P 配置文件修改时，read_models_config 应能通过 mtime 自动热加载新配置。"""
        cfg_file = self.write_profile(
            "profile-hot", [{"name": "claude-sonnet-5", "labelOverride": "model-v1"}])
        self.assertEqual(gateway.read_models_config()["claude-sonnet-5"], "model-v1")

        # 模拟用户在 3P 界面中修改模型并保存
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({
            "inferenceModels": [{"name": "claude-sonnet-5", "labelOverride": "model-v2"}]
        }), encoding="utf-8")
        # 显式提升 mtime 保证检测到时间差异
        new_mtime = time.time() + 2
        os.utime(str(cfg_file), (new_mtime, new_mtime))

        self.assertEqual(gateway.read_models_config()["claude-sonnet-5"], "model-v2")

    # ── map_model ──

    def test_map_model_execution(self):
        """验证 map_model 函数正确解析代号、忽略上下文后缀 [1m]。"""
        self.write_profile("profile-map",
                           [{"name": "claude-sonnet-5", "labelOverride": "space-bunny-free"}])

        self.assertEqual(gateway.map_model("claude-sonnet-5"), "space-bunny-free")
        self.assertEqual(gateway.map_model("claude-sonnet-5[1m]"), "space-bunny-free")
        self.assertEqual(gateway.map_model("Claude-Sonnet-5"), "space-bunny-free")
        self.assertEqual(gateway.map_model("unknown-custom-model"), "unknown-custom-model")

    def test_passthrough_strips_client_side_suffix(self):
        """后缀是客户端自己的上下文标记，任何情况下都不该发给上游 —— 包括没命中映射表时。"""
        self.write_profile("profile-suffix", [{"name": "claude-sonnet-5",
                                               "labelOverride": "space-bunny-free"}])
        saved = gateway.log_event
        gateway.log_event = lambda ev, req_id=None: None
        self.addCleanup(lambda: setattr(gateway, "log_event", saved))
        self.assertEqual(gateway.map_model("some-vendor-model[1m]"), "some-vendor-model")

    def test_unmapped_codename_falls_back_to_default_model(self):
        """客户端代号漏配时落到 default_model，而不是原样透传让上游 404。"""
        self.write_profile("profile-fallback", [{"name": "claude-opus-5-5",
                                                 "labelOverride": "deepseek-flash"}])
        self.write_bridge_config({"default_model": "deepseek-flash"})

        events = []
        saved = gateway.log_event
        gateway.log_event = lambda ev, req_id=None: events.append(ev)
        self.addCleanup(lambda: setattr(gateway, "log_event", saved))

        # 客户端模型目录里存在、但映射表没配的代号
        self.assertEqual(gateway.map_model("claude-opus-5"), "deepseek-flash")
        self.assertEqual([e["event"] for e in events], ["unknown_model_fallback"])
        # 配了的代号照旧走映射
        self.assertEqual(gateway.map_model("claude-opus-5-5"), "deepseek-flash")

    def test_fallback_never_hijacks_other_vendors(self):
        """非客户端代号（用户手填的真实上游模型名）永不被兜底改写。"""
        self.write_profile("profile-other", [{"name": "claude-opus-5-5",
                                              "labelOverride": "deepseek-flash"}])
        self.write_bridge_config({"default_model": "deepseek-flash"})
        self.assertEqual(gateway.map_model("kimi-k2"), "kimi-k2")
        self.assertEqual(gateway.map_model("deepseek-v4-pro"), "deepseek-v4-pro")

    def test_fallback_can_be_disabled(self):
        self.write_profile("profile-off", [{"name": "claude-opus-5-5",
                                            "labelOverride": "deepseek-flash"}])
        self.write_bridge_config({"default_model": "deepseek-flash",
                                  "unknown_model_fallback": False})
        self.assertEqual(gateway.map_model("claude-opus-5"), "claude-opus-5")

    def test_fallback_defaults_to_first_mapping_target(self):
        """没显式配 default_model 时，兜底取映射表里第一个目标 ——
        用户换了上游但没改 default_model，也落回他自己配的那个模型。"""
        self.write_profile("profile-nodefault", [{"name": "claude-opus-5-5",
                                                  "labelOverride": "my-own-model"}])
        self.assertEqual(gateway.map_model("claude-opus-5"), "my-own-model")

    def test_fallback_needs_somewhere_to_land(self):
        """映射表为空、也没配 default_model（纯 CLI 且没建 3P 配置）时保持原行为：原样透传。"""
        self.assertEqual(gateway.map_model("claude-opus-5"), "claude-opus-5")

    def test_relay_user_is_not_hijacked_into_third_party_model(self):
        """官方中转站用户的映射目标是 claude-* 本身，兜底绝不能把它换成第三方模型。"""
        self.write_profile("profile-relay", [{"name": "claude-opus-5-5",
                                              "labelOverride": "claude-opus-5-5"}])
        self.write_bridge_config({"default_model": ""})
        self.assertEqual(gateway.map_model("claude-opus-5"), "claude-opus-5-5")
        self.assertEqual(gateway.map_model("claude-opus-5-5"), "claude-opus-5-5")

    def test_map_model_warns_once_when_config_missing(self):
        """读不到 3P 配置时（映射表为空）：模型名原样透传，且只在进程内告警一次。

        否则纯 CLI 场景下每个请求都记一条 WARN，日志会被刷爆。
        """
        events = []
        saved = gateway.log_event
        gateway.log_event = lambda ev, req_id=None: events.append(ev)
        self.addCleanup(lambda: setattr(gateway, "log_event", saved))

        for name in ("claude-sonnet-5", "claude-opus-5", "gpt-x"):
            self.assertEqual(gateway.map_model(name), name)

        self.assertEqual([e["event"] for e in events], ["models_config_empty"])


class AtomicWriteTestCase(unittest.TestCase):
    """不依赖 3P 目录，单独成类（夹具里的那些 3P 路径对它没有意义）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_atomic_write_json_preserves_mode(self):
        """原子写必须保留原文件权限位：3P 配置含 API Key，落成 0644 等于对同机用户公开。"""
        path = os.path.join(self._tmp.name, "profile.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"inferenceGatewayApiKey": "sk-secret"}, fh)
        os.chmod(path, 0o600)

        common.atomic_write_json(
            path, {"inferenceGatewayApiKey": "sk-secret", "inferenceModels": []})

        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(path + ".tmp"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["inferenceModels"], [])

        # 目标不存在时按最保守的 0600 落盘，而不是跟随 umask
        fresh = os.path.join(self._tmp.name, "fresh.json")
        common.atomic_write_json(fresh, {"a": 1})
        self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o600)


class Test3pGlobDerivation(unittest.TestCase):
    def test_3p_glob_derives_from_config_dir(self):
        """THREEP_CONFIG_GLOB 必须由 get_3p_config_dir() 派生，避免网关/同步各读一个目录。

        在子进程里设 CLAUDE_TUNER_3P_DIR 后再 import：模块常量只在导入时求值一次，
        同进程内改 env 证明不了派生关系。
        """
        code = (
            "import os, sys;"
            f"sys.path.insert(0, {SCRIPTS_DIR!r});"
            "import common;"
            "print(common.THREEP_CONFIG_GLOB)"
        )
        env = dict(os.environ, CLAUDE_TUNER_3P_DIR="/tmp/ccds-3p-derived")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(),
                         os.path.join("/tmp/ccds-3p-derived", "*.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
