"""读顶部资源区：粮/木/石 + 石头下方的储备资源（铁、经验书、钉子/卷轴）。

为什么四条资源要分两种读法：
  粮/木/石 在顶栏有文字行 + 进度条，且带「当前+超出」两段，直接 OCR 数字即可；
  铁/经验书/钉子、卷轴 没有进度条，只有小图标 + 白字数字竖排在石头下面，
  字小、贴着图标，正屏 OCR 经常漏，必须放大 + 二值化才能读稳。

为什么要读这些：
  造兵、练兵、修建筑、打造装备都消耗资源。资源不够时反复点只会被游戏拒绝，
  既浪费时间又刷无意义日志。脚本据实际资源决定能不能做，做不了就把队列暂停。

实测坐标（720x1280 竖屏，雷电模拟器）：
  粮文本 y≈30   条 y=44   木文本 y≈81  条 y=94   石文本 y≈131  条 y=145
  石头下方的储备资源竖排（图标在左 x≈8~48，数字在 x≈54~62）：
     铁       y≈185      经验书  y≈225      钉子/卷轴  y≈266
  行距 40px。右侧 x≈304 是奖励点（不是资源，单独算）。
"""
import logging
import re
from pathlib import Path

from .vision import ROOT

log = logging.getLogger(__name__)

ICON_DIR = Path(ROOT) / "assets" / "res_icons"

# 三条基础资源的文本行（y 分段，避免窄矩形把数字框漏掉）
BASE_BANDS = {"grain": (16, 58), "wood": (66, 108), "stone": (116, 160)}

# 条子 y 坐标（读百分比用，和 build.resource_fill 一致）
RES_BARS = {"grain": 44, "wood": 94, "stone": 145}

# 石头下方的储备资源：界面从上到下的真实顺序（按图标确认）
# 第1行 白色铁块=铁、第2行 红黄纸卷=卷轴、第3行 白灰锥=钉子。
# 经验书不在这一列（它属于另一处显示，不在这里读）。
# 0 的那一项游戏不画，后面的会整体上移，所以按“第几行”顺次对应。
STOCK_ORDER = ["iron", "scroll", "nail", "book"]  # 铁 / 卷轴 / 钉子 /（预留）
STOCK_NAMES = {"iron": "铁", "scroll": "卷轴", "nail": "钉子", "book": "经验书"}

# 储备资源竖列的大致范围：只在这里找图标行
STOCK_AREA = (2, 150, 56, 340)


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


def read_stock(sc):
    """读石头下方的储备资源：铁 / 卷轴 / 钉子（界面从上到下的顺序）。

    为什么不能只用整屏 OCR：7 和 1 这种一位数太小，正屏识别经常漏掉，只剩第一个数字。
    实测必须把这一列（x 0~120, y 150~340）裁出来放大 3 倍 + 二值化，三行才都能读出来
    （26@185 / 7@225 / 1@266，行距 40，首行 185）。
    0 的那项游戏不画、后面的上移，所以按“第几行”顺次对应。
    """
    out = {k: None for k in STOCK_ORDER}
    rows = []

    def collect(items, scale=1.0, oy=0.0):
        for b in items:
            cx, cy = b.center[0] / scale, b.center[1] / scale + oy
            if not (30 <= cx <= 100) or not (150 <= cy <= 340):
                continue
            v = _number(b.text)
            if v is None:
                continue
            if rows and abs(cy - rows[-1][0]) < 20:
                if cx < rows[-1][1]:
                    rows[-1] = (cy, cx, v)
                continue
            rows.append((cy, cx, v))

    # 先用整屏结果（快）
    collect(sc.boxes)
    # 行数不够就做放大 + 二值化的精读（1、7 这种一位数必须走这条路）
    if len(rows) < 3:
        try:
            import cv2
            x1, y1, x2, y2 = 0, 150, 120, 340
            crop = sc.img[y1:y2, x1:x2]
            if crop.size:
                big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
                inv = 255 - cv2.inRange(gray, 170, 255)
                from .vision import OCR
                found = OCR.read(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR))
                rows = []
                collect(found, scale=3.0, oy=float(y1))
        except Exception:
            log.debug("储备资源精读失败", exc_info=True)

    rows.sort()
    for i, (cy, cx, v) in enumerate(rows[:len(STOCK_ORDER)]):
        expect = 185 + 40 * i
        if abs(cy - expect) <= 22:
            out[STOCK_ORDER[i]] = v
        else:
            log.debug("储备资源第%d行 y=%.0f 偏离预期 %d，忽略", i + 1, cy, expect)
    return out


def _row_number(img, cy):
    """针对单行做放大 + 二值化后读数字（正屏 OCR 漏读时用）。"""
    try:
        import cv2
    except ImportError:
        return None
    crop = img[max(0, cy - 20):cy + 20, 40:200]
    if crop.size == 0:
        return None
    big = cv2.resize(crop, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    inv = 255 - cv2.inRange(gray, 170, 255)
    from .vision import OCR
    for e in OCR.read(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR)):
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
