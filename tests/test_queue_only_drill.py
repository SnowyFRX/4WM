"""三层限制：练兵只执行用户明确加入队列的操作。

- 队列为空：不打开校场、不自动练兵、不自动召回；
- 队列有活：每次实际点击前必须核对队首仍是用户这条指令（ops.attempt）。

本环境 cv2 DLL 可能加载失败，用桩模块绕过（被测逻辑不依赖真实 cv2/numpy）。
"""
import sys
import types
import unittest
from unittest import mock

for _name in ("cv2", "numpy"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except Exception:
            sys.modules[_name] = types.ModuleType(_name)

from jwm.tasks.drill import Drill


class _Ops:
    def __init__(self, job):
        self.job = job
        self.attempts = []

    def head(self, kind):
        return self.job

    def guard_ready(self, kind):
        return True

    def attempt(self, kind, job, click, **kw):
        self.attempts.append((kind, job["id"]))
        click()
        return True


class _Bot:
    def __init__(self, ops):
        self.operations = ops
        self.cfg = {}
        self.dev = mock.Mock()
        self.ui = mock.Mock()


class QueueOnlyDrillTests(unittest.TestCase):
    def test_empty_queue_does_not_open_drill_or_train(self):
        bot = _Bot(_Ops(None))
        drill = Drill(bot)
        with mock.patch.object(drill, "open_drill") as opened, \
                mock.patch.object(drill, "queued_drill") as queued:
            self.assertEqual(drill.run(), 0)
        opened.assert_not_called()
        queued.assert_not_called()

    def test_head_job_is_executed(self):
        job = {"id": "j1", "army": "编队1", "count": 1, "slot": 0}
        bot = _Bot(_Ops(job))
        drill = Drill(bot)
        with mock.patch.object(drill, "queued_drill", return_value=1) as queued:
            self.assertEqual(drill.run(), 1)
        queued.assert_called_once_with(job)

    def test_every_training_click_rechecks_queue_head(self):
        job = {"id": "j1", "army": "编队1", "count": 2, "slot": 1}
        ops = _Ops(job)
        drill = Drill(_Bot(ops))
        clicks = []
        with mock.patch.object(drill, "avatar_centers", return_value=[105, 217]), \
                mock.patch.object(drill, "train_soldier",
                                  side_effect=lambda x, approve=None: (
                                      approve(lambda: clicks.append(x)) and (True, ""))), \
                mock.patch.object(drill, "queue", return_value=(0, 3)), \
                mock.patch.object(drill.ui, "grab"):
            got, blocked = drill._train_army_checked(
                "编队1", limit=2, slot=1,
                approve=lambda click: ops.attempt("drill", job, click))
        self.assertEqual(got, 2)
        self.assertIsNone(blocked)
        self.assertEqual(clicks, [105, 105])
        self.assertEqual(ops.attempts, [("drill", "j1"), ("drill", "j1")])


if __name__ == "__main__":
    unittest.main()
