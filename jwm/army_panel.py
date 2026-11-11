"""“所有军队”面板解析：编队状态、位置、兵力、血量、兵种构成、行军倒计时。

移植自旧版 live.py 的纯图像/解析部分（坐标常量是真机逐个量出来的，勿随意改）。
供 recruit.read_armies 调用，给网页编队显示提供更完整的信息。
"""
import logging
import re
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# 编队状态词。“待机中”这种由游戏原样给出，脚本能读懂的就这些
STATE_WORDS = r"待机中|行军中|驻守中|返回中|攻占中|攻打中|采集中|屯田中|治疗中|休整中|守家中|增援中|撤退中"

_HUNT = r"^编队\s*(\d+)$"

# 士兵头像在卡片里的位置（本线军队面板真机实测，2026-11-10）：
#   名字锚点 y=353 时，头像行 y=378~448（中心 ≈ ny+60），
#   横向 9 个头像中心 = 79 / 147 / ... 间距 68，块宽 37。
# 注意：旧版 live.py 的 UNIT_X0=69 在这版界面上偏左 10px，会截到卡片留白。
UNIT_X0 = 79
UNIT_DX = 68
UNIT_DY = 60
UNIT_MAX = 9
# 头像取样窗口（半宽/半高）。实测头像块 37x35，这里取 19 半宽把完整头像包进来。
UNIT_HW = 19
UNIT_HH = 18

# 兵种头像的主色特征
UNIT_SILVER = "长枪兵"   # 银灰头盔
UNIT_BROWN = "连弩兵"    # 棕色斗笠


def avatar_at(img, i, ny):
    """第 i 个头像的取样框；越界返回 None。"""
    y = ny + UNIT_DY
    x = UNIT_X0 + UNIT_DX * i
    if x + UNIT_HW > img.shape[1] or y + UNIT_HH > img.shape[0] or x - UNIT_HW < 0 or y - UNIT_HH < 0:
        return None
    return x, y, img[y - UNIT_HH:y + UNIT_HH, x - UNIT_HW:x + UNIT_HW]


def unit_strip(img, ny):
    """把这张卡片「名字下方那一整条士兵区」截下来，供网页显示。

    固定截满 9 个兵位的宽度，不按实际兵数动态裁：
    这样每张编队卡片图一样宽、行与行能对齐，一眼就能横向比较各队人数和兵种；
    按实际兵数裁的话，3 个兵的图很短、9 个兵的很长，看起来七零八落。

    实测：士兵区 y = ny+25 ~ ny+95（高 70）。左边这条要按像素砍干净：
      x=0~19   卡片自己的深色/彩色描边
      x=20~41  卡片左侧那道更白的米黄高光（比兵位底色白一档，留着很脏）
      x=42 起  正常的兵位底色
    所以从 42 起裁，图直接从兵位底色开始，左边不留白条。9 个兵位到 x≈648。
    网页里统一按高度缩放，各队图一样宽、大小一致。
    """
    y1, y2 = ny + 25, ny + 95
    h, w = img.shape[:2]
    # 屏幕底部有「招募…」横幅压在卡片上，那时截出来是错的：不截，等它滚到中间
    if y1 < 0 or y2 > 1080 or ny > 1000:
        return None
    if y2 - y1 < 20:
        return None
    x1, x2 = 42, min(w, 648)
    block = img[y1:y2, x1:x2]
    if block.size == 0:
        return None
    # 自检：这一条里应有“等间距头像块”，没有就说明截错区域
    hsv = cv2.cvtColor(block, cv2.COLOR_BGR2HSV)
    fg = (hsv[:, :, 1] >= 60) | (hsv[:, :, 2] <= 100)
    col = fg.mean(axis=0)
    on = (col > 0.25).astype(int)
    runs, prev = 0, 0
    for v in on:
        if v and not prev:
            runs += 1
        prev = v
    if runs < 2:
        return None
    return block


