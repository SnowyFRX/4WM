"""暂停闸：暂停期间不向模拟器发输入，恢复后照常。"""
import threading
import time
import unittest

from jwm import pause


class PauseGateTests(unittest.TestCase):
    def setUp(self):
        # 这是进程级单例，别的测试可能把它留在暂停状态，每个用例先复位
        pause.set_paused(False)

    def tearDown(self):
        pause.set_paused(False)

    def test_default_is_running(self):
        self.assertFalse(pause.paused())

    def test_pause_and_resume(self):
        self.assertTrue(pause.set_paused(True))
        self.assertTrue(pause.paused())
        self.assertFalse(pause.set_paused(False))
        self.assertFalse(pause.paused())

    def test_toggle(self):
        self.assertTrue(pause.toggle())
        self.assertTrue(pause.paused())
        self.assertFalse(pause.toggle())
        self.assertFalse(pause.paused())

    def test_wait_blocks_until_resume(self):
        """暂停时 wait() 必须真的阻塞，恢复后立刻返回。"""
        pause.set_paused(True)
        done = []

        def waiter():
            pause.wait()
            done.append(time.time())

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertEqual(done, [], "暂停期间 wait 不该返回")

        pause.set_paused(False)
        t.join(timeout=2)
        self.assertEqual(len(done), 1, "恢复后 wait 应该返回")

    def test_wait_returns_immediately_when_running(self):
        start = time.time()
        self.assertTrue(pause.wait(timeout=1))
        self.assertLess(time.time() - start, 0.2)


class ActionNameTests(unittest.TestCase):
    """冻结脚本的动作名不能和「暂停开荒路线」撞车。

    一开始我把冻结脚本也叫 pause，结果 route.command({"action": "pause"})
    不再暂停路线，路线持久化的回归测试当场就红了。
    """

    def setUp(self):
        pause.set_paused(False)

    def tearDown(self):
        pause.set_paused(False)

    def test_route_pause_still_pauses_route(self):
        import tempfile
        from pathlib import Path
        from jwm import route_map as RM

        with tempfile.TemporaryDirectory() as d:
            old = RM.ROUTE_FILE
            RM.ROUTE_FILE = Path(d, "route.json")
            try:
                rm = RM.RouteMap()
                rm.known = {(244, 291): "land"}
                rm.home = (243, 291)
                rm.command({"action": "start", "revision": 0, "path": [[244, 291]]})
                self.assertTrue(rm.active)
                rm.command({"action": "pause", "revision": rm.revision})
                self.assertFalse(rm.active, "action=pause 必须还是「暂停路线」")
                self.assertFalse(pause.paused(), "不能把脚本一起冻住")
            finally:
                RM.ROUTE_FILE = old

    def test_freeze_is_separate(self):
        from jwm import route_map as RM
        rm = RM.RouteMap()
        rm.command({"action": "freeze"})
        self.assertTrue(pause.paused())
        rm.command({"action": "unfreeze"})
        self.assertFalse(pause.paused())


if __name__ == "__main__":
    unittest.main()
