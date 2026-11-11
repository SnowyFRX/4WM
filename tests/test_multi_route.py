"""多条开荒路线：各自独立进度、轮转执行、互不串台。

用户需求（原话）：可以建多个小队，然后分别用于进行不同的开荒路线。
只有一台模拟器，所以两条路线不可能真并行 —— 用轮转：
一条路线刚派兵出去、要等行军时，脚本就去推进另一条。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from jwm import route_map as RM


class _Tile:
    def __init__(self, march_until=0, retry_at=0):
        self.data = {"march_until": march_until, "retry_at": retry_at}

    def tile(self, *a, **k):
        return dict(self.data)


class _State:
    def __init__(self, home):
        self.home = list(home)
        self.tiles = {}

    def tile(self, x, y):
        rec = self.tiles.get((x, y))
        return dict(rec) if rec else {}

    def set_tile(self, x, y, **kw):
        self.tiles.setdefault((x, y), {}).update(kw)


class _Expand:
    def __init__(self, state):
        self.state = state


class MultiRouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = RM.ROUTE_FILE
        RM.ROUTE_FILE = Path(self.dir.name, "route.json")

    def tearDown(self):
        RM.ROUTE_FILE = self.old
        self.dir.cleanup()

    def make(self, known):
        rm = RM.RouteMap()
        rm.home = (243, 291)
        rm.known = dict(known)
        rm.expand = _Expand(_State((243, 291)))
        return rm

    # 两格都在主城 (243,291) 旁边，满足「第一格必须紧挨已占领地」
    A = [242, 291]
    A2 = [241, 291]
    B = [243, 290]
    B2 = [243, 289]

    def test_second_route_is_added_not_replacing_the_first(self):
        known = {(242, 291): "land", (241, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A, self.A2]})
        first = rm.route_key
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        self.assertEqual(len(rm.routes), 2)
        self.assertNotEqual(first, rm.route_key)
        self.assertEqual([r["key"] for r in rm.routes][0], first)

    def test_each_route_keeps_its_own_progress(self):
        known = {(242, 291): "land", (241, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A, self.A2]})
        a = rm.route_key
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        b = rm.route_key
        self.assertEqual(rm._route_of(a)["index"], 0)
        self.assertEqual(rm._route_of(b)["index"], 0)

    def test_rotation_skips_a_route_that_is_waiting_for_its_march(self):
        known = {(242, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A]})
        a_head = (242, 291)
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        b_key = rm.route_key
        # 第一条路线刚派兵出去，正在行军
        rm.expand.state.tiles[a_head] = {"march_until": time.time() + 600,
                                         "route_home": [243, 291]}
        step = rm.next()
        self.assertIsNotNone(step)
        self.assertEqual(step[2], (243, 290), "应该跳到第二条路线")
        self.assertEqual(rm.step_route_key(), b_key)

    def test_all_routes_waiting_means_no_step(self):
        known = {(242, 291): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A]})
        rm.expand.state.tiles[(242, 291)] = {"march_until": time.time() + 600,
                                             "route_home": [243, 291]}
        self.assertIsNone(rm.next())

    def test_paused_route_is_skipped(self):
        known = {(242, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A]})
        a = rm.route_key
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        rm.command({"action": "pause", "revision": rm.revision, "key": rm.route_key})
        step = rm.next()
        self.assertEqual(step[2], (242, 291), "暂停的那条不该被轮到")
        self.assertEqual(rm.step_route_key(), a)

    def test_removing_a_route_keeps_the_others(self):
        known = {(242, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A]})
        first = rm.route_key
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        rm.command({"action": "remove_route", "revision": rm.revision, "key": rm.route_key})
        self.assertEqual([r["key"] for r in rm.routes], [first])

    def test_all_routes_are_persisted_and_restored(self):
        known = {(242, 291): "land", (243, 290): "land"}
        rm = self.make(known)
        rm.command({"action": "start", "revision": 0, "path": [self.A]})
        rm.command({"action": "start", "revision": rm.revision, "mode": "add",
                    "path": [self.B]})
        self.assertEqual(len(rm.routes), 2)

        again = RM.RouteMap()
        again.identify((243, 291))
        self.assertEqual(len(again.routes), 2)
        self.assertTrue(all(r["index"] == 0 for r in again.routes))

    def test_old_single_route_file_still_loads(self):
        RM.ROUTE_FILE.write_text(json.dumps({
            "home": [243, 291], "path": [[242, 291]], "index": 0,
            "active": True, "route_key": "old#1",
        }, ensure_ascii=False), encoding="utf-8")
        again = RM.RouteMap()
        again.identify((243, 291))
        self.assertEqual(len(again.routes), 1)
        self.assertEqual(again.route_key, "old#1")


if __name__ == "__main__":
    unittest.main()