def unit_mix(img, ny):
    """数一数这张卡片里每个兵种几个：{"长枪兵": 4, "连弩兵": 2}。

    用**头像模板匹配**识别兵种，不再按颜色猜：
    实测同一兵种的头像内部差异 ≈0，不同兵种之间差异 59~71，颜色阈值根本分不开
    （这正是之前“兵种构成都不对”的原因）。
    模板放在 assets/unit_icons/，文件名就是兵种名（unit_长枪兵.png）。
    认不出的记成“其他兵种”，宁可说不认识也不瞎猜。
    """
    templates = _unit_templates()
    out = {}
    for i in range(UNIT_MAX):
        a = avatar_at(img, i, ny)
        if a is None:
            break
        _, _, roi = a
        if cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2].std() < 12:
            continue  # 纯色块 = 空位，不是兵
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        if hsv[:, :, 2].mean() < 100 and (hsv[:, :, 1] >= 80).mean() < 0.05:
            continue  # 灰暗剪影 = 空位
        name = _match_unit(roi, templates)
        out[name] = out.get(name, 0) + 1
    return out


_TPL_CACHE = {"dir": None, "items": []}


def _imread_unicode(path):
    """读图片，支持中文路径。

    cv2.imread 在 Windows 上遇到非 ASCII 路径会直接失败（返回 None），
    而本项目路径里有中文（用户名/文档），所以必须走 fromfile + imdecode。
    """
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if not buf.size:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    except OSError:
        return None


def _unit_templates():
    """加载 assets/unit_icons/unit_<名字>.png 作为兵种模板。"""
    global _TPL_CACHE
    d = Path(__file__).resolve().parent.parent / "assets" / "unit_icons"
    if _TPL_CACHE["dir"] == str(d) and _TPL_CACHE["items"]:
        return _TPL_CACHE["items"]
    items = []
    if d.exists():
        for p in sorted(d.glob("unit_*.png")):
            im = _imread_unicode(p)
            if im is None or not im.size:
                continue
            name = p.stem[len("unit_"):]
            items.append((name, cv2.resize(im, (32, 30), interpolation=cv2.INTER_AREA).astype(np.int16)))
    _TPL_CACHE = {"dir": str(d), "items": items}
    return items


def _match_unit(roi, templates, thr=14.0):
    """把头像和模板比，返回最像的兵种名；都不像就“其他兵种”。"""
    if not templates:
        return "其他兵种"
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    a = cv2.resize(g, (32, 30), interpolation=cv2.INTER_AREA).astype(np.int16)
    best, score = "其他兵种", 999.0
    for name, tpl in templates:
        d = float(np.abs(a - tpl).mean())
        if d < score:
            best, score = name, d
    return best if score <= thr else "其他兵种"


def _row(img, status_y):
    """士兵行下沿的血条：数红色段 = 士兵数，红/（红+灰）= 血量。"""
    best = None
    for y in range(max(0, status_y - 72), max(1, status_y - 16)):
        row = img[y, 40:690].astype(int)
        red = (row[:, 2] > 175) & (row[:, 1] < 130) & (row[:, 0] < 125)
        gray = (abs(row[:, 2] - row[:, 1]) < 12) & (row[:, 2] > 105) & (row[:, 2] < 180)
        if red.sum() + gray.sum() >= 20 and (best is None or red.sum() + gray.sum() > best[1].sum() + best[2].sum()):
            best = (y, red, gray)
    if not best:
        return None, None
    _, red, gray = best
    segs, run = 0, 0
    for v in red:
        if v:
            run += 1
        else:
            if run >= 8:
                segs += 1
            run = 0
    if run >= 8:
        segs += 1
    total = int(red.sum() + gray.sum())
    return segs or None, (float(red.sum()) / total if total else None)


