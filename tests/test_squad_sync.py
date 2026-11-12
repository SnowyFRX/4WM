"""小队同步：指派和召回都必须「整队一起」，要么全走、要么都不动。

用户的要求很直接（2026-11-12）：
  「你只需保证指派和召回时，一个小队里面的所有编队都能同步不就行了」

所以两条路都按同一个契约实现：
  - 指派：小队成员只要有一个上不了（行军太远/选不中/出征位不够），整队不派；
  - 召回：小队成员只要有一个算不准（位置读不到），整队不动，绝不只叫回一半。
"""
import unittest

from jwm.tasks.expand import Expand


def army(name, hp, loc="(240,287) 行军中", secs=30, tap_y=400):
    return (tap_y, name, secs, loc, hp)


SQUAD = {"name": "一队", "armies": ["编队2", "编队3"], "on": True}


class RecallSynchronisedTests(unittest.TestCase):
    """召回：整队同步。"""

    def pick(self, armies, thr=0.7, weak=(), names=None, squads=(SQUAD,)):
        out = Expand.recall_targets(armies, thr=thr, weak=weak, names=names, squads=list(squads))
        return [row[1] for row in out]

    def test_member_with_unknown_location_comes_back_with_the_squad(self):
        """队友位置读不到，也要跟着一起回 —— 否则队还是被拆开。"""
        names = self.pick([
            army("编队2", 0.5, loc="荒地(239,289)"),
            army("编队3", 1.0, loc=""),
        ])
        self.assertEqual(sorted(names), ["编队2", "编队3"])

    def test_squad_with_all_members_readable_comes_back_whole(self):
        names = self.pick([
            army("编队2", 0.5, loc="荒地(239,289)"),
            army("编队3", 1.0, loc="荒地(239,289)"),
        ])
        self.assertEqual(sorted(names), ["编队2", "编队3"])

    def test_squad_with_a_member_already_home_is_fine(self):
        """队友已经在主城，不算「读不到」，其余成员照常整队召回。"""
        names = self.pick([
            army("编队2", 0.5, loc="荒地(239,289)"),
            army("编队3", 1.0, loc="主城"),
        ])
        self.assertEqual(names, ["编队2"])

    def test_a_member_missing_from_this_page_is_still_recalled_here(self):
        """队友在别的页（还没翻到）：这一页先把要召的勾上，翻到那页会一起勾。"""
        names = self.pick([army("编队2", 0.5, loc="荒地(239,289)")])
        self.assertEqual(names, ["编队2"])


class DispatchSynchronisedTests(unittest.TestCase):
    """指派：整队同步（成员没齐就一个都不派）。"""

    def test_squad_is_not_dispatched_when_a_member_cannot_join(self):
        picked = Expand.squad_ready(["编队2"], ["编队2", "编队3"])
        self.assertFalse(picked, "成员没齐就该整队不派")

    def test_squad_is_dispatched_when_every_member_joined(self):
        picked = Expand.squad_ready(["编队3", "编队2"], ["编队2", "编队3"])
        self.assertTrue(picked)

    def test_no_squad_means_nothing_to_wait_for(self):
        self.assertTrue(Expand.squad_ready([], []))


if __name__ == "__main__":
    unittest.main()
