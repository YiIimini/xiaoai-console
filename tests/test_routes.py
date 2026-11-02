"""前后端路由一致性：前端怎么调，服务端就必须怎么接。

这个用例是为「未知接口: /api/diag/mi」写的——前端用 GET 调、
服务端只注册在 POST 里，点「测试登录」就报未知接口。
静态比对，不发真实请求，不会产生副作用。
"""
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_JS = ROOT / "app" / "web" / "app.js"
SERVER = ROOT / "app" / "server.py"

CALL_RE = re.compile(r'api\(\s*"([^"]+)"\s*(,)?')


def frontend_calls():
    """从 app.js 抽出 (方法, 路径)。有第二个参数＝带 body＝POST。"""
    src = APP_JS.read_text(encoding="utf-8")
    out = []
    for m in CALL_RE.finditer(src):
        path, has_body = m.group(1), bool(m.group(2))
        if not path.startswith("/api/"):
            continue
        tail = src[m.end():m.end() + 4].lstrip()
        dynamic = tail.startswith("+")      # api("/api/bridge/" + cmd, {}) 这种
        base = path.split("?")[0]
        method = None if dynamic else ("POST" if has_body else "GET")
        out.append((method, base))
    return out


def section(text, name):
    """取 server.py 里某个方法体的文本。"""
    start = text.index("def %s(" % name)
    rest = text[start + 10:]
    nxt = len(rest)
    for other in ("    def do_GET(", "    def do_POST(", "    def do_OPTIONS("):
        i = rest.find(other)
        if i != -1:
            nxt = min(nxt, i)
    return rest[:nxt]


class RouteConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.text = SERVER.read_text(encoding="utf-8")
        self.get_body = section(self.text, "do_GET")
        self.post_body = section(self.text, "do_POST")

    def _handled(self, method, path):
        if method is None:                  # 动态拼的路径，两种方法任一对上都算
            return self._handled("GET", path) or self._handled("POST", path)
        body = self.get_body if method == "GET" else self.post_body
        if '"%s"' % path in body:
            return True
        # 前缀路由（服务端用 startswith 或切前缀）
        for literal in re.findall(r'"([^"]+)"', body):
            if not literal.startswith("/api/"):
                continue
            if literal.endswith("/") and path.startswith(literal):
                return True
            if path.endswith("/") and literal.startswith(path):
                return True
        return False

    def test_every_frontend_call_has_a_route(self):
        missing = [(m, p) for m, p in frontend_calls() if not self._handled(m, p)]
        # 去重后报告，便于一眼看出是哪几个
        missing = sorted(set(missing))
        self.assertEqual(missing, [], "前端调用了服务端没接的接口：%s" % (missing,))

    def test_diag_endpoints_accept_get(self):
        """「测试登录」「测试连通」都是 GET，曾经只注册在 POST。"""
        self.assertIn('"/api/diag/mi"', self.get_body)
        self.assertIn('"/api/diag/llm"', self.get_body)

    def test_get_only_post_rejected_with_405(self):
        """POST 到只读接口要给 405 提示，而不是「未知接口」。"""
        self.assertIn("_GET_ONLY_PATHS", self.post_body)
        self.assertIn("405", self.post_body)

    def test_unknown_post_suggests_method(self):
        self.assertIn("_POST_ONLY_PATHS", self.get_body)
        self.assertIn("405", self.get_body)


if __name__ == "__main__":
    unittest.main()
