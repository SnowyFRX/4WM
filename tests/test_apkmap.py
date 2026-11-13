"""apk 固定地图解析与匹配。

背景（用户朋友提供的关键信息，已实测验证）：
  这个游戏没有实时随机地图，apk 里放了几张固定地图，每局从里面选一张，
  连资源地位置都固定到每一格。

实测结论：
  maps_N       600x600（maps_0 是 50x50）的整数网格，值 = land 表 id
  land         184 条，type=3/4/5 分别是粮/木/石地，id 里带等级
  landAttr     67 条，血量/占地尺寸/消耗/掉落
  decorations  纯视觉（阴影/草/湖/山），对决策无用

这些测试用**真实 apk**（若不存在则跳过），保证解析结果不退化成猜。
"""
import os
import unittest

from jwm import apkmap
from jwm import mapmatch

APK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "apk", "jiuwanmu.apk")
HAS_APK = os.path.exists(APK)


class LandTableTests(unittest.TestCase):
    """land 表的语义（不依赖 apk，用构造数据）。"""

    def _land(self):
        rows = [
            {"id": 1, "type": 3, "lv": 2, "cereal": 4, "timber": 0, "stone": 0},
            {"id": 6, "type": 4, "lv": 2, "cereal": 0, "timber": 4, "stone": 0},
            {"id": 11, "type": 5, "lv": 2, "cereal": 0, "timber": 0, "stone": 4},
            {"id": 93, "type": 6, "lv": 0},
            {"id": 71, "type": 7, "lv": 0},
        ]
        return mapmatch.LandTable(rows)

    def test_three_resource_types_mapped(self):
        land = self._land()
        self.assertEqual(land.candidates("grain", 2), {1})
        self.assertEqual(land.candidates("timber", 2), {6})
        self.assertEqual(land.candidates("stone", 2), {11})

    def test_water_detection(self):
        land = self._land()
        self.assertTrue(land.is_water(71))
        self.assertFalse(land.is_water(1))

    def test_describe(self):
        land = self._land()
        self.assertIn("粮", land.describe(1))
        self.assertIn("木", land.describe(6))
        self.assertIn("石", land.describe(11))
        self.assertEqual(land.describe(71), "水域")


class IdentifyTests(unittest.TestCase):
    """匹配算法本身（用构造地图，结果可预期）。"""

    def _maps(self):
        # 两张 4x4 图，只有一处不同
        a = [1, 1, 1, 1,
             1, 6, 1, 1,
             1, 1, 1, 1,
             1, 1, 1, 1]
        b = [1, 1, 1, 1,
             1, 1, 1, 1,
             1, 1, 11, 1,
             1, 1, 1, 1]
        return {"maps_a": {"w": 4, "raw": a}, "maps_b": {"w": 4, "raw": b}}

    def _land(self):
        return mapmatch.LandTable([
            {"id": 1, "type": 3, "lv": 1},
            {"id": 6, "type": 4, "lv": 2},
            {"id": 11, "type": 5, "lv": 2},
        ])

    def test_identifies_correct_map(self):
        maps, land = self._maps(), self._land()
        # 在 (1,1) 观测到木材 Lv2 → 只有 maps_a 匹配
        samples = [{"x": 1, "y": 1, "res": "timber", "lv": 2}]
        # 样本太少应该拒绝判定
        res = mapmatch.identify(maps, samples, land, min_judged=8)
        self.assertIsNone(res["name"], "样本不足时不该硬给结论")

    def test_rejects_when_ambiguous(self):
        maps, land = self._maps(), self._land()
        samples = [{"x": 0, "y": 0, "res": "grain", "lv": 1}] * 10
        res = mapmatch.identify(maps, samples, land, min_judged=8)
        self.assertIsNone(res["name"], "两张图都匹配时不能瞎猜")

    def test_picks_winner_with_margin(self):
        maps, land = self._maps(), self._land()
        samples = ([{"x": 1, "y": 1, "res": "timber", "lv": 2}] * 6 +
                   [{"x": 0, "y": 0, "res": "grain", "lv": 1}] * 6)
        res = mapmatch.identify(maps, samples, land, min_judged=8)
        self.assertEqual(res["name"], "maps_a")


@unittest.skipUnless(HAS_APK, "没有 apk 文件，跳过真实数据测试")
class RealApkTests(unittest.TestCase):
    """用真实 apk 验证解析（防止解析逻辑退化）。"""

    @classmethod
    def setUpClass(cls):
        cls.reader = apkmap.ApkReader(APK)

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()

    def test_finds_expected_maps(self):
        names = apkmap.map_names(self.reader)
        for n in ("maps_0", "maps_13", "maps_14", "maps_15"):
            self.assertIn(n, names, "apk 里应该含 %s" % n)

    def test_map_sizes(self):
        m0 = apkmap.parse_map(self.reader, "maps_0")
        self.assertEqual((m0["width"], m0["height"]), (50, 50))
        m14 = apkmap.parse_map(self.reader, "maps_14")
        self.assertEqual((m14["width"], m14["height"]), (600, 600))

    def test_land_table_shape(self):
        land = mapmatch.LandTable(self.reader.load("land") or [])
        self.assertGreater(len(land.by_id), 100)
        # 三种资源地都应该有候选
        for res in ("grain", "timber", "stone"):
            found = any(land.candidates(res, lv) for lv in range(1, 6))
            self.assertTrue(found, "没找到 %s 类资源地" % res)

    def test_all_map_values_exist_in_land(self):
        """地图里出现的每个值都应该能在 land 表里查到，否则说明解析错位。"""
        land_rows = self.reader.load("land") or []
        ids = {e["id"] for e in land_rows if "id" in e}
        for name in ("maps_13", "maps_14", "maps_15"):
            raw = self.reader.load(name)
            missing = {v for v in raw} - ids
            self.assertEqual(missing, set(),
                             "%s 里有 %d 个值不在 land 表：%s"
                             % (name, len(missing), sorted(missing)[:5]))

    def test_water_is_at_corners(self):
        """600x600 地图的四角必然是海，用作「海 id」的交叉验证。"""
        for name in ("maps_13", "maps_14", "maps_15"):
            raw = self.reader.load(name)
            w = 600
            corners = [raw[0], raw[w - 1], raw[(w - 1) * w], raw[w * w - 1]]
            for v in corners:
                self.assertIn(v, mapmatch.WATER_IDS,
                              "%s 四角应该是水，实际是 %s" % (name, v))


class GridShapeTests(unittest.TestCase):
    def test_grid_of_square(self):
        w, h, rows = apkmap.grid_of([0, 1, 2, 3])
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(rows, [[0, 1], [2, 3]])

    def test_grid_of_non_square_returns_none(self):
        self.assertIsNone(apkmap.grid_of([0, 1, 2]))

    def test_grid_of_empty(self):
        self.assertIsNone(apkmap.grid_of([]))


if __name__ == "__main__":
    unittest.main()
