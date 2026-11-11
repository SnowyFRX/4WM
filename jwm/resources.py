"""读顶部资源区：粮/木/石 + 石头下方的四种储备资源（经验书、铁、卷轴、钉子）。

为什么储备资源要单独处理：
  粮/木/石 在顶栏有文字 + 进度条，直接 OCR 数字即可；
  储备资源没有进度条，只有小图标 + 白字数字竖排在石头下面，字小容易漏读，
  而且**数量为 0 的那项游戏不画、下面的整体上移**，
  所以必须靠图标认种类（assets/res_icons/），不能按“第几行”硬对。

为什么要读这些：
  造兵、练兵、修建筑、打造装备都消耗资源。资源不够时反复点只会被游戏拒绝，
  既浪费时间又刷无意义日志。脚本据实际资源决定能不能做，做不了就把队列暂停。

实测坐标（720x1280 竖屏，雷电模拟器）：
  粮文本 y≈30  条 y=44   木文本 y≈81  条 y=94   石文本 y≈131  条 y=145
  储备资源竖排：图标 x≈4~52，数字 x≈45~80，行距约 40，首行 y≈185
  右侧 x≈304 是奖励点（不是资源，单独算）。
"""
import logging
import re
from pathlib import Path

import cv2
import numpy as np

from .vision import ROOT

log = logging.getLogger(__name__)

ICON_DIR = Path(ROOT) / "assets" / "res_icons"

# 三条基础资源的文本行（y 分段，避免窄矩形把数字框漏掉）
BASE_BANDS = {"grain": (16, 58), "wood": (66, 108), "stone": (116, 160)}

# 条子 y 坐标（读百分比用，和 build.resource_fill 一致）
RES_BARS = {"grain": 44, "wood": 94, "stone": 145}

# 石头下方的储备资源：界面固定顺序（从上到下）
# 经验书 → 铁 → 卷轴 → 钉子。数量为 0 的那项游戏不画，下面的整体上移。
# 所以**绝不能按“第几行”对应**：只剩经验书和铁时第 1 行是经验书；
# 只剩铁/卷轴/钉子时第 1 行是铁。必须靠图标认种类。
STOCK_ORDER = ["book", "iron", "scroll", "nail"]
STOCK_NAMES = {"book": "经验书", "iron": "铁", "scroll": "卷轴", "nail": "钉子"}

# 储备资源竖列范围：图标在 x 4~52，数字在 x 45~80
STOCK_AREA = (2, 165, 90, 420)  # 从石头行（中心 y≈131、OCR 框可到 160）下方开始
# 储备资源最多 4 行，行距 40；数量为 0 的游戏不画、下面的整体上移，
# 所以这里只作为“图标已经认出来、但数字 OCR 漏了”时的补读位置。
STOCK_ROWS = (185, 225, 265, 305)


def _number(text):
    """从 "21500+251" / "9,400" / "21500" 这类文本里取主数值。

    游戏显示「当前+超出仓库」两段，主数值是加号前面那段。
    """
    if not text:
        return None
    cleaned = text.replace(" ", "").replace(",", "").replace("，", "")
    # 优先：加号前那一段（这才是手上的量）
    m = re.match(r"^(\d+)\+", cleaned)
    if m:
        return int(m.group(1))
    # 没有加号：整段数字
    m = re.match(r"^(\d+)$", cleaned)
    if m:
        return int(m.group(1))
    # OCR 认花了：取最长数字串
    nums = re.findall(r"\d{2,}", cleaned)
    return int(max(nums, key=len)) if nums else None


def read_amounts(sc):
    """读三条基础资源的当前数量。返回 {"grain": int|None, "wood": ..., "stone": ...}。

    按 OCR 框的纵向位置归类（资源那一行在各自的 y 附近），比按矩形区域更稳：
    数字框的中心 y 有时会偏出窄区域，但不会偏出整条资源行。
    """
    out = {k: None for k in BASE_BANDS}
    for b in sc.boxes:
        x, y = b.center
        if not (60 <= x <= 300):
            continue
        for name, (y1, y2) in BASE_BANDS.items():
            if not (y1 <= y <= y2):
                continue
            v = _number(b.text)
            if v is not None and (out[name] is None or v > out[name]):
                out[name] = v
            break
    return out


