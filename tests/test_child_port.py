"""子服务端口解析：控制台不能赌 8766。

子服务端口会退让（8766 被占 → 8767 → 8768 → 随机）。控制台写死一个端口转发，
就会出现「脚本明明在跑，网页显示没运行」——因为请求打到了一个没人听的端口上。
"""
import json
import socket
import unittest
from pathlib import Path

import console
from jwm import route_map as RM

import shutil
import tempfile
from pathlib import Path

# 这台机器的 TEMP 指向不可写目录，tempfile.gettempdir() 会回落到当前工作目录，
# 于是测试临时文件直接丢进仓库根目录。这里固定到 logs/_test_tmp 并自动清理。
TMP_ROOT = Path(__file__).resolve().parent.parent / "logs" / "_test_tmp"


def make_tmp(testcase, prefix):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = tempfile.mkdtemp(prefix=prefix, dir=str(TMP_ROOT))
    testcase.addCleanup(shutil.rmtree, path, True)
    return Path(path)



class FakeProc:
    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


class ReadChildUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp(self, "child_")
        self.path = self.tmp / "web_child_url.txt"

    def test_round_trip(self):
        RM.announce_child_url("http://127.0.0.1:8767/jwm/", 8767, path=self.path, pid=4321)
        info = RM.read_child_url(self.path)
        self.assertEqual(info["port"], 8767)
        self.assertEqual(info["pid"], 4321)
        self.assertEqual(info["url"], "http://127.0.0.1:8767/jwm/")

    def test_url_gets_trailing_slash(self):
        RM.announce_child_url("http://127.0.0.1:8766/jwm", 8766, path=self.path, pid=1)
        self.assertTrue(RM.read_child_url(self.path)["url"].endswith("/"))

    def test_missing_file_is_none(self):
        self.assertIsNone(RM.read_child_url(self.tmp / "nope.txt"))

    def test_corrupt_file_is_none(self):
        for junk in ("", "   ", "{not json", "[]", "ftp://x/y"):
            with self.subTest(junk=junk):
                self.path.write_text(junk, encoding="utf-8")
                self.assertIsNone(RM.read_child_url(self.path))

    def test_legacy_plain_url_still_parses(self):
        """旧版只写一行地址，没有 pid；不能被当成坏文件。"""
        self.path.write_text("http://127.0.0.1:8766/jwm/\n", encoding="utf-8")
        info = RM.read_child_url(self.path)
        self.assertEqual(info["port"], 8766)
        self.assertIsNone(info["pid"])

    def test_rejects_non_local_host(self):
        self.path.write_text(json.dumps({"url": "http://10.0.0.5:8766/jwm/", "port": 8766}),
                             encoding="utf-8")
        self.assertIsNone(RM.read_child_url(self.path), "只允许本机地址")


class BindTests(unittest.TestCase):
    def test_bind_uses_requested_port_when_free(self):
        srv = RM.bind_local(console.BaseHTTPRequestHandler, 0)
        try:
            self.assertGreater(srv.server_port, 0)
            self.assertEqual(srv.server_address[0], "127.0.0.1")
        finally:
            srv.server_close()

    def test_bind_moves_on_when_port_taken(self):
        first = RM.bind_local(console.BaseHTTPRequestHandler, 0)
        try:
            taken = first.server_port
            second = RM.bind_local(console.BaseHTTPRequestHandler, taken)
            try:
                self.assertNotEqual(second.server_port, taken, "被占用时必须让到下一个端口")
            finally:
                second.server_close()
        finally:
            first.server_close()


class ChildBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp(self, "base_")
        self._old_state = dict(console._state)
        self._old_file = RM.CHILD_URL_FILE
        # _child_base 用 HERE/data/web_child_url.txt，测试里指到临时文件
        self._old_here = console.HERE
        console.HERE = self.tmp
        (self.tmp / "data").mkdir()

    def tearDown(self):
        console._state.clear()
        console._state.update(self._old_state)
        RM.CHILD_URL_FILE = self._old_file
        console.HERE = self._old_here

    def _announce(self, port, pid):
        RM.announce_child_url("http://127.0.0.1:%d/jwm/" % port, port,
                              path=self.tmp / "data" / "web_child_url.txt", pid=pid)

    def test_uses_actual_port_when_pid_matches(self):
        console._state["proc"] = FakeProc(pid=777)
        self._announce(8768, 777)
        self.assertEqual(console._child_base(), "http://127.0.0.1:8768/jwm/")

    def test_ignores_stale_file_from_previous_run(self):
        console._state["proc"] = FakeProc(pid=888)
        self._announce(8768, 777)     # 上一次运行留下的
        self.assertEqual(console._child_base(), "http://127.0.0.1:%d/jwm/" % (console.PORT + 1))

    def test_falls_back_without_file(self):
        console._state["proc"] = FakeProc(pid=777)
        self.assertEqual(console._child_base(), "http://127.0.0.1:%d/jwm/" % (console.PORT + 1))

    def test_falls_back_when_not_running(self):
        console._state["proc"] = None
        self._announce(8768, 777)
        self.assertEqual(console._child_base(), "http://127.0.0.1:%d/jwm/" % (console.PORT + 1))


class NoHardcodedPortTests(unittest.TestCase):
    def test_console_source_has_no_proxy_port_literal(self):
        """代理地址必须来自解析结果，不能又写回一个 8766。"""
        src = Path(console.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"http://127.0.0.1:8766/jwm/" + sub', src)
        self.assertEqual(src.count("_child_base() + sub"), 2, "GET 和 POST 两处都要走解析")


class PortAvailableTests(unittest.TestCase):
    def test_8766_may_be_occupied_by_other_tool(self):
        """这条不是断言 8766 一定被占，而是证明「端口可能不在」这件事是真的：
        一个普通 socket 就能占住它，而脚本照样会正常启动、只是换了端口。"""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            srv = RM.bind_local(console.BaseHTTPRequestHandler, busy)
            try:
                self.assertNotEqual(srv.server_port, busy)
            finally:
                srv.server_close()
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
