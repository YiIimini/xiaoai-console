"""执行沙箱的回归测试：重点是命令注入与路径越界。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import tools


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("hello")
        self.cfg = {
            "repo": self.repo,
            "allowed_cmds": "ls,cat,git,python3,rm",
            "shell_timeout": 10,
            "max_output": 2000,
            "allow_write": True,
        }

    def run_cmd(self, cmd):
        return tools.dispatch(self.cfg, "run_shell", {"cmd": cmd})

    def test_normal_command_ok(self):
        self.assertIn("exit=0", self.run_cmd("ls"))

    def test_newline_injection_blocked(self):
        """P0：换行曾被用来绕过白名单再执行一条命令。"""
        self.assertIn("拒绝", self.run_cmd("ls\nid -un"))
        self.assertIn("拒绝", self.run_cmd("ls\rwhoami"))
        self.assertIn("拒绝", self.run_cmd("ls\n\nrm -rf /"))

    def test_metachars_blocked(self):
        for c in ("ls; id", "ls && id", "ls | sh", "ls > out", "ls `id`", "ls $(id)"):
            with self.subTest(cmd=c):
                self.assertIn("拒绝", self.run_cmd(c))

    def test_dangerous_head_never_runs(self):
        """即使写进白名单，危险命令也不执行（先命中片段黑名单或危险头，都算拒绝）。"""
        out = self.run_cmd("rm -rf .")
        self.assertTrue("危险命令" in out or "被禁片段" in out, out)
        self.assertIn("危险命令", self.run_cmd("rm -r build"))

    def test_not_in_whitelist(self):
        self.assertIn("不在白名单", self.run_cmd("tail a.txt"))

    def test_network_tools_always_blocked(self):
        """联网类命令永远不放行，避免被用来外带数据。"""
        for c in ("curl http://x", "wget http://x", "nc -l 1234", "ssh host"):
            with self.subTest(cmd=c):
                self.assertIn("危险命令", self.run_cmd(c))

    def test_arbitrary_code_head_is_flagged(self):
        out = self.run_cmd("python3 -c 'print(1)'")
        self.assertIn("任意代码", out)

    def test_path_traversal_blocked(self):
        out = tools.dispatch(self.cfg, "read_file", {"path": "../../etc/passwd"})
        self.assertIn("越界", out)

    def test_write_respects_flag(self):
        self.cfg["allow_write"] = False
        out = tools.dispatch(self.cfg, "write_file", {"path": "b.txt", "content": "x"})
        self.assertIn("关闭", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "b.txt")))

    def test_write_inside_repo_ok(self):
        out = tools.dispatch(self.cfg, "write_file", {"path": "sub/b.txt", "content": "hi"})
        self.assertIn("ok", out)
        with open(os.path.join(self.repo, "sub", "b.txt")) as f:
            self.assertEqual(f.read(), "hi")

    def test_missing_repo_reports_clearly(self):
        self.cfg["repo"] = "/definitely/not/here"
        self.assertIn("不存在", self.run_cmd("ls"))


if __name__ == "__main__":
    unittest.main()