def parse_armies(sc):
    """解析“所有军队”面板。返回 (编队列表, 拥有数, 上限)。

    每项含 name/pos/state/timer/soldiers/hp/chests/mix/busy/ny。
    锚点优先用“编队N”名字；玩家改过名时退化用状态词定位卡片。
    """
    boxes = sc.boxes
    cap = owned = None
    for b in boxes:
        m = re.search(r"拥有军队\s*\(?\s*(\d+)\s*/\s*(\d+)", b.text)
        if m:
            owned, cap = int(m.group(1)), int(m.group(2))
            break

    anchors = []  # (状态行y, 名字, 位置)
    for b in boxes:
        m = re.match(_HUNT, b.text.strip())
        if not m:
            continue
        name, ny = b.text.strip(), b.center[1]
        pos = ""
        for o in boxes:
            if o is b:
                continue
            if abs(o.center[1] - ny) <= 22 and o.center[0] > 250:
                pos = o.text.strip()
                break
        anchors.append((ny, name, pos))

    # 第二遍：改过名的编队（如「挨揍」）名字不是「编队N」，直接按“左半边的卡片名字行”识别。
    # 名字行的特征：x < 200、y > 300、2~6 个汉字、不是状态词、不是位置、不是顶部按钮文字。
    # 真机的名字都贴在卡片最左边（x≈60~130），位置文字则靠右且通常带「地(坐标)」，
    # 所以这里必须收紧：旧条件 x<250 把「木头地」「主城」这类位置/地名也当成了编队名，
    # 日志里出现「6 支（拥有 5）」就是这么来的。
    used = [a[0] for a in anchors]
    for b in boxes:
        t = b.text.strip()
        if b.center[0] >= 200 or b.center[1] < 300:
            continue
        if not (1 <= len(t) <= 6) or not re.fullmatch(r"[\u4e00-\u9fa5]+", t):
            continue
        if re.search(STATE_WORDS, t) or re.search(r"拥有军队|招募|选择|建筑|所有军队|领土|战报|图鉴", t):
            continue
        if re.search(r"地$|城$|营$|场$|矿$|林$|山$|湖$|河$|海$|田$", t):
            continue   # 地图格名/建筑名，不是编队名
        ny = b.center[1]
        if any(abs(ny - y) < 80 for y in used):
            continue
        pos = ""
        for o in boxes:
            if abs(o.center[1] - ny) <= 22 and o.center[0] > 250:
                pos = o.text.strip()
                break
        anchors.append((ny, t, pos))
        used.append(ny)

    anchors.sort(key=lambda a: a[0])
    out = []
    for i, (ny, name, pos) in enumerate(anchors):
        # 卡片纵向范围：本卡名字上下各留一点，上界要够到状态行（有的卡状态在名字上方）
        y2 = anchors[i + 1][0] if i + 1 < len(anchors) else ny + 180
        state, timer, chests = "", "", 0
        for b in boxes:
            # 卡片纵向范围：状态可能在名字下方（常规卡片，+40 起）
            # 也可能在名字上方（「挨揍」那张，ny-80 起），两头都要看。
            if not (ny - 80 <= b.center[1] <= y2):
                continue
            t = b.text.strip()
            if (not state and b.center[0] < 300 and len(t) <= 10
                    and b.center[1] != ny
                    and re.search(STATE_WORDS, t)):
                m2 = re.match(r"^(.*?)(\d+:\d{1,2}(?::\d{1,2})?)$", t)
                if m2 and m2.group(1).strip():
                    state, timer = m2.group(1).strip(), m2.group(2)
                else:
                    state = t
            cm = re.search(r"获得宝箱\s*[xX×]\s*(\d+)", t)
            if cm:
                chests = int(cm.group(1))
        # 血条/兵力只在卡片完整可见时读：名字太靠下（y>1000）说明被底部横幅压着，
        # 硬读会把横幅上的红字当成血条，数出错误的兵力（实测「挨揍」被读成 5，实际 9）。
        sold, hp = (None, None) if ny > 1000 else _row(sc.img, ny + 119)
        if not state:
            # 整屏 OCR 偶尔会漏掉卡片左下那行小字，单独把这块再识别一次
            try:
                from .vision import OCR
                for e in OCR.read(sc.img, (30, ny + 85, 330, ny + 160)):
                    t2 = (e.text or "").strip()
                    if not t2 or len(t2) > 10 or not re.search(r"[一-龥]", t2):
                        continue
                    m3 = re.match(r"^(.*?)(\d+:\d{1,2}(?::\d{1,2})?)$", t2)
                    if m3 and m3.group(1).strip():
                        state, timer = m3.group(1).strip(), m3.group(2)
                    else:
                        state = t2
                    break
            except Exception:
                pass
        if not state and "主城" in pos:
            state = "待机中"
        mix = {} if ny > 1000 else unit_mix(sc.img, ny)
        out.append({
            "name": name,
            "pos": pos,
            "state": state,
            "timer": timer,
            "soldiers": sum(mix.values()) or (sold or 0),
            "hp": round(hp, 3) if hp is not None else None,
            "chests": chests,
            "busy": bool(state) and "待机" not in state,
            "ny": ny,
            "mix": mix,
            "units_img": unit_strip(sc.img, ny),
        })
    return out, owned, cap
