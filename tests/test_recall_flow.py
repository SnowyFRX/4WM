"""打不出兵不血刃时必须真的召回队伍，不能只在网页上写一句「已召回」。

真机踩过的两个坑（2026-11-11 晚）：
  1. `run_route` 在 `too_hard` 分支直接 return，下面那段 `self.recall()` 是死代码；
     而 `_diagnose_stuck` 却对玩家说「已召回补兵」——队伍根本没动。
  2. `_need_supply` 在**没召回**的情况下就写了 recall_at 冷却，
     于是日志一直说「刚召回不久，这次先不打断它们」，实际一次都没召回。
"""
import time
import unittest

from jwm.tasks.expand import Expand


class _State:
    def __init__(self, data=None):
        self.data = data or {}
        self.saves = 0

    def save(self):
        self.saves += 1

    def tile(self, *a, **k):
        return {}

    def set_tile(self, *a, **k):
        pass


class _Route:
    def __init__(self):
        self.reports = []
        self.skips = []

    def report(self, revision, status, **k):
        self.reports.append(status)

    def note_skip(self, xy, reason, auto=False):
        self.skips.append((xy, reason))

    def avoid_and_replan(self, xy):
        return False


def make_expand(recalled=(), cfg=None):
    exp = Expand.__new__(Expand)
    exp.state = _State({})
    exp.route = _Route()
    exp.cfg = cfg or {"stuck_limit": 3, "heal_before_retry": 0.95, "recall_hp": 0.7,
                      "recall_cooldown_seconds": 600, "route_mode": "strict"}
    exp.need_heal = True
    exp.calls = []

    def fake_recall(names=None, thr=None):
        exp.calls.append({"names": names, "thr": thr})
        return list(recalled)

    exp.recall = fake_recall
    return exp


class RecallIsActuallyCalledTests(unittest.TestCase):
    def test_recall_runs_when_troops_cannot_fight(self):
        exp = make_expand(recalled=[("编队2", 232)])
        picked = exp._recall_if_needed()
        self.assertEqual(len(exp.calls), 1)
        self.assertEqual(picked, [("编队2", 232)])
        self.assertFalse(exp.need_heal, "召回后要清掉 need_heal，否则每格都重复召回")

    def test_cooldown_is_written_only_after_a_real_recall(self):
        exp = make_expand(recalled=[("编队2", 232)])
        exp._recall_if_needed()
        self.assertIn("recall_at", exp.state.data)

    def test_nothing_recalled_still_clears_the_flag_and_logs(self):
        exp = make_expand(recalled=[])
        picked = exp._recall_if_needed()
        self.assertEqual(picked, [])
        self.assertFalse(exp.need_heal)

    def test_recent_recall_is_not_repeated(self):
        exp = make_expand(recalled=[("编队2", 232)])
        exp.state.data["recall_at"] = time.time() - 60      # 1 分钟前刚召回
        exp._recall_if_needed()
        self.assertEqual(exp.calls, [], "冷却内不该再召回")

    def test_cooldown_expires(self):
        exp = make_expand(recalled=[("编队2", 232)])
        exp.state.data["recall_at"] = time.time() - 601
        exp._recall_if_needed()
        self.assertEqual(len(exp.calls), 1)


class StuckReportHonestyTests(unittest.TestCase):
    def test_message_does_not_claim_a_recall_that_did_not_happen(self):
        exp = make_expand()
        exp._diagnose_stuck(1, (243, 291), (238, 290), "轻微损失", recalled=())
        text = " ".join(exp.route.reports)
        self.assertNotIn("已召回", text)

    def test_message_names_the_armies_that_really_came_back(self):
        exp = make_expand()
        exp._diagnose_stuck(1, (243, 291), (238, 290), "轻微损失",
                            recalled=[("编队2", 232), ("编队3", 232)])
        text = " ".join(exp.route.reports)
        self.assertIn("编队2", text)
        self.assertIn("编队3", text)


class OnlyRecallWhenCantWinTests(unittest.TestCase):
    """用户的规则：只有「打不出兵不血刃」才回城补血。

    以前两个错误的召回触发点：
      1. `_need_supply` 把编队写进 understrength，招兵任务随后看到就把在外打地的
         队伍召回主城 → 表现是「打一格就回城」；
      2. 非路线模式 `dispatched == 0 or understrength -> recall()`，一轮没打成也召回。
    """

    def test_cannot_win_sets_recall_intent_but_does_not_touch_refill_list(self):
        exp = make_expand()
        exp._need_supply(["编队2", "编队3"])
        self.assertTrue(exp.need_heal)
        self.assertNotIn("understrength", exp.state.data,
                         "打不动不该写补兵名单（那会让招兵任务把出征队伍拽回家）")

    def test_supply_with_no_armies_does_nothing(self):
        exp = make_expand()
        exp.need_heal = False          # 从「没有请求」的状态开始
        self.assertFalse(exp._need_supply([]))
        self.assertFalse(exp.need_heal)

    def test_real_recall_does_not_pollute_the_refill_list(self):
        exp = make_expand(recalled=[("编队2", 232)])
        exp._recall_if_needed()
        self.assertNotIn("understrength", exp.state.data)


class BusyArmyTests(unittest.TestCase):
    def test_fighting_and_marching_armies_count_as_busy(self):
        exp = make_expand()
        exp.bot = type("B", (), {"_armies_cache": {
            "编队2": {"state": "战斗中"}, "编队3": {"state": "行军中"},
            "编队1": {"state": "待机中"}, "编队4": {"state": "驻守中"},
        }})()
        busy = exp._busy_armies()
        self.assertEqual(sorted(busy), ["编队2", "编队3"])

    def test_no_cache_means_no_judgement(self):
        exp = make_expand()
        exp.bot = type("B", (), {"_armies_cache": {}})()
        self.assertEqual(exp._busy_armies(), [])


if __name__ == "__main__":
    unittest.main()
