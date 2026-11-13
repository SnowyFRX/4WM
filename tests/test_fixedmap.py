"""固定地图渲染与「按主城认图」。

这一层是网页全图视图的后端：渲染一段地图、查一格详情、按主城猜本局是哪张图。
测试都用构造数据，不依赖 apk，跑得快。
"""
import unittest

from jwm import fixedmap
from jwm.fixedmap import FixedMap, PALETTE, WATER_IDS


def make_land():
    """最小 land 表：荒地 lv1、粮地 lv2、建筑 type6。"""
    rows = [
        {"id": 0, "type": 3, "lv": 1, "cereal": 1, "timber": 1, "stone": 1},
        {"id": 1, "type": 3, "lv": 2, "cereal": 4, "timber": 0, "stone": 0},
        {"id": 93, "type": 6, "lv": 0},
        {"id": 71, "type": 7, "lv": 0},
    ]
    return {e["id"]: e for e in rows}


def make_map(name="maps_x", n=20):
    """一张 n×n 的图：中间 4×4 建筑群，右上角是水，其余荒地。

    建筑群做 16 格，对齐真实数据（maps_14 主城周围 15 格建筑），
    guess() 的「≥10 格且领先 3 倍」阈值才有意义。
    """
    grid = [[0 for _ in range(n)] for _ in range(n)]
    for y in range(5, 9):
        for x in range(5, 9):
            grid[y][x] = 93
    grid[0][n - 1] = 71
    grid[0][n - 2] = 71
    return FixedMap(name, grid, make_land(), {})


class LandKindTests(unittest.TestCase):
    def test_lv1_is_empty_not_resource(self):
        """lv=1 的三项产量都是 1 —— 那是荒地，不是资源地。"""
        m = make_map()
        self.assertEqual(m.kind(0), "empty")

    def test_lv2_is_grain(self):
        m = make_map()
        self.assertEqual(m.kind(1), "grain")

    def test_building_and_water(self):
        m = make_map()
        self.assertEqual(m.kind(93), "building")
        self.assertEqual(m.kind(71), "water")
        self.assertIn(71, WATER_IDS)


class RenderTests(unittest.TestCase):
    def test_render_region_size(self):
        m = make_map()
        arr = fixedmap.render_array(m, region=(0, 0, 4, 3), scale=2)
        # 4x3 格、每格放 2 倍 → 高 6 宽 8
        self.assertEqual(arr.shape, (6, 8, 3))

    def test_flip_reverses_rows(self):
        m = make_map()
        normal = fixedmap.render_array(m, region=(0, 0, 20, 20))
        flipped = fixedmap.render_array(m, region=(0, 0, 20, 20), flip=True)
        # 第 0 行应该对应原图最后一行
        self.assertTrue((normal[0] == flipped[19]).all())
        self.assertTrue((normal[19] == flipped[0]).all())
        # 水在右上角：翻转后应跑到右下角
        self.assertEqual(tuple(normal[0][19]), PALETTE["water"])
        self.assertEqual(tuple(flipped[19][19]), PALETTE["water"])

    def test_region_is_clamped(self):
        m = make_map()
        arr = fixedmap.render_array(m, region=(-5, -5, 100, 100))
        self.assertEqual(arr.shape, (20, 20, 3))


class GuessTests(unittest.TestCase):
    """按主城建筑密度认图：明显领先才给结论。"""

    def _guess_with(self, maps, x, y):
        from jwm import fixedsrv
        old = fixedsrv.load_maps
        fixedsrv.load_maps = lambda: maps
        try:
            return fixedsrv.guess(x, y)
        finally:
            fixedsrv.load_maps = old

    def test_picks_clear_winner(self):
        a = make_map("maps_a")      # 主城处 9 格建筑
        b = make_map("maps_b")
        for y in range(b.h):
            for x in range(b.w):
                if b.grid[y][x] == 93:
                    b.grid[y][x] = 0
        res = self._guess_with({"maps_a": a, "maps_b": b}, 6, 6)
        self.assertEqual(res["pick"], "maps_a")
        self.assertIn("建筑", res["why"])

    def test_refuses_when_ambiguous(self):
        a = make_map("maps_a")
        b = make_map("maps_b")       # 同样也有 9 格建筑
        res = self._guess_with({"maps_a": a, "maps_b": b}, 6, 6)
        self.assertIsNone(res["pick"])
        self.assertIn("无法判断", res["why"])

    def test_out_of_range_is_skipped(self):
        small = make_map("maps_small", n=10)
        res = self._guess_with({"maps_small": small}, 500, 500)
        self.assertEqual(res["ranking"], [])
        self.assertIsNone(res["pick"])


if __name__ == "__main__":
    unittest.main()
