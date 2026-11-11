"""脚本重启不能把已提交的路线弄丢。

用户反馈过：脚本一重启，网页上的路线就空了，得重新逐格点一遍。
路线就是命令，重启后应当原样回来（同一个主城才恢复）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from jwm import route_map as RM


class RoutePersistTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = RM.ROUTE_FILE
        RM.ROUTE_FILE = Path(self.dir.name, "route.json")

    def tearDown(self):
        RM.ROUTE_FILE = self.old
        self.dir.cleanup()

    def make(self, known):
        rm = RM.RouteMap()
        rm.known = known
        return rm

    def test_route_survives_a_restart(self):
        known = {(244, 291): "land", (244, 292): "land"}
        rm = self.make(known)
        rm.home = (243, 291)
        rm.command({"action": "start", "revision": 0, "path": [[244, 291], [244, 292]]})
        self.assertEqual(len(rm.path), 2)
        rm.command({"action": "pause", "revision": rm.revision})

        again = self.make(known)
        again.home = None
        again.identify((243, 291))
        self.assertEqual([tuple(p) for p in again.path], [(244, 291), (244, 292)])
        self.assertFalse(again.active)
        self.assertIn("路线", again.status)

    def test_route_is_not_restored_for_another_home(self):
        known = {(244, 291): "land"}
        rm = self.make(known)
        rm.home = (243, 291)
        rm.command({"action": "start", "revision": 0, "path": [[244, 291]]})

        again = self.make(known)
        again.identify((999, 999))
        self.assertEqual(len(again.path), 0)


if __name__ == "__main__":
    unittest.main()
