"""本机网页和开荒队列。只有脚本主线程操作模拟器。"""
import json
import logging
import secrets
import threading
import time
import webbrowser
from collections import Counter, deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("route_map.html")
CACHE = Path(__file__).resolve().parent.parent / "data" / "map_cache.json"
ROUTE_FILE = Path(__file__).resolve().parent.parent / "data" / "route.json"
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
        self.pending_scans = deque()   # 框选刷新时排队的多个中心
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
        # 多条路线：每条 {key, path, index, active}。self.current 指向正在编辑/刚提交的那条。
        # 脚本按顺序轮转执行，遇到「当前格还在行军/等资源」的路线就跳去下一条，
        # 避免一条路线等行军时整台机器空转。
        self.routes = []
        self.current = ""
        self._step_key = ""
        self.revision = 0
        self.status = "请在地图上按顺序选格，然后开始开荒。"
        self.suggested = None
        self.pending_cache = False   # ask 模式下：有缓存可供用户选择沿用
        self.skips = []              # 最近被跳过的格子及原因（网页上如实展示）
        self.route_mode = "strict"   # strict=严格听用户的 / flexible=允许自动绕行
        self.route_key = ""          # 这条路线自己的身份（小队按它绑定，可同时存在多条路线）
        self.region = None           # 用户框选的待刷新区域 (x1,y1,x2,y2)
        self.runtime = dict(task="game", detail="等待连接游戏", mode="waiting", since=time.time())
        self.recent_event = None
        self.operations = operations

    # ---------- 路线存取（多条路线，但对老代码保持「当前那条」的读写语义） ----------
    def _route_of(self, key=None):
        key = self.current if key is None else key
        for r in self.routes:
            if r["key"] == key:
                return r
        return None

    def _ensure_route(self):
        """拿到「当前路线」；还没有就建一条占位的（key 在提交时再定）。"""
        r = self._route_of()
        if r is None:
            r = {"key": self.current or "", "path": (), "index": 0, "active": False}
            self.routes.append(r)
            self.current = r["key"]
        return r

    @property
    def path(self):
        r = self._route_of()
        return tuple(r["path"]) if r else ()

    @path.setter
    def path(self, value):
        self._ensure_route()["path"] = tuple(tuple(p) for p in value)

    @property
    def index(self):
        r = self._route_of()
        return int(r["index"]) if r else 0

    @index.setter
    def index(self, value):
        self._ensure_route()["index"] = int(value)

    @property
    def active(self):
        r = self._route_of()
        return bool(r["active"]) if r else False

    @active.setter
    def active(self, value):
        self._ensure_route()["active"] = bool(value)

    @property
    def route_key(self):
        return self.current

    @route_key.setter
    def route_key(self, value):
        r = self._ensure_route()
        old = r["key"]
        r["key"] = str(value or "")
        self.current = r["key"]
        if old != r["key"]:
            for other in self.routes:
                if other is not r and other["key"] == r["key"]:
                    other["key"] = old or other["key"] + "'"   # 极少见的撞车，避免出现两条同名

    def route_summary(self):
        """给网页的路线列表（不含具体格子，省流量）。"""
        with self.lock:
            out = []
            for r in self.routes:
                path = r["path"]
                out.append(dict(key=r["key"], count=len(path), index=int(r["index"]),
                                active=bool(r["active"]), current=(r["key"] == self.current),
                                head=list(path[r["index"]]) if int(r["index"]) < len(path) else None))
            return out

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
                        pendingCache=self.pending_cache, skips=[dict(s) for s in self.skips],
                        routeMode=self.route_mode, routeKey=self.route_key,
                        routes=[dict(key=r["key"], count=len(r["path"]), index=int(r["index"]),
                                     active=bool(r["active"]), current=(r["key"] == self.current),
                                     head=(list(r["path"][r["index"]]) if int(r["index"]) < len(r["path"]) else None))
                                for r in self.routes],
                        region=list(self.region) if self.region else None,
                        scanning=bool(self.scan or self.scanning), warming=self.warming, mapError=self.map_error)

    def command(self, data):
        action = data.get("action")
        with self.lock:
            if action == "operations" and self.operations:
                self.operations.command(data)
            elif action == "scan":
                point = data.get("center")
                target = None
                if point is not None:
                    if not (self.home and isinstance(point, list) and len(point) == 2
                            and all(type(n) is int and n >= 0 for n in point)
                            and all(abs(n - h) <= SCAN_RADIUS for n, h in zip(point, self.home))):
                        raise ValueError("该位置超出主城周边的可读取范围。")
                    target = tuple(point)
                # 上一张还没读完时，把这次当成「下一个」记下来，读完自动跟过去。
                # 以前这里直接抛「地图正在读取，请稍等」——用户点了没反应、只能反复点，
                # 而且一旦扫描卡住就永远等不到。现在点了就算排队，一定会有反馈。
                if self.scan or self.scanning:
                    self.pending_scans = deque([target]) if target else deque()
                    self.status = ("已排队：这一轮读完后镜头会自动移到 %s。" % (target,)
                                   if target else "已排队：这一轮读完后重读主城周边。")
                else:
                    self.viewport = self._viewport(data.get("viewport"))
                    self.scan = ("point", target) if target else ("home", None)
                self.map_error = ""
            elif action == "scan_region":
                # 用户在地图上框一块区域 → 只把这几个视野重扫一遍（别的格子不动）
                self.scan_region(data.get("region"))
                self.map_error = ""
            elif action == "use_cache":
                # 用户选择沿用上次缓存
                n = self._cache_load(self.home) if self.home else 0
                self.pending_cache = False
                self.status = ("已沿用上次地图（%d 格）。" % n) if n else "没有可用缓存。"
            elif action == "fresh_scan":
                # 用户选择重新读取：清掉内存里的地图，让脚本重扫
                self.known.clear()
                self.res_map.clear()
                self.frames.clear()
                self.view = None
                self.pending_cache = False
                self.scan = ("home", None)
                self.status = "正在重新读取地图…"
            elif action == "route_mode":
                # 网页上切换「严格听你的 / 允许自动绕行」
                mode = str(data.get("mode") or "").lower()
                if mode not in ("strict", "flexible"):
                    raise ValueError("模式只能是 strict 或 flexible。")
                self.route_mode = mode
                # expand.cfg 就是 bot.cfg["expand"] 的引用，改这里立刻生效
                try:
                    exp = getattr(self, "expand", None)
                    target = getattr(exp, "cfg", None) if exp is not None else None
                    if isinstance(target, dict):
                        target["route_mode"] = mode
                except Exception:
                    log.debug("同步 route_mode 失败", exc_info=True)
                self.status = ("已切换为严格模式：脚本不会改动你的路线。"
                               if mode == "strict" else
                               "已切换为绕行模式：打不动的地会自动跳开并告诉你。")
            elif action == "autoplan":
                # 只生成建议路线，绝不自动生效：前端把它填进草稿，用户确认后才提交
                self.suggested = [list(p) for p in self._auto_plan(data.get("count", 30))]
                self.status = ("已生成 %d 格建议（仅建议，不会自己生效）。"
                               "点「用这条建议」才会填进你的路线。" % len(self.suggested))
            elif action in ("start", "pause", "resume", "select_route", "remove_route"):
                if data.get("revision") != self.revision:
                    raise ValueError("路线已经更新，请同步后重试。")
                if action == "select_route":
                    key = str(data.get("key") or "")
                    if self._route_of(key) is None:
                        raise ValueError("没有这条路线。")
                    self.current = key
                    self.status = "已切到这条路线（%d 格）。" % len(self.path)
                elif action == "remove_route":
                    key = str(data.get("key") or self.current)
                    r = self._route_of(key)
                    if r is None:
                        raise ValueError("没有这条路线。")
                    self.routes.remove(r)
                    if self.current == key:
                        self.current = self.routes[0]["key"] if self.routes else ""
                    self.status = "已删除这条路线。"
                elif action == "start":
                    path = self._validate(data.get("path"))
                    first = path[0]
                    key = "%d,%d>%d,%d#%d" % (
                        (self.home or (0, 0))[0], (self.home or (0, 0))[1],
                        first[0], first[1], len(path))
                    mode = str(data.get("mode") or "replace")
                    existing = self._route_of(key)
                    if mode == "add" or (existing is None and self._route_of() is not None
                                         and self.path):
                        # 追加一条新路线（「再加一条」），不动其他路线
                        r = {"key": key, "path": path, "index": 0, "active": True}
                        if existing is None:
                            self.routes.append(r)
                        else:
                            existing.update(path=path, index=0, active=True)
                        self.current = key
                    elif existing is not None:
                        existing.update(path=path, index=0, active=True)
                        self.current = key
                    else:
                        # 编辑的是空路线（第一次提交），就地填内容
                        r = self._ensure_route()
                        r.update(key=key, path=path, index=0, active=True)
                        self.current = key
                    self.status = "路线已提交，等待脚本执行。"
                elif action == "resume":
                    r = self._route_of(str(data.get("key") or self.current))
                    if r is None or int(r["index"]) >= len(r["path"]):
                        raise ValueError("没有可继续的路线。")
                    r["active"] = True
                    self.status = "等待继续开荒。"
                else:
                    r = self._route_of(str(data.get("key") or self.current))
                    if r is None:
                        raise ValueError("没有这条路线。")
                    r["active"] = False
                    self.status = "已暂停这条路线。"
                self.revision += 1
                self._route_save()
            else:
                raise ValueError("无效的操作。")
            self.wake.set()

    def scan_region(self, box):
        """把用户框选的一片区域排进扫描队列（只刷这块，不动别的地块）。

        视野一次只放得下约 11x13 格，所以按 6 格间距把区域拆成几个中心点，
        脚本会一口气扫完；已确认占领、水域这类不需要刷的格子会在网页上继续补。
        """
        self.viewport = self._viewport(box)
        x1, y1, x2, y2 = self.viewport
        self.region = self.viewport
        hx, hy = self.home
        step = 6
        centers = []
        y = y2
        while y >= y1:
            x = x1
            while x <= x2:
                if abs(x - hx) <= SCAN_RADIUS and abs(y - hy) <= SCAN_RADIUS:
                    centers.append((x, y))
                x += step
            y -= step
        if not centers:
            raise ValueError("这块区域里没有可读取的位置。")
        # 注意：command() 已经持有 self.lock（这里不能再次加锁，threading.Lock 不可重入）
        self.pending_scans = deque(centers[1:])
        self.scan = ("point", centers[0])
        self.status = "正在刷新你框选的区域（%d 个视野）。" % len(centers)

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
            route = self._route_of(self._step_key) or self._route_of()
            if route is None or not route["path"]:
                return False
            try:
                bad = tuple(int(v) for v in bad)
            except (TypeError, ValueError):
                return False
            path = tuple(route["path"])
            index = int(route["index"])
            head = path[:index]                  # 已处理的部分不动
            rest = [p for p in path[index:] if tuple(p) != bad]
            if not rest:
                # 卡的就是最后一格：剔掉即可，路线自然结束
                route["path"] = tuple(tuple(p) for p in head)
                route["index"] = len(route["path"])
                route["active"] = False
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
            route["path"] = tuple(tuple(p) for p in head) + tuple(tuple(p) for p in chained)
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

    def note_skip(self, xy, reason, auto=False):
        """记下「这一格为什么没打」，网页上如实列出来。

        用户明确要过：脚本可以跳过，但必须让他看见跳了哪格、为什么。
        auto=True 表示是脚本自己决定跳的（不是游戏规则拦的）。
        """
        with self.lock:
            entry = {"xy": [int(xy[0]), int(xy[1])], "reason": str(reason),
                     "auto": bool(auto), "at": time.time()}
            self.skips = [s for s in self.skips if s["xy"] != entry["xy"]]
            self.skips.insert(0, entry)
            del self.skips[40:]

    def clear_skips(self):
        with self.lock:
            self.skips = []

    def take_scan_peek(self):
        """只看一眼有没有排队的地图请求，不消费。"""
        with self.lock:
            if self.scan:
                return self.scan
            if self.pending_scans:
                return ("point", self.pending_scans[0])
            if self.retry_scan_at and time.monotonic() >= self.retry_scan_at and self.view:
                return ("point", self.retry_target or self.view["center"])
            return None

    def take_scan(self):
        with self.lock:
            if not self.scan and self.pending_scans:
                self.scan = ("point", self.pending_scans.popleft())
            if not self.scan and self.retry_scan_at and time.monotonic() >= self.retry_scan_at:
                self.scan = ("point", self.retry_target or self.view["center"]) if self.view else ("home", None)
            if not self.scan:
                return None
            request, self.scan = self.scan, None
            self.scanning = True
            return request[1], self.view is None

    def _route_save(self):
        """把所有路线存盘：脚本重启后你的路线不该消失。"""
        if not self.home or not self.routes:
            return
        try:
            ROUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = ROUTE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "home": list(self.home),
                "routes": [{"key": r["key"],
                            "path": [list(p) for p in r["path"]],
                            "index": int(r["index"]),
                            "active": bool(r["active"])} for r in self.routes],
                "current": self.current,
                "at": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(ROUTE_FILE)
        except OSError:
            log.debug("路线存盘失败", exc_info=True)

    def _route_load(self, home):
        """读回上次提交的路线（同一个主城才用）。返回是否恢复成功。

        兼容旧格式（单条路线存在 path/index/active/route_key 里）。
        """
        try:
            data = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if list(data.get("home") or []) != list(home):
            return False

        def clean(path):
            out = []
            for pair in path or []:
                if isinstance(pair, list) and len(pair) == 2 and all(type(n) is int for n in pair):
                    out.append(tuple(pair))
            return tuple(out)

        routes = []
        for item in data.get("routes") or []:
            path = clean(item.get("path"))
            if path:
                routes.append({"key": str(item.get("key") or ""), "path": path,
                               "index": max(0, min(int(item.get("index") or 0), len(path))),
                               "active": bool(item.get("active"))})
        if not routes:
            # 旧格式：单条路线
            path = clean(data.get("path"))
            if path:
                routes = [{"key": str(data.get("route_key") or ""), "path": path,
                           "index": max(0, min(int(data.get("index") or 0), len(path))),
                           "active": bool(data.get("active"))}]
        if not routes:
            return False
        # 注意：identify() 已经持有 self.lock（threading.Lock 不可重入），这里不能再加锁
        self.routes = routes
        want = str(data.get("current") or "")
        self.current = want if any(r["key"] == want for r in routes) else routes[0]["key"]
        self.revision += 1
        cur = self._route_of()
        self.status = ("已恢复 %d 条路线（当前这条 %d 格，进行到第 %d 格）。"
                       % (len(routes), len(cur["path"]), int(cur["index"]) + 1))
        log.info("已恢复上次提交的路线：%d 条，当前这条 %d 格，继续第 %d 格",
                 len(routes), len(cur["path"]), int(cur["index"]) + 1)
        return True

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
                self.routes = []       # 换主城：所有路线都作废
                self.current = ""
                self._step_key = ""
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
                # 会话里第一次认出主城：按设置的缓存策略决定要不要沿用上次地图
                mode = self._cache_mode()
                if mode == "refresh":
                    log.info("按设置不使用地图缓存，将重新读取。")
                    self.status = "正在重新读取地图…"
                else:
                    n = self._cache_load(home)
                    if n:
                        log.info("已载入上次扫到的 %d 格地图（同一主城，无需重扫）", n)
                        self.status = "已沿用上次的地图，可直接选点。"
                    elif mode == "ask":
                        self.pending_cache = True
            if fresh and not self.path:
                # 路线也要跟着回来：脚本重启不该让用户重新点一遍
                self._route_load(home)
            if self.operations:
                self.operations.identify(home)

    def _cache_mode(self):
        """从配置读缓存策略：reuse(默认沿用) / refresh(每次重读) / ask(启动时问)。

        ask 模式下不自动载入，网页上会问「沿用上次缓存还是重新读取」。
        """
        try:
            import yaml
            from .vision import ROOT as _ROOT
            cfg = yaml.safe_load(open(Path(_ROOT) / "config.yaml", encoding="utf-8")) or {}
            mode = ((cfg.get("web") or {}).get("map_cache_mode") or "reuse").lower()
            return mode if mode in ("reuse", "refresh", "ask") else "reuse"
        except Exception:
            return "reuse"

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
            return self.scan is not None or bool(self.pending_scans)

    def scan_failed(self, reason, retry=False, target=None):
        with self.lock:
            self.scanning = False
            self.warming = False
            self.map_error = reason
            self.retry_scan_at = time.monotonic() + 20 if retry else 0
            self.retry_target = target if retry else None

    def next(self):
        """取下一条要处理的地块：在多条路线间轮转，跳过正在等待的路线。

        「等待」的判据来自 state 里那一格的行军截止时间：
        一条路线刚派兵出去，它下一格要等行军，这个空档正好用来推进别的路线。
        全都不可推进时返回 None，脚本这一轮就不出兵（不空点）。
        """
        with self.lock:
            if not self.home:
                return None
            state = None
            try:
                exp = getattr(self, "expand", None)
                state = getattr(exp, "state", None)
            except Exception:
                state = None
            now = time.time()
            for r in self.routes:
                if not r["active"]:
                    continue
                idx = int(r["index"])
                path = r["path"]
                if idx >= len(path):
                    continue
                xy = tuple(path[idx])
                if state is not None:
                    rec = state.tile(*xy) or {}
                    if rec.get("route_home") == list(self.home):
                        if float(rec.get("march_until") or 0) > now:
                            continue      # 这一格还在行军，先推进别的路线
                        if float(rec.get("retry_at") or 0) > now:
                            continue      # 刚失败过、在退避
                self._step_key = r["key"]
                return self.revision, self.home, xy
            return None

    def step_route_key(self):
        return self._step_key

    def step_still_valid(self, revision):
        """刚才那一步还是不是它自己那条路线的当前格？

        多条路线轮转时不能用「next() 结果是否相同」来判断（next 可能已经转到
        别的路线上了），只核对：版本没变、这条路线还在跑、且队首还是刚才那格。
        """
        with self.lock:
            if self.revision != revision:
                return False
            r = self._route_of(self._step_key)
            if r is None or not r["active"]:
                return False
            idx = int(r["index"])
            return idx < len(r["path"])

    def confirmed_neighbor(self, point):
        x, y = point
        with self.lock:
            home = home_tiles(self.home)
            return any(p in home or self.known.get(p) == "owned"
                       for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))

    def advance(self, revision):
        """跳过当前这一格，往后挪一个（只在明确不该打时用）。"""
        with self.lock:
            if self.revision != revision:
                return
            r = self._route_of(self._step_key) or self._route_of()
            if r is None:
                return
            r["index"] = int(r["index"]) + 1
            if int(r["index"]) >= len(r["path"]):
                r["active"] = False
                self.status = "这条路线已走完。"
            self._route_save()

    def report(self, revision, status, occupied=False, land=False):
        with self.lock:
            if self.revision != revision:
                return
            self.status = status
            r = self._route_of(self._step_key) or self._route_of()
            if r is None:
                return
            idx = int(r["index"])
            if idx >= len(r["path"]):
                return
            if occupied or land:
                x, y = r["path"][idx]
                kind = "owned" if occupied else "land"
                self.known[(x, y)] = kind
                for view, _ in self.frames.values():
                    if any((t["x"], t["y"]) == (x, y) and t["kind"] != kind for t in view["tiles"]):
                        view["tiles"] = [{**t, "kind": kind} if (t["x"], t["y"]) == (x, y) else t
                                         for t in view["tiles"]]
            if occupied:
                r["index"] = idx + 1
                if int(r["index"]) >= len(r["path"]):
                    r["active"] = False
                    self.status = "这条路线全部占领。"
                self._route_save()   # 进度要跟着存，重启后别重打已占领的格

    def confirm(self, revision, click):
        """暂停或改线生效后，旧路线不能再点击出兵确认。"""
        with self.lock:
            r = self._route_of(self._step_key) or self._route_of()
            if r is not None and r["active"] and self.revision == revision:
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
                # 浏览器会用 ?v=4 这种版本号绕缓存；路由只认路径，不能把查询串算进去
                path = self.path.split("?", 1)[0]
                if self.local() and path == prefix:
                    self.reply(200, "text/html; charset=utf-8", page_bytes())
                elif self.local() and path == prefix + "state":
                    self.reply_json(200, route.snapshot())
                elif self.local() and path.startswith(prefix + "map/"):
                    number = path[len(prefix + "map/"):]
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
                path = self.path.split("?", 1)[0]
                # 网页上的「停止脚本」按钮
                if path == prefix + "stop":
                    self.reply_json(200, {"stopping": True})
                    threading.Thread(target=_shutdown, daemon=True).start()
                    return
                if path != prefix + "command":
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
            # 由控制台拉起时（open_browser=False），这个页面只是内部子服务，
            # 绝不能把 web_url.txt 改成 8766 —— 否则用户下次照着文件打开的是
            # 脚本子页面，脚本一停就彻底打不开，也看不到控制台的启动按钮。
            # 对外唯一地址始终由 console.py 写成 8765。
            try:
                data_dir = Path(CACHE).parent
                name = "web_url.txt" if open_browser else "web_child_url.txt"
                (data_dir / name).write_text(url, encoding="utf-8")
            except OSError:
                pass
            if open_browser:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join()
