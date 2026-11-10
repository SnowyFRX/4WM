"""九万亩 · 常驻控制台（轻量启动器）。

设计目标（用户要求）：
  - 只跑一个极轻的网页进程，占十几 MB、CPU 几乎为 0；
  - 网页上点「启动脚本」才真正拉起 run.py，点「停止脚本」就结束它；
  - 脚本不跑的时候，后台只剩这个启动器，不占模拟器、不截图、不 OCR。

用法：双击 控制台.bat（或 python console.py），然后浏览器打开固定地址。
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8765
PREFIX = "/jwm/"
PAGE = HERE / "jwm" / "console.html"
LOG = HERE / "logs" / "console.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("console")

_state = {"proc": None, "started": 0.0, "exit": None, "log_tail": [],
          "seen": 0.0}   # seen = 最近一次网页轮询的时间
_lock = threading.Lock()

# 网页关掉后多久自动退出控制台（脚本没在跑才退）。网页每 2 秒轮询一次状态。
IDLE_EXIT_SECONDS = 90


def _running():
    p = _state["proc"]
    return p is not None and p.poll() is None


def _tail_run_log(n=40):
    """读 bot.log 最后几行，显示在网页上。

    bot.log 是 UTF-8 写的（run.py 里 FileHandler 指定了 encoding="utf-8"），
    以前按 GBK 读会整段乱码，这里按 UTF-8 读、失败再退回 GBK。
    """
    p = HERE / "logs" / "bot.log"
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            raw = f.read()
    except OSError:
        return []
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _start():
    with _lock:
        if _running():
            return False, "脚本已经在运行"
        py = HERE / ".venv" / "Scripts" / "python.exe"
        if not py.exists():
            py = Path(sys.executable)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # 子进程自己起网页服务会抢 8765；这里让它用 8766，避免冲突
        env["DSH_JWM_NO_WEB"] = "1"
        logf = open(HERE / "logs" / "run_live.log", "ab")
        _state["proc"] = subprocess.Popen(
            [str(py), "run.py"], cwd=str(HERE), stdout=logf, stderr=subprocess.STDOUT,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _state["started"] = time.time()
        _state["exit"] = None
        log.info("已启动脚本 PID=%s", _state["proc"].pid)
        return True, "已启动"


def _stop():
    with _lock:
        p = _state["proc"]
        if not _running():
            return False, "脚本没在运行"
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        _state["exit"] = time.time()
        log.info("已停止脚本")
        return True, "已停止"


def _snapshot():
    running = _running()
    if not running and _state["proc"] is not None and _state["exit"] is None:
        _state["exit"] = time.time()
    return {
        "running": running,
        "pid": _state["proc"].pid if running else None,
        "started": _state["started"] if running else None,
        "uptime": int(time.time() - _state["started"]) if running else 0,
        "log": _tail_run_log(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, mime, body):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def _local(self):
        return self.headers.get("Host", "").startswith("127.0.0.1:")

    def do_GET(self):
        if not self._local():
            self._reply(404, "text/plain", b"")
            return
        if self.path in (PREFIX, PREFIX + "index.html"):
            try:
                self._reply(200, "text/html; charset=utf-8", PAGE.read_bytes())
            except OSError:
                self._reply(500, "text/plain; charset=utf-8", "缺少 console.html".encode())
        elif self.path == PREFIX + "state":
            _state["seen"] = time.time()
            self._reply(200, "application/json; charset=utf-8",
                        json.dumps(_snapshot(), ensure_ascii=False).encode())
        else:
            self._reply(404, "text/plain", b"")

    def do_POST(self):
        if not self._local():
            self._reply(404, "text/plain", b"")
            return
        if self.path == PREFIX + "start":
            ok, msg = _start()
        elif self.path == PREFIX + "stop":
            ok, msg = _stop()
        else:
            self._reply(404, "text/plain", b"")
            return
        self._reply(200, "application/json; charset=utf-8",
                    json.dumps({"ok": ok, "message": msg, ** _snapshot()}, ensure_ascii=False).encode())


def _open_browser(url):
    """用系统默认程序打开网址。

    webbrowser.open 在后台线程/无控制台环境下经常静默失败（就是你遇到的：
    只弹黑框、浏览器不出来）。这里改成直接调 Windows 的 start 命令，最可靠。
    """
    try:
        if os.name == "nt":
            os.startfile(url)          # 等价于双击一个 .url，最接近用户预期
            return True
    except OSError:
        pass
    try:
        import webbrowser
        return webbrowser.open(url, new=2)
    except Exception:
        return False


def _notify(url):
    """不自动开浏览器，只在右下角弹个气泡提示地址（避免 Edge 权限冲突弹窗）。

    自动开浏览器会踩到「Edge 以提升权限运行、普通权限无法唤起」的确认框，
    每次都要点一下很烦。这里改成：只提示地址，用户自己点书签。
    """
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass
    try:
        import subprocess
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            "$n.ShowBalloonTip(8000,'九万亩控制台','地址已就绪：%s','Info');"
            "Start-Sleep -Seconds 9;$n.Dispose()" % url
        )
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _write_shortcuts(url):
    """只更新地址文件，不再往桌面塞第二个图标。

    桌面上只保留「九万亩」一个图标（双击 = 起控制台 + 开网页）。
    之前还额外生成一个「九万亩控制台.url」，两个图标让人困惑，去掉。
    """
    try:
        (HERE / "data").mkdir(exist_ok=True)
        (HERE / "data" / "web_url.txt").write_text(url, encoding="utf-8")
        (HERE / "打开控制台.txt").write_text(
            "九万亩控制台地址\n"
            "================\n"
            + url + "\n\n"
            "说明：\n"
            "  1. 双击桌面「九万亩」图标即可：会先起控制台，再打开这个网页。\n"
            "  2. 控制台已经在跑时，再双击它只会打开网页，不会重复启动。\n"
            "  3. 端口被别的程序占用时脚本会自动换端口，这个文件里的地址会自动更新。\n"
            "     所以交接时：让对方看这个文件，不要硬记 8765。\n",
            encoding="utf-8")
    except OSError:
        pass
    # 清掉以前在桌面留下的多余图标（只删我们生成的那个网址快捷方式）
    for base in {os.environ.get("USERPROFILE", ""), str(Path.home())}:
        if not base:
            continue
        stale = Path(base) / "Desktop" / "九万亩控制台.url"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass


def _idle_watch():
    """网页关掉一段时间后自动退出，不占用户后台。

    规则：网页每 2 秒轮询一次 state；如果连续 IDLE_EXIT_SECONDS 秒没人来，
    并且脚本没在跑，就退出控制台（脚本在跑时不退，免得把它丢成孤儿进程）。
    """
    while True:
        time.sleep(15)
        seen = _state.get("seen") or 0
        if not seen:
            continue
        if time.time() - seen < IDLE_EXIT_SECONDS:
            continue
        if _running():
            continue
        log.info("网页已关闭超过 %d 秒，控制台自动退出。", IDLE_EXIT_SECONDS)
        os._exit(0)


def main():
    (HERE / "logs").mkdir(exist_ok=True)
    server = None
    for port in (PORT, PORT + 1, PORT + 2, PORT + 3):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        log.error("端口都被占用，无法启动控制台")
        return
    url = "http://127.0.0.1:%d%s" % (server.server_port, PREFIX)
    log.info("控制台地址: %s", url)
    _write_shortcuts(url)
    _notify(url)
    _state["seen"] = time.time()
    threading.Thread(target=_idle_watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop()


if __name__ == "__main__":
    main()
