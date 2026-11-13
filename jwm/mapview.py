"""大地图解析：网格校准、领地识别、资源地图标分类、可攻占边界计算。

坐标约定：
  - 屏幕网格 (i, j)：第 i 列、第 j 行，左上角像素 = (ox + i*TILE, oy + j*TILE)
  - 游戏坐标 (x, y)：x 向右增大，y 向上增大（弹窗里显示的 (25,24) 这种）

识别核心（颜色掩膜 / 形态学 / 轮廓 / 霍夫 / 网格拟合）在 `gridvision`，
这里只负责业务：把网格参数套到格子上、认领地、分类资源、算边界。
"""
import logging
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import gridvision
from .vision import ROOT, imread

log = logging.getLogger(__name__)

TILE = 67.5  # 720x1280 下默认缩放的格子边长；按领地边线和主城选框实测更新
MIN_TILE, MAX_TILE = 40.0, 110.0
GREEN_BGR = np.array([77, 132, 54])  # 自己领地边界线颜色（只在 HSV 失效时兜底）

# 一次缩放允许的格宽变化幅度。拖一次滚轮缩放不会有 1.8 倍这种跳变，
# 出现就说明认错了（典型是「半格歧义」把 54 认成 108 或反过来），
# 这时宁可不动 TILE —— 主城选框（2x2 的 span/2）下一帧会给出准确值。
TILE_BAND = (0.70, 1.40)


def set_tile_size(value):
    """用游戏选框实测格子边长；缩放变化后资源模板也必须重建。"""
    global TILE, _bank, _bank_tile
    value = float(value)
    if not MIN_TILE <= value <= MAX_TILE:
        raise ValueError("地图格子尺寸超出可识别范围，请重新校准。")
    if abs(value - TILE) > 0.25:
        TILE = value
        _bank = None
        _bank_tile = 0.0      # 和 _bank 一起失效，避免账本与实际不一致
    return TILE

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
    x: float  # 屏幕左上角（**浮点**：格子间距是 69.5 这类小数，
              #   取整会让每格漂 0.5px，网页按真实 tile 画就对不齐）
    y: float
    owned: bool = False
    res: str = None  # grain/tree/stone/None
    level: int = 1  # 1=荒地, 2..5 资源地等级
    score: float = 0.0
    blocked: bool = False  # 水/被 UI 挡住等
    border_edges: set = field(default_factory=set)

    @property
    def center(self):
        """点击用的整数中心（手指/点击只接受整数坐标）。"""
        return int(round(self.x + TILE / 2)), int(round(self.y + TILE / 2))

    @property
    def left(self):
        """格子左上角的浮点位置，给「像素↔格子」换算用。"""
        return float(self.x)

    @property
    def top(self):
        return float(self.y)


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
_bank_tile = 0.0      # 这份模板是按哪个 TILE 缩放的


def bank():
    """资源图标模板库，按当前 TILE 缓存。

    模板是按格子尺寸预缩放的（s = tile/80），所以 TILE 变了必须重建。
    以前只建一次就永久缓存：玩家缩放地图后 TILE 从 67.5 变成 69.5，
    模板还是按 67.5 缩的，尺寸差 3%，匹配分从 0.85+ 掉到 0.6 左右，
    全部低于 0.80 阈值 → 整张地图的 res 都识别不出来。
    """
    global _bank, _bank_tile
    if _bank is None or abs(_bank_tile - TILE) > 1e-6:
        _bank = SpriteBank(TILE)
        _bank_tile = TILE
    return _bank


def green_mask(img, tol=40):
    """领界线掩膜（bool 数组）。

    主路径是 `gridvision.line_mask`：先量出地面 HSV 主色，再按
    「饱和度更高 + 更暗」做相对过滤，换季换光照都不用改参数。

    兜底：极少数皮肤下（比如夜里、滤镜）掩膜几乎全空，此时退回旧的
    BGR 距离。留着它不是为了效果，是为了「至少还能认出一点」，
    别让整张地图直接罢工。
    """
    m = gridvision.line_mask(img)
    if cv2.countNonZero(m) < m.size * 5e-4:
        log.warning("HSV 掩膜几乎为空（皮肤/光照异常），退回 BGR 距离阈值")
        d = np.abs(img.astype(np.int16) - GREEN_BGR.astype(np.int16)).sum(axis=2)
        m = np.where(d < tol, 255, 0).astype(np.uint8)
    return gridvision.clean_mask(m) > 0


def _line_centers(img):
    """竖线/横线的像素位置（调试用；拟合已经把这一步含在 guess 里了）。"""
    m = gridvision.clean_mask(gridvision.line_mask(img))
    return gridvision.line_positions(m)


def _guess(img):
    """整帧的网格估计（格宽 + 相位），把 TILE 当先验传进去。"""
    return gridvision.guess(img, prior=TILE)


def estimate_tile_size(img):
    """估计当前缩放下的格宽；证据不足返回 None。"""
    g = _guess(img)
    return g.tile if g else None


