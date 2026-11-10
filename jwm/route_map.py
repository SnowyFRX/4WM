"""本机网页和开荒队列。只有脚本主线程操作模拟器。"""
import json
import logging
import secrets
import threading
import time
import webbrowser
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("route_map.html")
CACHE = Path(__file__).resolve().parent.parent / "data" / "map_cache.json"
CACHE_MAX_AGE = 7 * 24 * 3600  # 缓存最长留一周（领地会变，太旧就没意义）
MAX_FRAMES = 28
SCAN_RADIUS = 48
PRODUCTION = {1: 1, 2: 4, 3: 7, 4: 10, 5: 14}  # 资源地每小时产量；荒地按 3（1粮+1木+1石）


def adjacent(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def home_tiles(home):
    return {(home[0] + dx, home[1] + dy) for dx in (0, 1) for dy in (0, 1)} if home else set()


class RouteMap:
    def __init__(self, operations=None):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.scan = ("home", None)
        self.scanning = False
        self.warming = False
        self.retry_scan_at = 0
        self.retry_target = None
        self.map_error = ""
        self.home = None
        self.view = None
        self.frames = {}
        self.viewport = None
        self.frame = 0
        self.known = {}
        self.res_map = {}
        self.path = ()
        self.active = False
        self.index = 0
        self.revision = 0
        self.status = "请在地图上按顺序选格，然后开始开荒。"
        self.suggested = None
        self.runtime = dict(task="game", detail="等待连接游戏", mode="waiting", since=time.time())
        self.recent_event = None
        self.operations = operations

    def phase(self, detail, task=None, mode="running"):
        with self.lock:
            task = task or self.runtime["task"]
            if (task, detail, mode) == (self.runtime["task"], self.runtime["detail"], self.runtime["mode"]):
                return
            self.runtime = dict(task=task, detail=detail, mode=mode, since=time.time())
            if mode in ("waiting", "done", "error"):
                self.recent_event = self.runtime

    def snapshot(self):
        with self.lock:
            return dict(view=self.view, frames=[entry[0] for entry in self.frames.values()],
                        scanRadius=SCAN_RADIUS,
                        operations=self.operations.snapshot() if self.operations else None,
                        path=self.path, home=self.home, active=self.active,
                        index=self.index, revision=self.revision, status=self.status,
                        runtime=self.runtime, recentEvent=self.recent_event, suggestedPath=self.suggested,
                        scanning=bool(self.scan or self.scanning), warming=self.warming, mapError=self.map_error)

    def command(self, data):
        action = data.get("action")
        with self.lock:
            if action == "operations" and self.operations:
                self.operations.command(data)
            elif action == "scan":
                if self.scan or self.scanning:
                    raise ValueError("地图正在读取，请稍等。")
                point = data.get("center")
                if point is None:
                    self.scan = ("home", None)
                elif (self.home and isinstance(point, list) and len(point) == 2
                      and all(type(n) is int and n >= 0 for n in point)
                      and all(abs(n - h) <= SCAN_RADIUS for n, h in zip(point, self.home))):
                    self.viewport = self._viewport(data.get("viewport"))
                    self.scan = ("point", tuple(point))
                else:
                    raise ValueError("该位置超出主城周边的可读取范围。")
                self.map_error = ""
            elif action == "autoplan":
                # 只生成建议路线（前端填进草稿，用户检查后再提交），不碰当前 path/active/revision
                self.suggested = [list(p) for p in self._auto_plan(data.get("count", 30))]
                self.status = "已生成 %d 格建议路线，检查后点「开始开荒」。" % len(self.suggested)
            elif action in ("start", "pause", "resume"):
                if data.get("revision") != self.revision:
                    raise ValueError("路线已经更新，请同步后重试。")
                if action == "start":
                    self.path = self._validate(data.get("path"))
                    self.index = 0
                    self.active = True
                    self.status = "路线已提交，等待脚本执行。"
                elif action == "resume":
                    if self.index >= len(self.path):
                        raise ValueError("没有可继续的路线。")
                    self.active = True
                    self.status = "等待继续开荒。"
                else:
                    self.active = False
                    self.status = "已暂停开荒。"
                self.revision += 1
            else:
                raise ValueError("无效的操作。")
            self.wake.set()

    def _viewport(self, box):
        if box is None:
            return None
        if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
            raise ValueError("地图视野范围无效。")
        x1, y1, x2, y2 = box
        hx, hy = self.home
        if not (max(0, hx - SCAN_RADIUS) <= x1 <= x2 <= hx + SCAN_RADIUS
                and max(0, hy - SCAN_RADIUS) <= y1 <= y2 <= hy + SCAN_RADIUS):
            raise ValueError("地图视野超出可读取范围。")
        return tuple(box)

    def _validate(self, raw):
        if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
            raise ValueError("请选择 1 到 200 格。")
        path = []
        for pair in raw:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
                raise ValueError("路线中有无效坐标。")
            xy = tuple(pair)
            if self.known.get(xy) not in ("land", "owned_hint") or xy in path:
                raise ValueError("路线包含重复、未扫描或不可选的地块。")
            if path and not adjacent(path[-1], xy):
                raise ValueError("路线必须上下左右逐格相连。")
            path.append(xy)
        first = path[0]
        if (not any(adjacent(first, p) for p in home_tiles(self.home))
                and not any(self.known.get((first[0] + dx, first[1] + dy)) in ("owned", "owned_hint")
                            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))):
            raise ValueError("第一格必须紧挨已占领地。")
        return tuple(path)

    def _auto_plan(self, count=30):
        """在已扫描的地图上自动规划一条开荒路线（省去手动逐格点选）。

        策略 = 螺旋开路 + 贴领地贪心成链：
          - 圈数以主城(2x2)为中心的切比雪夫距离，奇数圈是「路径圈」（占它能把可打范围推最远）；
          - 每一步从与链尾四邻相接的可占地里挑：路径圈优先 → 圈数小优先 →
            前方开阔（未选邻居多，避免走进死胡同）→ 贴领地/已选格（占地后领地连成一片）。
        只生成满足 _validate 的合法链：逐格相邻、不重复、第一格贴已占领地。
        调用方需持有 self.lock。
        """
        if not self.home:
            raise ValueError("请先读取主城周边地图。")
        owned = {xy for xy, k in self.known.items() if k == "owned"} | home_tiles(self.home)
        remaining = {xy for xy, k in self.known.items() if k in ("land", "owned_hint")}
        hx, hy = self.home

        def ring(xy):
            dx = max(hx - xy[0], 0, xy[0] - (hx + 1))
            dy = max(hy - xy[1], 0, xy[1] - (hy + 1))
            return max(dx, dy)

        def neighbors(xy):
            x, y = xy
            return ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))

        count = max(1, min(int(count or 30), 200))

        def value(xy):
            res, level = self.res_map.get(xy, (None, 1))
            return PRODUCTION.get(level or 1, 1) if res else 3

        path, used, cur = [], set(owned), None
        while len(path) < count:
            if cur is None:
                pool = [xy for xy in remaining if any(adjacent(xy, p) for p in owned)]
            else:
                pool = [xy for xy in remaining if adjacent(xy, cur)]
            if not pool:
                break

            def score(xy):
                r = ring(xy)
                res = self.res_map.get(xy, (None, 1))[0]
                open_n = sum(1 for p in neighbors(xy) if p in remaining)  # 前方开阔度
                touch = sum(1 for p in neighbors(xy) if p in used)        # 贴领地/已选格
                # 资源地优先于荒地 → 路径圈优先 → 圈数小优先 → 价值高优先 → 开阔 → 贴领地
                return (0 if res else 1, r % 2 != 1, r, -value(xy), -open_n, -touch, xy)

            pool.sort(key=score)
            nxt = pool[0]
            path.append(nxt)
            remaining.discard(nxt)
            used.add(nxt)
            cur = nxt
        if not path:
            raise ValueError("已扫描地图里没有可开路的地块，请先读取主城周边或扩大视野。")
        return tuple(path)

    def avoid_and_replan(self, bad):
        """把打不过的格子从路线里剔掉，并从当前进度重排一条避开它的合法路线。

        返回 True = 已重排；False = 剔掉之后接不上（没有可绕行的相邻地），
        此时保持原路线不动，让调用方提示用户（不硬闯、不跳格）。
        """
        with self.lock:
            if not self.path:
                return False
            try:
                bad = tuple(int(v) for v in bad)
            except (TypeError, ValueError):
                return False
            head = self.path[:self.index]                  # 已处理的部分不动
            rest = [p for p in self.path[self.index:] if tuple(p) != bad]
            if not rest:
                # 卡的就是最后一格：剔掉即可，路线自然结束
                self.path = tuple(tuple(p) for p in head)
                self.status = "终点打不过，已移除该格。"
                self.revision += 1
                return True
            # 重排前先确认剩下的格子还能串成一条相邻链（剔除后会断开，必须重新串）
            cur = tuple(head[-1]) if head else None
            rest_t = [tuple(p) for p in rest]
            if cur is None:
                chained = self._chain_from(None, rest_t)
            elif not adjacent(cur, rest_t[0]):
                chained = self._chain_from(cur, rest_t)
            else:
                chained = rest_t
            if not chained:
                return False
            rest = [list(p) for p in chained]
            self.path = tuple(tuple(p) for p in head) + tuple(tuple(p) for p in rest)
            self.status = "已绕开 (%d,%d)，路线已重排。" % bad
            self.revision += 1
            return True

    def _chain_from(self, start, candidates):
        """从 start 出发，用 candidates 贪心串出最长的一条相邻链（相邻优先、近者优先）。

        start 为 None 时以第一个候选格为起点（head 为空 = 还没出兵过的情况）。
        """
        left = list(candidates)
        if not left:
            return []
        chain = []
        if start is None:
            cur = left.pop(0)
            chain.append(cur)
        else:
            cur = start
        while left:
            near = [p for p in left if adjacent(cur, p)]
            if not near:
                break
            nxt = min(near, key=lambda p: (abs(p[0] - cur[0]) + abs(p[1] - cur[1]), p))
            chain.append(nxt)
            left.remove(nxt)
            cur = nxt
        return chain

    def take_scan(self):
        with self.lock:
            if not self.scan and self.retry_scan_at and time.monotonic() >= self.retry_scan_at:
                self.scan = ("point", self.retry_target or self.view["center"]) if self.view else ("home", None)
            if not self.scan:
                return None
            request, self.scan = self.scan, None
            self.scanning = True
            return request[1], self.view is None

    def _cache_save(self):
        """把已扫到的地图存盘，下次启动同一个账号直接复用。

        必须连 frames（截图帧）一起存：网页的地图画布是用这些帧渲染的，
        只存格子类型的话，重启后状态显示“载入了 N 格”但画布是空的，等于没缓存。
        """
        if not self.home or not self.known:
            return
        try:
            import base64

            frames = []
            for number, (view, image) in self.frames.items():
                frames.append({"view": view, "img": base64.b64encode(image).decode("ascii")})
            data = {
                "home": list(self.home),
                "at": time.time(),
                "known": {"%d,%d" % k: v for k, v in self.known.items()},
                "res": {"%d,%d" % k: list(v) for k, v in self.res_map.items() if v and v[0]},
                "frames": frames,
                "frame": self.frame,
                "view": self.view,
            }
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE)
        except OSError:
            log.debug("地图缓存写入失败", exc_info=True)

    def _cache_load(self, home):
        """读回上次的地图（含截图帧）。主城对得上、数据不过期才用。返回载入的格子数。"""
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if list(data.get("home") or []) != list(home):
            return 0  # 换了账号/主城，缓存作废
        if time.time() - float(data.get("at") or 0) > CACHE_MAX_AGE:
            return 0
        known, res = {}, {}
        for k, v in (data.get("known") or {}).items():
            try:
                x, y = (int(n) for n in k.split(","))
            except ValueError:
                continue
            known[(x, y)] = v
        for k, v in (data.get("res") or {}).items():
            try:
                x, y = (int(n) for n in k.split(","))
            except ValueError:
                continue
            res[(x, y)] = (v[0], int(v[1]))
        if known:
            self.known.update(known)
            self.res_map.update(res)
        # 恢复截图帧：地图画布靠它渲染，没有它网页就是空白
        try:
            import base64

            restored = 0
            for item in (data.get("frames") or []):
                view = item.get("view") or {}
                raw = base64.b64decode(item.get("img") or "")
                if not view.get("tiles") or not raw:
                    continue
                number = int(view.get("frame") or 0)
                self.frames[number] = (view, raw)
                restored += 1
            if restored:
                self.frame = max(int(data.get("frame") or 0), max(self.frames))
                self.view = data.get("view") or self.frames[max(self.frames)][0]
                log.info("已恢复 %d 张地图截图（画布可直接显示）", restored)
        except Exception:
            log.debug("地图截图恢复失败", exc_info=True)
        return len(known)

    def identify(self, home):
        with self.lock:
            if self.home is not None and self.home != home:
                self.path = ()
                self.index = 0
                self.active = False
                self.revision += 1
                self.status = "主城坐标已变化，请重新读取地图并选点。"
                self.known.clear()
                self.res_map.clear()
                self.view = None
                self.frames.clear()
                self.viewport = None
                self.scan = ("home", None)
            fresh = self.home != home
            self.home = home
            if fresh and not self.known:
                # 会话里第一次认出主城：先把上次存的地图读回来，省掉重新扫描
                n = self._cache_load(home)
                if n:
                    log.info("已载入上次扫到的 %d 格地图（同一主城，无需重扫）", n)
                    self.status = "已载入上次的地图，可直接选点或补读视野。"
            if self.operations:
                self.operations.identify(home)

    def publish(self, home, center, tiles, image):
        self.identify(home)
        with self.lock:
            tiles = [{**t, "kind": "owned"} if t["kind"] == "owned_hint"
                     and self.known.get((t["x"], t["y"])) == "owned" else t for t in tiles]
            self.known.update({(t["x"], t["y"]): t["kind"] for t in tiles})
            self.res_map.update({(t["x"], t["y"]): (t.get("res"), t.get("level") or 1) for t in tiles})
            self.frame += 1
            self.view = dict(frame=self.frame, center=center)
            coords = {(t["x"], t["y"]) for t in tiles}
            for number, (view, _) in list(self.frames.items()):
                if view["center"] == center and all((t["x"], t["y"]) in coords for t in view["tiles"]):
                    del self.frames[number]
            self.frames[self.frame] = ({**self.view, "tiles": tiles}, image)
            if len(self.frames) > MAX_FRAMES:
                self._evict_frame()
            # 扫到新内容就存一次（帧多了会变大，所以只在帧数变化时写）
            if len(self.frames) != getattr(self, "_cache_frames", -1):
                self._cache_frames = len(self.frames)
                self._cache_save()

    def _evict_frame(self):
        if self.viewport is None:
            del self.frames[next(iter(self.frames))]
            return
        x1, y1, x2, y2 = self.viewport
        visible = {number: {(t["x"], t["y"]) for t in view["tiles"]
                            if x1 <= t["x"] <= x2 and y1 <= t["y"] <= y2}
                   for number, (view, _) in self.frames.items()}
        coverage = Counter(p for cells in visible.values() for p in cells)
        # 优先清掉不会让当前视野出现空洞的旧截图。
        oldest = min((number for number in visible if number != self.frame),
                     key=lambda number: sum(coverage[p] == 1 for p in visible[number]))
        del self.frames[oldest]

    def scan_finished(self):
        with self.lock:
            self.scanning = False
            self.warming = False
            self.map_error = ""
            self.retry_scan_at = 0
            self.retry_target = None

    def start_warmup(self):
        with self.lock:
            self.scanning = False
            self.warming = True

    def scan_requested(self):
        with self.lock:
            return self.scan is not None

    def scan_failed(self, reason, retry=False, target=None):
        with self.lock:
            self.scanning = False
            self.warming = False
            self.map_error = reason
            self.retry_scan_at = time.monotonic() + 20 if retry else 0
            self.retry_target = target if retry else None

    def next(self):
        with self.lock:
            if self.active and self.index < len(self.path):
                return self.revision, self.home, self.path[self.index]

    def confirmed_neighbor(self, point):
        x, y = point
        with self.lock:
            home = home_tiles(self.home)
            return any(p in home or self.known.get(p) == "owned"
                       for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))

    def report(self, revision, status, occupied=False, land=False):
        with self.lock:
            if self.revision != revision:
                return
            self.status = status
            if occupied or land:
                x, y = self.path[self.index]
                kind = "owned" if occupied else "land"
                self.known[(x, y)] = kind
                for view, _ in self.frames.values():
                    if any((t["x"], t["y"]) == (x, y) and t["kind"] != kind for t in view["tiles"]):
                        view["tiles"] = [{**t, "kind": kind} if (t["x"], t["y"]) == (x, y) else t
                                         for t in view["tiles"]]
            if occupied:
                self.index += 1
                if self.index == len(self.path):
                    self.active = False
                    self.status = "路线全部占领。"

    def confirm(self, revision, click):
        """暂停或改线生效后，旧路线不能再点击出兵确认。"""
        with self.lock:
            if self.active and self.revision == revision:
                return click()
            return None

    @contextmanager
    def serve(self, port=None, path="/jwm/", open_browser=True):
        """起网页服务。

        端口和路径都固定（默认 127.0.0.1:8765/jwm/），这样地址永远是同一个，
        可以存书签、做桌面快捷方式。端口被占用时自动退到下一个可用端口。
        """
        route = self
        prefix = path
        # 页面按修改时间热加载：改完 route_map.html 刷新浏览器就生效，不用重启脚本
        cache = {"mtime": 0.0, "bytes": b""}

        def _shutdown():
            """网页上点「停止脚本」时调这里：稍等一下再退出，保证响应先发出去。"""
            time.sleep(0.3)
            log.info("收到网页停止指令，脚本退出。")
            import os as _os
            _os._exit(0)

        def page_bytes():
            try:
                mtime = PAGE.stat().st_mtime
            except OSError:
                return cache["bytes"]
            if mtime != cache["mtime"] or not cache["bytes"]:
                cache["bytes"] = PAGE.read_bytes()
                cache["mtime"] = mtime
            return cache["bytes"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def reply(self, status, mime, payload, cache="no-store"):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", cache)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (ConnectionError, TimeoutError):
                    pass

            def reply_json(self, status, data):
                self.reply(status, "application/json; charset=utf-8",
                           json.dumps(data, ensure_ascii=False).encode("utf-8"))

            def local(self):
                return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

            def do_GET(self):
                if self.local() and self.path == prefix:
                    self.reply(200, "text/html; charset=utf-8", page_bytes())
                elif self.local() and self.path == prefix + "state":
                    self.reply_json(200, route.snapshot())
                elif self.local() and self.path.startswith(prefix + "map/"):
                    number = self.path[len(prefix + "map/"):]
                    with route.lock:
                        item = route.frames.get(int(number)) if len(number) < 12 and number.isascii() and number.isdigit() else None
                        image = item[1] if item else b""
                    if image:
                        self.reply(200, "image/jpeg", image, "private, max-age=86400, immutable")
                    else:
                        self.reply(404, "image/jpeg", b"")
                else:
                    self.reply(404, "text/plain", b"")

            def do_POST(self):
                if not self.local():
                    self.reply(404, "text/plain", b"")
                    return
                # 网页上的「停止脚本」按钮
                if self.path == prefix + "stop":
                    self.reply_json(200, {"stopping": True})
                    threading.Thread(target=_shutdown, daemon=True).start()
                    return
                if self.path != prefix + "command":
                    self.reply(404, "text/plain", b"")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Content-Type") != "application/json" or not 0 < length <= 8192:
                        raise ValueError("无效的请求。")
                    self.connection.settimeout(3)
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("无效的操作。")
                    route.command(data)
                except (ValueError, TimeoutError) as e:
                    self.reply_json(400, {"error": str(e)})
                    return
                except OSError:
                    log.exception("保存网页队列失败")
                    self.reply_json(500, {"error": "无法保存脚本队列，请检查数据目录后重试。"})
                    return
                self.reply_json(200, route.snapshot())

        # 优先固定端口，被占用就往后试几个，实在不行才用随机端口
        server = None
        want = int(port or 8765)
        for candidate in (want, want + 1, want + 2, want + 3, 0):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                break
            except OSError:
                continue
        if server is None:
            raise RuntimeError("网页服务启动失败：本机端口都被占用了。")

        with server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}{prefix}"
            log.info("网页开荒地图: %s", url)
            # 把地址写到文件，桌面快捷方式/浏览器书签都用它
            try:
                (Path(CACHE).parent / "web_url.txt").write_text(url, encoding="utf-8")
            except OSError:
                pass
            if open_browser:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join()
