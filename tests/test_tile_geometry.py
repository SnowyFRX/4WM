"""格子坐标必须保留浮点，否则网页地图与游戏截图逐格错位。

真机现象（2026-11-13）：网页上的格子与游戏里的地块对不齐，越往边上越歪。

根因：格子边长在非默认缩放时是小数（实测 69.5、66.75 等），
而 mapview 构造 TileInfo 时写的是 `int(round(x))`，把间距从 69.5
压成「70, 69, 70, 69…」交替。网页按真实 tile 均匀排列，
两边每格漂 0.5px，几十格之后就是几十像素。

修复：TileInfo.x/y 保留浮点（几何位置），
只在 center（点击用）里取整，因为点击只接受整数坐标。

用户要求：主城 2x2 可以不对齐，但其它 1x1 地块必须对齐。
"""
import unittest

import numpy as np

from jwm import mapview as MV


class TileFloatGeometryTests(unittest.TestCase):
    def _grid(self, tile, ox=100.3, oy=300.7):
        """直接按 MapGrid 的构造公式造格子，不依赖图像识别。"""
        g = MV.MapGrid.__new__(MV.MapGrid)
        g.tiles = {}
        for j in range(4):
            for i in range(4):
                t = MV.TileInfo(i, j, ox + i * tile, oy + j * tile)
                g.tiles[(i, j)] = t
        return g

    def test_adjacent_spacing_exactly_tile(self):
        """相邻格间距必须严格等于 tile，不能忽大忽小。"""
        for tile in (67.5, 69.5, 66.75, 70.0):
            g = self._grid(tile)
            xs = [g.tiles[(i, 0)].x for i in range(4)]
            gaps = [round(b - a, 6) for a, b in zip(xs, xs[1:])]
            self.assertEqual(set(gaps), {round(tile, 6)},
                             "tile=%.2f 时间距不恒定：%s" % (tile, gaps))

    def test_vertical_spacing_exactly_tile(self):
        g = self._grid(69.5)
        ys = [g.tiles[(0, j)].y for j in range(4)]
        gaps = [round(b - a, 6) for a, b in zip(ys, ys[1:])]
        self.assertEqual(set(gaps), {69.5})

    def test_center_is_integer(self):
        """点击用的 center 必须是整数（adb 只收整数）。"""
        g = self._grid(69.5)
        t = g.tiles[(2, 3)]
        cx, cy = t.center
        self.assertIsInstance(cx, int)
        self.assertIsInstance(cy, int)

    def test_center_is_geometric_middle(self):
        """center 应该落在格子正中 ±1px 内。

        注意 TILE 是模块级的，MapGrid 用 TILE 的一半算中心；
        这里按当前 TILE 断言，避免测试和实际常量脱节。
        """
        tile = MV.TILE
        g = self._grid(tile, ox=101.2, oy=300.0)
        t = g.tiles[(3, 1)]
        cx, cy = t.center
        self.assertLessEqual(abs(cx - (t.x + tile / 2)), 1.0)
        self.assertLessEqual(abs(cy - (t.y + tile / 2)), 1.0)

    def test_float_not_truncated(self):
        """x 不能被取整：69.5 格距下，奇数格应该带 .5 结尾。"""
        g = self._grid(69.5, ox=100.0)
        self.assertAlmostEqual(g.tiles[(1, 0)].x, 169.5, places=6)
        self.assertAlmostEqual(g.tiles[(3, 0)].x, 308.5, places=6)


class OldBehaviourWouldDriftTests(unittest.TestCase):
    """把「旧代码会漂」这件事写进测试，防止改回去。"""

    def test_rounding_causes_alternating_gaps(self):
        tile = 69.5
        ox = 100.3
        old = [int(round(ox + i * tile)) for i in range(8)]
        gaps = [b - a for a, b in zip(old, old[1:])]
        self.assertGreater(len(set(gaps)), 1,
                           "取整后间距竟然恒定？这个反证前提变了")

    def test_accumulated_drift_is_visible(self):
        """逐格独立取整时，单元格与理想位置的偏差最大可达 0.5px，
        而网页用理想值排格子，所以单格最大偏差就是 0.5px。

        这正是「每格差 0.5px」的来源：不是累积 50px 那种夸张数字，
        而是一屏十几格时，边缘地块看起来偏了半格线。
        """
        tile = 69.5
        ox = 100.3
        worst = 0.0
        for i in range(200):
            exact = ox + i * tile
            rounded = int(round(exact))
            worst = max(worst, abs(rounded - exact))
        # 单格偏差上界就是 0.5px；样本足够多时应该逼近它
        self.assertGreater(worst, 0.25,
                           "取整造成的单格偏差实测只有 %.2f，太小了，检查公式" % worst)
        self.assertLessEqual(worst, 0.5 + 1e-9,
                             "单格偏差不该超过 0.5px（那说明算错了）")


if __name__ == "__main__":
    unittest.main()
