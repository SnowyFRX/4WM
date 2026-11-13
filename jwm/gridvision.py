"""网格识别核心：HSV 掩膜 → 形态学 → 轮廓 / 霍夫 → 网格参数。

为什么重写
----------
旧做法（mapview.green_mask）：把每个像素和写死的 `(77,132,54)` 比 BGR 曼哈顿距离，
小于 40 就算「领界线」。三个硬伤：

  1. 写死绝对色值。实测 19 张真机截图，地面是 HSV≈(47..49, 97..101, 186..190)，
     领界线是 (69,162,134)。**色相只差 20**，但饱和度高一截、亮度低一截。
     BGR 距离把「色相接近」和「明暗接近」混成一个数，树冠、灌木、
     山石阴影里都有大量落在阈值里的像素。
  2. 换季/换光照就直接崩。地面一亮，距离整体跨过 40，整条线断成碎点。
  3. 只看颜色，不看形状。树是圆团、线是细长条，这个信息完全没用上。

新做法（五步，每步都能单独关掉或调参）
--------------------------------------
  1. `line_mask`：先把地面主色（众数）量出来，再**相对**它做 inRange。
     判据是「比地面更饱和 + 更暗 + 色相在绿色区间」，不写死绝对颜色。
  2. `clean_mask` / `axis_mask`：3x3 闭运算补 1~2px 断口；
     1xN 的横竖开运算磨掉零散斑点、只留长笔画（无位移，见函数注释）。
  3. `quad_cells`：findContours + approxPolyDP 找四边形，面积按中位数做
     **相对**过滤 —— 大建筑（海蜇皮那种占 2x2 的）和小噪点一起剔掉。
  4. `hough_lines`：Canny + HoughLinesP 找横竖直线，合并共线段。
     它是「颜色分不开」时的备选，也是投影的补充（短线段投影抓不到）。
  5. `fit_lines`：把线位置拟合成 p = phase + k·tile，格宽扫描用圆周集中度
     打分，最小二乘精修。相位从这一步直接读出来。

实测结论（19 张真机截图，2026-11-13）
------------------------------------
这游戏**只画领地外框**，格子本身没有逐格描边 —— 所以闭环四边形很少成立，
主路径是「线拟合」，轮廓那一路用于「有闭环就交叉验证」和剔除大建筑。
另外「隔一格才画一条线」会让 40 和 80 都完全说得通（半格歧义），
`_best_tile` 里专门处理了这种情况，见那里。

调参指南（改之前先看这里）
--------------------------
真正要动的只有三个数，都在文件头部：

  LINE_S_OFFSET  领界线比地面「饱和多少」。掩膜里的线太细/断 → 调小（15→10）；
                 树冠、粮田边缘被吸进来 → 调大（15→22）。
  LINE_V_OFFSET  领界线比地面「暗多少」。漏检 → 调小（35→25）；
                 地面本身的纹理（草地斑块）被当成线 → 调大（35→45）。
  LINE_HUE       绿色色相区间。默认 (28,100)。粮田黄 H≈27、水蓝 H≈105 在区间外；
                 如果换到偏青的皮肤，把上限抬到 110。

改完直接跑：python -m jwm.gridvision logs/shots/xxx.png
它会打印量出来的地面色 / 实际用的阈值 / 两条轴的拟合结果，并把掩膜、
轮廓、霍夫线画成图存到 logs/_gridvision/ 下，肉眼比对最快。
"""
import logging
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger(__name__)

# ---------------- 可调参数 ----------------
# 领界线相对地面的色差（实测值见模块文档）。这三个数是唯一需要手调的东西。
LINE_S_OFFSET = 15      # 饱和度至少比地面高这么多
LINE_V_OFFSET = 35      # 亮度至少比地面低这么多
LINE_HUE = (28, 100)    # 绿色色相区间（OpenCV 的 H 是 0..179）
LINE_S_MIN = 60         # 绝对下限：灰白石头 / 白字 S 很低，不该进掩膜
LINE_V_MIN = 30         # 太黑的是 UI 阴影、描边，不该进掩膜

# 地面主色取样区（避开顶部资源栏和底部按钮栏）
GROUND_ROI = (60, 280, 660, 1040)

# 实测兜底值：2026-11-13 的 19 张真机截图，地面 HSV 众数落在
# (47..49, 97..101, 186..190)，取中位数。只有在画面里一点绿色都量不到
# （全被弹窗盖住）时才会用到它。
GROUND_FALLBACK = (47, 101, 190)

