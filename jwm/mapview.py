"""大地图解析：网格校准、领地识别、资源地图标分类、可攻占边界计算。

坐标约定：
  - 屏幕网格 (i, j)：第 i 列、第 j 行，左上角像素 = (ox + i*TILE, oy + j*TILE)
  - 游戏坐标 (x, y)：x 向右增大，y 向上增大（弹窗里显示的 (25,24) 这种）
"""
import logging
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from .vision import ROOT, imread

log = logging.getLogger(__name__)

TILE = 67.5  # 720x1280 下默认缩放的格子边长
GREEN_BGR = np.array([77, 132, 54])  # 自己领地边界线颜色

# 屏幕上被 UI 挡住的区域（不参与地图解析）
UI_ZONES = [
    (0, 0, 720, 270),  # 顶部资源栏、章节横幅；离顶部太近的格子弹窗会被挡住
    (240, 190, 480, 300),  # 活动倒计时横幅（如“攻城拔寨”，深色，会被误认成主城）
    (0, 0, 240, 160),  # 左上资源栏
    (240, 0, 420, 110),  # 元宝/奖励点
    (630, 160, 720, 320),  # 右上菜单
    (0, 1150, 720, 1280),  # 底部按钮栏
    (0, 1150, 110, 1280),  # 左下进城按钮
    (620, 1060, 720, 1280),  # 右下军队按钮/任务
    (330, 1070, 720, 1145),  # 任务提示条
]

RES_TYPES = ("grain", "tree", "stone")
RES_NAME = {"grain": "粮食", "tree": "木头", "stone": "石头"}
SPRITE_DIR = os.path.join(ROOT, "assets", "sprites")


@dataclass
class TileInfo:
    i: int
    j: int
    x: int  # 屏幕左上角
    y: int
    owned: bool = False
    res: str = None  # grain/tree/stone/None
    level: int = 1  # 1=荒地, 2..5 资源地等级
    score: float = 0.0
    blocked: bool = False  # 水/被 UI 挡住等
    border_edges: set = field(default_factory=set)

    @property
    def center(self):
        return int(self.x + TILE / 2), int(self.y + TILE / 2)


class SpriteBank:
    """资源地图标模板（四季），缩放到当前格子大小。"""

    def __init__(self, tile=TILE):
        self.items = []  # (res, level, season, tpl_bgr(float), mask(bool), dx, dy)
        s = tile / 80.0
        for season in range(4):
            for res in RES_TYPES:
                for k in range(1, 5):
                    p = os.path.join(SPRITE_DIR, "land_%d" % season, "%s_10%d.png" % (res, k))
                    spr = imread(p)
                    if spr is None:
                        continue
                    sp = cv2.resize(spr, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST)
                    a = sp[:, :, 3] > 200
                    ys, xs = np.nonzero(a)
                    y1, y2, x1, x2 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
                    tpl = sp[y1:y2, x1:x2, :3].astype(np.float32)
                    self.items.append((res, k + 1, season, tpl, a[y1:y2, x1:x2], int(x1), int(y1)))

    def classify(self, img, x, y, jitter=3, seasons=None):
        """对屏幕上左上角为 (x,y) 的格子分类，返回 (res, level, score)。"""
        best = (None, 1, 0.0)
        H, W = img.shape[:2]
        for res, lv, season, tpl, mask, dx, dy in self.items:
            if seasons is not None and season not in seasons:
                continue
            h, w = mask.shape
            m = mask.reshape(-1)
            t = tpl.reshape(-1, 3)[m]
            t = t - t.mean(axis=0)
            tn = np.sqrt((t * t).sum())
            if tn < 1e-3:
                continue
            for oy in range(-jitter, jitter + 1):
                for ox in range(-jitter, jitter + 1):
                    px, py = int(x + dx + ox), int(y + dy + oy)
                    if px < 0 or py < 0 or px + w > W or py + h > H:
                        continue
                    roi = img[py:py + h, px:px + w].astype(np.float32).reshape(-1, 3)[m]
                    r = roi - roi.mean(axis=0)
                    rn = np.sqrt((r * r).sum())
                    if rn < 1e-3:
                        continue
                    sc = float((r * t).sum() / (rn * tn))
                    if sc > best[2]:
                        best = (res, lv, sc)
        return best


