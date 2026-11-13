"""网格识别核心（gridvision）的单元测试。

全部用合成图，不依赖真机截图：
  - 底色 / 边界线配色取自 2026-11-13 的真机截图实测值
    （地面 HSV≈(47,101,190)、领界线 HSV≈(69,162,134)）；
  - 干扰物（树冠、粮田、水面、大建筑、UI 边框）都按真机上的颜色画。

这样这些测试既能在 CI 上跑，又能复现「颜色接近、干扰物多」的真实难点。
"""
import unittest

import cv2
import numpy as np

from jwm import gridvision as gv
from jwm import mapview as mv

GROUND = (115, 190, 148)     # BGR，真机实测 HSV=(47,101,190)
LINE = (74, 134, 49)         # BGR，真机实测 HSV=(69,162,134)
TREE = (100, 175, 120)       # 树冠：亮一档的绿（V≈175，落在亮度门槛之上）
WATER = (199, 144, 74)       # 水面蓝
GRAIN = (122, 214, 232)      # 粮田黄
STONE = (166, 176, 176)      # 石头灰


def frame(w=400, h=400, ground=GROUND):
    return np.full((h, w, 3), ground, np.uint8)


def draw_grid(img, tile=81, ox=23, oy=31, color=LINE, thick=2):
    h, w = img.shape[:2]
    x = ox
    while x < w:
        cv2.line(img, (int(round(x)), 0), (int(round(x)), h - 1), color, thick)
        x += tile
    y = oy
    while y < h:
        cv2.line(img, (0, int(round(y))), (w - 1, int(round(y))), color, thick)
        y += tile
    return img


def scene(w=720, h=1280, tile=81, ox=23, oy=31, trees=14, seed=7):
    """一张「有树、有水、有粮田、有大建筑」的地图截图，网格在 tile 上。"""
    img = frame(w, h)
    rng = np.random.default_rng(seed)
    for _ in range(trees):                     # 先画地形，再画网格线（游戏也是这样）
        cx = int(rng.integers(30, w - 30))
        cy = int(rng.integers(300, h - 140))
        cv2.circle(img, (cx, cy), int(rng.integers(12, 22)), TREE, -1)
    cv2.rectangle(img, (470, 300), (680, 420), WATER, -1)
    cv2.rectangle(img, (150, 620), (300, 700), GRAIN, -1)
    cv2.rectangle(img, (250, 700), (410, 860), (120, 120, 110), -1)   # 大建筑
    for _ in range(60):                        # 零散石头
        cx = int(rng.integers(0, w - 1))
        cy = int(rng.integers(300, h - 1))
        cv2.circle(img, (cx, cy), 3, STONE, -1)
    return draw_grid(img, tile=tile, ox=ox, oy=oy)


