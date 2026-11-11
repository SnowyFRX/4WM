"""主城弹窗必须被认出来，否则读地图会把整块弹窗也截进去。

用户真机反馈：定位主城时点开的主城弹窗（带编队列表）被当成干净地图读了。
它和普通地块弹窗不同：没有选中框，但有「进入 / 行军」+ 坐标。
"""
import re
import unittest

from jwm.ui import UI


class _Box:
    def __init__(self, text):
        self.text = text
        self.box = (300, 500, 400, 530)

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) // 2, (self.box[1] + self.box[3]) // 2)


class _Screen:
    """只实现 has_home_popup 用到的那部分。"""

    def __init__(self, texts):
        self.boxes = [_Box(t) for t in texts]
        self.img = None

    def text(self, pattern, region=None):
        rx = re.compile(pattern)
        for b in self.boxes:
            if rx.search(b.text):
                return b
        return None


def has_home_popup(texts):
    # has_home_popup 不碰 self 的任何状态，跳过 __init__ 直接调真实实现
    return UI.has_home_popup(UI.__new__(UI), _Screen(texts))


class HomePopupTests(unittest.TestCase):
    def test_popup_with_coord_and_enter_march_is_detected(self):
        self.assertTrue(has_home_popup(["海蜇皮", "(243,291)", "进入", "行军", "编队5"]))

    def test_collapsed_popup_still_detected(self):
        # 收起状态没有编队列表，也得认出来
        self.assertTrue(has_home_popup(["(243,291)", "进入", "行军"]))

    def test_clean_map_is_not_mistaken_for_popup(self):
        self.assertFalse(has_home_popup(["粮食地", "(240,290)"]))

    def test_world_chat_without_buttons_is_not_popup(self):
        # 屏幕上恰好有坐标文字但没有进入/行军，不能误判
        self.assertFalse(has_home_popup(["(120,80)"]))


if __name__ == "__main__":
    unittest.main()
