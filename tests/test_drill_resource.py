"""练兵必须按资源决定做不做，空试不能算完成。

真机证据（logs/shots/1111_180223_err_drill.png）：
  点「训练」后游戏弹「资源不足 536/580」，旧代码只认「元宝/立即完成/购买」，
  于是把这次空试当成成功返回 True，接着计进 got、扣减队列、写统计。
"""
import unittest

from jwm.tasks.drill import Drill


class CostParseTests(unittest.TestCase):
    def test_parse_cost_reads_amounts_and_duration(self):
        cost = Drill.parse_cost("训练费用 536 0:30")
        self.assertEqual(cost["amounts"], [536])
        self.assertEqual(cost["duration"], "0:30")

    def test_parse_cost_handles_groups_of_resources(self):
        cost = Drill.parse_cost("训练费用 1,200 800 0:45")
        self.assertEqual(cost["amounts"], [1200, 800])

    def test_parse_cost_of_empty_panel_is_empty(self):
        self.assertEqual(Drill.parse_cost(""), {"amounts": [], "duration": ""})


class ShortfallTests(unittest.TestCase):
    def test_shortfall_reports_exact_gap(self):
        gap = Drill.shortfall([536], {"grain": 400, "wood": 9000, "stone": 9000})
        self.assertEqual(len(gap), 1)
        self.assertIn("536", gap[0])
        self.assertIn("400", gap[0])

    def test_enough_resources_means_no_shortfall(self):
        self.assertEqual(Drill.shortfall([536], {"grain": 803, "wood": 3958, "stone": 3756}), [])

    def test_unknown_amount_does_not_block(self):
        """读不到资源时不许凭空拦人（宁可不判，也不能凭空说资源不足）。"""
        self.assertEqual(Drill.shortfall([536], {}), [])

    def test_side_numbers_are_not_mistaken_for_cost(self):
        """真机日志出现过 [636, 1]：那个 1 是旁边的等级/数量，不是费用。"""
        gap = Drill.shortfall([636, 1], {"grain": 1002, "wood": 590, "stone": 925})
        self.assertEqual(len(gap), 1)
        self.assertIn("636", gap[0])

    def test_only_side_numbers_means_no_shortfall(self):
        self.assertEqual(Drill.shortfall([1, 2], {"grain": 1002, "wood": 590}), [])


class RejectionTests(unittest.TestCase):
    def test_insufficient_resource_dialog_is_a_failure_not_success(self):
        """点训练后弹「资源不足」→ 必须返回失败并标成 resource。"""
        class Screen:
            img = None

            def text(self, pattern, region=None):
                return "资源不足" if "资源" in pattern else None

        verdict = Drill.verify_training_click(Screen())
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], "resource")

    def test_clean_panel_without_queue_change_is_unverified(self):
        class Screen:
            img = None

            def text(self, pattern, region=None):
                return None

        verdict = Drill.verify_training_click(Screen(), (1, 3), (1, 3))
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], "unverified")

    def test_queue_increase_means_the_click_was_accepted(self):
        class Screen:
            img = None

            def text(self, pattern, region=None):
                return None

        verdict = Drill.verify_training_click(Screen(), (1, 3), (2, 3))
        self.assertTrue(verdict[0])

    def test_gem_dialog_is_still_refused(self):
        class Screen:
            img = None

            def text(self, pattern, region=None):
                return "立即完成" if "元宝" in pattern else None

        ok, why = Drill.verify_training_click(Screen())
        self.assertFalse(ok)
        self.assertIn("元宝", why)


if __name__ == "__main__":
    unittest.main()
