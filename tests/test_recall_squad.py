"""召回要按「小队」整体走，不能把一支小队拆成一半回城、一半留在外面。

用户反馈（2026-11-12）：
  「召回回血时会把小队中各编队拆分开」

根因：recall() 逐支编队按自己的血量判断要不要召回。
出兵那条路有 _dispatch_whole_squad（同一小队一起走、不拆开），
召回这条路没有对应逻辑 —— 小队里只要有一支血高一点，它就被留在外面，
于是本来一起出征的两支，回城时就散成了两支各自行动的队伍。
"""
import unittest

from jwm.tasks.expand import Expand


def army(name, hp, loc="(240,287) 行军中", secs=30, tap_y=400):
    return (tap_y, name, secs, loc, hp)


SQUAD = {"name": "一队", "armies": ["编队2", "编队3"], "on": True}


class RecallTargetsTests(unittest.TestCase):
    def pick(self, armies, thr=0.7, weak=(), names=None, squads=(SQUAD,)):
        out = Expand.recall_targets(armies, thr=thr, weak=weak, names=names, squads=list(squads))
        return [row[1] for row in out], out

    def test_a_squad_comes_back_together_when_one_member_is_hurt(self):
        """一支 60%、一支 80%：整队一起回来，不能只召回掉血的那支。"""
        names, out = self.pick([army("编队2", 0.6), army("编队3", 0.8)])
        self.assertEqual(sorted(names), ["编队2", "编队3"])
        why = {row[1]: row[5] for row in out}
        self.assertIn("同队", why["编队3"], "队友要回来的理由应该写成「同队」")

    def test_an_army_outside_any_squad_uses_its_own_hp(self):
        """没有小队时还是按单支判断：80% 不用回城。"""
        names, _ = self.pick([army("编队5", 0.8)], squads=())
        self.assertEqual(names, [])

    def test_an_army_outside_any_squad_is_recalled_when_hurt(self):
        names, _ = self.pick([army("编队5", 0.5)], squads=())
        self.assertEqual(names, ["编队5"])

    def test_named_recall_takes_only_the_named_ones(self):
        """点名召回只召点名的，不因为同队就把别人也拽回来。"""
        names, _ = self.pick([army("编队2", 0.6), army("编队3", 1.0)], names=("编队2",))
        self.assertEqual(names, ["编队2"])

    def test_armies_already_home_are_left_alone(self):
        names, _ = self.pick([army("编队2", 0.6, loc="主城"), army("编队3", 0.8)])
        self.assertEqual(names, [])

    def test_a_squad_entirely_at_home_is_not_recalled(self):
        names, _ = self.pick([army("编队2", 0.3, loc="主城"), army("编队3", 0.3, loc="主城")])
        self.assertEqual(names, [])

    def test_understrength_member_pulls_the_squad_home(self):
        """缺员名单里的成员也算「需要回城补兵」，队友一起走。"""
        names, out = self.pick([army("编队2", 1.0), army("编队3", 1.0)], weak=("编队2",))
        self.assertEqual(sorted(names), ["编队2", "编队3"])

    def test_unknown_location_is_not_touched_without_a_name(self):
        """位置没读清的编队不乱动（和以前的行为一致）。"""
        names, _ = self.pick([army("编队2", 0.2, loc="")])
        self.assertEqual(names, [])

    def test_real_case_from_the_log_2026_11_11_2325(self):
        """真机日志（11-11 23:25:48）的现场：编队2 64%、编队3 100%，同在地块 (240,287)。

        当时的日志是「召回 编队2（位于粮食地(240,287)，血量64%）回主城休整」——
        只把编队2 叫回来，编队3 留在外面，一起出征的两支就这么散了。
        """
        armies = [
            army("编队2", 0.64, loc="粮食地(240,287)"),
            army("编队3", 1.00, loc="粮食地(240,287)"),
        ]
        names, out = self.pick(armies)
        self.assertEqual(sorted(names), ["编队2", "编队3"],
                         "同队的两支必须一起回城，否则小队就被拆开了")
        why = {row[1]: row[5] for row in out}
        self.assertIn("回主城休整", why["编队2"])
        self.assertIn("同队", why["编队3"])


if __name__ == "__main__":
    unittest.main()
