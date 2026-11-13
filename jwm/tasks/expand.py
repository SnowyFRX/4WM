"""开荒：自动攻占领地周边的地块。

流程（每轮）：
  1. 回到大地图并对准主城，解析画面得到“可攻占边界”。
  2. 给每个候选格打分（资源类型权重 × 产量 + 积分 + 前方潜力），按距离打折后排序。
  3. 依次点开候选格，OCR 读取名称/坐标/难度；不可攻占或近期判定打不动的跳过。
  4. 点“攻占”，按行军时间从近到远逐个勾选空闲编队，直到战斗预测达到可接受档位
     （默认只接受“兵不血刃”），确认出兵；全部勾上仍达不到就取消并记为“暂时打不动”。
"""
import logging
import re
import time
from dataclasses import dataclass

from .. import mapview as mv
from .. import vision as V
from ..ui import UI

log = logging.getLogger(__name__)

LOSS_LABELS = ["兵不血刃", "轻微损失", "损兵折将", "损失过半", "全军覆没", "胜负难料"]
SAFE_LABEL = "兵不血刃"  # 唯一允许自动出兵的预测结果
DIFF_TIERS = ["简单", "入门", "普通", "进阶", "困难", "地狱"]
PRODUCTION = {1: 1, 2: 4, 3: 7, 4: 10, 5: 14}  # 每小时产量
POINTS = {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}  # 领土积分
RES_CN = {"粮食": "grain", "木头": "tree", "石头": "stone"}

R_FORECAST = (520, 930, 720, 1015)  # 预测标签所在区域
R_SELECTED = (250, 960, 470, 1020)  # “已选：k/5”


@dataclass
class TileDetail:
    name: str = ""
    x: int = None
    y: int = None
    tier: int = None  # 0..5 对应 简单..地狱
    tier_lv: int = None
    res: str = None
    level: int = 1
    attackable: bool = False
    has_attack: bool = False
    center: tuple = None  # 弹窗出现后（镜头可能平移）被选格子的屏幕中心
    span: int = 0  # 选中框宽度（1 格约 75px，主城 2x2 约 140px）