# 网格几何的合理范围（和 mapview 的 MIN_TILE/MAX_TILE 保持一致）
MIN_TILE, MAX_TILE = 40.0, 110.0


# ---------------- 1. 颜色 ----------------
def _green_mode(bgr):
    """在饱和的绿色像素里取众数；绿色像素太少返回 None。

    只让「绿色」参与投票，是为了防弹窗：主城弹窗、编队面板是白底，
    整块盖住取样区时，全画面众数会量出一片纯白 (S≈8, V=255)，
    阈值随之报废（实测就是这样把整张图刷成「全是线」的）。
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).reshape(-1, 3)[::2]
    sel = hsv[(hsv[:, 0] >= 25) & (hsv[:, 0] <= 115) &
              (hsv[:, 1] >= 40) & (hsv[:, 2] >= 30)]
    if len(sel) < 200:
        return None
    q = (sel // 8).astype(np.int32)
    key = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    vals, counts = np.unique(key, return_counts=True)
    same = sel[key == vals[int(np.argmax(counts))]]
    return tuple(int(v) for v in np.median(same, axis=0))


def ground_hsv(img, roi=GROUND_ROI):
    """量出画面里「地面」的 HSV 主色。

    用众数而不是均值：地图上还有树、水、建筑、UI，均值会被拉偏；
    地面占画面二三十个百分点，分桶取众数最稳（19 张真机图全部落在
    H=47..49 / S=97..101 / V=186..190，很集中）。

    取样区被弹窗盖住时（绿色像素还不够 200 个），改到整幅画面再量一次；
    再不行就退回实测中位数 `GROUND_FALLBACK`。
    """
    h, w = img.shape[:2]
    x1, y1, x2, y2 = roi
    crop = img[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
    for candidate in (crop, img):
        if candidate.size == 0:
            continue
        got = _green_mode(candidate)
        if got is not None:
            return got
    return GROUND_FALLBACK


def mask_bounds(ground, *, s_offset=LINE_S_OFFSET, v_offset=LINE_V_OFFSET,
                hue=LINE_HUE, s_min=LINE_S_MIN, v_min=LINE_V_MIN):
    """由地面色推出 inRange 的上下界（单独拆出来，方便打印/测试）。"""
    _, gs, gv = ground
    lo = np.array([hue[0], max(s_min, gs + s_offset), v_min], np.uint8)
    hi_v = gv - v_offset
    if hi_v <= v_min:          # 夜战/暗色皮肤：地面本身就很暗，退化成只看饱和度
        hi_v = 255
    hi = np.array([hue[1], 255, min(255, hi_v)], np.uint8)
    return lo, hi


def line_mask(img, ground=None, **tune):
    """领界线掩膜（uint8，0/255）。ground 传 None 时自动量。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if ground is None:
        ground = ground_hsv(img)
    lo, hi = mask_bounds(ground, **tune)
    return cv2.inRange(hsv, lo, hi)


# ---------------- 2. 形态学 ----------------
def clean_mask(mask, *, close_px=3, open_px=0, dilate_px=0):
    """闭运算补断口；开运算默认不开（原因见下）。

    闭运算（先膨胀后腐蚀，3x3 奇数核）把领界线被树冠、建筑切断的 1~2px
    缺口接上，奇数核是对称的，不会让线移位。

    **为什么默认不做开运算**：领界线只有 2~3px 宽。要磨掉小噪点就得用
    2x2 这种小核，而 OpenCV 的偶数核锚点只能取整（2x2 的锚点在左上），
    开运算会把整条线往左上推 1px —— 实测相位因此偏 1px，一屏十几格之后
    点击就落到隔壁格。要这种效果请用 axis_mask（1xN 的奇数核，无位移）。
    """
    m = mask
    if close_px >= 2:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((close_px, close_px), np.uint8))
    if open_px >= 3:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((open_px, open_px), np.uint8))
    if dilate_px >= 2:
        m = cv2.dilate(m, np.ones((dilate_px, dilate_px), np.uint8))
    return m


