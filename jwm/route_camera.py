"""把模拟器地图上的画面格子对应到游戏坐标。"""
import logging
import re
from collections import deque
from time import perf_counter

import cv2

from . import mapview as mv
from .route_map import home_tiles

log = logging.getLogger(__name__)

SCREEN_CENTER = (360, 640)
MAP_TOP, MAP_BOTTOM = 270, 1150
# 只用于网页拼图。与这些浮层相交的地块从本帧丢弃，重叠视野仍可提供同一地块。
ATLAS_HUD = (
    (635, MAP_TOP, 720, 451),  # 右侧竖栏
    (639, 466, 720, 617),     # 活动和商店入口
    (0, 980, 88, MAP_BOTTOM),  # 左下城池、聊天按钮
    (633, 980, 720, MAP_BOTTOM),  # 右下军队、任务按钮
    (0, 1070, 720, MAP_BOTTOM),  # 长度随文字变化的任务提示
)


class GameNotReady(ValueError):
    pass


class HomeUnavailable(ValueError):
    pass


def popup_position(sc, detail, *, land=False):
    """拒绝把同屏的其他坐标（如编队标签）当作弹窗坐标。"""
    if not detail.center:
        return None
    cx, cy = detail.center
    titles = [b for b in sc.boxes if b.text.replace(" ", "") == detail.name] if land else []
    positions = set()
    for box in sc.boxes:
        match = re.fullmatch(r"\(?(\d+)[,，](\d+)\)?", box.text.replace(" ", ""))
        if not match:
            continue
        x, y = box.center
        if abs(x - cx) > 300 or abs(y - cy) > 270:
            continue
        if land and not any((abs(y - t.center[1]) <= 26 and abs(x - t.center[0]) <= 200)
                            or (0 <= y - t.center[1] <= 58 and abs(x - t.center[0]) <= 80)
                            for t in titles):
            continue
        positions.add(tuple(map(int, match.groups())))
    return next(iter(positions)) if len(positions) == 1 else None


def popup_owned(sc, point):
    """已核对坐标的地块弹窗中，「修建」与「行军」是己方领地的操作。"""
    x, y = point
    coord = sc.text(rf"^\(?{x}\s*[,，]\s*{y}\)?$")
    if coord is None:
        return False
    cx, cy = coord.center
    region = (max(0, cx - 225), cy, min(720, cx + 225), min(1280, cy + 230))
    build = sc.text(r"^修建$", region)
    march = sc.text(r"^行军$", region)
    return bool(build and march and abs(build.center[0] - march.center[0]) < 30
                and 15 <= march.center[1] - build.center[1] <= 100)


def measured_shift(before, after, dx, dy, *, min_score=0.78, min_matches=2):
    """用重叠地形实测拖动距离，避免多次拖动后累计一格偏差。"""
    margin, size = 68, 96
    x0 = max(22, 22 + margin - dx)
    x1 = min(698, 698 - margin - dx) - size
    y0 = max(335, 335 + margin - dy)
    y1 = min(1035, 1035 - margin - dy) - size
    xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
    matches = []
    for x, y in ((xm, ym), (x0, y0), (x1, y0), (x0, y1), (x1, y1),
                 (xm, y0), (xm, y1), (x0, ym), (x1, ym)):
        patch = before[y:y + size, x:x + size]
        if patch.std(axis=(0, 1)).mean() < 9:
            continue
        left, top = x + dx - margin, y + dy - margin
        search = after[top:top + 2 * margin + size, left:left + 2 * margin + size]
        scores = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
        _, score, _, (mx, my) = cv2.minMaxLoc(scores)
        scores[max(0, my - 9):my + 10, max(0, mx - 9):mx + 10] = -1
        if score >= min_score and score - scores.max() >= 0.045:
            matches.append((mx - margin + dx, my - margin + dy))
            sx, sy = matches[-1]
            if sum(abs(sx - x) <= 4 and abs(sy - y) <= 4 for x, y in matches) >= min_matches:
                return sx, sy
    raise ValueError("镜头移动后未能核对地图坐标。")


