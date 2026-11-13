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


def _adb_env():
    """给 adb 一个可写的临时目录。

    真机踩到的坑（2026-11-11）：进程继承到的 TMP 指向
    `C:\\Users\\...\\DSH~1\\ascii\\tmp`，adb 连 adb.log 都写不进去，于是
    「cannot open ... adb.log: Permission denied / failed to start daemon」，
    截图全部超时。这里把 TMP/TEMP 固定到项目内 logs/adb_tmp（一定可写）。
    """
    try:
        from .vision import ROOT
        base = os.path.join(ROOT, "logs", "adb_tmp")
    except Exception:
        base = os.path.join(tempfile.gettempdir(), "jwm_adb_tmp")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    env = dict(os.environ)
    env["TMP"] = env["TEMP"] = base
    return env


class Shell:
    """常驻 adb shell，省掉每次启动 adb 进程的开销。

    为什么要专门一个读线程：

    以前的写法是「先 os.read 再检查超时」。os.read 在管道上没有数据时会**无限阻塞**，
    所以只要 adb 进程活着但一个字节都不吐（连着不响应、设备卡死、命令挂住），
    那个 15 秒超时根本轮不到执行 —— 脚本就永久停在那里，只能杀进程。
    真机上表现为「点了停止没反应 / 整晚不动」。

    现在读由后台线程负责，run() 只带着截止时间等条件变量：
    到点一定抛 TimeoutError，不必等 adb 先说话。

    每次命令用**唯一**结束标记（序号+随机数）。固定标记有个坑：
    上一条命令超时被放弃后，它的标记迟到落进管道，下一条命令会把它当成自己的结束，
    于是拿到上一条的输出。现在标记唯一，对不上就是不对上；超时后整个 shell 作废重建。
    """

    MARK_PREFIX = b"__JWM_END_"

    def __init__(self, adb_cmd, env=None, timeout=15):
        self.p = subprocess.Popen(adb_cmd + ["shell"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=0, env=env or _adb_env())
        self.lock = threading.Lock()
        self._cv = threading.Condition()
        self._buf = bytearray()
        self._eof = False
        self._dead = False
        self._seq = 0
        self._fd = self.p.stdout.fileno()
        self._reader = threading.Thread(target=self._pump, name="adb-shell-read", daemon=True)
        self._reader.start()
        try:
            self.run("true", timeout=timeout)   # 预热；连不上就让调用方看到明确异常
        except Exception:
            # 构造失败也要把进程和管道收干净：adb 每次抖动都会重建一次，
            # 漏掉的句柄会一直攒着。
            self.close()
            raise

    # ---------- 内部 ----------
    def _pump(self):
        """后台把管道里的字节搬进缓冲区，顺便唤醒等待者。"""
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._cv:
                self._buf += chunk
                self._cv.notify_all()
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def _fail(self, exc):
        """出错就把整个 shell 作废：绝不留下「半截管道」给下一条命令。"""
        self._dead = True
        try:
            self.p.kill()
        except Exception:
            pass
        with self._cv:
            self._cv.notify_all()
        raise exc

    @property
    def dead(self):
        return self._dead or self.p.poll() is not None

    # ---------- 对外 ----------
    def run(self, cmd, timeout=15):
        if self.dead:
            raise ConnectionError("adb shell 已失效，需要重连")
        with self.lock:
            self._seq += 1
            mark = self.MARK_PREFIX + ("%d_%08x__" % (self._seq, random.getrandbits(32))).encode()
            try:
                self.p.stdin.write(cmd.encode("utf-8") + b"; echo " + mark + b"\n")
                self.p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                self._fail(ConnectionError("adb shell 写入失败（%s）: %s" % (e, cmd)))

            deadline = time.monotonic() + timeout
            while True:
                with self._cv:
                    idx = self._buf.find(mark)
                    if idx >= 0:
                        out = bytes(self._buf[:idx])
                        # 标记之后如果还有字节，那是上一条命令的残留，不属于这条命令
                        self._buf.clear()
                        return out.decode("utf-8", "ignore")
                    if self._eof or self.p.poll() is not None:
                        self._fail(ConnectionError("adb shell 进程已退出: " + cmd))
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self._fail(TimeoutError("adb shell 无响应（%ss）: %s" % (timeout, cmd)))
                    # 有数据就立刻回到循环顶部取，没数据才等一小会
                    self._cv.wait(min(left, 0.02))

    def close(self):
        self._dead = True
        try:
            self.p.kill()
        except Exception:
            pass
        try:
            self.p.wait(timeout=1)   # 回收僵尸进程；不 wait 的话 Popen 析构会告警
        except Exception:
            pass
        with self._cv:
            self._cv.notify_all()
        try:
            self._reader.join(timeout=1)
        except Exception:
            pass
        # 先杀进程让读线程拿到 EOF，再关管道，避免读线程在已关闭的 fd 上打转
        for f in (self.p.stdin, self.p.stdout):
            try:
                if f is not None:
                    f.close()
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
        r = subprocess.run(self._cmd(args, with_serial), capture_output=True, timeout=timeout,
                           env=_adb_env())
        if check and r.returncode != 0:
            raise RuntimeError("adb 失败: %s\n%s" % (" ".join(args), r.stderr.decode("utf-8", "ignore")))
        return r.stdout

    # 这些命令会让模拟器「动」：点击、滑动、按键、启动/关闭应用。
    # 暂停期间必须拦住 —— 否则用户手动操作时脚本还在点，两边互相打断。
    # 截图/查询不在此列：只读，不干扰用户，网页还要靠它显示画面。
    _MOTION_PREFIXES = ("input ", "monkey ", "am force-stop")

    def shell(self, cmd, timeout=30):
        """执行一条 shell 命令。优先常驻 shell，坏了就重建，重建不了退回一次性调用。

        顺序是有讲究的：常驻 shell 失败 → 重建一次 → 还不行再退回每次起 adb 进程。
        每一步失败都要**先把旧 shell 作废**，不能拿半截管道继续写 —— 否则下一条命令
        可能读到上一条的残留输出。
        """
        if cmd.startswith(self._MOTION_PREFIXES):
            from . import pause
            if pause.paused():
                log.info("脚本已暂停：%s 这条命令先压着不发", cmd.split()[0])
                pause.wait()          # 停在这儿，恢复后继续
                log.info("脚本已恢复，继续执行")
        if self.sh is not None and getattr(self.sh, "dead", False):
            self.sh.close()
            self.sh = None
        if self.sh is not None:
            try:
                return self.sh.run(cmd, timeout=timeout)
            except Exception as e:
                log.warning("常驻 shell 出错（%s），重新连接", e)
                try:
                    self.sh.close()
                except Exception:
                    pass
                self.sh = None
            try:
                self.sh = Shell(self._cmd([]))
                return self.sh.run(cmd, timeout=timeout)
            except Exception as e:
                log.warning("重建常驻 shell 失败（%s），退回一次性调用", e)
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