def stock_rows(img):
    """保留给模板采集用：找图标行。日常读数不再依赖它（见 read_stock）。"""
    try:
        import cv2  # noqa: F401
        import numpy as np
    except ImportError:
        return []
    x1, y1, x2, y2 = STOCK_AREA
    band = img[y1:y2, x1:x2]
    if band.size == 0:
        return []
    contrast = (band.max(axis=2).astype(int) - band.min(axis=2).astype(int)).mean(axis=1)
    if not len(contrast):
        return []
    base = float(np.median(contrast))
    thr = base + max(10.0, base * 0.15)
    rows, run = [], None
    for i, c in enumerate(contrast):
        if c >= thr:
            if run is None:
                run = i
        elif run is not None:
            if i - run >= 10:
                rows.append((y1 + run, y1 + i))
            run = None
    if run is not None and len(contrast) - run >= 10:
        rows.append((y1 + run, y1 + len(contrast)))
    return rows


_ICON_CACHE = {"dir": None, "items": []}


def _read_png_gray(path):
    """读灰度图，支持中文路径（cv2.imread 在非 ASCII 路径下会失败）。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if buf.size else None
    except OSError:
        return None


def _icon_templates():
    """加载 assets/res_icons/icon_<name>.png 作为图标模板。"""
    global _ICON_CACHE
    d = ICON_DIR
    if _ICON_CACHE["dir"] == str(d) and _ICON_CACHE["items"]:
        return _ICON_CACHE["items"]
    items = []
    if d.exists():
        for name in STOCK_ORDER:
            p = d / ("icon_%s.png" % name)
            if not p.exists():
                continue
            im = _read_png_gray(p)
            if im is None or not im.size:
                continue
            items.append((name, cv2.resize(im, (40, 36), interpolation=cv2.INTER_AREA).astype(np.int16)))
    _ICON_CACHE = {"dir": str(d), "items": items}
    return items


def _match_icon(patch, templates, thr=35.0):
    """拿一行的图标和模板比，返回 (名字, 分数)。分数越小越像。

    阈值定 35：真实的同一资源跨帧差异实测最高 31.2（铁），必须放过去；
    但没做过模板的卷轴/钉子会被“最像的经验书”吸到 39 左右（实测 39.8），
    35 能把它挡在外面。宁可这一行留空，也不能把钉子写成经验书。
    """
    if not templates or patch is None or not patch.size:
        return None, 999.0
    g = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if patch.ndim == 3 else patch
    a = cv2.resize(g, (40, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
    best, score = None, 999.0
    for name, tpl in templates:
        d = float(np.abs(a - tpl).mean())
        if d < score:
            best, score = name, d
    return (best if score <= thr else None), score


def read_stock(sc):
    """读储备资源：经验书 / 铁 / 卷轴 / 钉子。

    做法（关键：不能按行号硬对）：
      1. 找出左侧竖列里有哪几行（每行一个数字 + 一个图标）；
      2. 每行用图标模板认出它是什么资源（assets/res_icons/icon_*.png）；
      3. 数字取自同一行。
    数量为 0 的那项游戏不画、后面的上移，所以图标识别是唯一可靠的办法。
    模板缺失时保持 None，不瞎猜。
    """
    out = {k: None for k in STOCK_ORDER}
    # 1) 找行：数字在 x 45~80；从 165 开始，排除石头行的碎片数字
    rows = []
    for b in sc.boxes:
        x, y = b.center
        if not (40 <= x <= 90) or not (STOCK_AREA[1] <= y <= STOCK_AREA[3]):
            continue
        t = b.text.strip()
        if not t.isdigit():
            continue
        if rows and abs(y - rows[-1][0]) < 20:
            continue
        rows.append((y, int(t)))
    rows.sort()

    # 小数字（1、7）正屏可能漏读，行数不足时做放大 + 二值化精读补一遍
    if len(rows) < 4:
        try:
            x1, y1, x2, y2 = 40, STOCK_AREA[1], 100, STOCK_AREA[3]
            crop = sc.img[y1:y2, x1:x2]
            if crop.size:
                big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
                inv = 255 - cv2.inRange(gray, 170, 255)
                from .vision import OCR
                extra = []
                for e in OCR.read(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR)):
                    cx, cy = e.center[0] / 3 + x1, e.center[1] / 3 + y1
                    t = (e.text or "").strip()
                    if not t.isdigit():
                        continue
                    if any(abs(cy - r[0]) < 20 for r in rows):
                        continue
                    extra.append((cy, int(t)))
                rows = sorted(rows + extra)
        except Exception:
            log.debug("储备资源精读失败", exc_info=True)

    # 数字 OCR 在白色小字上会整行漏掉（实测同一画面重读一次就能读到）。
    # 只要图标认得出来，就按固定行位补读数字，别让经验书/铁整项变成未知。
    tpls = _icon_templates()
    for cy in STOCK_ROWS:
        if any(abs(cy - r[0]) < 20 for r in rows):
            continue
        patch = sc.img[max(0, cy - 22):cy + 22, 4:52]
        name, score = _match_icon(patch, tpls)
        if name is None:
            continue
        val = _row_number(sc.img, cy)
        if val is None:
            continue
        rows.append((cy, val))
        log.debug("储备资源行 y=%d 图标=%s（差异 %.1f），数字 OCR 漏读，按图标补读为 %d",
                  cy, name, score, val)
    rows.sort()

    if not rows:
        return out

    for cy, val in rows:
        cy = int(cy)
        patch = sc.img[max(0, cy - 22):cy + 22, 4:52]
        name, score = _match_icon(patch, tpls)
        # 认不出就保留 None。不能按主色猜：顶栏石头等灰白区域也会被误判成铁，
        # 实测曾把真正的 iron=7 覆盖成 stone 行误读出的 iron=1。
        log.debug("储备资源行 y=%d 值=%d → %s（差异 %.1f）", cy, val, name or "未匹配", score)
        if name is None:
            continue
        if out[name] is None:
            out[name] = val
    return out


def _row_number(img, cy):
    """针对单行放大后读数字（正屏 OCR 漏读时用）。

    先读彩色放大图：白字在浅色地图上对比度足够，OCR 实测准确率 100%。
    二值化版本只在彩色图失败时兜底 —— 实测 40~200 宽的二值图会把数字
    整段漏掉，以前只走二值化，所以“图标认得、数字却是 None”。
    """
    try:
        import cv2
    except ImportError:
        return None
    top = max(0, cy - 40)
    crop = img[top:cy + 40, 40:200]
    if crop.size == 0:
        return None
    big = cv2.resize(crop, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    from .vision import OCR
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    inv = 255 - cv2.inRange(gray, 170, 255)
    for picture in (big, cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR)):
        for e in OCR.read(picture):
            # 80px 高的裁剪会带上相邻行（行距只有 40），只取中心在本行附近的框
            if abs(top + e.center[1] / 5 - cy) > 16:
                continue
            v = _number(e.text)
            if v is not None:
                return v
    return None


def read_fill(img):
    """顶部三条资源条的填充比例 {grain, wood, stone}（0~1，读不到为 None）。"""
    try:
        import cv2
        out = {}
        for name, y in RES_BARS.items():
            best = None
            for yy in range(y - 3, y + 4):
                row = img[yy, 57:220].astype(int)
                filled = ((row[:, 0] > 180) & (row[:, 2] < 140)) | \
                         ((row[:, 2] > 180) & (row[:, 1] < 120) & (row[:, 0] < 120))
                empty = (abs(row[:, 2] - 219) < 20) & (abs(row[:, 1] - 190) < 20) & (abs(row[:, 0] - 166) < 20)
                if filled.sum() + empty.sum() > 120:
                    ratio = filled.sum() / float(filled.sum() + empty.sum())
                    best = ratio if best is None else max(best, ratio)
            out[name] = best
        return out
    except Exception:
        log.debug("资源条读取失败", exc_info=True)
        return {}


def read_points(sc):
    """右侧那个独立的数字是奖励点（不是资源，攻占土地会消耗它）。"""
    for b in sc.boxes:
        x, y = b.center
        if 280 <= x <= 330 and y <= 60:
            v = _number(b.text)
            if v is not None:
                return v
    return None


def snapshot(sc):
    """一次调用拿到全部资源（基础三条 + 储备三项 + 奖励点 + 占比）。"""
    amounts = read_amounts(sc)
    amounts.update(read_stock(sc))
    return {"amounts": amounts, "fill": read_fill(sc.img), "points": read_points(sc)}


def lowest(fill, threshold=0.15):
    """哪种资源快见底了（占比低于 threshold）。返回资源名列表。"""
    return [k for k, v in (fill or {}).items() if v is not None and v < threshold]