class RouteCamera:
    def __init__(self, expand):
        self.expand = expand
        self.anchor = None
        self.home = None
        self.recent = deque(maxlen=6)

    def _read_home(self):
        # 登录、更新或重连界面由玩家处理，地图读取只在已经进入游戏时进行。
        self.expand.bot.phase("正在定位主城并校准地图坐标")
        ui = self.expand.ui
        sc = ui.grab()
        kind = ui.classify(sc)
        if sc.text(r"重连中|更新提示|当前版本过低"):
            raise GameNotReady("游戏正在重连或需要更新，进入大地图后再读取。")
        if kind in ("login", "agreement", "updating", "reconnect", "unknown"):
            raise GameNotReady("等待进入游戏大地图，进入后会自动定位主城。")
        if kind != "map":
            sc = ui.to_map(auto_login=False)
        self.expand.anchor = None
        grid, point = self.expand.home_view(sc, shortcut_center=True)
        if point:
            img = self._read_home_popup(point)
            if img is not None:
                return img
            if grid is None:
                # 快捷键已回城，但主城点击点与预期不符，再用画面确认一次。
                _, point = self.expand.home_view(ui.to_map(auto_login=False))
                if point:
                    img = self._read_home_popup(point)
                    if img is not None:
                        return img
        raise HomeUnavailable("本轮未能自动确认主城位置，脚本会重试。")

    def _read_home_popup(self, point):
        from .tasks.expand import parse_popup

        ui = self.expand.ui
        self.expand.dev.tap(*point, jitter=0, delay=1.2)
        sc = ui.grab_stable()
        detail = parse_popup(sc)
        home = popup_position(sc, detail)
        selected = ui.has_tile_popup(sc)
        clean = ui.close(sc) if selected else sc
        if (not selected or not home or not 1.5 * mv.TILE < detail.span < 2.7 * mv.TILE
                or detail.has_attack or ui.classify(clean) != "map"):
            return None
        expected = detail.center[0] - mv.TILE / 2, detail.center[1] + mv.TILE / 2
        try:
            grid = mv.MapGrid(clean.img, classify=False)
        except ValueError:
            grid = None
        ij = grid.find_city() if grid is not None else None
        city = grid.tiles[(ij[0], ij[1] + 1)].center if ij is not None else None
        if city and max(abs(a - b) for a, b in zip(city, expected)) <= 16:
            anchor = city
        else:
            # 主城外观或领地线认不出来时，用已核对的弹窗选中框校准。
            try:
                dx, dy = measured_shift(sc.img, clean.img, 0, 0)
            except ValueError:
                return None
            anchor = (expected[0] + dx, expected[1] + dy)
            if city and home != self.home and max(abs(a - b) for a, b in zip(city, anchor)) > 16:
                return None
        self.home = home
        self.anchor = (*anchor, *home)
        return clean.img

    def xy(self, point):
        ax, ay, gx, gy = self.anchor
        return gx + round((point[0] - ax) / mv.TILE), gy - round((point[1] - ay) / mv.TILE)

    def capture(self, target=None, expected_home=None):
        img = self._read_home()
        if expected_home is not None and self.home != expected_home:
            return None
        return self._capture_from(img, target)

    def _resume_view(self, home):
        """只有当前截图与已标定截图的大片地形吻合时，才复用坐标。"""
        ui = self.expand.ui
        sc = ui.grab()
        if ui.in_city(sc):
            sc = ui.to_map(auto_login=False)
        if not ui.on_map(sc) or ui.has_tile_popup(sc):
            return None
        for saved_home, anchor, previous in reversed(self.recent):
            if saved_home != home:
                continue
            try:
                dx, dy = measured_shift(previous, sc.img, 0, 0, min_score=0.92, min_matches=3)
            except ValueError:
                continue
            # 比较匹配点以外的大面积地形。草地纹理或固定按钮相似不足以认定是同一地点。
            before = previous[385:945, 100:550]
            after = sc.img[385 + dy:945 + dy, 100 + dx:550 + dx]
            if (cv2.absdiff(before, after).max(axis=2) <= 16).mean() < 0.86:
                continue
            ax, ay, gx, gy = anchor
            self.anchor = (ax + dx, ay + dy, gx, gy)
            self.home = home
            return sc.img
        return None

    def _browse(self, target, home):
        self.expand.bot.phase(f"正在对齐地图视野 {target}")
        started = perf_counter()
        img = self._resume_view(home) if self.recent else None
        reused = img is not None
        if img is None:
            img = self._read_home()
        if self.home != home:
            return None
        try:
            grid = self._capture_from(img, target, quick=True)
        except ValueError:
            # 快速拖动丢失参照时，从主城用原来的稳速流程重定位。
            grid = self.capture(target, home)
            reused = False
        log.debug("网页地图读取 %.2fs，%s", perf_counter() - started,
                  "复用已校准视野" if reused else "从主城校准")
        return grid

    def _capture_from(self, img, target, *, quick=False):
        if target is not None:
            for _ in range(16):
                ax, ay, gx, gy = self.anchor
                x = ax + (target[0] - gx) * mv.TILE
                y = ay - (target[1] - gy) * mv.TILE
                dx, dy = SCREEN_CENTER[0] - x, SCREEN_CENTER[1] - y
                if max(abs(dx), abs(dy)) < 40:
                    break
                dx, dy = (round(max(-210, min(210, d))) for d in (dx, dy))
                ui = self.expand.ui
                if quick:
                    # 短拖动完成后直接等待地图稳定，省去通用导航的额外截图等待。
                    x, y = SCREEN_CENTER
                    self.expand.dev.swipe(x - dx // 2, y - dy // 2, x + dx // 2, y + dy // 2, 380)
                    sc = ui.grab_stable(timeout=0.7)
                else:
                    self.expand.pan(dx, dy)
                    sc = ui.grab()
                try:
                    sx, sy = self._map_shift(img, sc, dx, dy)
                except ValueError:
                    if not quick:
                        raise
                    # 地图可能刚松手，还在继续移动。先补一帧，避免重新校准整张图。
                    sc = ui.grab_stable(timeout=0.7)
                    try:
                        sx, sy = self._map_shift(img, sc, dx, dy)
                    except ValueError:
                        # 到达边缘时仍发布可见部分，但必须先验证镜头确实没有移动。
                        sx, sy = self._map_shift(img, sc, 0, 0, min_score=0.92, min_matches=3)
                        if max(abs(sx), abs(sy)) >= 10:
                            raise
                stopped = max(abs(sx), abs(sy)) < 10
                if stopped and not quick:
                    raise ValueError("地图已到边缘或镜头未移动。")
                self.anchor = (ax + sx, ay + sy, gx, gy)
                img = sc.img
                if stopped:
                    break
            else:
                raise ValueError("位置离主城太远，超出本次地图扫描范围。")
        ax, ay, _, _ = self.anchor
        phase = ((ax - mv.TILE / 2) % mv.TILE, (ay - mv.TILE / 2) % mv.TILE)
        return mv.MapGrid(img, phase=phase, classify=False)

    def _map_shift(self, previous, sc, dx, dy, **confidence):
        ui = self.expand.ui
        if not ui.on_map(sc) or ui.has_tile_popup(sc):
            raise ValueError("游戏已离开大地图，请重新读取。")
        return measured_shift(previous, sc.img, dx, dy, **confidence)

    def _classify_tiles(self, grid):
        """识别可见可占地的资源类型/等级，供自动规划按价值选格。

        速度控制：只分类非占领、非遮挡、非水域的格子；季节皮肤确定后只用该季节模板
        （4 季 → 1 季，模板数减到 1/4）。识别不出的保持 res=None（按荒地处理）。
        """
        exp = self.expand
        if exp.season is None:
            try:
                exp.detect_season(grid)
            except Exception:
                log.debug("季节检测失败", exc_info=True)
        b = mv.bank()
        for t in grid.tiles.values():
            if t.blocked or t.owned or t.res is not None:
                continue
            if grid._is_water(t):
                continue
            try:
                res, lv, sc = b.classify(grid.img, t.x, t.y, seasons=exp.seasons())
                if sc >= 0.80:
                    t.res, t.level, t.score = res, lv, sc
            except Exception:
                log.debug("格子资源识别失败", exc_info=True)

    def tiles(self, grid):
        self._classify_tiles(grid)
        result = []
        city = home_tiles(self.home)
        for tile in grid.tiles.values():
            if tile.blocked:
                continue
            if any(tile.x < right and tile.x + mv.TILE > left
                   and tile.y < bottom and tile.y + mv.TILE > top
                   for left, top, right, bottom in ATLAS_HUD):
                continue
            x, y = self.xy(tile.center)
            if min(x, y) < 0:
                continue
            if (x, y) in city:
                kind = "owned"
            elif grid._is_water(tile):
                kind = "water"
            else:
                kind = "owned_hint" if tile.owned else "land"
            result.append(dict(x=x, y=y, left=tile.x, top=tile.y, kind=kind,
                               res=tile.res, level=tile.level))
        return result

    def publish(self, route, target, warm=False):
        grid = self.capture() if target is None else self._browse(target, route.home)
        if grid is None:
            route.identify(self.home)
            raise ValueError("主城已变化，正在重新读取。")
        self._publish_grid(route, grid)
        if warm:
            route.start_warmup()
            x, y = self.xy(SCREEN_CENTER)
            for index, (dx, dy) in enumerate(((3, 4), (-3, 4), (-3, -4), (3, -4)), 1):
                if route.scan_requested():
                    break
                try:
                    self.expand.bot.phase(f"正在预读主城周边地图（{index}/4）")
                    grid = self._capture_from(grid.img, (x + dx, y + dy), quick=True)
                except ValueError:
                    # 匹配失败后锚点与最后成功的截图可能已不在同一视野。
                    break
                self._publish_grid(route, grid)
        route.scan_finished()

    def _publish_grid(self, route, grid):
        img = grid.img[MAP_TOP:MAP_BOTTOM]
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise ValueError("地图截图生成失败，请重新读取。")
        self.recent.append((self.home, self.anchor, grid.img))
        route.publish(self.home, self.xy(SCREEN_CENTER), self.tiles(grid), jpg.tobytes())