class MaskTests(unittest.TestCase):
    """第 1 步：HSV 掩膜。"""

    def test_ground_color_is_measured_not_assumed(self):
        img = frame()
        self.assertEqual(gv.ground_hsv(img), (47, 101, 190))

    def test_mask_keeps_the_border_line_and_drops_the_ground(self):
        img = draw_grid(frame(), tile=81)
        m = gv.line_mask(img)
        self.assertGreater((m[31, 10:390] > 0).mean(), 0.9, "边界线没被认出来")
        # 取格内空地（x 30..95、y 205..270 里没有任何网格线）
        self.assertEqual(int((m[205:270, 30:95] > 0).sum()), 0, "地面被误认成线")

    def test_thresholds_are_relative_to_the_ground(self):
        """同一个掩膜，地面换一档颜色也要跟着走 —— 这就是「不写死绝对色值」。"""
        img = frame(ground=(150, 205, 178))
        draw_grid(img, tile=81, color=(70, 140, 44))
        self.assertGreater((gv.line_mask(img)[31, 10:390] > 0).mean(), 0.9)
        ground = gv.ground_hsv(img)
        lo, hi = gv.mask_bounds(ground)
        self.assertEqual(int(lo[1]), int(ground[1]) + gv.LINE_S_OFFSET)
        self.assertLessEqual(int(hi[2]), int(ground[2]) - gv.LINE_V_OFFSET)

    def test_theme_shift_breaks_fixed_bgr_but_not_hsv(self):
        """换季/换主题后线色暗一档：写死的 BGR 距离就整条漏掉。

        这是老实现的真实失效方式：`|像素 - (77,132,54)| 之和 < 40`，
        线色变成 (60,150,60) 时距离是 17+18+6=41，刚好跨过阈值，一条不剩。
        """
        img = frame(ground=(120, 190, 120))
        draw_grid(img, tile=81, color=(60, 150, 60))
        old = np.abs(img.astype(np.int16) - mv.GREEN_BGR.astype(np.int16)).sum(axis=2) < 40
        self.assertEqual(old[31, 10:390].mean(), 0.0, "旧阈值竟然还认得出来？前提变了")
        self.assertGreater((gv.line_mask(img)[31, 10:390] > 0).mean(), 0.9)

    def test_water_farmland_and_stone_are_not_lines(self):
        img = frame(300, 300)
        cv2.rectangle(img, (10, 10), (120, 120), WATER, -1)
        cv2.rectangle(img, (150, 10), (260, 120), GRAIN, -1)
        cv2.rectangle(img, (10, 150), (120, 260), STONE, -1)
        m = gv.line_mask(img)
        for name, box in (("水面", (20, 20, 110, 110)), ("粮田", (160, 20, 250, 110)),
                          ("石头", (20, 160, 110, 250))):
            x1, y1, x2, y2 = box
            self.assertEqual(int((m[y1:y2, x1:x2] > 0).sum()), 0, "%s被当成线" % name)


class MorphologyTests(unittest.TestCase):
    """第 2 步：形态学。"""

    def test_close_bridges_a_2px_gap(self):
        """领界线被树冠切断的地方通常就缺 1~2px，闭运算正好接得上。"""
        img = frame(200, 120)
        cv2.line(img, (10, 60), (190, 60), LINE, 2)
        raw = gv.line_mask(img).copy()
        raw[58:63, 90:92] = 0                             # 挖掉 2px
        self.assertEqual(int((raw[60, 90:92] > 0).sum()), 0)
        self.assertEqual(int((gv.clean_mask(raw)[60, 90:92] > 0).sum()), 2)

    def test_close_does_not_invent_bridges(self):
        """10px 的大豁口不能被闭运算「补」上 —— 那会把两条线连成一条。"""
        img = frame(200, 120)
        cv2.line(img, (10, 60), (190, 60), LINE, 2)
        raw = gv.line_mask(img).copy()
        raw[57:64, 85:95] = 0
        self.assertEqual(int((gv.clean_mask(raw)[60, 90] > 0)), 0)

    def test_axis_mask_drops_specks_and_keeps_lines(self):
        """零散小斑点磨掉、长线留下 —— 这是开运算是该干的事。

        大块树冠不归它管（见 axis_mask 的说明），所以这里用小斑点。
        """
        img = frame(300, 300)
        cv2.rectangle(img, (145, 145), (152, 152), LINE, -1)   # 8x8 的草叶
        cv2.line(img, (30, 260), (270, 260), LINE, 2)          # 一条线
        m = gv.line_mask(img)
        ax = gv.axis_mask(m, run=9)
        self.assertEqual(int((ax[144:154, 144:154] > 0).sum()), 0, "小斑点没被去掉")
        self.assertGreater(int((ax[260, 30:270] > 0).sum()), 200, "线被误删")

    def test_cleaning_does_not_shift_the_line(self):
        """形态学不能把线挪位。

        2x2 这种偶数核的开运算锚点取整，会把整条线推 1px，相位跟着偏，
        一屏十几格之后点击就落错格。所以默认只做 3x3 闭运算（奇数核对称）。
        """
        img = frame(200, 120)
        cv2.line(img, (10, 60), (190, 60), LINE, 2)
        raw = gv.line_mask(img)
        clean = gv.clean_mask(raw)
        before = gv.projection_lines(raw)[1]
        after = gv.projection_lines(clean)[1]
        self.assertEqual(len(after), 1)
        self.assertLessEqual(abs(after[0] - before[0]), 0.6,
                             "清洗后线位移 %.2fpx" % (after[0] - before[0]))


