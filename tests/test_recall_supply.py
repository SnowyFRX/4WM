"""召回必须真的把编队叫回来并排进补兵名单。

用户反馈 8：打不出兵不血刃时脚本说「召回补兵」，实际没落实。
根因：recall() 只点「确定」就走，没有任何回执；而且被召回的名字没有写进
state.data["understrength"]，招兵任务根本不知道要给谁补兵。
"""
import unittest
from types import SimpleNamespace

from jwm.tasks.expand import Expand


class _FakeState:
    def __init__(self):
        self.data = {"understrength": []}

    def save(self):
        self.saved = True


class RecallBookkeepingTests(unittest.TestCase):
    def make_expand(self):
        exp = Expand.__new__(Expand)
        exp.state = _FakeState()
        exp.recalled = []
        return exp

    def test_recalled_armies_are_queued_for_refill(self):
        exp = self.make_expand()
        exp.mark_understrength(["编队2", "编队4"])
        self.assertEqual(sorted(exp.state.data["understrength"]), ["编队2", "编队4"])
        self.assertTrue(getattr(exp.state, "saved", False))

    def test_duplicates_are_not_added_twice(self):
        exp = self.make_expand()
        exp.mark_understrength(["编队2"])
        exp.mark_understrength(["编队2", "编队3"])
        self.assertEqual(sorted(exp.state.data["understrength"]), ["编队2", "编队3"])

    def test_empty_names_are_ignored(self):
        exp = self.make_expand()
        exp.mark_understrength(["", None, "编队5"])
        self.assertEqual(exp.state.data["understrength"], ["编队5"])


class RecallVerifyTests(unittest.TestCase):
    """召回点了确定之后要读一次回执，不能直接假定成功。"""

    def test_marching_state_is_accepted(self):
        sc = SimpleNamespace(text=lambda *a, **k: "行军中 0:12:04")
        self.assertTrue(Expand.recall_accepted(sc))

    def test_untouched_panel_is_not_accepted(self):
        sc = SimpleNamespace(text=lambda *a, **k: None)
        self.assertFalse(Expand.recall_accepted(sc))


if __name__ == "__main__":
    unittest.main()
