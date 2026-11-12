"""ADB 常驻 shell 的超时与残留输出。

以前的 bug：`run()` 先 `os.read` 再检查超时。os.read 在管道没数据时无限阻塞，
所以只要 adb 活着却不吐字节（设备卡死、命令挂住），那个 15 秒超时永远轮不到执行
—— 脚本就永久停住，网页点停止也只能硬杀进程。

这几条测试锁的是「到点一定失败」，而不是「adb 恰好回了话才能失败」。
"""
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from jwm.device import Device, Shell

# 一个最小 adb shell 模拟器：收到 "命令; echo 标记" 就回 "OUT:命令" + 标记
GOOD = """
import sys
for line in sys.stdin:
    line = line.rstrip("\\n")
    cmd, sep, mark = line.rpartition("; echo ")
    if not sep:
        continue
    sys.stdout.write("OUT:" + cmd + "\\n")
    sys.stdout.write(mark + "\\n")
    sys.stdout.flush()
"""

# 收到命令但永不回应（模拟设备卡死）。预热命令 true 要正常回，否则连构造都过不去
SILENT = """
import sys, time
for line in sys.stdin:
    line = line.rstrip("\\n")
    cmd, sep, mark = line.rpartition("; echo ")
    if not sep:
        continue
    if cmd.strip() == "true":
        sys.stdout.write(mark + "\\n")
        sys.stdout.flush()
        continue
    time.sleep(30)
"""

# 启动了立刻退出（模拟 adb 崩了）
EXIT_NOW = """
import sys
sys.exit(3)
"""

# 第一条真实命令装死，0.5 秒后才把标记补发出来（模拟残留输出）
LATE_MARK = """
import sys, time
first = True
for line in sys.stdin:
    line = line.rstrip("\\n")
    cmd, sep, mark = line.rpartition("; echo ")
    if not sep:
        continue
    if cmd.strip() == "true":
        sys.stdout.write(mark + "\\n")
        sys.stdout.flush()
        continue
    if first:
        first = False
        time.sleep(0.5)
        sys.stdout.write(mark + "\\n")
        sys.stdout.flush()
        continue
    sys.stdout.write("SECOND\\n")
    sys.stdout.write(mark + "\\n")
    sys.stdout.flush()
"""


class ShellFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jwm_adb_")

    def _cmd(self, script):
        path = Path(self.tmp, "fake_adb_%d.py" % abs(hash(script)))
        path.write_text(textwrap.dedent(script), encoding="utf-8")
        return [sys.executable, str(path)]

    def shell(self, script, warmup=8.0):
        # 预热给足时间：Windows 起一个 Python 子进程本身就要几百毫秒，
        # 拿 0.2 秒去预热测的是解释器启动，不是我们要验的超时逻辑。
        return Shell(self._cmd(script), env=os.environ.copy(), timeout=warmup)


class NormalTests(ShellFixture):
    def test_returns_command_output(self):
        sh = self.shell(GOOD)
        try:
            self.assertEqual(sh.run("wm size").strip(), "OUT:wm size")
        finally:
            sh.close()

    def test_consecutive_commands_do_not_mix(self):
        sh = self.shell(GOOD)
        try:
            self.assertEqual(sh.run("first").strip(), "OUT:first")
            self.assertEqual(sh.run("second").strip(), "OUT:second")
        finally:
            sh.close()

    def test_end_mark_not_leaked_into_output(self):
        sh = self.shell(GOOD)
        try:
            self.assertNotIn("__JWM_END_", sh.run("hello"))
        finally:
            sh.close()

    def test_marks_are_unique_per_command(self):
        """固定标记会让上一条的迟到标记冒充下一条的结束标记。"""
        sh = self.shell(GOOD)
        try:
            seqs = []
            for i in range(3):
                sh.run("cmd%d" % i)
                seqs.append(sh._seq)
            self.assertEqual(seqs, sorted(set(seqs)), "序号应逐条递增且不重复，标记才能唯一")
            self.assertEqual(len(set(seqs)), 3)
        finally:
            sh.close()


class TimeoutTests(ShellFixture):
    def test_silent_shell_fails_within_timeout(self):
        """核心回归：adb 一个字节都不吐时，也必须在规定时间内失败。"""
        sh = self.shell(SILENT)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            sh.run("input tap 1 1", timeout=0.3)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, "超时没有真正生效（%.1f 秒才返回）" % elapsed)
        self.assertGreaterEqual(elapsed, 0.25)
        sh.close()

    def test_shell_dead_after_timeout(self):
        """超时后整条 shell 作废，不许拿半截管道继续写。"""
        sh = self.shell(SILENT)
        try:
            with self.assertRaises(TimeoutError):
                sh.run("first", timeout=0.3)
            self.assertTrue(sh.dead)
            with self.assertRaises(ConnectionError):
                sh.run("second", timeout=0.3)
        finally:
            sh.close()

    def test_exited_process_raises_connection_error(self):
        with self.assertRaises(ConnectionError):
            self.shell(EXIT_NOW)

    def test_late_mark_does_not_satisfy_next_command(self):
        """上一条命令的标记迟到落进管道，不能被下一条当成自己的结束。"""
        sh = self.shell(LATE_MARK)
        try:
            with self.assertRaises(TimeoutError):
                sh.run("stuck", timeout=0.2)
            # 等残留标记真的到达
            time.sleep(0.6)
            with self.assertRaises(ConnectionError):
                sh.run("next", timeout=1.0)
        finally:
            sh.close()


class DeviceFallbackTests(unittest.TestCase):
    def test_dead_shell_is_dropped_and_falls_back(self):
        """常驻 shell 作废后，重建失败要退回一次性 adb 调用，而不是抛穿。"""
        class DeadShell:
            dead = True

            def __init__(self):
                self.closed = 0

            def close(self):
                self.closed += 1

            def run(self, cmd, timeout=15):
                raise AssertionError("作废的 shell 不该再被写入")

        dev = Device.__new__(Device)
        dev.sh = DeadShell()
        dev._cmd = lambda args, **kw: ["jwm-no-such-adb-binary"]
        dev._run = lambda args, **kw: b"one-shot-out"
        self.assertEqual(dev.shell("wm size"), "one-shot-out")
        self.assertEqual(dev.sh, None)
        self.assertEqual(dev.sh, None)

    def test_failed_shell_is_rebuilt_once(self):
        """第一次失败要重建；重建成功就用新 shell 执行。"""
        class FlakyShell:
            dead = False

            def __init__(self):
                self.calls = 0

            def close(self):
                pass

            def run(self, cmd, timeout=15):
                self.calls += 1
                raise ConnectionError("simulated")

        old = FlakyShell()
        dev = Device.__new__(Device)
        dev.sh = old
        dev._cmd = lambda args, **kw: ["jwm-no-such-adb-binary"]
        dev._run = lambda args, **kw: b"fallback-out"
        self.assertEqual(dev.shell("wm size"), "fallback-out")
        self.assertEqual(old.calls, 1, "旧 shell 应被尝试过再放弃")


if __name__ == "__main__":
    unittest.main()
