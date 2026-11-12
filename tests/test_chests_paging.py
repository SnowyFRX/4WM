"""领取编队宝箱：必须真的逐屏翻页，而不是把同一屏读八遍。

真机现象（用户反馈 2026-11-12）：
  「领取编队宝箱时下翻后无法有效领取」——军队面板一屏只放 4 张卡，
  第 5 支往下的宝箱要下滑才看得到，但脚本翻页后拿到的还是上一屏。

根因：Chests.run 传给 scroll.walk 的 grab 是缓存的 box["sc"]，
滑完之后没有任何地方重新截图，于是 walk 每一轮都读到同一张旧屏，
指纹相同 → 第一页就 break，后面的编队根本没被检查。
"""
import re
import unittest
from unittest.mock import patch

from jwm.tasks import chests as chests_mod
from jwm.tasks.chests import Chests

CHEST_RE = r"获得宝箱\s*[xX×]\s*\d+"


class _Hit:
    """screen.text() 的返回：只要 center 就够（点击用）。"""

    def __init__(self, x, y):
        self.center = (x, y)


class _Screen:
    """一屏军队面板：img 供翻页指纹用，text() 按正则返回命中。"""

    def __init__(self, page, has_chest):
        self.page = page
        self.has_chest = has_chest
        # 指纹：不同页必须不同，同一页稳定
        self.img = ("page-%d" % page).encode("utf-8") * 8

    def text(self, pattern):
        if self.has_chest and re.search(CHEST_RE, "获得宝箱 x1"):
            if pattern == CHEST_RE or "获得宝箱" in pattern:
                return _Hit(300, 400)
        if pattern.startswith("所有军队") or "拥有军队" in pattern:
            return _Hit(360, 200)
        return None

    def has(self, name, box=None, thr=None):
        return _Hit(660, 1200)


class _FakeUI:
    """grab() 每次调用都返回“当前看到的那一屏”。"""

    def __init__(self, screens):
        self.screens = screens
        self.idx = 0
        self.grabs = 0

    def current(self):
        return self.screens[min(self.idx, len(self.screens) - 1)]

    def grab(self):
        self.grabs += 1
        return self.current()

    def grab_stable(self, timeout=2.0):
        return self.grab()

    def to_map(self):
        return self.current()

    def wait_text(self, pattern, timeout=5):
        return self.current()

    def tap_text(self, sc, pattern, box=None, delay=0.0):
        return True

    def close(self):
        return None

    def on_map(self, sc=None):
        return True


class _FakeDev:
    def __init__(self, ui):
        self.ui = ui
        self.swipes = 0
        self.taps = []

    def tap(self, x, y, delay=0.0):
        self.taps.append((x, y))

    def swipe(self, x1, y1, x2, y2, dur=400, delay=0.0):
        self.swipes += 1
        # 一次滑动 = 往下看一屏
        if self.ui.idx < len(self.ui.screens) - 1:
            self.ui.idx += 1


class _FakeState:
    def __init__(self):
        self.counters = {}

    def inc(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


class _FakeBot:
    def __init__(self, ui):
        self.ui = ui
        self.dev = _FakeDev(ui)
        self.state = _FakeState()

    def phase(self, *a, **k):
        return None


class ChestPagingTests(unittest.TestCase):
    def _run(self, screens):
        ui = _FakeUI(screens)
        bot = _FakeBot(ui)

        def drain():
            # 领掉当前这一屏的宝箱：真实游戏里领完之后那张卡上的图标就没了
            ui.current().has_chest = False
            return (1, False)

        task = Chests(bot)
        with patch.object(Chests, "open_army_panel", return_value=screens[0]), \
             patch.object(Chests, "_drain", side_effect=drain):
            claimed = task.run()
        return claimed, ui, bot

    def test_claims_chests_from_pages_below_the_first_screen(self):
        """第 3 屏才有宝箱：翻页必须真的翻过去，并且领到那一个。"""
        screens = [
            _Screen(1, False),
            _Screen(2, False),
            _Screen(3, True),
        ]
        claimed, ui, bot = self._run(screens)
        self.assertEqual(claimed, 1, "第 3 屏的宝箱没被领到 —— 翻页没生效")
        self.assertGreaterEqual(ui.grabs, 3, "翻页后没有重新截图，读到的是旧屏")

    def test_every_page_carrying_a_chest_is_claimed(self):
        """每一屏都有宝箱：三屏三个，一个都不能漏。"""
        screens = [_Screen(1, True), _Screen(2, True), _Screen(3, True)]
        claimed, ui, bot = self._run(screens)
        self.assertEqual(claimed, 3)


if __name__ == "__main__":
    unittest.main()
