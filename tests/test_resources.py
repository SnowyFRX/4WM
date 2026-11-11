import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jwm import resources


class ResourceStockTests(unittest.TestCase):
    def screenshot(self, rows):
        return SimpleNamespace(
            img=np.full((1280, 720, 3), 255, dtype=np.uint8),
            boxes=[SimpleNamespace(center=(60, y), text=str(value)) for y, value in rows],
        )

    def test_unmatched_icons_stay_unknown_instead_of_color_guess(self):
        sc = self.screenshot([(185, 13), (225, 7), (265, 5), (305, 2)])
        with patch.object(resources, "_icon_templates", return_value=[]):
            result = resources.read_stock(sc)
        self.assertEqual(result, {"book": None, "iron": None, "scroll": None, "nail": None})

    def test_icon_that_only_looks_vaguely_like_book_is_rejected(self):
        templates = dict(resources._icon_templates())
        if "book" not in templates:
            self.skipTest("缺少经验书模板")
        template = templates["book"]
        # 真实日志里没做模板的钉子被经验书吸到平均差 39.8，必须拒绝
        patch = np.clip(template + 40, 0, 255).astype(np.uint8)
        name, _ = resources._match_icon(patch, [("book", template)])
        self.assertIsNone(name)

    def test_stone_row_is_not_included_in_special_resources(self):
        sc = self.screenshot([(158, 1), (185, 13), (225, 7), (265, 5)])
        # 158 是石头行的碎片，应该被行区过滤掉；剩下 3 行会让 read_stock 走
        # 「行数不足 → 放大补读」那条路。补读要真跑 OCR，一张 736x1472 的图，
        # 机器一紧张就 OOM。这里把 OCR 换成空结果：既不跑模型，也正好模拟
        # 「小数字确实读不出来」的真实情况，测试要验的仍是「石头不进 iron」。
        with patch.object(resources, "_icon_templates", return_value=[("iron", None)]), \
             patch.object(resources, "_match_icon", return_value=("iron", 0.0)), \
             patch("jwm.vision.OCR.read", return_value=[]):
            result = resources.read_stock(sc)
        self.assertEqual(result["iron"], 13)


if __name__ == "__main__":
    unittest.main()