_bank = None


def bank():
    global _bank
    if _bank is None:
        _bank = SpriteBank()
    return _bank


def green_mask(img, tol=40):
    d = np.abs(img.astype(np.int16) - GREEN_BGR.astype(np.int16)).sum(axis=2)
    return d < tol


def estimate_phase(img):
    """用自己领地的绿色边界线估计网格相位 (ox, oy)，找不到返回 None。"""
    m = green_mask(img)
    cols = m.sum(axis=0)
    rows = m.sum(axis=1)

    def runs(arr, min_len):
        idx = np.nonzero(arr >= min_len)[0]
        if len(idx) == 0:
            return []
        groups, cur = [], [idx[0]]
        for v in idx[1:]:
            if v - cur[-1] <= 1:
                cur.append(v)
            else:
                groups.append(cur)
                cur = [v]
        groups.append(cur)
        return [(g[0] + g[-1]) / 2.0 for g in groups if len(g) <= 6]

    vx = runs(cols, 40)
    hy = runs(rows, 40)
    if not vx or not hy:
        return None

    def phase(centers):
        # 线在格子内侧 ~2px，左右两侧边界各偏一边；取圆周均值
        ang = np.array([(c % TILE) / TILE * 2 * np.pi for c in centers])
        mean = np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())
        return (mean / (2 * np.pi) * TILE) % TILE

    return phase(vx), phase(hy)


def in_ui(x, y, w=TILE, h=TILE):
    for zx1, zy1, zx2, zy2 in UI_ZONES:
        if x < zx2 and x + w > zx1 and y < zy2 and y + h > zy1:
            return True
    return False


