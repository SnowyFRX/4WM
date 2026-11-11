import json
import os
import tempfile
import unittest
from unittest.mock import patch

from jwm.state import State


class StateSaveTests(unittest.TestCase):
    def test_save_survives_transient_replace_permission_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            state = State(path)
            state.data["stats"] = {"recruited": 3}
            real_replace = os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(5, "拒绝访问")
                return real_replace(src, dst)

            with patch("jwm.state.os.replace", side_effect=flaky_replace):
                state.save()
            self.assertEqual(calls["n"], 2)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["stats"]["recruited"], 3)

    def test_save_falls_back_to_direct_write_when_replace_keeps_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            state = State(path)
            state.data["stats"] = {"recruited": 4}
            with patch("jwm.state.os.replace", side_effect=PermissionError(5, "拒绝访问")):
                state.save()
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["stats"]["recruited"], 4)


if __name__ == "__main__":
    unittest.main()
