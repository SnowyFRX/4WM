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


class DecorationTests(unittest.TestCase):
    """装饰层：按「两个角」解释区间，且不能盖掉资源色。"""

    def _write(self, items):
        import json
        import os
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "decorations_x.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"name": "x", "count": len(items), "items": items}, f)
        return p

    def test_rect_not_linear_span(self):
        """一条跨行的记录只能铺一个矩形，不能从行尾铺到下一行行首。

        这是实测踩到的坑：按线性 range(start, end) 展开，整张图会变成横条纹。
        """
        # 20 宽。start=(2,1) end=(4,2) → 3x2 的矩形，共 6 格
        p = self._write([{"start": 1 * 20 + 2, "decoration": 6, "end": 2 * 20 + 4}])
        table = {6: {"icon": "lake_109", "type": 6}}
        grid = fixedmap.load_decorations(p, 20, 20, table)
        filled = [(x, y) for y in range(20) for x in range(20) if grid[y][x]]
        self.assertEqual(len(filled), 6)
        self.assertTrue(all(2 <= x <= 4 and 1 <= y <= 2 for x, y in filled))
        # 第 1 行以外的格必须干净
        self.assertEqual(grid[3].count(0), 20)
        self.assertEqual(grid[0].count(0), 20)

    def test_shadow_is_skipped(self):
        p = self._write([{"start": 0, "decoration": 1, "end": 0}])
        table = {1: {"icon": "shadow_101", "type": 4}}
        grid = fixedmap.load_decorations(p, 10, 10, table)
        self.assertEqual(sum(row.count(0) for row in grid), 100)

    def test_lake_overrides_resource(self):
        m = make_map()
        m.deco = [[0] * m.w for _ in range(m.h)]
        m.deco[5][5] = fixedmap.DECO_LAKE
        self.assertEqual(m.color_at(5, 5), fixedmap.DECO_COLOR[fixedmap.DECO_LAKE])

    def test_resource_keeps_colour(self):
        """资源格不能被草地/山丘的颜色顶掉，否则分不出哪格能占。"""
        m = make_map()
        m.deco = [[fixedmap.DECO_GRASS] * m.w for _ in range(m.h)]
        c = m.color_at(3, 3)          # (3,3) 是荒地 0 → 该被草地染色
        self.assertEqual(c, fixedmap.DECO_COLOR[fixedmap.DECO_GRASS])
        m.grid[3][3] = 1              # 换成粮地
        c2 = m.color_at(3, 3)
        self.assertNotEqual(c2, fixedmap.DECO_COLOR[fixedmap.DECO_GRASS])
        self.assertGreater(c2[0], c2[2])   # 仍然偏黄

    def test_building_ignores_deco(self):
        m = make_map()
        m.deco = [[fixedmap.DECO_MOUNTAIN] * m.w for _ in range(m.h)]
        self.assertEqual(m.color_at(6, 6), PALETTE["building"])


    def test_adjacent_same_deco_merges_into_one_block(self):
        """同一片湖是逐格标记的，必须合并成一块。

        以前按「每条记录画一张整贴图」来，一片 5×2 的湖会被叠画十几次，
        边缘叠出一条条横带，看着像错位。
        """
        items = []
        for y in range(3, 5):             # 3×2 的一小片湖
            for x in range(4, 7):
                items.append({"start": y * 20 + x, "decoration": 6, "end": y * 20 + x})
        p = self._write(items)
        table = {6: {"icon": "lake_109", "type": 6}}
        grid, rects = fixedmap.load_decorations(p, 20, 20, table, want_rects=True)
        self.assertEqual(len(rects), 1, "六格同一张图应该合成一块")
        self.assertEqual(rects[0], (4, 3, 6, 4, "lake_109"))
        # 每一格仍然要标成「湖」，色块兜底时才不会露底
        self.assertEqual(grid[4][6], fixedmap.DECO_LAKE)

    def test_different_deco_stays_separate(self):
        """相邻但不是同一张图（湖 vs 山）不能合并。"""
        items = [{"start": 5 * 20 + 5, "decoration": 6, "end": 5 * 20 + 5},
                 {"start": 5 * 20 + 6, "decoration": 7, "end": 5 * 20 + 6}]
        p = self._write(items)
        table = {6: {"icon": "lake_109", "type": 6}, 7: {"icon": "hill_105", "type": 5}}
        _, rects = fixedmap.load_decorations(p, 20, 20, table, want_rects=True)
        self.assertEqual(len(rects), 2)


if __name__ == "__main__":
    unittest.main()
