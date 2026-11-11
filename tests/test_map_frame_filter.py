"""地图拼图不能把游戏 UI / 主城展开信息截进去。

用户反馈 3：开荒地图偶尔把游戏界面 UI、甚至主城展开的信息当成干净地图。

做法分两层：
  1. 固定的 HUD（右侧竖栏、底部按钮、任务提示）永远从帧里剔掉；
  2. 动态弹窗（主城展开的编队信息、战斗预测、资源不足提示）按帧检测，
     检测到就整帧不用 —— 这样不会为了避开弹窗把主城周围的地块全丢掉。
"""
import unittest

from jwm.route_camera import ATLAS_HUD, tile_clear_of_hud, popup_covering


class HudTests(unittest.TestCase):
    def test_hud_regions_are_integers_and_inside_screen(self):
        for x1, y1, x2, y2 in ATLAS_HUD:
            self.assertTrue(0 <= x1 < x2 <= 720, (x1, x2))
            self.assertTrue(0 <= y1 < y2 <= 1280, (y1, y2))

    def test_detected_popup_takes_its_tiles_out_of_the_frame(self):
        popup = [(60, 480, 660, 700)]     # 主城展开信息大致占这一块
        self.assertFalse(tile_clear_of_hud(100, 520, 167, 587, popup))
        self.assertFalse(tile_clear_of_hud(330, 500, 397, 567, popup))

    def test_tiles_outside_the_popup_stay_usable(self):
        popup = [(60, 480, 660, 700)]
        self.assertTrue(tile_clear_of_hud(100, 700, 167, 767, popup))
        self.assertTrue(tile_clear_of_hud(250, 850, 317, 917, popup))

    def test_fixed_hud_never_covers_the_middle_of_the_map(self):
        """固定的遮挡区不能吃掉主城周围，否则拼图永远缺中心。"""
        self.assertTrue(tile_clear_of_hud(250, 700, 317, 767))
        self.assertTrue(tile_clear_of_hud(250, 850, 317, 917))

    def test_popup_covering_reports_the_overlap(self):
        overlay = (60, 480, 660, 900)
        self.assertEqual(popup_covering([overlay], (100, 520, 167, 587)), overlay)
        self.assertIsNone(popup_covering([overlay], (250, 950, 317, 1017)))


if __name__ == "__main__":
    unittest.main()