def axis_mask(mask, run=9):
    """把零散的斑点、短弧线磨掉，只留够长的横/竖笔画。

    做法是「按方向各做一次开运算，再取并集」：一条长竖线能被 (run,1) 的核
    完整保留，却会被 (1,run) 的核腐蚀干净，反之亦然；而 5x5 的草叶、
    石头上的一条亮边，两个方向都活不过 9px。开运算用的是 1xN 的**奇数核**，
    不会像 2x2 那样把整条线推 1px（细线也保得住）。

    注意它**不能**去掉大块树冠（48px 的圆盘在 (1,9) 的开运算下照样活着）。
    树冠是靠颜色挡掉的：树冠亮度比地面低不了那么多，落在 `LINE_V_OFFSET`
    的门槛之上，压根没进掩膜；进了掩膜的圆团则由轮廓那一步按面积过滤。
    """
    h = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, run), np.uint8))
    v = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((run, 1), np.uint8))
    return cv2.bitwise_or(h, v)


# ---------------- 3. 轮廓 ----------------
@dataclass
class Quad:
    """一个被判定为「格子」的四边形（外接正矩形 + 面积）。"""
    x: int
    y: int
    w: int
    h: int
    area: float

    @property
    def side(self):
        return (self.w + self.h) / 2.0

    @property
    def cx(self):
        return self.x + self.w / 2.0

    @property
    def cy(self):
        return self.y + self.h / 2.0


def _is_quad(cnt):
    """approxPolyDP 后是不是一个像格子的四边形。

    像素风边界线是阶梯状的，严格逼近经常给出 5~8 个点。所以：
    先按 2% 周长逼近，是 4 点且凸 → 通过；
    否则退回 minAreaRect —— 外接矩形是正方形、且轮廓填充率够高，也认。
    """
    peri = cv2.arcLength(cnt, True)
    if peri < 4 * MIN_TILE * 0.8:
        return False
    approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
    if len(approx) == 4 and cv2.isContourConvex(approx) and abs(cv2.contourArea(approx)) > 0:
        return True
    (cx, cy), (rw, rh), _ = cv2.minAreaRect(cnt)
    if rw < 1 or rh < 1:
        return False
    ar = max(rw, rh) / min(rw, rh)
    if ar > 1.35:
        return False
    return abs(cv2.contourArea(cnt)) >= 0.55 * rw * rh


def quad_cells(mask, *, min_side=MIN_TILE * 0.65, max_side=MAX_TILE * 1.7,
               keep=2.0, max_quads=400):
    """找格子四边形，用**相对**面积过滤掉大建筑和小噪点。

    过滤逻辑（两道）：
      1. 绝对：边长必须落在 [min_side, max_side]。海蜇皮那种 2x2 主城
         边长约 2 格，能不能过这条看 max_side；纯噪点几十平方像素直接出局。
      2. 相对：面积取中位数，只保留 [中位数/keep, 中位数*keep] 以内的。
         地图上格子是同一个尺寸，尺寸明显偏离群体的就是别的东西 ——
         这条不依赖缩放，换了 zoom 也不用重新调参。
         注意 keep 是「倍数」不是「比例」，取 1.0 会退化成「只留正好等于
         中位数的那一个」，而同一个格子的内外轮廓本来就会差几个百分点。

    返回按面积从大到小排好（调用方一般只关心中位数）。
    """
    cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    raw = []
    for c in cnts:
        if not _is_quad(c):
            continue
        x, y, w, h = cv2.boundingRect(c)
        side = (w + h) / 2.0
        if not min_side <= side <= max_side:
            continue
        area = abs(cv2.contourArea(c))
        if area <= 0:
            continue
        raw.append(Quad(x, y, w, h, area))
    if not raw:
        return []
    med = float(np.median([q.area for q in raw]))
    out = [q for q in raw if med / keep <= q.area <= med * keep]
    out.sort(key=lambda q: -q.area)
    return out[:max_quads]


def tile_from_quads(quads, *, min_count=4, max_cv=0.12):
    """由四边形边长估计格宽；证据不足或尺寸发散时返回 None。"""
    if len(quads) < min_count:
        return None
    sides = np.array([q.side for q in quads], dtype=float)
    if sides.std() > max_cv * sides.mean():
        return None
    tile = float(np.median(sides))
    return tile if MIN_TILE <= tile <= MAX_TILE else None


