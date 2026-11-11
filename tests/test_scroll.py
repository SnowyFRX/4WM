"""列表翻页：必须逐屏读到最后一屏，并且到尽头就停，不能无限重复读。

真机现象（用户反馈 1）：
  - 军队面板一屏只放 4 张卡，第 5 支往下的宝箱/状态读不到；
  - 读到尽头后还在继续滑，同一批卡片被重复解析，日志出现「6 支（拥有 5）」。
"""
import hashlib
import unittest

from jwm.scroll import walk


def _screen(tag):
    """最小 Screen 替身：只要 img 和 tags，walk 只用 img 判断是否还在动。"""
    return type("S", (), {"img": hashlib.sha256(tag.encode()).digest()[:16],
                          "tags": tag})()


class WalkTests(unittest.TestCase):
    def test_reads_every_page_then_stops_at_the_end(self):
        pages = ["p1", "p2", "p3", "p3", "p3"]   # 第 4 页开始滚不动了
        seen = []

        def grab():
            return _screen(pages[min(len(seen), len(pages) - 1)])

        def swipe():
            pass

        def on_page(sc):
            seen.append(sc.tags)

        count = walk(grab, swipe, on_page, max_pages=10)
        self.assertEqual(seen, ["p1", "p2", "p3"])
        self.assertEqual(count, 3)

    def test_page_callback_can_stop_early(self):
        pages = ["p1", "p2", "p3"]
        seen = []

        def grab():
            return _screen(pages[min(len(seen), len(pages) - 1)])

        def on_page(sc):
            seen.append(sc.tags)
            return False   # 已经找够了，别再滑

        walk(grab, lambda: None, on_page, max_pages=10)
        self.assertEqual(seen, ["p1"])

    def test_hard_page_cap_is_respected(self):
        counter = {"n": 0}

        def grab():
            return _screen("page%d" % counter["n"])

        def swipe():
            counter["n"] += 1

        count = walk(grab, swipe, lambda sc: None, max_pages=3)
        self.assertEqual(count, 3)

    def test_same_content_is_not_counted_twice(self):
        """滚动没生效（内容完全一样）时不能把同一屏算成两屏。"""
        seen = []
        walk(lambda: _screen("only"), lambda: None, lambda sc: seen.append(sc.tags), max_pages=5)
        self.assertEqual(seen, ["only"])


if __name__ == "__main__":
    unittest.main()
