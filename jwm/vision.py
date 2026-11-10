"""图像识别：模板匹配 + OCR。"""
import logging
import os
import re
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "assets", "templates")

_tpl_cache = {}


def imread(path, flags=cv2.IMREAD_UNCHANGED):
    # cv2.imread 不支持中文路径，用 imdecode
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, flags)


def template(name):
    """读取 assets/templates/<name>.png（带透明通道时生成 mask）。"""
    if name not in _tpl_cache:
        path = os.path.join(TEMPLATE_DIR, name + ".png")
        img = imread(path)
        if img is None:
            raise FileNotFoundError(path)
        mask = None
        if img.ndim == 3 and img.shape[2] == 4:
            alpha = img[:, :, 3]
            if alpha.min() < 250:
                mask = cv2.merge([alpha, alpha, alpha])
            img = img[:, :, :3]
        _tpl_cache[name] = (img, mask)
    return _tpl_cache[name]


@dataclass
class Match:
    x: int  # 中心点
    y: int
    score: float
    w: int
    h: int

    @property
    def center(self):
        return self.x, self.y


def match(img, name, region=None, threshold=0.85, *, flip=None):
    """在 img 的 region=(x1,y1,x2,y2) 内找模板，返回最佳 Match 或 None。"""
    tpl, mask = template(name)
    if flip is not None:
        tpl = cv2.flip(tpl, flip)
        if mask is not None:
            mask = cv2.flip(mask, flip)
    ox, oy = 0, 0
    if region:
        x1, y1, x2, y2 = region
        img = img[y1:y2, x1:x2]
        ox, oy = x1, y1
    if img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
        return None
    method = cv2.TM_CCORR_NORMED if mask is not None else cv2.TM_CCOEFF_NORMED
    res = cv2.matchTemplate(img, tpl, method, mask=mask)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return None
    h, w = tpl.shape[:2]
    return Match(ox + loc[0] + w // 2, oy + loc[1] + h // 2, float(score), w, h)


def exists(img, name, region=None, threshold=0.85):
    return match(img, name, region, threshold) is not None


def color_ratio(img, region, lo, hi):
    """region 内 BGR 落在 [lo, hi] 的像素比例。"""
    x1, y1, x2, y2 = region
    roi = img[y1:y2, x1:x2]
    m = cv2.inRange(roi, np.array(lo, np.uint8), np.array(hi, np.uint8))
    return float(np.count_nonzero(m)) / max(1, m.size)


# ---------------- OCR ----------------
@dataclass
class TextBox:
    text: str
    score: float
    box: tuple  # (x1, y1, x2, y2)

    @property
    def center(self):
        return (self.box[0] + self.box[2]) // 2, (self.box[1] + self.box[3]) // 2


class OCR:
    _engine = None

    @classmethod
    def engine(cls):
        if cls._engine is None:
            from rapidocr import RapidOCR

            log.info("加载 OCR 模型（首次较慢）...")
            cls._engine = RapidOCR(params={"Global.log_level": "error", "Global.use_cls": False})
        return cls._engine

    @classmethod
    def read(cls, img, region=None, min_score=0.5):
        ox, oy = 0, 0
        if region:
            x1, y1, x2, y2 = region
            img = img[y1:y2, x1:x2]
            ox, oy = x1, y1
        res = cls.engine()(img)
        out = []
        if res is None or res.boxes is None:
            return out
        for box, txt, sc in zip(res.boxes, res.txts, res.scores):
            if sc < min_score:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append(TextBox(txt, float(sc), (int(min(xs)) + ox, int(min(ys)) + oy, int(max(xs)) + ox, int(max(ys)) + oy)))
        return out


def find_text(boxes, pattern):
    """在 OCR 结果里找第一个匹配 pattern（正则或子串）的文本框。"""
    rx = re.compile(pattern)
    for b in boxes:
        if rx.search(b.text):
            return b
    return None


def find_all_text(boxes, pattern):
    rx = re.compile(pattern)
    return [b for b in boxes if rx.search(b.text)]