class QuadTests(unittest.TestCase):
    """第 3 步：轮廓 + 四边形 + 面积过滤。"""

    def _closed_cells(self, tile=81, n=2, offset=100, size=520):
        img = frame(size, size)
        for i in range(n):
            for j in range(n):
                x, y = offset + i * tile, offset + j * tile
                cv2.rectangle(img, (x, y), (x + tile, y + tile), LINE, 2)
        return img

    def test_closed_cells_are_found(self):
        m = gv.clean_mask(gv.line_mask(self._closed_cells()))
        quads = gv.quad_cells(m)
        self.assertGreaterEqual(len(quads), 4)
        for q in quads:
            self.assertAlmostEqual(q.side, 81, delta=5)

    def test_big_building_is_rejected(self):
        img = frame(600, 600)
        cv2.rectangle(img, (100, 100), (353, 353), LINE, 2)      # 254px 的大家伙
        for i in range(2):
            for j in range(2):
                x, y = 400 + i * 81, 400 + j * 81
                cv2.rectangle(img, (x, y), (x + 81, y + 81), LINE, 2)
        quads = gv.quad_cells(gv.clean_mask(gv.line_mask(img)))
        self.assertTrue(quads)
        self.assertTrue(all(q.side < 150 for q in quads), "大建筑没被面积过滤掉")

    def test_dust_is_rejected(self):
        img = frame(400, 400)
        for i in range(2):
            for j in range(2):
                x, y = 100 + i * 81, 100 + j * 81
                cv2.rectangle(img, (x, y), (x + 81, y + 81), LINE, 2)
        cv2.rectangle(img, (20, 300), (40, 320), LINE, 2)        # 20px 的小噪点
        quads = gv.quad_cells(gv.clean_mask(gv.line_mask(img)))
        self.assertTrue(all(q.side > 40 for q in quads), "小噪点没被滤掉")

    def test_one_cell_is_not_enough_for_a_tile(self):
        """单个四边形可能是弹窗边框，不足以定格宽。"""
        img = frame(400, 400)
        cv2.rectangle(img, (100, 100), (181, 181), LINE, 2)
        quads = gv.quad_cells(gv.clean_mask(gv.line_mask(img)))
        self.assertTrue(quads)
        self.assertIsNone(gv.tile_from_quads(quads))


class HoughTests(unittest.TestCase):
    """第 4 步：Canny + HoughLinesP。"""

    def test_hough_finds_both_axis(self):
        img = frame(400, 400)
        for x in (50, 131, 212):
            cv2.line(img, (x, 20), (x, 380), LINE, 2)
        for y in (60, 141):
            cv2.line(img, (20, y), (380, y), LINE, 2)
        vx, hy = gv.hough_lines(gv.clean_mask(gv.line_mask(img)))
        self.assertEqual(len(vx), 3)
        self.assertEqual(len(hy), 2)
        for got, want in zip(vx, (50, 131, 212)):
            self.assertLessEqual(abs(got - want), 2.5)
        for got, want in zip(hy, (60, 141)):
            self.assertLessEqual(abs(got - want), 2.5)

    def test_double_edges_of_one_stroke_merge_into_one_line(self):
        """线是 3~4px 的描边，Canny 会把两条边各算一条；不合并就会重影。"""
        segs = [(100.0, 0, 60), (104.0, 70, 140)]
        self.assertEqual(len(gv.merge_segments(segs, gap=6.0)), 1)
        self.assertEqual(len(gv.merge_segments(segs, gap=3.0)), 2)


