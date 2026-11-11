"""列表翻页：逐屏读到底，到尽头就停。

为什么单独抽出来：
  军队面板一屏只放 4 张卡，第 5 支往下的宝箱/状态必须滑动才能看到。
  以前每个调用点各写一套滑动（固定像素、固定页数），滑到尽头后还在继续滑，
  同一批卡片被重复解析，日志里就出现「6 支（拥有 5）」这种自相矛盾的读数。

约定：
  - grab() 返回一屏（有 .img，用于判断画面是否还在变化）；
  - swipe() 执行一次滑动；
  - on_page(screen) 处理这一屏；返回 False 表示「够了，别再滑」；
  - 画面滑不动了 / 达到页数上限就停。

返回实际读了几屏。
"""
import logging

log = logging.getLogger(__name__)


def _fingerprint(screen):
    img = getattr(screen, "img", None)
    if img is None:
        return id(screen)
    try:
        import numpy as np

        a = np.asarray(img)
        if a.size:
            # 抽样取特征，避免整帧比较的开销；只要「是否变化」可靠就够
            step = max(1, a.size // 4096)
            return a.ravel()[::step].tobytes()
    except Exception:
        log.debug("翻页指纹计算失败", exc_info=True)
    return id(screen)


def walk(grab, swipe, on_page, max_pages=6, same=None):
    """逐屏下滑，返回读到的屏数。

    same(a, b) 可注入，默认按图像指纹比较（滚动没生效就停）。
    """
    same = same or (lambda a, b: a == b)
    pages = 0
    last = None
    for _ in range(max(1, int(max_pages))):
        sc = grab()
        mark = _fingerprint(sc)
        if last is not None and same(last, mark):
            break            # 滑动没带动列表 = 到底了（这一屏已经读过）
        pages += 1
        last = mark
        if on_page(sc) is False:
            break
        swipe()
    return pages


def reach_bottom(grab, swipe, on_page, max_pages=6):
    """一直滑到底为止（用于「先把整张列表读完」这类场景）。"""
    return walk(grab, swipe, lambda sc: on_page(sc) is not False, max_pages=max_pages)