def parse_popup(sc):
    d = TileDetail()
    for b in sc.boxes:
        t = b.text.replace(" ", "")
        m = re.fullmatch(r"\(?(\d+),(\d+)\)?", t)
        if m and d.x is None:
            d.x, d.y = int(m.group(1)), int(m.group(2))
            continue
        m = re.fullmatch(r"(荒地|([2-5])级(粮食|木头|石头)地)", t)
        if m and not d.name:
            d.name = t
            if m.group(2):
                d.level = int(m.group(2))
                d.res = RES_CN[m.group(3)]
            continue
        m = re.fullmatch(r"(简单|入门|普通|进阶|困难|地狱)(\d+)", t)
        if m and d.tier is None:
            d.tier = DIFF_TIERS.index(m.group(1))
            d.tier_lv = int(m.group(2))
    if not d.name:
        # 其他名字（玩家/山贼/山脉等）取弹窗标题
        top = [b for b in sc.boxes if re.fullmatch(r"\(\d+,\d+\)", b.text.replace(" ", ""))]
        if top:
            cy = top[0].box[1]
            above = [b for b in sc.boxes if 0 < cy - b.box[3] < 40]
            if above:
                d.name = above[0].text
    d.has_attack = sc.text(r"^攻占$") is not None
    d.attackable = d.has_attack and bool(re.match(r"荒地|[2-5]级", d.name or ""))
    brackets = UI.brackets(sc)
    if brackets:
        x1, y1, x2, y2 = brackets
        d.center = ((x1 + x2) // 2, (y1 + y2) // 2)
        d.span = x2 - x1
    return d


class Expand:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui
        self.state = bot.state
        self.cfg = bot.cfg.get("expand", {})
        self.anchor = None  # (屏幕中心x, 屏幕中心y, 游戏x, 游戏y)，当前镜头下有效
        self.season = None
        self.route = bot.route
        self.route_revision = None
        if self.route:
            from ..route_camera import RouteCamera

            self.route_camera = RouteCamera(self)

    # ---------------- 评分 ----------------
    def tile_value(self, res, level):
        w = self.cfg.get("weights", {})
        if res is None:  # 荒地：三种资源各 +1
            v = sum(w.get(k, 1.0) for k in ("grain", "tree", "stone")) * PRODUCTION[1]
        else:
            v = w.get(res, 1.0) * PRODUCTION[level]
        return v + self.cfg.get("points_weight", 0.5) * POINTS[level]

    def _classify_tile(self, grid, o):
        if o.res is None and o.score == 0.0:
            res, lv, sc = mv.bank().classify(grid.img, o.x, o.y, seasons=self.seasons())
            if sc >= 0.8:
                o.res, o.level, o.score = res, lv, sc
            else:
                o.score = sc or 0.001
        return o

    def ring(self, x, y):
        """到主城（2x2）的“圈数”：紧贴主城的一圈是 1。"""
        hx, hy = self.state.data["home"]
        dx = hx - x if x < hx else (x - hx - 1 if x > hx + 1 else 0)
        dy = hy - y if y < hy else (y - hy - 1 if y > hy + 1 else 0)
        return max(dx, dy)

    def rank_spiral(self, grid):
        """螺旋开地：奇数圈（1,3,5…）是路径圈要占满，偶数圈是间隔圈不占荒地。

        排序：
          1. 相邻的资源地（任意圈，都值得占），按价值/距离；
          2. 路径圈上的荒地：先内圈后外圈，优先接在已占的同圈格子后面（沿圈推进 = 螺旋）；
          3. 间隔圈上的荒地只当“桥”：能新连通下一圈格子/资源地的才考虑，其余永远不占。
        """
        gap = max(0, int(self.cfg.get("spiral_gap", 1)))
        period = gap + 1

        def is_path(r):
            return r >= 1 and (r - 1) % period == 0

        xy = {k: self.game_xy(t) for k, t in grid.tiles.items()}
        owned = {xy[k] for k, t in grid.tiles.items() if t.owned}
        for key, rec in self.state.data.get("tiles", {}).items():
            if rec.get("status") == "owned":
                owned.add(tuple(int(v) for v in key.split(",")))
        by_xy = {v: grid.tiles[k] for k, v in xy.items()}
        nb4 = ((1, 0), (-1, 0), (0, 1), (0, -1))

        def accessible(p, extra=None):
            return any((p[0] + a, p[1] + b) in owned or (p[0] + a, p[1] + b) == extra for a, b in nb4)

        dp = self.cfg.get("distance_penalty", 0.35)
        retry_h = self.cfg.get("retry_hours", 3)
        cat0, cat1, cat2 = [], [], []
        for t in grid.frontier():
            p = xy[(t.i, t.j)]
            r = self.ring(*p)
            if t.res:
                cat0.append((self.tile_value(t.res, t.level) / (1 + dp * r), t))
            elif is_path(r):
                contig = sum(1 for a, b in nb4 if (p[0] + a, p[1] + b) in owned and is_path(self.ring(p[0] + a, p[1] + b)))
                cat1.append(((r, -contig), t))
            else:
                # 间隔圈荒地：算它作为“桥”能新连通多少路径圈格子/资源地
                gain = 0.0
                for a, b in nb4:
                    q = (p[0] + a, p[1] + b)
                    if q in owned or accessible(q):
                        continue
                    o = by_xy.get(q)
                    if o is not None and (o.blocked and grid._is_water(o)):
                        continue
                    if o is not None:
                        self._classify_tile(grid, o)
                        if o.res:
                            gain += 1 + self.tile_value(o.res, o.level) / 10
                            continue
                    if is_path(self.ring(*q)) and self.ring(*q) > r:
                        gain += 1
                if gain > 0:
                    cat2.append(((r, -gain), t))
        cat0.sort(key=lambda x: -x[0])
        cat1.sort(key=lambda x: x[0])
        cat2.sort(key=lambda x: x[0])
        # 桥只在“没有可打的路径圈格子”时才用，而且一次只占一块（螺旋只需要一个入口）
        open_path = [t for _, t in cat1 if not self.state.tile_blocked(*xy[(t.i, t.j)], retry_h)]
        bridges = [] if open_path else [t for _, t in cat2]
        return [t for _, t in cat0] + [t for _, t in cat1] + bridges

    def rank(self, grid):
        if self.target_cells() and self.anchor:
            return self.rank_target(grid)
        if self.cfg.get("strategy", "spiral") == "spiral" and self.anchor and self.state.data.get("home"):
            return self.rank_spiral(grid)
        owned = grid.owned_tiles()
        if not owned:
            return []
        cx = sum(t.i for t in owned) / len(owned)
        cy = sum(t.j for t in owned) / len(owned)
        dist_penalty = self.cfg.get("distance_penalty", 0.35)
        lookahead = self.cfg.get("lookahead", 0.3)
        out = []
        for t in grid.frontier():
            v = self.tile_value(t.res, t.level)
            # 前方潜力：相邻未占格里最好的资源地
            best_next = 0
            for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                o = grid.tiles.get((t.i + di, t.j + dj))
                if o is None or o.owned or o.blocked:
                    continue
                self._classify_tile(grid, o)
                if o.res:
                    best_next = max(best_next, self.tile_value(o.res, o.level))
            d = abs(t.i - cx) + abs(t.j - cy)
            score = (v + lookahead * best_next) / (1 + dist_penalty * d)
            out.append((score, t))
        out.sort(key=lambda x: -x[0])
        return [t for _, t in out]

    def seasons(self):
        return [self.season] if self.season is not None else None

    def detect_season(self, grid):
        """用画面里识别得最好的资源图标判断当前季节皮肤，之后只用该季节模板（更快）。"""
        best = {}
        b = mv.bank()
        n = 0
        for t in grid.tiles.values():
            if t.blocked or t.owned:
                continue
            res, lv, sc = b.classify(grid.img, t.x, t.y, jitter=2)
            if sc >= 0.9:
                # 找出是哪个季节
                for s in range(4):
                    r2, l2, s2 = b.classify(grid.img, t.x, t.y, jitter=2, seasons=[s])
                    best[s] = best.get(s, 0) + s2
                n += 1
            if n >= 4:
                break
        if best:
            self.season = max(best, key=best.get)
            log.info("地图季节皮肤: land_%d", self.season)

    # ---------------- 坐标锚点 ----------------
    def anchor_from_city(self, grid):
        """主城坐标已知且主城在画面里时，直接用它校正锚点（不用点击）。"""
        home = self.state.data.get("home")
        ij = grid.find_city()
        if not home or ij is None:
            return False
        bl = grid.tiles.get((ij[0], ij[1] + 1))  # 2x2 主城的左下格 = 主城坐标
        if bl is None:
            return False
        new = (bl.center[0], bl.center[1], home[0], home[1])
        if self.anchor:
            ax, ay, gx, gy = self.anchor
            exp = (gx + round((new[0] - ax) / mv.TILE), gy - round((new[1] - ay) / mv.TILE))
            if exp != tuple(home):
                log.debug("锚点偏差 %s → 用主城位置校正", exp)
        self.anchor = new
        return True

    def center_city(self):
        """回主城并返回主城附近的地图网格。"""
        return self.home_view()[0]

    def home_view(self, sc=None, shortcut_center=False):
        """自动回城，返回网格和主城屏幕位置。网页校准可直接使用快捷键的落点。"""
        sc = sc if sc is not None else self.ui.to_map()
        blind = iter(((0, 230), (0, -440), (230, 210), (-440, 0)))
        for _ in range(5):
            if not self.ui.on_map(sc) or self.ui.has_tile_popup(sc):
                sc = self.ui.to_map()
            sc, returned = self.ui.use_home_shortcut(sc)
            if returned and shortcut_center:
                return None, (360, 640)
            if not self.ui.on_map(sc) or self.ui.has_tile_popup(sc):
                sc = self.ui.to_map()
            try:
                grid = mv.MapGrid(sc.img, classify=False)
            except ValueError:
                grid = None
            ij = grid.find_city() if grid is not None else None
            if ij is None:
                shift = next(blind, None)
                if shift is None:
                    return None, None
                self.anchor = None
                self.pan(*shift)
                sc = self.ui.grab()
                continue
            t = grid.tiles[ij]
            cx, cy = t.x + mv.TILE, t.y + mv.TILE  # 2x2 主城的中心
            if 200 < cx < 520 and 450 < cy < 850:
                return grid, (int(cx), int(cy))
            # 从屏幕中间起手拖（起点落在顶部横幅上会拖不动地图）
            self.anchor = None
            self.pan(360 - cx, 640 - cy)
            sc = self.ui.grab()
        return None, None

    def calibrate(self, grid):
        """点一下主城，用弹窗里的坐标建立“屏幕格 ↔ 游戏坐标”的对应。"""
        ij = grid.find_city()
        if ij is None:
            return False
        t = grid.tiles[ij]
        self.dev.tap(int(t.x + mv.TILE), int(t.y + mv.TILE), delay=0.8)
        sc = self.ui.grab_stable()
        ok = False
        if self.ui.has_tile_popup(sc):
            d = parse_popup(sc)
            if d.center and d.x is not None and d.span > 0 and not d.attackable:
                try:
                    mv.set_tile_size(d.span / 2)
                except ValueError:
                    log.info("主城选框尺寸异常，本次不校准坐标：%s", d.span)
                else:
                    # 主城坐标是左下那一格；格宽以这次选框实测为准。
                    cx, cy = d.center
                    self.anchor = (cx - mv.TILE / 2, cy + mv.TILE / 2, d.x, d.y)
                    self.state.data["home"] = [d.x, d.y]
                    for a in (0, 1):
                        for b in (0, 1):
                            self.state.data["tiles"]["%d,%d" % (d.x + a, d.y + b)] = {"name": "主城", "status": "owned"}
                    self.state.save()
                    ok = True
                    log.info("坐标校准完成：主城 (%d,%d)，格宽 %.1f", d.x, d.y, mv.TILE)
            self.ui.close()
        elif self.ui.classify(sc) != "map":
            self.ui.to_map()
        return ok

    def game_xy(self, t):
        if not self.anchor:
            return None
        ax, ay, gx, gy = self.anchor
        cx, cy = t.center
        return gx + round((cx - ax) / mv.TILE), gy - round((cy - ay) / mv.TILE)

    # ---------------- 主流程 ----------------
    # 视野内没地可打时，依次往北/东/南/西拖 4 格再找（手指拖动向量，像素）
    PANS = [(0, 270, "北"), (-270, 0, "东"), (0, -270, "南"), (270, 0, "西")]

    # ---------------- 目标模式 ----------------
    def target_cells(self):
        t = self.cfg.get("target")
        if not t:
            return []
        size = int(self.cfg.get("target_size", 1))
        return [(t[0] + a, t[1] + b) for a in range(size) for b in range(size)]

    def target_dist(self, p):
        return min(abs(p[0] - c[0]) + abs(p[1] - c[1]) for c in self.target_cells())

    def _owned_game(self, grid=None):
        """已占领地的游戏坐标集合（主城 2x2 + state 记录 + 当前画面识别出的领地）。"""
        owned = set()
        home = self.state.data.get("home")
        if home:
            hx, hy = home
            owned.update({(hx + a, hy + b) for a in (0, 1) for b in (0, 1)})
        for key, rec in self.state.data.get("tiles", {}).items():
            if rec.get("status") == "owned":
                try:
                    owned.add(tuple(int(v) for v in key.split(",")))
                except ValueError:
                    pass
        if grid is not None and self.anchor:
            for t in grid.tiles.values():
                if t.owned:
                    g = self.game_xy(t)
                    if g:
                        owned.add(g)
        return owned

    @staticmethod
    def _adjacent(x, y, owned):
        # 只算上下左右 4 邻接，不含对角线；必须一格一格推
        return any((x + a, y + b) in owned for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    def rank_target(self, grid):
        """往目标方向开路：离目标越近越先打（同距离时资源地优先）。"""
        owned = [self.game_xy(t) for t in grid.owned_tiles()]
        front = min((self.target_dist(p) for p in owned if p), default=None)
        out, extra = [], []
        for t in grid.frontier():
            p = self.game_xy(t)
            if p is None:
                continue
            dist = self.target_dist(p)
            if front is not None and dist > front:
                # 离目标更远的：荒地不要；附近（最多远 2 格）的资源地排在最后，编队有空时顺手打
                if t.res and dist <= front + 2:
                    extra.append(((dist, -self.tile_value(t.res, t.level)), t))
                continue
            out.append(((dist, -self.tile_value(t.res, t.level)), t))
        out.sort(key=lambda x: x[0])
        extra.sort(key=lambda x: x[0])
        return [t for _, t in out] + [t for _, t in extra]

    def pan(self, dx, dy):
        """按手指拖动向量平移镜头（像素），锚点跟着平移。"""
        dx = max(-450, min(450, int(dx)))
        dy = max(-450, min(450, int(dy)))
        if abs(dx) < 40 and abs(dy) < 40:
            return
        # 一次拖太远（~400px 以上）实测会少走一两格，拆成几段
        n = max(1, -(-max(abs(dx), abs(dy)) // 250))
        for k in range(n):
            sx, sy = dx * (k + 1) // n - dx * k // n, dy * (k + 1) // n - dy * k // n
            self.dev.swipe(360 - sx // 2, 640 - sy // 2, 360 + sx // 2, 640 + sy // 2, 800, delay=0.8)
        if self.anchor:
            ax, ay, gx, gy = self.anchor
            self.anchor = (ax + dx, ay + dy, gx, gy)

    def screen_of(self, p):
        ax, ay, gx, gy = self.anchor
        return ax + (p[0] - gx) * mv.TILE, ay - (p[1] - gy) * mv.TILE

    def face_target(self):
        """镜头移到“领地里离目标最近的格子”附近，让通往目标的边界在画面中间。"""
        try:
            grid = mv.MapGrid(self.dev.screenshot(), classify=False)
        except ValueError:
            return
        if not self.anchor_from_city(grid) and self.anchor is None:
            self.calibrate(grid)
            grid = mv.MapGrid(self.dev.screenshot(), classify=False)
            self.anchor_from_city(grid)
        if not self.anchor:
            return
        owned = [self.game_xy(t) for t in grid.owned_tiles()]
        owned += [tuple(int(v) for v in k.split(",")) for k, r in self.state.data["tiles"].items()
                  if r.get("status") == "owned" and r.get("name") != "主城"]
        if not owned:
            return
        front = min(owned, key=self.target_dist)
        tc = self.target_cells()[0]
        # 画面中心放在前沿和目标之间（偏前沿）
        cx = (2 * self.screen_of(front)[0] + self.screen_of(tc)[0]) / 3
        cy = (2 * self.screen_of(front)[1] + self.screen_of(tc)[1]) / 3
        for _ in range(3):
            if abs(cx - 360) < 60 and abs(cy - 640) < 60:
                break
            dx, dy = 360 - cx, 640 - cy
            self.pan(dx, dy)
            cx += max(-450, min(450, dx))
            cy += max(-450, min(450, dy))
        log.info("目标模式：前沿 %s → 目标 %s（距离 %d）", front, tuple(tc), self.target_dist(front))

    def run(self):
        """执行一轮开荒，返回出兵次数。"""
        if self.route:
            return self.run_route()
        self.anchor = None
        self.tried = set()
        self.need_heal = False
        self.ui.to_map()    # 先回到干净的大地图（关掉残留弹窗/面板，否则会挡住边界识别）
        self.center_city()  # 把主城拖到画面中央，开荒时地块位置更可预测
        if self.target_cells():
            self.face_target()
            dispatched, stop = self.explore(self.cfg.get("max_candidates_per_round", 8))
        else:
            dispatched, stop = self.explore(self.cfg.get("max_candidates_per_round", 8))
        if stop == "exhausted" and self.cfg.get("pan_search", True) and not self.target_cells():
            for dx, dy, name in self.PANS:
                log.info("视野内没有可打的地，往%s边看看", name)
                self.ui.to_home()
                self.anchor = None
                self.anchor_from_city(mv.MapGrid(self.dev.screenshot(), classify=False))
                self.dev.swipe(360 - dx // 2, 640 - dy // 2, 360 + dx // 2, 640 + dy // 2, 1200, delay=1.5)
                if self.anchor:
                    # 实测拖动距离 = 镜头移动距离，锚点跟着平移
                    ax, ay, gx, gy = self.anchor
                    self.anchor = (ax + dx, ay + dy, gx, gy)
                n, stop = self.explore(4)
                dispatched += n
                if stop in ("no_army", "bag_full"):
                    break
        self.ui.to_map()
        if stop == "no_army":
            log.info("没有空闲编队了")
        if self.need_heal:
            # 只有「打不出兵不血刃」才召回。以前这里还有一条
            # `dispatched == 0 or understrength -> recall()`：一轮没打成、或名单里有名字
            # 就把队伍全叫回去 —— 那正是「打一格就回城」的来源，删掉。
            self._recall_if_needed()
        return dispatched

    def route_pending(self):
        step = self.route.next() if self.route else None
        return step is not None and step[0] != self.route_revision

    def run_route(self):
        from ..route_camera import popup_owned, popup_position

        step = self.route.next()
        if step is None:
            return 0
        revision, home, xy = step
        self.route_revision = revision
        self.route.report(revision, f"正在检查 {xy}。")
        self.need_heal = False
        camera = self.route_camera
        try:
            grid = camera.capture(xy, expected_home=home)
        except ValueError as exc:
            self.route.report(revision, str(exc))
            return 0
        if grid is None:
            self.route.identify(camera.home)
            return 0
        visible = {camera.xy(t.center): t for t in grid.tiles.values() if not t.blocked}
        tile = visible.get(xy)
        if tile is None:
            self.route.report(revision, f"{xy} 被界面遮挡或超出视野，等待重新识别。")
            return 0
        if grid._is_water(tile):
            self.route.report(revision, f"{xy} 识别为水域，请调整路线。")
            return 0
        if not self.route.step_still_valid(revision):
            return 0
        # 严格模式：你自己排的路线就是命令。历史「打不动」的冷却、自动判定
        # 都不能让你想打的格被悄悄跳过 —— 只把情况报告给你，由你决定。
        strict = str(self.cfg.get("route_mode", "strict")).lower() == "strict"
        if not strict and self.state.tile_blocked(*xy, self.cfg.get("retry_hours", 3)):
            self.route.note_skip(xy, "近期判定打不动（冷却中）", auto=True)
            self.route.report(revision, f"{xy} 近期判定为打不动，先跳过（可在网页查看原因）。")
            self.route.advance(revision)
            return 0
        rec = self.state.tile(*xy) or {}
        if strict and rec.get("status") == "hard":
            self.route.report(revision, f"{xy} 之前标记过打不动，但严格模式下照你的路线继续试。")
        self.bot.phase(f"正在核对地块 {xy} 的坐标和归属")
        self.dev.tap(*tile.center, jitter=0, delay=1.5)
        try:
            sc = self.ui.grab_stable()
            d = parse_popup(sc)
            actual = popup_position(sc, d, land=True)
            if not 0 < d.span < 1.4 * mv.TILE or actual != xy:
                if not d.span:
                    message = f"{xy} 的地块选框未识别。"
                elif d.span >= 1.4 * mv.TILE:
                    message = f"{xy} 选中了多格建筑。"
                elif actual is None:
                    message = f"{xy} 的弹窗坐标未读清。"
                else:
                    message = f"目标是 {xy}，实际弹窗显示 {actual}。"
                log.debug("路线地块核对失败，目标=%s，选框宽度=%s，OCR 坐标=%s，弹窗核对=%s",
                          xy, d.span, (d.x, d.y), actual)
                # 选框括号偶尔认不出来（靠主城/被屋檐压住时实测会 span=0）。
                # 这不该让路线原地卡死：如果弹窗自己的坐标就是对上的，而且
                # 弹窗里有「修建 + 行军」，那就是自家的地 —— 直接记为已占领往前走。
                if (d.span == 0 and (d.x, d.y) == xy and popup_owned(sc, xy)):
                    log.info("%s 选框没认出来，但弹窗坐标对上且是自家领地，按已占领处理", xy)
                    self.route.report(revision, f"{xy} 已从游戏弹窗确认占领（选框未识别），下一轮继续。",
                                      occupied=True)
                    return 0
                self.route.report(revision, message + "未出兵。")
                return 0
            if not d.attackable:
                if not d.has_attack and popup_owned(sc, xy):
                    self.route.report(revision, f"{xy} 已从游戏弹窗确认占领，下一轮继续。", occupied=True)
                else:
                    self.route.report(revision, f"{xy} 暂时无法攻占，等待游戏更新。")
                return 0
            self.route.report(revision, f"游戏弹窗确认 {xy} 可攻占。", land=True)
            if not (self.route.confirmed_neighbor(xy)
                    or self._adjacent(*xy, {p for p, t in visible.items() if t.owned})):
                self.route.report(revision, f"{xy} 未确认与领地相邻，等待前沿更新。")
                return 0
            record = self.state.tile(*xy) or {}
            if record.get("route_home") == list(home):
                marching = record.get("march_until", 0) > time.time()
                # 严格模式只等「正在行军占领」，不再因为历史判定把你选的地判成等待
                blocked = (not strict) and self.state.tile_blocked(*xy, self.cfg.get("retry_hours", 3))
                if marching or blocked:
                    label = record.get("label") or "等待刷新"
                    self.route.report(revision, f"{xy} {'等待占领' if marching else '等待重试'}（{label}）。")
                    return 0
            d.x, d.y = xy

            def approve(click):
                return self.route.confirm(revision, click)

            result, label = self.attack_tile(sc, d, approve=approve, route_home=home)
        finally:
            self.ui.to_map(auto_login=False)
        if result == "ok":
            self.route.report(revision, f"已向 {xy} 出兵，等待占领。")
            return 1
        if result == "too_hard":
            # 打不出兵不血刃 = 兵不够/带伤。这里必须**真的**召回，不能再只写一句话
            # （以前的 bug：too_hard 分支直接 return，下面那段 recall 是死代码，
            #  而 _diagnose_stuck 却对玩家说「已召回补兵」）。
            recalled = self._recall_if_needed()
            self._diagnose_stuck(revision, home, xy, label, recalled=recalled)
            return 0
        if self.need_heal:
            # 召回只是把在外的低血编队叫回主城补兵，不改你的路线；
            # 但要在网页上说一句，别让你以为兵莫名其妙消失了。
            recalled = self._recall_if_needed()
            if recalled:
                self.route.report(revision, f"{xy} 兵不够，已召回 "
                                  + "、".join(n for n, _ in recalled) + " 回主城回血/补兵。")
        elif result == "no_army":
            # 没有可派编队 = 队伍都在外面打地/行军，或者都带伤不可派。
            # 这时候**绝不能召回**：把刚出征的队伍叫回来，等于每打一格就回城一趟。
            # 真正的召回理由是「打不出兵不血刃」（上面的 too_hard 分支）。
            busy = self._busy_armies()
            self.route.report(revision, f"{xy} 等编队空出来："
                              + ("、".join(busy) + " 正在战斗/行军" if busy else "它们还在外面")
                              + "，先不召回。")
        messages = {
            "no_army": "等待空闲编队",
            "bag_full": "背包已满，领取宝箱后重试",
            "no_points": "奖励点不足",
            "too_hard": label or "未读到兵不血刃",
            "no_confirm": "未识别到出兵确认按钮",
            "no_panel": "未识别到出兵面板",
        }
        self.route.report(revision, f"{xy} {messages.get(result, '本轮未出兵，等待重试')}。")
        return 0

    def _diagnose_stuck(self, revision, home, xy, label, recalled=()):
        """一格打不动时怎么办 —— 由用户选：严格听用户，还是允许脚本绕行。

        route_mode:
          strict    = 你选的格就是命令。打不动就停在原地、报给你，绝不改路线。
          flexible  = 打不动时脚本可以自动绕开并重排，但会在网页上写明跳过了哪格。

        recalled 是这一轮**真的**召回了哪些编队；空就别说「已召回」。
        """
        rec = self.state.tile(*xy) or {}
        fails = int(rec.get("stuck", 0)) + 1
        self.state.set_tile(*xy, stuck=fails, label=label or "", route_home=list(home))
        limit = int(self.cfg.get("stuck_limit", 3))
        mode = str(self.cfg.get("route_mode", "strict") or "strict").lower()
        done = ("已召回 " + "、".join(n for n, _ in recalled) + " 回主城回血/补兵。") if recalled else ""

        if mode == "strict":
            # 严格模式：只是重试，永远不动用户排的路线
            self.route.report(revision, f"{xy} 打不出兵不血刃（第 {fails} 次）。"
                                       + (done or "已在补兵名单里，等资源/兵力恢复再试。")
                                       + "（严格模式：脚本不会改动你的路线）")
            self.route.note_skip(xy, "打不出兵不血刃", auto=False)
            return

        if fails < limit:
            self.route.report(revision, f"{xy} 打不出兵不血刃（第 {fails}/{limit} 次），"
                                       + (done or "稍后重试。"))
            return
        # 达到阈值：标记为难打，并尝试在已扫描地图上重规划一条避开它的路线
        self.state.set_tile(*xy, status="hard", label=label or "打不过", route_home=list(home))
        log.info("%s 连续 %d 次打不出兵不血刃，标记难打", xy, fails)
        if self.route.avoid_and_replan(xy):
            self.route.note_skip(xy, "打不过，已自动绕开", auto=True)
            self.route.report(revision, f"{xy} 打不过，已自动绕开并重排路线。")
        else:
            self.route.note_skip(xy, "打不过，附近没有可绕行的地", auto=True)
            self.route.report(revision, f"{xy} 打不过，且没有可绕行的相邻地；"
                                       f"请调整路线或先练兵/补兵（已暂停在这一格）。")

    def explore(self, max_try):
        """在当前视野里依次尝试候选格。返回 (出兵次数, 结束原因)。"""
        retry_h = self.cfg.get("retry_hours", 3)
        if self.target_cells():
            retry_h = self.cfg.get("target_retry_minutes", 10) / 60.0
        dispatched = 0
        calibrated = False
        recenter = 0
        tried_screen = self.tried
        for _ in range(max_try):
            img = self.dev.screenshot()
            try:
                grid = mv.MapGrid(img, classify=False)
            except ValueError:
                if recenter < 2:
                    recenter += 1
                    log.info("找不到领地边界，把镜头拉回主城再试")
                    if self.center_city() is None:
                        return dispatched, "no_border"
                    continue
                log.info("画面里找不到自己领地的边界")
                return dispatched, "no_border"
            if self.season is None:
                self.detect_season(grid)
            if not self.anchor_from_city(grid) and self.anchor is None and not calibrated:
                calibrated = True
                if self.calibrate(grid):
                    continue  # 镜头可能因弹窗平移，重新截图
            grid._classify(self.seasons())
            owned_game = self._owned_game(grid)
            if self.bot.cfg.get("debug"):
                self._save_debug(grid)
            cands = []
            for t in self.rank(grid):
                xy = self.game_xy(t)
                if xy and (xy in tried_screen or self.state.tile_blocked(*xy, retry_h)):
                    continue
                if (t.center, self.anchor) in tried_screen:
                    continue
                cands.append(t)
            if not cands:
                return dispatched, "exhausted"
            t = cands[0]
            tried_screen.add((t.center, self.anchor))
            guess = self.game_xy(t)  # 点之前按锚点推算的坐标；弹窗读不出来时用它记录
            if guess:
                tried_screen.add(guess)
            log.info("尝试地块 屏幕%s %s 估计=%s%d", t.center, guess or "", t.res or "荒地", t.level)
            self.dev.tap(*t.center, delay=1.5)
            sc = self.ui.grab()
            if not self.ui.has_tile_popup(sc):
                log.info("没有弹出地块信息，跳过")
                if guess:
                    self.state.set_tile(*guess, status="noattack", name="")
                if self.ui.classify(sc) != "map":
                    self.ui.to_map()
                continue
            d = parse_popup(sc)
            if d.x is None and guess:
                log.info("  弹窗读不出坐标，按推算坐标 %s 记录，30 分钟内不再点", guess)
                self.state.set_tile(*guess, status="noattack", name=d.name or "")
                self.ui.close()
                continue
            if d.center and d.x is not None and 0 < d.span < 1.4 * mv.TILE:
                self.anchor = (d.center[0], d.center[1], d.x, d.y)
            if d.x is not None:
                tried_screen.add((d.x, d.y))
            log.info("地块 %s (%s,%s) 难度=%s%s", d.name, d.x, d.y,
                     DIFF_TIERS[d.tier] if d.tier is not None else "?", d.tier_lv or "")
            if d.x is not None and not self._adjacent(d.x, d.y, owned_game):
                log.info("  (%s,%s) 与已占领地不相邻（对角线不算），跳过", d.x, d.y)
                self.state.set_tile(d.x, d.y, name=d.name or "", status="noattack")
                self.ui.close()
                continue
            in_target = d.x is not None and ((d.x, d.y) in self.target_cells() or (guess and guess in self.target_cells()))
            name_ok = not d.name or any(n in d.name for n in self.cfg.get("attack_names", ["山贼营寨"]))
            if not d.attackable and d.has_attack and in_target and name_ok:
                # 按坐标认目标（弹窗标题有时读不出来）；名字读到了就再核对一次
                log.info("  到达目标 %s (%s,%s)，按兵不血刃规则尝试进攻", d.name or "目标", d.x, d.y)
                d.attackable = True
            if not d.attackable:
                is_land = bool(re.match(r"荒地|[2-5]级", d.name or ""))
                if is_land:
                    # 土地但没有“攻占”：可能是自己的地 / 正在交战 / OCR 漏读，短时间屏蔽
                    log.info("  没有“攻占”按钮，30 分钟后再看")
                    if self.bot.cfg.get("debug"):
                        self.bot.dump("noattack_%s_%s" % (d.x, d.y))
                elif d.name:
                    log.info("  %s 不可攻占", d.name)
                if d.x is not None:
                    self.state.set_tile(d.x, d.y, name=d.name, status="noattack" if is_land else "invalid")
                self.ui.close()
                continue
            if d.x is not None and self.state.tile_blocked(d.x, d.y, retry_h):
                log.info("  近期已尝试过（%s），跳过", (self.state.tile(d.x, d.y) or {}).get("status"))
                self.ui.close()
                continue
            # 先验过滤：这类地历史上几乎没打下来过就跳过，省掉“逐支编队勾选+等预测”那 30 秒。
            if self._should_skip_by_learn(d):
                self.ui.close()
                continue
            result, label = self.attack_tile(sc, d)
            if result == "ok":
                dispatched += 1
            elif result in ("no_army", "bag_full"):
                return dispatched, result
            self.ui.to_map()
        return dispatched, "max_try"

    def attack_tile(self, sc, d, approve=None, route_home=None):
        fields = {"route_home": list(route_home), "march_until": 0} if route_home else {}
        if not self._allowed_tier(d):
            log.info("难度超过设置上限，跳过")
            self._mark(d, "hard", label="难度上限", **fields)
            self.ui.close()
            return "too_hard", "难度上限"
        if not self.ui.tap_text(sc, r"^攻占$", delay=1.5) and route_home:
            return "no_panel", None
        learn_key = self.state.learn_key(d.res, d.level, d.tier)
        # 1) 用户选了一个开荒小队 → 只派这个小队里的编队，不再自己试
        squad = None
        try:
            if self.bot.operations:
                # 小队可以绑定某条路线：只让属于这条路线的小队上场，别的路线的小队不插手
                squad = self.bot.operations.squad_pick(
                    route_key=getattr(self.route, "route_key", "") if route_home else None)
        except Exception:
            log.debug("读取开荒小队失败", exc_info=True)
        if squad:
            self._combo_allow = squad.get("armies") or ()
            self._combo_prefer = self._combo_allow
            log.info("  使用开荒小队「%s」：%s", squad.get("name"), "、".join(self._combo_allow))
        else:
            # 2) 没有小队 → 用历史成功组合做优先顺位（仍然自动挑）
            self._combo_allow = ()
            try:
                combos = self.state.combo_get(learn_key)
                self._combo_prefer = combos[0]["armies"] if combos else ()
            except Exception:
                self._combo_prefer = ()
                log.debug("读取编队组合失败", exc_info=True)
        result, label = self.dispatch(approve, learn_key=learn_key)
        log.info("出兵结果: %s %s", result, label or "")
        # 战绩学习：这类地（资源+等级+难度）记一次出征结果。自动策略和 route 选格都走这里，
        # 成功（预测兵不血刃并派出）记 ok=True，打不动记 ok=False，供 _should_skip_by_learn 筛选。
        if result in ("ok", "too_hard") and (d.res or d.name):
            try:
                self.state.learn_add(self.state.learn_key(d.res, d.level, d.tier), result == "ok")
            except Exception:
                log.debug("战绩学习记录失败", exc_info=True)
        if result == "ok":
            if route_home:
                fields["march_until"] = time.time() + self.march_seconds + 60
            self._mark(d, "attacking", label=label, **fields)
            self.state.inc("expand_dispatched")
        elif result == "bag_full":
            self.bot.chests.run()
        elif result not in ("no_army", "paused") and (not route_home or result == "too_hard"):
            heal_thr = self.cfg.get("heal_before_retry", 0.95)
            # 打不出兵不血刃 = 兵力或血量不够。真正把它们排进补兵名单并请求召回，
            # 而不是只在网页上写一句「已召回补兵」（以前就是只写不做的）。
            if result == "too_hard" and self._need_supply(getattr(self, "tried_armies", ())):
                log.info("  兵力不足：已把参与的编队排进补兵名单，回城后自动补")
            elif result == "too_hard" and getattr(self, "tried_min_hp", 1.0) < heal_thr:
                log.info("  编队带伤（最低血量 %.0f%%），回城回血后再试", self.tried_min_hp * 100)
                self.need_heal = True
                self.state.set_tile(d.x, d.y, name=d.name, status="attacking", label=label, **fields)
            else:
                self._mark(d, "hard", label=label, **fields)
                if (d.x, d.y) in self.target_cells():
                    log.warning("目标 %s (%s,%s) 预测「%s」，不是兵不血刃，需要你亲自判断是否进攻",
                                d.name, d.x, d.y, label)
        return result, label

    BUSY_WORDS = r"战斗|行军|攻占|攻打|返回|治疗|休整"

    def _busy_armies(self):
        """正在战斗/行军/治疗中的编队名 —— 这些召不回来，别去空跑一趟。

        依据是军队面板最近一次读到的状态（bot.army_snapshot），读不到就返回空，
        让调用方按原逻辑走（宁可不判，也不能凭空拦住召回）。
        """
        try:
            cache = getattr(self.bot, "_armies_cache", None) or {}
        except Exception:
            return []
        out = []
        for name, info in cache.items():
            st = (info or {}).get("state") or ""
            if re.search(self.BUSY_WORDS, st):
                out.append(name)
        return out

    def _recall_if_needed(self, force=False):
        """真正执行召回。返回 [(名字, 行军秒数)]，空列表表示没有可召回的编队。

        以前这里没有单独的方法：`_need_supply` 只设了 need_heal 和一个假的冷却，
        真正调 recall() 的那段又是死代码，所以玩家看到「已召回补兵」但队伍根本没动。
        """
        thr = self.cfg.get("heal_before_retry", 0.95) if self.need_heal else self.cfg.get("recall_hp", 0.7)
        gap = float(self.cfg.get("recall_cooldown_seconds", 600))
        now = time.time()
        last = float(self.state.data.get("recall_at") or 0)
        if not force and now - last < gap:
            log.info("  距上次召回不到 %d 分钟，这次先不重复召回（补兵名单已记下）", gap // 60)
            self.need_heal = False
            return []
        try:
            recalled = self.recall(thr=thr)
        except Exception as exc:
            log.warning("  召回失败：%s", exc)
            recalled = []
        self.need_heal = False
        if recalled:
            # 冷却只在实际召回之后才记，否则会出现「从没召回却一直在冷却」的死循环
            self.state.data["recall_at"] = now
            self.state.save()
            log.info("  已召回 %d 支：%s（回主城回血/补兵）", len(recalled),
                     "、".join(n for n, _ in recalled))
        else:
            log.info("  没有可召回的编队（在外的队伍可能已经在回城路上，或都不在城外）")
        return recalled

    def _need_supply(self, names):
        """打不出兵不血刃：**只**表示这一轮该召回，不往补兵名单里塞名字。

        以前这里会 mark_understrength()，于是招兵任务（recruit.py 看到在外的
        缺员编队会 recall）把刚出征的队伍拽回主城 —— 表现就是「打一格就回城」。
        补兵名单只该由招兵任务按「真的缺员」自己判断，不能由「一格打不动」写。
        """
        names = [n for n in (names or []) if n]
        if not names:
            return False
        self.need_heal = True
        log.info("  %s 这一轮打不出兵不血刃，准备召回它们回血（不改你的路线）",
                 "、".join(names))
        return True

    def mark_understrength(self, names):
        """把需要补兵的编队记进 state：招兵任务据此在它们回城后补满。"""
        weak = [n for n in (self.state.data.get("understrength") or []) if n]
        changed = False
        for name in names or []:
            name = name.strip() if isinstance(name, str) else ""
            if name and name not in weak:
                weak.append(name)
                changed = True
        if changed:
            self.state.data["understrength"] = weak
            self.state.save()

    @staticmethod
    def recall_accepted(sc):
        """召回点了确定之后，游戏会把编队改成「行军中/返回中」。读到了才算受理。"""
        try:
            return bool(sc.text(r"行军中|返回中|撤退中"))
        except Exception:
            return False

    def _squad_list(self):
        """当前配置的开荒小队（拿不到就当成没有小队，退回按单支判断）。"""
        ops = getattr(self.bot, "operations", None)
        if ops is None:
            return []
        try:
            return ops.squad_snapshot()
        except Exception:
            log.debug("读取小队配置失败，按单支判断", exc_info=True)
            return []

    @staticmethod
    def recall_targets(armies, thr, weak=(), names=None, squads=None):
        """挑出这一轮要召回哪些编队，返回 [(tap_y, name, secs, loc, hp, why)]。

        为什么要有这一步：出兵那边有 _dispatch_whole_squad，同一小队一起走、
        绝不拆开；召回这边以前是逐支按自己血量判断，于是小队里血高一点的那支
        被留在外面，一起出征的两支回城时散成两支 —— 用户反馈的「召回把小队拆开」。

        规则（按优先级）：
          1. 已经在主城的不用召；
          2. 位置没读清、又不是点名召回 → 不乱动（和以前一致，别误召）；
          3. 点名召回只认点名的；
          4. 单支需要：缺员名单里，或血量低于阈值；
          5. **小队整体**：小队里只要有一个人需要回城，这一队在外面的成员一起回。
        """
        weak = set(weak or ())
        want_named = set(names) if names is not None else None

        squad_of = {}
        squad_members = {}
        for sq in squads or ():
            if sq.get("on") is False:
                continue
            members = [str(n).strip() for n in (sq.get("armies") or []) if str(n).strip()]
            if not members:
                continue
            sq_name = sq.get("name") or "小队"
            squad_members[sq_name] = set(members)
            for n in members:
                squad_of[n] = sq_name

        rows = []
        for tap_y, name, secs, loc, hp in armies:
            if "主城" in (loc or ""):
                continue                      # 已经在主城
            # 位置读不到也先留着：它自己没理由时不召回，
            # 但同队有人在外的必须一起回来 —— 否则队伍照样被拆开。
            rows.append({"tap_y": tap_y, "name": name, "secs": secs,
                         "loc": loc, "hp": hp, "no_loc": not loc})

        # 第一遍：单支自己是否需要回城
        need = {}
        for r in rows:
            if want_named is not None:
                continue
            if r["no_loc"]:
                continue                      # 位置不明：不单独判定，等队友带
            if r["name"] in weak:
                need[r["name"]] = "缺员，回城补兵"
            elif r["hp"] is not None and r["hp"] < thr:
                need[r["name"]] = "回主城休整"

        # 第二遍：小队整体带回来（出兵不拆队，召回也不能拆）
        pulled = {}
        need_names = set(need)
        for r in rows:
            sq = squad_of.get(r["name"])
            if not sq:
                continue
            if squad_members.get(sq, set()) & need_names:
                pulled[r["name"]] = "同队有人要回城，整队一起走"

        out = []
        for r in rows:
            if want_named is not None:
                if r["name"] in want_named:
                    out.append((r["tap_y"], r["name"], r["secs"], r["loc"], r["hp"], "按指定召回"))
                continue
            # 自己有理由就写自己的（血量低/缺员），只有被队友带回来的才写「同队」
            why = need.get(r["name"]) or pulled.get(r["name"])
            if why:
                out.append((r["tap_y"], r["name"], r["secs"], r["loc"], r["hp"], why))
        return out

    def recall(self, names=None, thr=None):
        """把在外面的空闲编队调回主城：血量低的回血、缺员的回城补兵。

        names 给定时只召回这些编队；thr 是血量门槛（默认 recall_hp）。返回 [(名字, 行军秒数)]。
        """
        thr = self.cfg.get("recall_hp", 0.7) if thr is None else thr
        grid = self.center_city()
        if grid is None:
            log.warning("召回失败：找不到主城，无法打开行军面板")
            return []
        try:
            t = grid.tiles[grid.find_city()]
        except Exception:
            log.warning("召回失败：主城所在的格子识别不到")
            return []
        self.dev.tap(int(t.x + mv.TILE), int(t.y + mv.TILE), delay=1.3)
        sc = self.ui.grab()
        if not self.ui.tap_text(sc, r"^行军$", delay=1.5):
            log.warning("召回失败：点开主城后没有「行军」按钮（弹窗可能没出来）")
            self.ui.to_map()
            return []
        sc = self.ui.wait_text(r"选择要前往的军队", timeout=4)
        if sc is None:
            log.warning("召回失败：没等到「选择要前往的军队」面板")
            self.ui.to_map()
            return []
        weak = set(self.state.data.get("understrength", []))
        squads = self._squad_list()
        picked, seen = [], set()
        for page in range(3):
            armies = [a for a in self.list_armies(sc) if a[1] not in seen]
            if not armies:
                break
            # 先整屏算一次「该召谁」——含小队整体判断，再按结果去点
            targets = {row[1]: row[5] for row in self.recall_targets(
                armies, thr, weak=weak, names=names, squads=squads)}
            for a in armies:
                seen.add(a[1])
                tap_y, name, secs, loc, hp = a
                why = targets.get(name)
                if not why:
                    continue
                before = self.read_selected(sc)
                self.dev.tap(400, tap_y, delay=0.6)
                sc = self.ui.grab()
                after = self.read_selected(sc)
                # 核对「真的选中了这一支」：卡片位置偶尔会差半张，
                # 不核对就会点到旁边那支（真机踩过：主城待机的编队被一起召回）。
                if before and after and after[0] <= before[0]:
                    log.info("  召回 %s：这一下没选中（卡片位置偏了），跳过", name)
                    continue
                picked.append((name, secs))
                log.info("召回 %s（位于%s，血量%s，行军%ss）%s", name, loc or "?",
                         "%.0f%%" % (hp * 100) if hp is not None else "?", secs, why)
            sc = self.scroll_armies()
        if not picked:
            log.info("召回：这一轮没有需要召回的编队（点名 %s）",
                     "、".join(names) if names else "按血量/缺员自动判断")
            self.ui.to_map()
            return []
        accepted = False
        names_out = [n for n, _ in picked]
        self.ui.tap_text(self.ui.grab(), r"^确定$", (200, 1000, 520, 1100), delay=1.5)
        after = self.ui.grab_stable(timeout=2.5)
        # 受理的判据：面板已经关掉（回到大地图），或者能看到「行军中/返回中」。
        # 以前只找「行军中」四个字，而游戏点完确定会立刻关面板，于是永远报「没读到回执」，
        # 明明召回成功了却像失败一样。
        accepted = self.recall_accepted(after) or self.ui.on_map(after)
        if accepted:
            log.info("召回已受理，%s 正在回主城回血", "、".join(names_out))
        else:
            log.warning("召回没读到回执：%s（下一轮会按军队面板核对位置）", "、".join(names_out))
        self.ui.to_map()
        return picked

    def plan_image(self):
        """不出兵，只画出当前视野的开荒计划：每格圈数 + 候选顺序。"""
        import cv2

        self.ui.to_home()
        grid = mv.MapGrid(self.dev.screenshot(), classify=False)
        if self.season is None:
            self.detect_season(grid)
        if not self.anchor_from_city(grid):
            self.calibrate(grid)
            grid = mv.MapGrid(self.dev.screenshot(), classify=False)
            self.anchor_from_city(grid)
        grid._classify(self.seasons())
        order = self.rank(grid)
        img = grid.debug_image()
        retry_h = self.cfg.get("retry_hours", 3)
        if self.anchor and self.state.data.get("home"):
            for t in grid.tiles.values():
                xy = self.game_xy(t)
                if xy is None or t.blocked:
                    continue
                r = self.ring(*xy)
                col = (0, 120, 0) if r % 2 == 1 else (150, 150, 150)
                cv2.putText(img, "r%d" % r, (t.x + 40, t.y + int(mv.TILE) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.35, col, 1)
        n = 0
        for t in order:
            xy = self.game_xy(t)
            if xy and self.state.tile_blocked(*xy, retry_h):
                cv2.putText(img, "x", (t.x + 28, t.y + 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 200), 2)
                continue
            n += 1
            cv2.putText(img, str(n), (t.x + 22, t.y + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        return img

    def _save_debug(self, grid):
        import os

        import cv2

        d = os.path.join(V.ROOT, "logs", "expand")
        os.makedirs(d, exist_ok=True)
        files = sorted(os.listdir(d))
        for f in files[:-30]:  # 只保留最近 30 张
            os.remove(os.path.join(d, f))
        img = grid.debug_image()
        if self.anchor:
            for t in grid.tiles.values():
                xy = self.game_xy(t)
                if xy and not t.blocked:
                    cv2.putText(img, "%d,%d" % xy, (t.x + 4, t.y + int(mv.TILE) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (40, 40, 40), 1)
        cv2.imencode(".png", img)[1].tofile(os.path.join(d, time.strftime("%m%d_%H%M%S") + ".png"))

    def _allowed_tier(self, d):
        max_tier = self.cfg.get("max_difficulty")
        if not max_tier or d.tier is None:
            return True
        return d.tier <= DIFF_TIERS.index(max_tier)

    def _should_skip_by_learn(self, d):
        """这一类地值不值得试？用「期望收益」判断，比只看成功率聪明。

        期望收益 = 成功率 × 每小时产出。
        举例（实测数据）：
          荒地简单 83% × 3 = 2.5
          2级粮食地 33% × 4 = 1.3   ← 该跳过
          3级粮食地 40% × 7 = 2.8   ← 虽然成功率低，但收益高，值得试
        只看成功率会误杀 3 级地，只看产出会浪费在打不过的地上。

        数据超过 learn_stale_hours 就作废重试（中途可能升了兵种/装备/校场）。
        注意：route 模式是玩家手动选格，不调用此筛选（玩家明确要打就打），只记录战绩。
        """
        if not self.cfg.get("learn_enabled", True):
            return False
        key = self.state.learn_key(d.res, d.level, d.tier)
        rec = self.state.learn_get(key)
        try_n = int(rec.get("try", 0))
        if try_n < int(self.cfg.get("learn_min_try", 3)):
            return False
        stale_h = float(self.cfg.get("learn_stale_hours", 6))
        if time.time() - float(rec.get("t", 0)) > stale_h * 3600:
            log.debug("  %s 的学习数据超过 %.0f 小时，重新试一次", key, stale_h)
            return False
        rate = self.state.learn_rate(key)
        # 实际产出：资源地就是它那一级的产量；荒地是 1粮+1木+1石 = 3
        prod = PRODUCTION.get(int(d.level or 1), 1) if d.res else 3
        # 一次都没成功过的，别让"高产出"把它救回来（5级地产出14，12%成功率也有1.8，
        # 但 0 次成功说明是真的打不动）。样本够了就直接跳过。
        ok_n = int(rec.get("ok", 0))
        if ok_n == 0 and try_n >= int(self.cfg.get("learn_min_try", 3)):
            log.info("  这类地（%s）试了 %d 次一次没成，先跳过", key, try_n)
            self._mark(d, "hard", label="历史打不过", cool_h=self.cfg.get("retry_hours_wipe", 12))
            return True
        expect = rate * prod
        floor = float(self.cfg.get("learn_min_expect", 1.0))
        if expect >= floor:
            return False
        log.info("  这类地（%s）期望收益只有 %.1f（成功 %.0f%% × 产出 %d），先跳过不试了",
                 key, expect, rate * 100, prod)
        self._mark(d, "hard", label="历史打不过", cool_h=self.cfg.get("retry_hours_wipe", 12))
        return True

    def _mark(self, d, status, **kw):
        if d.x is None:
            return
        self.state.set_tile(d.x, d.y, name=d.name, status=status,
                            tier=d.tier, tier_lv=d.tier_lv, **kw)

    # ---------------- 选兵 ----------------
    def read_forecast(self, sc):
        for b in sc.read(R_FORECAST):
            for lab in LOSS_LABELS:
                if lab in b.text:
                    return lab
        return None

    def read_selected(self, sc):
        for b in sc.read(R_SELECTED):
            m = re.search(r"(\d+)\s*/\s*(\d+)", b.text)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def read_hp(img, status_y):
        """卡片里士兵头像下方血条（红=剩余，灰=损失）的平均血量比例。"""
        best = None
        for y in range(status_y - 45, status_y - 12):
            row = img[y, 40:690].astype(int)
            red = ((row[:, 2] > 180) & (row[:, 1] < 130) & (row[:, 0] < 120)).sum()
            gray = ((abs(row[:, 2] - row[:, 1]) < 12) & (row[:, 2] > 110) & (row[:, 2] < 175)).sum()
            if red + gray >= 15 and (best is None or red + gray > best[0] + best[1]):
                best = (red, gray)
        if not best:
            return None
        return best[0] / float(best[0] + best[1])

    def list_armies(self, sc, status=r"行军时间"):
        """编队卡片列表：[(点击y, 名字, 行军秒数, 位置, 血量)]，按行军时间排序。

        每张可选卡片底部都有一行“行军时间 h:mm:ss”（忙碌的编队显示“行军中”等），
        用它定位卡片，编队改过名也不影响。
        """
        out = []
        for tb in sc.boxes:
            if not re.search(status, tb.text) or not (280 < tb.center[1] < 960):
                continue
            m = re.search(r"(\d+):(\d+):(\d+)", tb.text)
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 10 ** 6
            name, loc = "", ""
            for b in sc.boxes:
                dy = tb.center[1] - b.center[1]
                if 70 < dy < 140 and b.center[1] > 305:  # 305 以上是面板标题
                    if b.box[0] < 250 and not name:
                        name = b.text.lstrip("口□0123456789 ")
                    elif b.box[0] >= 250 and not loc:
                        loc = b.text
            if not name:
                continue  # 名字被滚出屏幕的卡片不碰（可能已经勾选过，再点会取消）
            if self.is_selected(sc.img, tb.center[1]):
                continue  # 已勾选（名字左边是绿色序号），再点会取消勾选
            hp = self.read_hp(sc.img, tb.center[1])
            out.append((tb.center[1] - 60, name, secs, loc, hp))
        out.sort(key=lambda x: x[2])
        return out

    @staticmethod
    def is_selected(img, status_y):
        """卡片是否已勾选：名字左边的勾选框变成绿色序号。"""
        y1, y2 = max(0, status_y - 130), status_y - 95
        roi = img[y1:y2, 40:85].astype(int)
        green = (roi[:, :, 1] > 140) & (roi[:, :, 2] < 160) & (roi[:, :, 0] < 110) & (roi[:, :, 1] - roi[:, :, 2] > 30)
        return int(green.sum()) > 100

    def scroll_armies(self):
        """编队列表一屏只放得下 3 张卡片，往上滑露出后面的。返回新的 Screen。"""
        self.dev.swipe(360, 900, 360, 430, 600, delay=0.8)
        return self.ui.grab()

    def _combo_learn(self, key, armies, ok):
        """记一次编队组合结果（同类地下次优先复用成功的组合）。"""
        try:
            self.state.combo_add(key, armies, ok)
        except Exception:
            log.debug("编队组合学习记录失败", exc_info=True)

    def dispatch(self, approve=None, learn_key=None):
        """在“选择要出战的军队”界面挑兵。返回 (结果, 预测标签)。

        如果用户选了开荒小队（operations.squad_pick 命中），就只派小队里的编队，
        不再自己挨个试。learn_key 给定时，把最终选中的编队组合记进战斗学习。
        """
        self.bot.phase("正在检查可出战编队和战斗预测")
        # 硬规则（用户要求）：只有预测“兵不血刃”才出兵；会死人的进攻一律留给玩家亲自判断，配置改不了
        accept = [SAFE_LABEL]
        sc = self.ui.wait_text(r"选择要出战的军队", timeout=5)
        if sc is None:
            return "no_panel", None
        # 用户预设的编队：只允许点这几支
        allowed = set(getattr(self, "_combo_allow", ()) or ())
        if allowed:
            log.info("  按预设编队出兵：%s", "、".join(sorted(allowed)))
        if allowed:
            # 用户编好了小队：整队一次性同时指派，绝不把同一小队拆到不同地块。
            # 以前是逐支勾、第一支预测「兵不血刃」就 break，剩下的成员被下一格顺手派走。
            return self._dispatch_whole_squad(sc, allowed, approve, learn_key)
        label = None
        selected = 0
        seen = set()
        picked = []
        done = False
        self.tried_min_hp = 1.0
        self.march_seconds = 0
        min_hp = self.cfg.get("min_attack_hp", 0.3)
        for page in range(3):
            armies = [a for a in self.list_armies(sc) if a[1] not in seen]
            if not armies:
                break
            # 近战编队打头（扛伤害），然后远程编队；同类里血量高的先上、再按行军时间；残血编队不派
            # 近战编队 = 序号在前 melee_count 支（和 recruit 的分工一致）
            melee_count = self.bot.cfg.get("recruit", {}).get("melee_count", 2)

            def _is_melee(nm):
                m = re.search(r"(\d+)", nm or "")
                return bool(m) and int(m.group(1)) - 1 < melee_count

            # 历史成功组合里的编队优先出战（同类地上次谁打出了兵不血刃，这次先试谁）
            prefer = set(getattr(self, "_combo_prefer", ()) or ())
            armies.sort(key=lambda a: (a[1] not in prefer,
                                       not _is_melee(a[1]),
                                       -(round((a[4] if a[4] is not None else 1.0) * 5)), a[2]))
            for tap_y, name, secs, loc, hp in armies:
                seen.add(name)
                if allowed and name not in allowed:
                    log.debug("  - %s 不在预设组合里，跳过", name)
                    continue
                if secs > self.cfg.get("max_march_seconds", 1800):
                    continue
                if hp is not None and hp < min_hp:
                    log.info("  - %s 血量%.0f%%，太低不派", name, hp * 100)
                    continue
                before = self.read_selected(sc)
                self.dev.tap(400, tap_y)
                # 预测有“计算中”过程，连续两次读到相同标签才算数（只识别右下角一小块，很快）
                prev = None
                end = time.time() + 4
                while time.time() < end:
                    time.sleep(0.15)
                    sc = self.ui.grab()
                    label = self.read_forecast(sc)
                    if label and label == prev:
                        break
                    prev = label
                after = self.read_selected(sc)
                if before and after and after[0] <= before[0]:
                    log.debug("%s 无法选择（忙碌中?）", name)
                    continue
                selected += 1
                picked.append(name)
                self.march_seconds = max(self.march_seconds, secs)
                if hp is not None:
                    self.tried_min_hp = min(self.tried_min_hp, hp)
                log.info("  + %s (行军%ss, 血量%s) → 预测: %s", name, secs,
                         "%.0f%%" % (hp * 100) if hp is not None else "?", label)
                if label in accept or (after and after[0] >= after[1]):
                    done = True
                    break
            if done:
                break
            sc = self.scroll_armies()
        self.tried_armies = picked
        return self._commit_dispatch(sc, label, picked, approve, learn_key)

    @staticmethod
    def squad_ready(picked, allowed):
        """小队成员是否全部就位。

        用户定的规矩：指派和召回，一个小队里的编队必须同步。
        所以只要有成员没勾上（行军太远、选不中、出征位不够），
        整队就不派 —— 派一半出去等于把小队拆了。
        """
        allowed = [n for n in (allowed or []) if n]
        if not allowed:
            return True
        return set(allowed) <= set(picked or ())

    def _dispatch_whole_squad(self, sc, allowed, approve, learn_key):
        """把小队成员一次性全部勾上，再统一读一次预测。

        安全线和其他路径完全一样：最终预测不是「兵不血刃」就不出兵。
        区别只在勾选方式 —— 同一小队的编队一起走，不拆开。
        """
        self.bot.phase("正在同时指派开荒小队")
        picked = []
        self.tried_min_hp = 1.0
        self.march_seconds = 0
        full = False
        max_march = self.cfg.get("max_march_seconds", 1800)
        for _page in range(3):
            for tap_y, name, secs, loc, hp in sorted(self.list_armies(sc), key=lambda a: a[2]):
                if name not in allowed or name in picked:
                    continue
                if secs > max_march:
                    log.info("  - %s 行军 %ss 太远，不并入小队", name, secs)
                    continue
                before = self.read_selected(sc)
                self.dev.tap(400, tap_y)
                time.sleep(0.5)
                sc = self.ui.grab()
                after = self.read_selected(sc)
                if before and after and after[0] <= before[0]:
                    log.debug("  %s 无法选择（忙碌中?）", name)
                    continue
                picked.append(name)
                self.march_seconds = max(self.march_seconds, secs)
                if hp is not None:
                    self.tried_min_hp = min(self.tried_min_hp, hp)
                log.info("  + %s (行军%ss, 血量%s) 已并入小队", name, secs,
                         "%.0f%%" % (hp * 100) if hp is not None else "?")
                if after and after[0] >= after[1]:
                    full = True      # 出征位已满，再加不进去了
                    break
            if full or set(allowed) <= set(picked):
                break
            sc = self.scroll_armies()
        label = self.read_forecast(self.ui.grab())
        if not self.squad_ready(picked, allowed):
            missing = [n for n in allowed if n not in picked]
            log.warning("  小队没齐（%s 没并入），整队不派 —— 派一半就把小队拆了",
                        "、".join(missing))
            self.ui.close()
            self.tried_armies = []
            return "no_army", None
        if picked:
            log.info("  小队同时指派：%s → 预测: %s", "、".join(picked), label or "未读到")
        self.tried_armies = picked
        return self._commit_dispatch(sc, label, picked, approve, learn_key)

    def _commit_dispatch(self, sc, label, picked, approve, learn_key):
        """选中编队之后的统一收尾：复核预测 → 确认出兵 → 处理拦截提示。"""
        if not picked:
            self.ui.close()
            return "no_army", None
        if label not in [SAFE_LABEL]:
            self.ui.close()
            if learn_key:
                # 这套组合没打出兵不血刃，记一笔失败（下次不优先复用）
                self._combo_learn(learn_key, picked, False)
            return "too_hard", label
        # 出兵前最后确认一次：重新截图读预测，必须仍是“兵不血刃”
        time.sleep(0.3)
        sc = self.ui.grab()
        self.bot.phase("正在复核出兵前的战斗预测")
        final = self.read_forecast(sc)
        if final != SAFE_LABEL:
            log.warning("  出兵前复核预测为 %s，取消出兵", final)
            self.ui.close()
            return "too_hard", final

        def confirm():
            return self.ui.tap_text(sc, r"^确定$", (200, 1000, 520, 1100), delay=1.5)

        confirmed = approve(confirm) if approve else confirm()
        if not confirmed:
            self.ui.close()
            return ("paused" if confirmed is None else "no_confirm"), label
        if learn_key:
            self._combo_learn(learn_key, picked, True)  # 这套组合打出了兵不血刃
        sc = self.ui.grab()
        if self.ui.on_map(sc):
            return "ok", label  # 面板已关闭，没有额外提示
        if sc.text("背包容量不足"):
            self.ui.tap_text(sc, r"^取消$", delay=1.0)
            self.ui.close()
            return "bag_full", label
        if sc.text("奖励点不足"):
            if self.cfg.get("allow_without_reward_points", True):
                if approve:
                    if not approve(lambda: self.ui.tap_text(sc, r"^确定$", delay=1.5)):
                        self.ui.tap_text(sc, r"^取消$", delay=1.0)
                        return "paused", label
                else:
                    self.ui.tap_text(sc, r"^确定$", delay=1.5)
            else:
                self.ui.tap_text(sc, r"^取消$", delay=1.0)
                self.ui.close()
                return "no_points", label
        return "ok", label
