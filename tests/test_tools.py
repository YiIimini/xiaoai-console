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
            # 用跨平台命令：git 在所有 CI runner 上都有
            "allowed_cmds": "git,ls,cat,tail,python3,python,rm",
            "shell_timeout": 10,
            "max_output": 2000,
            "allow_write": True,
        }

    def run_cmd(self, cmd):
        return tools.dispatch(self.cfg, "run_shell", {"cmd": cmd})

    def test_normal_command_ok(self):
        """用 git 而不是 ls：Windows runner 上没有 ls。"""
        self.assertIn("exit=0", self.run_cmd("git --version"))

    def test_newline_injection_blocked(self):
        """P0：换行曾被用来绕过白名单再执行一条命令。"""
        self.assertIn("拒绝", self.run_cmd("git --version\nid -un"))
        self.assertIn("拒绝", self.run_cmd("git --version\rwhoami"))
        self.assertIn("拒绝", self.run_cmd("git --version\n\nrm -rf /"))

    def test_metachars_blocked(self):
        for c in ("git; id", "git && id", "git | sh", "git > out", "git `id`", "git $(id)"):
            with self.subTest(cmd=c):
                self.assertIn("拒绝", self.run_cmd(c))

    def test_dangerous_head_never_runs(self):
        """即使写进白名单，危险命令也不执行（先命中片段黑名单或危险头，都算拒绝）。"""
        out = self.run_cmd("rm -rf .")
        self.assertTrue("危险命令" in out or "被禁片段" in out, out)
        self.assertIn("危险命令", self.run_cmd("rm -r build"))

    def test_not_in_whitelist(self):
        self.assertIn("不在白名单", self.run_cmd("wc -l a.txt"))

    def test_network_tools_always_blocked(self):
        """联网类命令永远不放行，避免被用来外带数据。"""
        for c in ("curl http://x", "wget http://x", "nc -l 1234", "ssh host"):
            with self.subTest(cmd=c):
                self.assertIn("危险命令", self.run_cmd(c))

    def test_arbitrary_code_head_is_flagged(self):
        """解释器类命令可执行任意代码，必须给出显式提示（跨平台：谁在就用谁）。"""
        py = "python" if os.name == "nt" else "python3"
        out = self.run_cmd('%s -c "print(1)"' % py)
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
        self.cfg["repo"] = os.path.join(tempfile.gettempdir(), "no-such-repo-xyz")
        self.assertIn("不存在", self.run_cmd("git --version"))


if __name__ == "__main__":
    unittest.main()
