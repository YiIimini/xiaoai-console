"""配置可移植性：换电脑后路径必须自愈。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store


class PortableTest(unittest.TestCase):
    def test_relative_path_anchors_to_project(self):
        p = store.resolve("data/.mi.token")
        self.assertTrue(p.startswith(store.ROOT))
        self.assertTrue(p.endswith(os.path.join("data", ".mi.token")))

    def test_portable_collapses_inside_paths(self):
        self.assertEqual(store.portable_path(os.path.join(store.ROOT, "data", "x")), "data/x")

    def test_portable_keeps_outside_paths_absolute(self):
        outside = "/tmp/other-project"
        self.assertEqual(store.portable_path(outside), outside)

    def test_deep_merge_keeps_defaults(self):
        merged = store._deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
        self.assertEqual(merged["a"], {"x": 1, "y": 9})

    def test_import_rewrites_stale_machine_paths(self):
        incoming = {
            "mi": {"username": "u", "token_path": "/Users/old/secrets/.mi.token"},
            "agent": {"repo": "/Users/old/projects/gone"},
        }
        fixed = store.sanitize_for_import(incoming)
        self.assertEqual(fixed["mi"]["token_path"], "data/.mi.token")
        self.assertTrue(os.path.isdir(fixed["agent"]["repo"]))
        self.assertNotIn("old", fixed["agent"]["repo"])

    def test_export_strips_secrets_by_default(self):
        cfg = store.load_config()
        cfg["mi"]["password"] = "secret-pw"
        cfg["llm"]["api_key"] = "sk-secret"
        store.save_config(cfg)
        try:
            safe = store.export_config(include_secrets=False)["config"]
            self.assertEqual(safe["mi"]["password"], "")
            self.assertEqual(safe["llm"]["api_key"], "")
            full = store.export_config(include_secrets=True)["config"]
            self.assertEqual(full["mi"]["password"], "secret-pw")
        finally:
            store.save_config({"mi": {"password": ""}, "llm": {"api_key": ""}})


class MaskTest(unittest.TestCase):
    def test_mask_hides_credentials(self):
        cfg = store.load_config()
        cfg["mi"]["password"] = "pw123456"
        cfg["llm"]["api_key"] = "sk-abcdefghijklmn"
        masked = store.mask_config(cfg)
        self.assertEqual(masked["mi"]["password"], store.PASSWORD_MASK)
        self.assertNotIn("abcdefghij", masked["llm"]["api_key"])


if __name__ == "__main__":
    unittest.main()