# ---------------- 4. 霍夫直线 ----------------
def hough_lines(mask, *, min_len=26, max_gap=4, angle_tol=12.0, merge_gap=6.0):
    """Canny + HoughLinesP 找横竖直线，返回 (竖线 x 列表, 横线 y 列表)。

    只收「接近水平/垂直」的线段（容差 angle_tol 度），斜线是山坡、
    河流那些干扰物，和网格无关。返回的是**合并后**的中心位置，
    同一堵墙上被树冠打断的几段线会并成一条。

    merge_gap 默认 6px 而不是 3：领界线是 3~4px 宽的描边，Canny 会把
    它的**两条边**各算一条线，于是同一条线报出两个相差 3~4px 的位置。
    不合并的话拟合先被这些重复点带偏，然后残差超限被判「认不出来」。
    格宽最小 40px，两条真线不可能只隔 6px，合并是安全的。
    """
    if mask.size == 0 or not mask.any():
        return [], []
    edges = cv2.Canny(mask, 50, 150)
    segs = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=max(10, min_len // 2),
                           minLineLength=min_len, maxLineGap=max_gap)
    if segs is None:
        return [], []
    vs, hs = [], []
    for x1, y1, x2, y2 in segs.reshape(-1, 4):
        dx, dy = int(x2) - int(x1), int(y2) - int(y1)
        if dx == 0 and dy == 0:
            continue
        ang = abs(np.degrees(np.arctan2(dy, dx)))       # 0=横, 90=竖
        if ang >= 90 - angle_tol:                        # 竖线
            vs.append(((x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))
        elif ang <= angle_tol:                           # 横线
            hs.append(((y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
    return merge_segments(vs, gap=merge_gap), merge_segments(hs, gap=merge_gap)


def merge_segments(segs, gap=3.0):
    """把同一条线上的碎段合并成一个中心位置（按覆盖长度加权）。

    同一条线被树冠/建筑打断后会变成几段，HoughLinesP 每段各报一个位置，
    不合并的话后面算间距全是 0 和重复值。

    **只按位置聚类**，不要求纵向区间重叠：一条线被切成上下两段时，
    两段的 y 区间本来就不挨着，加上「重叠」条件会漏合并
    （早先的写法就因此吐出四个 275.0 并存的假线）。
    格宽最小 40px，两条平行的真线不可能相距 3px 以内，按位置聚类是安全的。
    """
    if not segs:
        return []
    segs = sorted(segs, key=lambda s: s[0])
    groups = [[segs[0]]]
    for s in segs[1:]:
        g = groups[-1]
        pos = sum(x[0] * (x[2] - x[1] + 1) for x in g) / sum(x[2] - x[1] + 1 for x in g)
        if abs(s[0] - pos) <= gap:
            g.append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        wsum = sum(x[2] - x[1] + 1 for x in g)
        out.append(sum(x[0] * (x[2] - x[1] + 1) for x in g) / wsum)
    return out


# ---------------- 5. 投影（原有方法的加强版） ----------------
def projection_lines(mask, *, min_frac=0.10, max_width=6, min_len=40):
    """按列/行统计绿像素，峰值位置就是竖线/横线。

    门槛不能写死成「40 个像素」（旧实现就是这样）：树冠、灌木会在某一列
    堆出几十个绿像素，凑够 40 就多出一条假线，假线一多，间距投票就投到
    错的格宽上。这里改成「至少覆盖画面高度的 10%」—— 一条真正被画出来的
    领界线一定横跨大半个视野，而树冠堆出来的列撑不到这个长度。
    """
    m = mask > 0
    cols, rows = m.sum(axis=0), m.sum(axis=1)
    need_c = max(min_len, int(min_frac * m.shape[0]))
    need_r = max(min_len, int(min_frac * m.shape[1]))

    def runs(arr, need):
        idx = np.nonzero(arr >= need)[0]
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
        # 宽度 > max_width 的峰是「一整片绿」而不是一条线，丢掉
        return [(g[0] + g[-1]) / 2.0 for g in groups if len(g) <= max_width]

    return runs(cols, need_c), runs(rows, need_r)


def merge_positions(*lists, tol=3.0):
    """把多路来源（投影 / 霍夫）给出的位置合并去重。"""
    vals = sorted(v for lst in lists for v in lst)
    if not vals:
        return []
    out = [vals[0]]
    for v in vals[1:]:
        if v - out[-1] <= tol:
            out[-1] = (out[-1] + v) / 2.0
        else:
            out.append(v)
    return out


# ---------------- 6. 组合 ----------------
@dataclass
class GridGuess:
    tile: float
    ox: float
    oy: float
    source: str = "projection"
    vlines: list = field(default_factory=list)
    hlines: list = field(default_factory=list)
    quads: list = field(default_factory=list)
    residual: float = 0.0
    support: int = 0

    def __bool__(self):
        return self.tile > 0


@dataclass
class LineFit:
    """一维网格拟合结果：p = phase + k * tile。"""
    tile: float
    phase: float
    residual: float
    support: int
    total: int
    conc: float = 0.0        # 内点落在同一相位上的集中度（0..1），越大越可信

    @property
    def ok(self):
        return self.tile > 0 and self.support >= 2

    @property
    def quality(self):
        return (round(self.conc, 3), self.support, -self.residual)


def line_sources(mask):
    """掩膜 → 两套「竖线/横线位置」，各自独立成组。

    为什么不直接并成一组：投影给出的位置很准但条数少，霍夫给出的位置多
    但同一条线常常报两个相差 2~4px 的值。混在一起拟合，重复点会把斜率
    拽偏，残差一大就被判成「认不出来」（实测 19 张里有 8 张因此失败）。
    所以两套各自拟合，谁的结果更硬就用谁。
    """
    axes = axis_mask(mask, run=7)
    pv, ph = projection_lines(mask)
    hv, hh = hough_lines(axes if axes.any() else mask)
    return {"projection": (pv, ph), "hough": (hv, hh)}


def line_positions(mask):
    """合并两套位置（给画图/调试用，不参与拟合决策）。"""
    pv, ph = projection_lines(mask)
    axes = axis_mask(mask, run=7)
    hv, hh = hough_lines(axes if axes.any() else mask)
    return merge_positions(pv, hv), merge_positions(ph, hh)


def _estimate_tile(gaps):
    """由「相邻位置间距」估格宽，返回 (tile, 支持数)。

    间距可能是 1 格（相邻两条线），也可能是 2、3 格（中间那条被建筑或
    UI 挡掉了）。做法不是取中位数 —— 实测有几张图里混进了 UI 边缘造成的
    20~30px 假间距，中位数会被拉到 26，然后整批线就「不属于同一个网格」了。
    改成投票：把每个间距除以 1..4 当候选格宽，谁的「整格命中数」多就信谁；
    命中数打平时选大的（40 和 80 都能整除 80 的间距，真实格宽是 80）。
    """
    gaps = gaps[(gaps >= MIN_TILE * 0.55) & (gaps <= MAX_TILE * 8)]
    if len(gaps) < 2:
        return None
    cand = np.concatenate([gaps / k for k in (1, 2, 3, 4)])
    cand = np.unique(np.round(cand[(cand >= MIN_TILE) & (cand <= MAX_TILE)], 2))
    if not len(cand):
        return None
    best = None
    for t in cand:
        k = np.maximum(1, np.rint(gaps / t))
        dev = np.abs(gaps / k - t)
        inl = dev <= max(1.5, 0.035 * t)
        n = int(inl.sum())
        if n < 2:
            continue
        score = (n, t)                       # 命中数优先，平手取更大的格宽
        if best is None or score > best[0]:
            best = score, t
    if best is None:
        return None
    t = best[1]
    k = np.maximum(1, np.rint(gaps / t))
    dev = np.abs(gaps / k - t)
    inl = dev <= max(1.5, 0.035 * t)
    # 用命中的这批间距再精修一次
    t = float(np.median(gaps[inl] / k[inl]))
    return t, int(inl.sum())


def _circular_phase(positions, tile):
    """位置 mod tile 的圆周平均，返回 (相位, 集中度 0..1)。

    集中度 = 单位向量的合成长度。所有线都落在同一相位时是 1；
    完全随机时接近 0。它就是「这个格宽靠不靠谱」的天然打分。
    """
    ang = 2 * np.pi * (np.asarray(positions, dtype=float) % tile) / tile
    v = np.exp(1j * ang)
    m = v.mean()
    return float((np.angle(m) / (2 * np.pi)) * tile) % tile, float(abs(m))


def _tile_score(p, t):
    """某个格宽对这批线位置的「吻合度」：(内点数, 内点集中度, 相位)。

    先按圆周相位把每条线归到最近的整数格，再数有多少条落在容差内。
    集中度只用内点算 —— 一条 UI 边框混进来不该把正确的格宽否掉。
    """
    phase, _ = _circular_phase(p, t)
    k = np.rint((p - phase) / t)
    dev = np.abs(p - (phase + k * t))
    inl = dev <= max(1.6, 0.06 * t)
    n = int(inl.sum())
    if n < 2:
        return None
    ang = 2 * np.pi * (p[inl] % t) / t
    return n, float(abs(np.exp(1j * ang).mean())), phase


def _best_tile(positions, *, step=0.2, min_count=3, prior=None, margin=0.02):
    """在 [MIN_TILE, MAX_TILE] 里扫一遍，找让所有线对齐得最好的格宽。

    打分 = (内点数, 内点集中度)。不用「间距命中数」：那个会偏袒大格宽
    （容差按格宽比例给，格宽越大越松），实测把 80.5 认成 83.6，差 4%，
    走到边上就是半格。

    **半格歧义**：地图隔一格才画一条边界线是常态，此时 40 和 80 的内点数、
    集中度全都一样 —— 数学上分不出来。这种情况取**大的**：把 80 当成 40，
    整张图格子翻倍、点哪都不对；把 40 当成 80，只是相邻两格并成一格。
    prior（上次校准的格宽）给了且对得上时，优先听 prior 的。
    """
    p = np.asarray(sorted(positions), dtype=float)
    if p.size < min_count:
        return None
    span = p[-1] - p[0]
    scored = []
    for t in np.arange(MIN_TILE, MAX_TILE + 1e-9, step):
        if span < 1.2 * t:      # 至少要跨两格，否则「一条线自己跟自己对齐」也满分
            continue
        s = _tile_score(p, t)
        if s is not None:
            scored.append((s[0], s[1], float(t), s[2]))
    if not scored:
        return None

    most = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == most]
    best_conc = max(s[1] for s in top)
    near = [s for s in top if s[1] >= best_conc - margin]

    # 去掉「别人的一半」：80 和 40 都说得通时，留着小的会让格宽缩一半
    keep = [s for s in near
            if not any(abs(s2[2] - k * s[2]) <= 0.05 * k * s[2]
                       for s2 in near for k in (2, 3))]
    if not keep:
        keep = near
    # 上次校准的格宽给了就先认它 —— 变焦是渐进的，同一档缩放里让格宽
    # 贴着先验走，比每帧重新投票更稳（不会左右抖 1px）。
    # 只允许在「和最优候选几乎一样齐」的那些里挑，否则先验会把格宽拽跑。
    tight = [s for s in keep if s[1] >= best_conc - 0.005] or keep
    if prior:
        near_prior = [s for s in tight if abs(s[2] - prior) <= 0.08 * prior]
        if near_prior:
            s = min(near_prior, key=lambda s: abs(s[2] - prior))
            return s[2], s[3]
    n, conc, t, phase = max(tight, key=lambda s: (round(s[1], 4), s[2]))
    return t, phase


def fit_lines(positions, min_support=4, prior=None, fixed_tile=None):
    """把一组线位置拟合成「等比数列」：p = phase + k·tile。

    两步：
      1. 扫描候选格宽，用圆周集中度挑出最可能的那个（见 _best_tile）；
         给了 fixed_tile 就跳过这一步，直接用给定的格宽（第二轮重拟合用）。
      2. 用最小二乘把格宽精修到 0.1px 以内，同时剔掉 UI 边框这类外点。

    为什么非要做第 2 步：一屏十几条线铺开六七百像素，直接量间距的误差
    有 ±1.5px，而相位差 1px，往边上走四十格点击就偏半格。

    min_support 默认 4：线上的点少于四条时，「等差网格」这个假设根本没被
    验证过（三条线总能完美拟合出某个格宽），认了也是瞎猜。已知格宽、只求
    相位时降成 2 就行 —— 那时格宽不是从这批点来的。
    """
    p = np.sort(np.asarray(list(positions), dtype=float))
    if p.size < 2:
        return None
    need = 2 if fixed_tile else min_support
    if fixed_tile:
        tile = float(fixed_tile)
    else:
        pick = _best_tile(p, prior=prior) if p.size >= 3 else None
        if pick is not None:
            tile = pick[0]
        else:
            est = _estimate_tile(np.diff(p))
            if est is None:
                return None
            tile = est[0]

    idx = np.arange(p.size)
    a = float(_circular_phase(p, tile)[0])
    tol = max(1.6, 0.06 * tile)
    for _ in range(5):
        q = p[idx]
        k = np.rint((q - a) / tile)
        keep = np.abs(q - (a + k * tile)) <= tol
        if int(keep.sum()) < need:
            return None
        q2, k2 = q[keep], k[keep]
        if len(np.unique(k2)) < 2:
            return None
        # 只拿内点拟合：外点（UI 边框、弹窗边缘）参与最小二乘会把斜率拽偏，
        # 偏了之后下一轮 k 归错格，越迭代越离谱。
        A = np.column_stack([np.ones_like(k2), k2])
        (a, b), *_ = np.linalg.lstsq(A, q2, rcond=None)
        if not np.isfinite(b) or b <= 0:
            return None
        tile = float(b)
        tol = max(1.6, 0.06 * tile)
        idx = idx[keep]
        if keep.all():
            break

    q = p[idx]
    k = np.rint((q - a) / tile)
    pred = a + k * tile
    residual = float(np.sqrt(np.mean((q - pred) ** 2)))
    support = int(np.count_nonzero(np.abs(q - pred) <= max(1.6, 0.06 * tile)))
    if support < need or not MIN_TILE <= tile <= MAX_TILE:
        return None
    if residual > max(2.0, 0.10 * tile):     # 拟合散得太开：这批线不属于同一个网格
        return None
    phase, conc = _circular_phase(q, tile)   # 内点的集中度：这次拟合有多「齐」
    return LineFit(round(tile, 2), round(phase, 2), residual, support, int(p.size), conc)


def _axis_fit(sources, axis, tile=None, prior=None):
    """在「两套来源」里给某一条轴挑最好的拟合。

    axis='v' 取竖线、'h' 取横线。给了 tile 就按该格宽重拟合（fail-safe：
    保证两条轴最终用的是**同一个**格宽，否则相位会各算各的、对不上）。
    """
    best = None
    for _name, (vs, hs) in sources.items():
        f = fit_lines(vs if axis == "v" else hs, prior=prior, fixed_tile=tile)
        if f is None:
            continue
        if best is None or f.quality > best.quality:
            best = f
    return best


def _phase_of(positions, tile):
    """一组线位置 → 网格相位（落在 [0, tile) 的那个偏移）。

    用圆周平均而不是普通平均：位置是 mod tile 的，直接平均会在
    tile-1 和 0 之间被拉断（比如线在 53 和 1，平均成 27，差了半格）。
    """
    if positions is None or not len(positions) or tile <= 0:
        return None
    ang = np.array([2 * np.pi * (p % tile) / tile for p in positions])
    v = np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())
    return round(float((v / (2 * np.pi)) * tile) % tile, 2)


def guess(img, mask=None, *, ground=None, clean=True, prior=None):
    """一次算出 (格宽, 相位)，按「轮廓 → 投影/霍夫 → 四边形」逐级降级。

    哪一路先出结果就用哪一路，并记在 GridGuess.source 里，方便排查
    「这次为什么认得不对」。三个来源的可靠性排序：

      projection  竖线/横线各拟合一遍，两条轴的格宽还能互相印证，最可信
      quad        找到闭环格子就直接量边长，精度最高，但实机很少见闭环
      none        认不出来，调用方应该沿用上次的校准值，别乱改

    实测（19 张真机截图）：这游戏只画领地外框，格子本身没有逐格描边，
    所以闭环四边形大多不成立 —— 主路径是线拟合，轮廓用于「有就交叉验证」。
    """
    if mask is None:
        mask = line_mask(img, ground=ground)
    thin = clean_mask(mask) if clean else mask

    # 轮廓用「薄」掩膜：方向过滤会把像素风的阶梯转角切掉，反而让闭环断开
    quads = quad_cells(thin)
    sources = line_sources(thin)
    vx, hy = line_positions(thin)
    q_tile = tile_from_quads(quads)

    # 第一轮：每条轴的每种来源各拟合一回，按「集中度」挑出最硬的格宽。
    # 注意不能按「支持数」挑：线多的那条轴往往正是被干扰物污染的（实测
    # 有张图竖轴糊成 40、横轴干净到 0.997，按条数挑就把 80 认成 40 了）。
    first = {axis: _axis_fit(sources, axis, prior=prior) for axis in ("v", "h")}
    fx, fy = first["v"], first["h"]
    best = _pick_axis(fx, fy)
    if best is None:
        if q_tile and quads:
            ox = _phase_of([q.x for q in quads], q_tile)
            oy = _phase_of([q.y for q in quads], q_tile)
            if ox is not None and oy is not None:
                return GridGuess(q_tile, ox, oy, "quad", vx, hy, quads, 0.0, len(quads))
        return GridGuess(0.0, 0.0, 0.0, "none", vx, hy, quads)

    tile = best.tile
    # 第二轮：两条轴都用这个格宽重拟合。格宽必须统一，否则相位各算各的 ——
    # 竖轴那条糊成半格时，它会给出一个「mod 40」的相位，套到 80 的网格上就偏了。
    fx = _axis_fit(sources, "v", tile=tile) or fx
    fy = _axis_fit(sources, "h", tile=tile) or fy
    tile = _combine_tile(fx, fy)

    ox = fx.phase if fx else _phase_of(vx, tile)
    oy = fy.phase if fy else _phase_of(hy, tile)
    if ox is None:
        ox = _phase_of([q.x for q in quads], tile)
    if oy is None:
        oy = _phase_of([q.y for q in quads], tile)
    if ox is None or oy is None:
        return GridGuess(0.0, 0.0, 0.0, "none", vx, hy, quads)
    fits = [f for f in (fx, fy) if f]
    return GridGuess(tile, ox, oy,
                     "lines" if len(fits) == 2 else "lines/单轴",
                     vx, hy, quads,
                     max(f.residual for f in fits),
                     sum(f.support for f in fits))


def _pick_axis(fx, fy):
    """两条轴的初次拟合谁更可信；顺便收掉「半格歧义」。

    半格歧义：一格可能是另一格的 2、3 倍（地图隔一格才画一条边界线时，
    40 和 80 都完全说得通，两者集中度都能到 0.99）。数学上分不出来，
    就按「取大的」处理 —— 把 80 当成 40，整张图的格子会翻倍，点哪都不对；
    把 40 当成 80，只是相邻两格合并成一格，错得轻得多。
    """
    if not fx or not fy:
        return fx or fy
    big, small = (fx, fy) if fx.tile >= fy.tile else (fy, fx)
    alias = any(abs(big.tile - k * small.tile) <= 0.05 * k * small.tile for k in (2, 3))
    if alias and min(fx.conc, fy.conc) >= 0.95:
        return big
    return fx if fx.quality > fy.quality else fy


def _combine_tile(fx, fy):
    """两条轴的格宽合成一个：相差 6% 以内就加权平均，否则信更「齐」的那条。"""
    if fx and fy:
        if abs(fx.tile - fy.tile) <= 0.06 * max(fx.tile, fy.tile):
            w1, w2 = max(fx.conc, 0.01), max(fy.conc, 0.01)
            return round((fx.tile * w1 + fy.tile * w2) / (w1 + w2), 2)
        return fx.tile if fx.quality > fy.quality else fy.tile
    one = fx or fy
    return one.tile


# ---------------- 7. 调参用预览 ----------------
def preview(img, out_dir, tag="preview"):
    """把每一步的中间结果画出来存盘，调阈值时肉眼比对用。"""
    os.makedirs(out_dir, exist_ok=True)
    ground = ground_hsv(img)
    lo, hi = mask_bounds(ground)
    raw = line_mask(img, ground)
    thin = clean_mask(raw)
    quads = quad_cells(thin)
    vx, hy = line_positions(thin)
    fit_v, fit_h = fit_lines(vx), fit_lines(hy)

    over = img.copy()
    over[thin > 0] = (0, 0, 255)
    for q in quads:
        cv2.rectangle(over, (q.x, q.y), (q.x + q.w, q.y + q.h), (255, 0, 255), 2)
    for x in vx:
        cv2.line(over, (int(x), 0), (int(x), img.shape[0]), (255, 0, 0), 1)
    for y in hy:
        cv2.line(over, (0, int(y)), (img.shape[1], int(y)), (255, 0, 0), 1)

    def save(name, arr):
        p = os.path.join(out_dir, "%s_%s.png" % (tag, name))
        cv2.imencode(".png", arr)[1].tofile(p)
        return p

    paths = [save("mask", raw), save("clean", thin), save("overlay", over)]
    log.info("地面 HSV=%s  阈值 S[%d,%d] V[%d,%d]  四边形 %d  竖线 %d  横线 %d",
             ground, lo[1], hi[1], lo[2], hi[2], len(quads), len(vx), len(hy))
    for name, fit in (("竖", fit_v), ("横", fit_h)):
        if fit:
            log.info("  %s线拟合：格宽 %.2f 相位 %.2f 残差 %.2f 支持 %d/%d",
                     name, fit.tile, fit.phase, fit.residual, fit.support, fit.total)
        else:
            log.info("  %s线拟合：失败", name)
    return paths, (ground, lo, hi, quads, vx, hy)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print("用法: python -m jwm.gridvision <截图路径> [输出目录]")
        raise SystemExit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join("logs", "_gridvision")
    data = np.fromfile(src, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit("读不到图片：%s" % src)
    paths, (g, lo, hi, quads, vx, hy) = preview(image, dst, tag=os.path.basename(src)[:-4])
    gg = guess(image)
    print("网格估计：tile=%.2f 相位=(%.2f, %.2f) 来源=%s" % (gg.tile, gg.ox, gg.oy, gg.source))
    for p in paths:
        print("  ", p)
