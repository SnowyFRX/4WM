"""校场和医馆都叫「选择军队」，必须分得开。

真机事实（2026-11-11 21:1x）：练兵任务打开的是医馆「治疗伤兵」面板
（治疗费用 / 治疗队列 / 还没有伤兵），旧代码只认「选择军队」就当成校场，
于是拿医馆的编队卡片当校场卡片、一个兵种头像都找不到 → 反复空试。
"""
import unittest

from jwm.tasks.drill import Drill


class _Screen:
    def __init__(self, texts, img=None):
        self.texts = texts
        self.img = img

    def text(self, pattern, region=None):
        import re

        for t in self.texts:
            if re.search(pattern, t):
                return type("B", (), {"text": t})()
        return None


class LooksLikeDrillTests(unittest.TestCase):
    def test_heal_panel_is_not_the_drill_ground(self):
        heal = _Screen(["选择军队", "治疗费用", "治疗队列(0/6)", "还没有伤兵"])
        self.assertFalse(Drill.looks_like_drill(heal))

    def test_heal_panel_is_rejected_even_with_train_button_nearby(self):
        heal = _Screen(["选择军队", "治疗费用", "治疗", "^训练$"])
        self.assertFalse(Drill.looks_like_drill(heal))

    def test_real_drill_panel_is_accepted(self):
        drill = _Screen(["选择军队", "训练费用", "训练队列(0/3)", "训练"])
        self.assertTrue(Drill.looks_like_drill(drill))

    def test_plain_army_list_is_not_the_drill_ground(self):
        other = _Screen(["所有军队", "拥有军队(5/7)", "待机中"])
        self.assertFalse(Drill.looks_like_drill(other))


if __name__ == "__main__":
    unittest.main()