class FitTests(unittest.TestCase):
    """第 5 步：把线位置拟合回 (格宽, 相位)。"""

    def test_recovers_tile_and_phase(self):
        fit = gv.fit_lines([23, 104, 185, 266, 347])
        self.assertAlmostEqual(fit.tile, 81.0, places=1)
        self.assertAlmostEqual(fit.phase, 23.0, places=1)

    def test_outlier_bar_is_ignored(self):
        """混进一根 UI 边框（不在网格上）不能把格宽拽偏。"""
        fit = gv.fit_lines([23, 104, 185, 266, 347, 300])
        self.assertAlmostEqual(fit.tile, 81.0, places=1)
        self.assertEqual(fit.support, 5)

    def test_random_positions_are_rejected(self):
        self.assertIsNone(gv.fit_lines([10, 47, 96, 171, 268, 389]))
        self.assertIsNone(gv.fit_lines([12, 33, 58, 77, 96, 121]))

    def test_half_tile_ambiguity_prefers_the_larger(self):
        """隔一格才画线时 40 和 80 都说得通 —— 必须取大的。

        取小的会把整张图格子翻倍，点哪都不对；取大的只是相邻两格并一格。
        """
        self.assertGreater(gv._best_tile([30, 111, 192, 273])[0], 70)

    def test_prior_cannot_drag_the_tile_off(self):
        """先验只用来压抖动，不能把格宽拽到明显更差的候选上。"""
        self.assertAlmostEqual(gv._best_tile([30, 111, 192, 273], prior=79.0)[0],
                               81.0, delta=2.0)

    def test_half_tile_prior_does_not_win(self):
        """先验恰好是「半格」时也不能信 —— 那正是要防的错。"""
        self.assertGreater(gv._best_tile([30, 111, 192, 273], prior=40.5)[0], 70)


class GuessTests(unittest.TestCase):

    def test_full_scene(self):
        g = gv.guess(scene())
        self.assertTrue(g, "整张场景图没认出来")
        self.assertAlmostEqual(g.tile, 81, delta=2)
        self.assertAlmostEqual(g.ox, 23, delta=2)
        self.assertAlmostEqual(g.oy, 31, delta=2)
        self.assertEqual(g.source, "lines")

    def test_blank_map_has_no_guess(self):
        self.assertFalse(gv.guess(frame()))


class MapviewIntegrationTests(unittest.TestCase):
    """mapview 对外接口不变，但内部换成了新识别核心。"""

    def setUp(self):
        mv.set_tile_size(67.5)

    def tearDown(self):
        mv.set_tile_size(67.5)

    def test_green_mask_still_returns_bool(self):
        """_edge_line 靠 band.mean() 当覆盖率用，必须是 bool（0/1）。"""
        m = mv.green_mask(draw_grid(frame(200, 200), tile=81))
        self.assertEqual(m.dtype, np.bool_)
        self.assertTrue(m.any())

    def test_estimate_phase_matches_the_drawn_grid(self):
        img = draw_grid(frame(720, 1280), tile=67.5, ox=40, oy=55)
        phase = mv.estimate_phase(img)
        self.assertIsNotNone(phase)
        self.assertLessEqual(abs(phase[0] - 40), 2)
        self.assertLessEqual(abs(phase[1] - 55), 2)

    def test_implausible_zoom_jump_is_refused(self):
        """格宽突然变 1.6 倍，基本都是半格歧义认错了，别动 TILE。"""
        img = draw_grid(frame(720, 1280), tile=110, ox=20, oy=30)
        before = mv.TILE
        self.assertIsNone(mv.estimate_phase(img))
        self.assertEqual(mv.TILE, before)

    def test_map_grid_builds_from_a_scene(self):
        grid = mv.MapGrid(scene(tile=67.5, ox=40, oy=55), classify=False)
        self.assertGreater(len(grid.tiles), 100)
        self.assertAlmostEqual(mv.TILE, 67.5, delta=2)


if __name__ == "__main__":
    unittest.main()