class MapGrid:
    """一帧地图截图的解析结果。"""

    def __init__(self, img, phase=None, classify=True, seasons=None):
        self.img = img
        ph = phase or estimate_phase(img)
        if ph is None:
            raise ValueError("画面里没有找到自己领地的边界线")
        # 起点取到屏幕左上外一格
        self.ox = ph[0] - TILE
        self.oy = ph[1] - TILE
        H, W = img.shape[:2]
        self.cols = int((W - self.ox) // TILE) + 1
        self.rows = int((H - self.oy) // TILE) + 1
        self.tiles = {}
        for j in range(self.rows):
            for i in range(self.cols):
                x = self.ox + i * TILE
                y = self.oy + j * TILE
                t = TileInfo(i, j, int(round(x)), int(round(y)))
                if x < 0 or y < 0 or x + TILE > W or y + TILE > H or in_ui(x, y):
                    t.blocked = True
                self.tiles[(i, j)] = t
        self._flood_owned()
        if classify:
            self._classify(seasons)

    # ---- 领地 ----
    def _ring_v(self, t):
        """格子靠边一圈（避开中心图标和边界线）的地面亮度中位数。"""
        s = int(TILE)
        x, y = t.x, t.y
        H, W = self.img.shape[:2]
        if x < 0 or y < 0 or x + s > W or y + s > H:
            return None
        roi = self.img[y + 6:y + s - 6, x + 6:x + s - 6]
        v = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2]
        h, w = v.shape
        ring = np.concatenate([v[:10, :].ravel(), v[-10:, :].ravel(), v[10:-10, :10].ravel(), v[10:-10, -10:].ravel()])
        return float(np.median(ring))

    def _edge_line(self, gm, a, b):
        """a、b（b 在 a 的右侧或下方）公共边上的绿色边界线。

        返回 None（没有线）或 线中心相对格边的偏移（像素，正数=线在 b 一侧）。
        游戏把边界线画在领地内侧，所以偏移的正负就说明哪一侧是领地。
        """
        s = TILE
        H, W = gm.shape
        lo, hi = 0.2, 0.8
        if a.j == b.j:  # 竖边
            ex = self.ox + b.i * TILE
            y1, y2 = int(a.y + s * lo), int(a.y + s * hi)
            x1 = int(np.floor(ex)) - 6
            band = gm[max(0, y1):min(H, y2), max(0, x1):min(W, x1 + 13)]
            if band.size == 0:
                return None
            # 边界线是一条连续的直线：要有某一列覆盖大半条边（春天草地/树的描边会有零散同色像素）
            cov = band.mean(axis=0)
            if cov.max() <= 0.6:
                return None
            cols = np.nonzero(cov >= 0.5 * cov.max())[0]
            return float(cols.mean() + max(0, x1) - ex)
        ey = self.oy + b.j * TILE  # 横边
        x1, x2 = int(a.x + s * lo), int(a.x + s * hi)
        y1 = int(np.floor(ey)) - 6
        band = gm[max(0, y1):min(H, y1 + 13), max(0, x1):min(W, x2)]
        if band.size == 0:
            return None
        cov = band.mean(axis=1)
        if cov.max() <= 0.6:
            return None
        rows = np.nonzero(cov >= 0.5 * cov.max())[0]
        return float(rows.mean() + max(0, y1) - ey)

    def _flood_owned(self):
        gm = green_mask(self.img)
        line = {}  # (k1, k2) -> 偏移或 None
        seeds, outside = set(), set()
        rv = {k: self._ring_v(t) for k, t in self.tiles.items()}
        for k, t in self.tiles.items():
            for di, dj in ((1, 0), (0, 1)):
                k2 = (k[0] + di, k[1] + dj)
                if k2 not in self.tiles:
                    continue
                off = self._edge_line(gm, t, self.tiles[k2])
                line[(k, k2)] = line[(k2, k)] = off
                if off is None:
                    continue
                if off > 0.5:
                    seeds.add(k2)
                    outside.add(k)
                elif off < -0.5:
                    seeds.add(k)
                    outside.add(k2)
                elif rv[k] is not None and rv[k2] is not None:
                    seeds.add(k if rv[k] > rv[k2] else k2)
        outside -= seeds
        # 泛洪：从种子出发，穿过没有边界线的边。
        # 主城建筑会挡住部分边界线（屋顶盖住上边），所以只允许扩散到亮度不低于领地地面、
        # 且比线外地面更亮的格子（领地地面有一层提亮；开局只有主城时，线外的草地就挡住泛洪）。
        seed_v = [rv[k] for k in seeds if rv[k] is not None]
        floor_v = (np.percentile(seed_v, 75) - 8) if seed_v else 0
        out_v = [rv[k] for k in outside if rv[k] is not None]
        if out_v:
            floor_v = max(floor_v, float(np.median(out_v)) + 5)
        owned = set(seeds)
        stack = list(seeds)
        while stack:
            k = stack.pop()
            for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                k2 = (k[0] + di, k[1] + dj)
                if k2 not in self.tiles or k2 in owned or line.get((k, k2)) is not None:
                    continue
                if rv[k2] is None or rv[k2] < floor_v:
                    continue
                owned.add(k2)
                stack.append(k2)
        # 填洞：被领地包围的 2x2 块（通常是主城建筑，挡住了亮度判断）也算领地
        for (i, j) in list(self.tiles):
            block = [(i + a, j + b) for a in (0, 1) for b in (0, 1)]
            if any(k not in self.tiles for k in block) or all(k in owned for k in block):
                continue
            ring = [(i - 1, j), (i - 1, j + 1), (i + 2, j), (i + 2, j + 1),
                    (i, j - 1), (i + 1, j - 1), (i, j + 2), (i + 1, j + 2)]
            if all(k in owned for k in ring if k in self.tiles) and sum(k in self.tiles for k in ring) >= 6:
                owned.update(block)
        for k in owned:
            self.tiles[k].owned = True
        self._lines = line

    # ---- 分类 ----
    def _classify(self, seasons):
        b = bank()
        for t in self.tiles.values():
            if t.blocked or t.owned:
                continue
            if not self._is_frontier(t):
                continue
            res, lv, sc = b.classify(self.img, t.x, t.y, seasons=seasons)
            if sc >= 0.80:
                t.res, t.level, t.score = res, lv, sc
            else:
                t.score = sc
            if self._is_water(t):
                t.blocked = True

    def _is_water(self, t):
        s = int(TILE)
        roi = self.img[t.y + 8:t.y + s - 8, t.x + 8:t.x + s - 8]
        if roi.size == 0:
            return False
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, np.array([80, 60, 60]), np.array([110, 255, 255]))
        return blue.mean() / 255 > 0.5

    def _is_frontier(self, t):
        for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            o = self.tiles.get((t.i + di, t.j + dj))
            if o is not None and o.owned:
                return True
        return False

    def find_city(self):
        """主城是 2x2、被建筑挡住的领地块：在已占领格里找平均地面亮度最低的 2x2 块。
        返回其左上格 (i, j)，找不到返回 None。"""
        best = None
        for (i, j), t in self.tiles.items():
            block = [self.tiles.get((i + a, j + b)) for a in (0, 1) for b in (0, 1)]
            # 被 UI 挡住的格子不算：顶部的深色横幅会被误认成主城
            if any(o is None or o.blocked for o in block):
                continue
            ring = [self.tiles.get(k) for k in ((i - 1, j), (i - 1, j + 1), (i + 2, j), (i + 2, j + 1),
                                                 (i, j - 1), (i + 1, j - 1), (i, j + 2), (i + 1, j + 2))]
            n_owned = sum(o.owned for o in block) + sum(1 for o in ring if o is not None and o.owned)
            if n_owned < 4:
                continue
            vs = [self._ring_v(o) for o in block]
            if any(v is None for v in vs):
                continue
            # 主城是青灰色屋顶；打下来的村子、山贼营寨是黄褐色，也是暗的 2x2，别认错
            t0 = block[0]
            roi = self.img[t0.y:t0.y + int(2 * TILE), t0.x:t0.x + int(2 * TILE)].astype(int)
            if ((roi[:, :, 0] > roi[:, :, 2] + 15) & (roi[:, :, 0] > 90)).mean() < 0.08:
                continue
            v = sum(vs) / 4
            if best is None or v < best[0]:
                best = (v, (i, j))
        if best is None:
            return None
        # 主城建筑应明显比领地地面暗（开局只有主城时没有别的领地可比，靠屋顶颜色判断）
        bi, bj = best[1]
        block = {(bi + a, bj + b) for a in (0, 1) for b in (0, 1)}
        owned_v = [self._ring_v(t) for k, t in self.tiles.items() if t.owned and k not in block]
        owned_v = [v for v in owned_v if v is not None]
        if owned_v and best[0] > np.percentile(owned_v, 75) - 15:
            return None
        return best[1]

    def frontier(self):
        return [t for t in self.tiles.values() if not t.owned and not t.blocked and self._is_frontier(t)]

    def owned_tiles(self):
        return [t for t in self.tiles.values() if t.owned]

    def debug_image(self):
        out = self.img.copy()
        for t in self.tiles.values():
            s = int(TILE)
            if t.owned:
                cv2.rectangle(out, (t.x + 3, t.y + 3), (t.x + s - 3, t.y + s - 3), (0, 255, 0), 1)
            elif not t.blocked and self._is_frontier(t):
                color = {"grain": (0, 220, 255), "tree": (0, 160, 0), "stone": (200, 200, 200)}.get(t.res, (255, 0, 255))
                cv2.rectangle(out, (t.x + 3, t.y + 3), (t.x + s - 3, t.y + s - 3), color, 2)
                label = "%s%d %.2f" % ((t.res or "land")[:2], t.level, t.score)
                cv2.putText(out, label, (t.x + 4, t.y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1)
        return out
