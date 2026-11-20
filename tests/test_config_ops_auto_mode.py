#!/usr/bin/env python3
"""3P 配置里关掉 Auto 模式（`autoModeEnabled`）的单测。

为什么需要这个文件
------------------
客户端对 Auto 模式的求值是 `code: 值 !== false`：

- **不写这个键 = Auto 可用**（默认就是开）；
- 只有显式 `false` 才是关掉。

所以这个开关有两条容易写反的地方，各对应一个真实后果：

1. **关的时候写成 `true`** → 在 Code 侧看起来一样，却把 Cowork 侧的 Auto 一起打开了。
   关就是删键，不是写 `true`。
2. **无脑覆盖** → 用户自己在客户端配置里显式写的 `true`，不能因为我们点了一下开关就抹掉；
   只有「我们自己写进去的那个 `false`」才回收。纪律同 `merge_client_disabled`。

跑法：
    python3 -m unittest discover -s tests -p "test_*.py"
    python3 tests/test_config_ops_auto_mode.py
"""
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config_ops  # noqa: E402

FIELD = config_ops.AUTO_MODE_FIELD


class DisableTest(unittest.TestCase):
    """打开开关 = 写 false。"""

    def test_writes_false_on_empty_bundle(self):
        new, changed = config_ops.apply_auto_mode({}, True)
        self.assertIs(new[FIELD], False)
        self.assertEqual(changed, [FIELD])

    def test_idempotent_when_already_false(self):
        new, changed = config_ops.apply_auto_mode({FIELD: False}, True)
        self.assertIs(new[FIELD], False)
        self.assertEqual(changed, [])

    def test_overrides_user_true(self):
        """用户自己写了 true，我们点「关闭」是明确动作，照样关掉。"""
        new, changed = config_ops.apply_auto_mode({FIELD: True}, True)
        self.assertIs(new[FIELD], False)
        self.assertEqual(changed, [FIELD])


class EnableTest(unittest.TestCase):
    """关掉开关 = 回到客户端默认（删键），不是写 true。"""

    def test_removes_our_false(self):
        new, changed = config_ops.apply_auto_mode({FIELD: False}, False)
        self.assertNotIn(FIELD, new)
        self.assertEqual(changed, [FIELD])

    def test_never_writes_true(self):
        new, _ = config_ops.apply_auto_mode({FIELD: False}, False)
        self.assertNotIn(FIELD, new)

    def test_leaves_user_true_alone(self):
        """用户显式写的 true 不是我们写的，回收时不许动。"""
        new, changed = config_ops.apply_auto_mode({FIELD: True}, False)
        self.assertIs(new[FIELD], True)
        self.assertEqual(changed, [])

    def test_noop_when_key_absent(self):
        new, changed = config_ops.apply_auto_mode({}, False)
        self.assertNotIn(FIELD, new)
        self.assertEqual(changed, [])


class OtherKeysTest(unittest.TestCase):
    """这份文件是客户端的资产：别的键一个都不许碰。"""

    def test_preserves_sealed_key_and_others(self):
        bundle = {
            "inferenceGatewayApiKey": "__sealed__:v1:abc",
            "disabledBuiltinTools": ["WebSearch"],
            "inferenceModels": [{"name": "claude-sonnet-5-5"}],
        }
        new, _ = config_ops.apply_auto_mode(bundle, True)
        self.assertEqual(new["inferenceGatewayApiKey"], "__sealed__:v1:abc")
        self.assertEqual(new["disabledBuiltinTools"], ["WebSearch"])
        self.assertEqual(new["inferenceModels"], [{"name": "claude-sonnet-5-5"}])

    def test_does_not_mutate_input(self):
        bundle = {FIELD: True}
        config_ops.apply_auto_mode(bundle, True)
        self.assertIs(bundle[FIELD], True)

    def test_tolerates_non_dict(self):
        new, changed = config_ops.apply_auto_mode(None, True)
        self.assertEqual(new, {FIELD: False})
        self.assertEqual(changed, [FIELD])


if __name__ == "__main__":
    unittest.main(verbosity=2)
