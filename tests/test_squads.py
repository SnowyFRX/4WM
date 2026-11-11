import json
import os
import tempfile
import unittest
from pathlib import Path

from jwm.operations import Operations


class SquadTests(unittest.TestCase):
    def make_ops(self, directory):
        ops = Operations(path=Path(directory, "queues.json"))
        ops.squad_path = Path(directory, "army_combos.json")
        return ops

    def test_user_squad_is_just_a_name_and_member_armies(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([{"name": "一队", "armies": ["编队2", "编队4"]}], active="一队")
            self.assertEqual(ops.active_squad, "一队")
            pick = ops.squad_pick()
            self.assertEqual(pick["name"], "一队")
            self.assertEqual(pick["armies"], ["编队2", "编队4"])
            self.assertNotIn("match", pick)

    def test_old_rule_file_is_migrated_without_match_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_path.write_text(json.dumps({
                "combos": [{"name": "规则1", "match": {"res": "grain", "level": 3},
                            "armies": ["编队1", "编队3"], "on": True}]
            }, ensure_ascii=False), encoding="utf-8")
            ops._load_squads()
            self.assertEqual(ops.squad_pick()["armies"], ["编队1", "编队3"])
            self.assertNotIn("match", ops.squad_pick())

    def test_setting_active_squad_to_auto_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([{"name": "一队", "armies": ["编队2"]}], active="一队")
            ops.set_active_squad("")
            self.assertIsNone(ops.active_squad)


class MultiSquadRouteTests(unittest.TestCase):
    """多条开荒路线各用不同小队：小队要能分别绑定自己的路线。"""

    def make_ops(self, directory):
        ops = Operations(path=Path(directory, "queues.json"))
        ops.squad_path = Path(directory, "army_combos.json")
        return ops

    def test_several_squads_can_each_own_a_route(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([
                {"name": "近战", "armies": ["编队2", "编队3"], "route": "a"},
                {"name": "远程", "armies": ["编队4"], "route": "b"},
            ], active="近战")
            self.assertEqual([s["name"] for s in ops.squad_snapshot()], ["近战", "远程"])
            self.assertEqual(ops.squad_pick(route_key="a")["name"], "近战")
            self.assertEqual(ops.squad_pick(route_key="b")["name"], "远程")

    def test_squad_for_other_route_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([{"name": "近战", "armies": ["编队2"], "route": "a"}], active="近战")
            # 当前路线是 b，绑定在 a 上的小队不能插手 b
            self.assertIsNone(ops.squad_pick(route_key="b"))

    def test_unbound_squad_works_as_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([{"name": "通用", "armies": ["编队2"]}], active="通用")
            self.assertEqual(ops.squad_pick(route_key="anything")["name"], "通用")

    def test_route_binding_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = self.make_ops(directory)
            ops.squad_set([{"name": "近战", "armies": ["编队2"], "route": "a"}], active="近战")
            again = self.make_ops(directory)
            again._load_squads()
            self.assertEqual(again.squad_pick(route_key="a")["route"], "a")


if __name__ == "__main__":
    unittest.main()