def auto_tile_size(img, guess=None):
    """检测到缩放就更新 TILE。

    只有「变化幅度合理」才采纳：一次变焦不可能让格宽跳到 1.8 倍，
    这种跳变基本都是半格歧义（54 ↔ 108）认错了。认错时宁可保持原值 ——
    主城选框（2x2 的 span/2）下一次校准会给出准确格宽。
    """
    g = guess if guess is not None else _guess(img)
    measured = g.tile if g else None
    if measured is None or abs(measured - TILE) < 2.0:
        return TILE
    ratio = measured / TILE
    if not TILE_BAND[0] <= ratio <= TILE_BAND[1]:
        log.warning("格宽估计 %.1f 与当前 %.1f 相差 %.0f%%，像认错了，先不改",
                    measured, TILE, (ratio - 1) * 100)
        return TILE
    previous = TILE
    set_tile_size(measured)
    log.info("检测到地图缩放：格宽 %.1f → %.1f", previous, TILE)
    return TILE


def estimate_phase(img, guess=None):
    """估计网格相位 (ox, oy)，找不到返回 None。

    相位是「模格宽」的，所以格宽必须和 TILE 一致才谈得上相位。自动变焦
    被 TILE_BAND 拦下时（说明这次估计八成是半格歧义），这里直接返回 None ——
    拿一个模 108 的相位去排 54 的格子，点哪都歪，不如什么都不做。
    """
    g = guess if guess is not None else _guess(img)
    auto_tile_size(img, g)
    if not g or abs(g.tile - TILE) > 0.5:
        return None
    return g.ox, g.oy


def in_ui(x, y, w=TILE, h=TILE):
    for zx1, zy1, zx2, zy2 in UI_ZONES:
        if x < zx2 and x + w > zx1 and y < zy2 and y + h > zy1:
            return True
    return False


class MapGrid:
    """一帧地图截图的解析结果。"""

    def __init__(self, img, phase=None, classify=True, seasons=None):
        self.img = img
        self._gm = None      # 掩膜只算一次：变焦 + 领地泛洪共用
        if phase is None:
            self._gm = green_mask(img)
            guess = gridvision.guess(img, mask=self._gm.astype(np.uint8) * 255, prior=TILE)
            phase = estimate_phase(img, guess=guess)
        if phase is None:
            raise ValueError("画面里没有找到自己领地的边界线")
        # 起点取到屏幕左上外一格
        self.ox = phase[0] - TILE
        self.oy = phase[1] - TILE
        H, W = img.shape[:2]
        self.cols = int((W - self.ox) // TILE) + 1
        self.rows = int((H - self.oy) // TILE) + 1
        self.tiles = {}
        for j in range(self.rows):
            for i in range(self.cols):
                x = self.ox + i * TILE
                y = self.oy + j * TILE
                # 保留浮点：间距是 69.5 这类小数，取整会让每格累积 0.5px 偏移，
                # 网页按真实 tile 排格子时就越往外越歪。点击时 center 会自己取整。
                t = TileInfo(i, j, x, y)
                if x < 0 or y < 0 or x + TILE > W or y + TILE > H or in_ui(x, y, TILE, TILE):
                    t.blocked = True
                self.tiles[(i, j)] = t
        self._flood_owned()
        if classify:
            self._classify(seasons)

    # ---- 领地 ----
    def _ring_v(self, t):
        """格子靠边一圈（避开中心图标和边界线）的地面亮度中位数。"""
        s = int(TILE)
        # t.x / t.y 是浮点几何位置，numpy 切片必须用整数
        x, y = int(round(t.x)), int(round(t.y))
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
        gm = self._gm if self._gm is not None else green_mask(self.img)
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
            res, lv, sc = b.classify(self.img, int(round(t.x)), int(round(t.y)),
                                      seasons=seasons)
            if sc >= 0.80:
                t.res, t.level, t.score = res, lv, sc
            else:
                t.score = sc
            if self._is_water(t):
                t.blocked = True

    def _is_water(self, t):
        s = int(TILE)
        # x/y 现在是浮点几何坐标，numpy 切片必须整数
        y0, x0 = int(round(t.y)), int(round(t.x))
        roi = self.img[y0 + 8:y0 + s - 8, x0 + 8:x0 + s - 8]
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
            _y0, _x0 = int(round(t0.y)), int(round(t0.x))
            roi = self.img[_y0:_y0 + int(2 * TILE), _x0:_x0 + int(2 * TILE)].astype(int)
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
            # 画图接口只接受整数坐标（t.x/t.y 是浮点几何位置）
            tx, ty = int(round(t.x)), int(round(t.y))
            if t.owned:
                cv2.rectangle(out, (tx + 3, ty + 3), (tx + s - 3, ty + s - 3), (0, 255, 0), 1)
            elif not t.blocked and self._is_frontier(t):
                color = {"grain": (0, 220, 255), "tree": (0, 160, 0), "stone": (200, 200, 200)}.get(t.res, (255, 0, 255))
                cv2.rectangle(out, (tx + 3, ty + 3), (tx + s - 3, ty + s - 3), color, 2)
                label = "%s%d %.2f" % ((t.res or "land")[:2], t.level, t.score)
                cv2.putText(out, label, (tx + 4, ty + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1)
        return out
