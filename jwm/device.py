"""ADB 设备封装：截图、点击、滑动、按键、启动应用。

所有坐标都以 720x1280 竖屏为基准；如果设备分辨率不同，会按比例换算。

提速（实测，Android Studio 模拟器）：
  - 截图：adb raw 截图约 1.0s；模拟器控制台 `screenrecord screenshot` 长连接约 0.06s。
    其他模拟器（MuMu/雷电）没有控制台，退回设备端 gzip 压缩截图（约 0.55s）。
  - 点击：每次起 adb 进程约 0.2s；常驻 adb shell 约 0.05s。
  - 点击后不再固定等待，而是截图看画面变化，稳定了就返回（delay 只作为上限）。
"""
import gzip
import logging
import os
import random
import socket
import subprocess
import tempfile
import threading
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)

BASE_W, BASE_H = 720, 1280
PACKAGE = "inland.jwm.twgame"


class EmulatorConsole:
    """Android Studio 模拟器控制台（telnet 端口 = emulator-XXXX 里的 XXXX）。"""

    def __init__(self, port):
        token_path = os.path.expanduser("~/.emulator_console_auth_token")
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.sock.makefile("rwb")
        self._read()
        if os.path.exists(token_path):
            with open(token_path) as fh:
                self.cmd("auth " + fh.read().strip())
        fd, self.path = tempfile.mkstemp(prefix="jwm_shot_", suffix=".png")
        os.close(fd)

    def _read(self):
        lines = []
        while True:
            line = self.f.readline().decode("utf-8", "ignore")
            if not line:
                raise ConnectionError("模拟器控制台断开")
            lines.append(line)
            if line.startswith("OK") or line.startswith("KO"):
                if line.startswith("KO"):
                    raise RuntimeError("控制台命令失败: " + "".join(lines))
                return lines

    def cmd(self, c):
        self.f.write((c + "\n").encode())
        self.f.flush()
        return self._read()

    def screenshot(self):
        self.cmd("screenrecord screenshot " + self.path)
        data = np.fromfile(self.path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Shell:
    """常驻 adb shell，省掉每次启动 adb 进程的开销。"""

    MARK = b"__JWM_END__"

    def __init__(self, adb_cmd):
        self.p = subprocess.Popen(adb_cmd + ["shell"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=0)
        self.lock = threading.Lock()
        self.run("true")

    def run(self, cmd, timeout=15):
        with self.lock:
            self.p.stdin.write((cmd + "; echo " + self.MARK.decode() + "\n").encode())
            buf = b""
            end = time.time() + timeout
            while self.MARK not in buf:
                if time.time() > end or self.p.poll() is not None:
                    raise TimeoutError("adb shell 无响应: " + cmd)
                chunk = os.read(self.p.stdout.fileno(), 65536)
                if not chunk:
                    raise ConnectionError("adb shell 断开")
                buf += chunk
            return buf.split(self.MARK)[0].decode("utf-8", "ignore")

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def _small(img):
    return cv2.resize(img, (90, 160), interpolation=cv2.INTER_AREA).astype(np.int16)


class Device:
    def __init__(self, serial=None, adb="adb", tap_jitter=3):
        self.adb = adb
        self.serial = serial
        self.tap_jitter = tap_jitter
        self.size = None  # 设备真实分辨率 (w, h)
        self.console = None
        self.sh = None
        self.last = None  # 最近一张截图（缩小版，用于判断画面变化）
        if serial and ":" in serial:
            # 网络 adb（MuMu/雷电等模拟器）先连上
            self._run(["connect", serial], with_serial=False, check=False)
        self.size = self._query_size()
        if self.size != (BASE_W, BASE_H):
            log.warning("设备分辨率 %s 不是 720x1280，将按比例换算坐标；建议把模拟器设为 720x1280", self.size)
        self._open_fast_paths()

    def _open_fast_paths(self):
        serial = self.serial or ""
        if serial.startswith("emulator-"):
            try:
                self.console = EmulatorConsole(int(serial.split("-")[1]))
                self.console.screenshot()
                log.info("截图走模拟器控制台（快速）")
            except Exception as e:
                log.info("模拟器控制台不可用（%s），截图走 adb", e)
                self.console = None
        try:
            self.sh = Shell(self._cmd([]))
        except Exception as e:
            log.info("常驻 adb shell 不可用（%s）", e)
            self.sh = None

    # ---------------- 底层 ----------------
    def _cmd(self, args, with_serial=True):
        cmd = [self.adb]
        if with_serial and self.serial:
            cmd += ["-s", self.serial]
        return cmd + list(args)

    def _run(self, args, with_serial=True, check=True, timeout=30):
        r = subprocess.run(self._cmd(args, with_serial), capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("adb 失败: %s\n%s" % (" ".join(args), r.stderr.decode("utf-8", "ignore")))
        return r.stdout

    def shell(self, cmd, timeout=30):
        if self.sh is not None:
            try:
                return self.sh.run(cmd, timeout=timeout)
            except Exception as e:
                log.warning("常驻 shell 出错（%s），重新连接", e)
                self.sh.close()
                try:
                    self.sh = Shell(self._cmd([]))
                    return self.sh.run(cmd, timeout=timeout)
                except Exception:
                    self.sh = None
        return self._run(["shell", cmd], timeout=timeout).decode("utf-8", "ignore")

    def _query_size(self):
        out = self._run(["shell", "wm size"]).decode("utf-8", "ignore")
        size = None
        for line in out.splitlines():
            if ":" in line:
                w, h = line.split(":")[1].strip().split("x")
                size = (int(w), int(h))
        return size

    # ---------------- 截图 ----------------
    def _adb_screenshot(self):
        raw = self._run(["exec-out", "screencap | gzip -1"], timeout=20)
        try:
            raw = gzip.decompress(raw)
        except Exception:
            raw = self._run(["exec-out", "screencap"], timeout=20)
        # raw 格式: 宽(4) 高(4) 格式(4) [色彩空间(4), Android 9+] + RGBA 像素
        w = int.from_bytes(raw[0:4], "little")
        h = int.from_bytes(raw[4:8], "little")
        px = w * h * 4
        header = len(raw) - px
        if header not in (12, 16) or w <= 0 or h <= 0:
            png = self._run(["exec-out", "screencap", "-p"], timeout=20)
            return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        arr = np.frombuffer(raw, np.uint8, count=px, offset=header).reshape(h, w, 4)
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)

    def screenshot(self):
        """返回 720x1280 的 BGR 图像 (numpy)。"""
        img = None
        if self.console is not None:
            try:
                img = self.console.screenshot()
            except Exception as e:
                log.warning("控制台截图失败（%s），改用 adb", e)
                self.console.close()
                self.console = None
        if img is None:
            img = self._adb_screenshot()
        if img.shape[1] > img.shape[0]:
            log.warning("截图是横屏 (%sx%s)，游戏应为竖屏", img.shape[1], img.shape[0])
        if (img.shape[1], img.shape[0]) != (BASE_W, BASE_H):
            img = cv2.resize(img, (BASE_W, BASE_H), interpolation=cv2.INTER_AREA)
        self.last = img
        return img

    def settle(self, max_wait, interval=0.08, need_stable=3):
        """等画面变化后稳定下来（最多 max_wait 秒）。返回最后一张截图。

        - 画面变了、并且连续 need_stable 帧不再变 → 立刻返回；
        - 一直没变 → 等满 max_wait（可能是服务器响应慢）。
        地图上的小动画（士兵走动）整体差异很小，不影响判断。
        """
        start = time.time()
        prev = self.screenshot()
        ps = _small(prev)
        changed = False
        stable = 0
        while time.time() - start < max_wait:
            time.sleep(interval)
            cur = self.screenshot()
            cs = _small(cur)
            d = float(np.abs(cs - ps).mean())
            if d > 2.0:
                changed = True
                stable = 0
            else:
                stable += 1
                if changed and stable >= need_stable:
                    return cur
            ps = cs
        return self.last

    # ---------------- 输入 ----------------
    def _scale(self, x, y):
        if not self.size or self.size == (BASE_W, BASE_H):
            return int(x), int(y)
        return int(x * self.size[0] / BASE_W), int(y * self.size[1] / BASE_H)

    def tap(self, x, y, jitter=None, delay=0.0):
        """点击；delay>0 时等画面稳定（delay 是最长等待时间）。"""
        j = self.tap_jitter if jitter is None else jitter
        if j:
            x += random.randint(-j, j)
            y += random.randint(-j, j)
        sx, sy = self._scale(x, y)
        log.debug("tap (%d,%d)", x, y)
        self.shell("input tap %d %d" % (sx, sy))
        if delay:
            self.settle(delay)

    def swipe(self, x1, y1, x2, y2, duration_ms=600, delay=0.0):
        a = self._scale(x1, y1)
        b = self._scale(x2, y2)
        self.shell("input swipe %d %d %d %d %d" % (a[0], a[1], b[0], b[1], duration_ms))
        if delay:
            self.settle(delay)

    def back(self, delay=0.8):
        self.shell("input keyevent 4")
        if delay:
            self.settle(delay)

    # ---------------- 应用 ----------------
    def foreground_package(self):
        return self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")

    def game_running(self):
        return PACKAGE in self.foreground_package()

    def start_game(self):
        self.shell("monkey -p %s -c android.intent.category.LAUNCHER 1" % PACKAGE)

    def stop_game(self):
        self.shell("am force-stop %s" % PACKAGE)
