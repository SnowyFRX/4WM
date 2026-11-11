import os
import tempfile
import unittest
from types import SimpleNamespace

from jwm.state import State
from jwm.tasks.build import Build


class BuildCacheTests(unittest.TestCase):
    def make_build(self, cache, home=(243, 291)):
        state = State(os.path.join(tempfile.mkdtemp(), "state.json"))
        state.data["building_cache"] = cache
        observed = {}
        ops = SimpleNamespace(home=home, facts={}, observe=lambda **kw: observed.update(kw))
        bot = SimpleNamespace(state=state, operations=ops, dev=None, ui=None, cfg={})
        return Build(bot), observed, state

    def test_loads_cached_levels_without_rescanning(self):
        build, observed, _ = self.make_build({
            "home": [243, 291], "at": 100,
            "buildings": {"主城": {"level": 8}, "兵营": {"level": 6}},
        })
        self.assertTrue(build.load_cache())
        self.assertEqual(observed["buildings"]["主城"]["level"], 8)
        self.assertEqual(observed["buildingsCachedAt"], 100)

    def test_other_home_does_not_reuse_cache(self):
        build, _, _ = self.make_build({
            "home": [1, 1], "at": 100,
            "buildings": {"主城": {"level": 8}},
        })
        self.assertFalse(build.load_cache())

    def test_remember_writes_levels_for_next_start(self):
        build, _, state = self.make_build({})
        build.bot.operations.facts = {"buildings": {"主城": {"level": 8}}}
        build.remember_buildings()
        saved = state.data["building_cache"]
        self.assertEqual(saved["home"], [243, 291])
        self.assertEqual(saved["buildings"]["主城"]["level"], 8)


if __name__ == "__main__":
    unittest.main()
