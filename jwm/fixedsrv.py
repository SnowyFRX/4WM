"""把固定地图（apk 解出的整张地形）暴露给网页。

放在 route_map.py 外面单独一个模块，避免主文件继续膨胀。
"""
import os
import threading

from . import fixedmap

_LOCK = threading.Lock()
_CACHE = {"maps": None, "root": None}


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_maps():
    """惰性载入所有固定地图（约 1-2 秒，只做一次）。"""
    with _LOCK:
        root = _root()
        if _CACHE["maps"] is not None and _CACHE["root"] == root:
            return _CACHE["maps"]
        maps = fixedmap.load_all(os.path.join(root, "data", "maps"),
                                 apk_path=os.path.join(root, "apk", "jiuwanmu.apk"))
        _CACHE["maps"] = maps
        _CACHE["root"] = root
        return maps


def info():
    """给网页的固定地图元信息：有哪些图、尺寸、构成统计。"""
    maps = load_maps()
    out = {"maps": [], "available": bool(maps)}
    for name in sorted(maps):
        m = maps[name]
        out["maps"].append({
            "name": name,
            "width": m.w,
            "height": m.h,
            "stats": m.stats(),
        })
    return out


def png(path_tail, query):
    """按路径与查询参数渲染一段地图。

    path_tail: 形如 "maps_14"
    query:     {"x0": .., "y0": .., "w": .., "h": .., "scale": .., "flip": 0|1}

    x0/y0/w/h 都是**屏幕行序**里的格坐标；flip=1 时后端自己换算成地图 y。
    """
    maps = load_maps()
    name = path_tail.strip("/")
    m = maps.get(name)
    if m is None:
        raise KeyError("没有这张地图：%s" % name)

    def num(key, default):
        try:
            return int(query.get(key, default))
        except (TypeError, ValueError):
            return default

    x0 = max(0, min(m.w - 1, num("x0", 0)))
    y0 = max(0, min(m.h - 1, num("y0", 0)))
    w = max(1, min(m.w - x0, num("w", m.w)))
    h = max(1, min(m.h - y0, num("h", m.h)))
    # 放大倍数放到 48：要能一格格点选。可见格数会随倍数自动变少，
    # 所以「格数 × 倍数」不会爆，但还是要兜个上限防止有人手改 URL 拖垮进程。
    scale = max(1, min(48, num("scale", 1)))
    if w * h * scale * scale > 4000 * 4000:
        scale = max(1, int((4000 * 4000) / max(1, w * h)) ** 0.5)
    flip = num("flip", 0) != 0
    return fixedmap.render_png(m, region=(x0, y0, x0 + w, y0 + h),
                               scale=scale, flip=flip)


def tile(name, x, y):
    """一格的全部信息（网页点击时用）。"""
    m = load_maps().get(name)
    if m is None:
        return None
    return m.info(x, y)


def guess(x, y, radius=8):
    """**已停用**：以前用「主城周围 type=6 的格数」猜本局是哪张图。

    这个前提是错的：type=6（id=16 / id=93）是不可通行的地形障碍，
    不是建筑，主城也不在固定地形里。既然判据不存在，就不该继续猜 ——
    猜错整张图的后果比「让用户自己选一次」严重得多。

    现在固定返回「无法判断」，网页据此提示用户手选并记住选择。
    """
    maps = load_maps()
    return {"x": x, "y": y, "radius": radius,
            "ranking": [{"name": n, "building": 0} for n in sorted(maps)],
            "pick": None,
            "why": "apk 里没有能可靠判定「本局用哪张图」的字段，请手动选择（选过会记住）。"}
