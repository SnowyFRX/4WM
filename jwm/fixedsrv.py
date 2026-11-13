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
    scale = max(1, min(8, num("scale", 1)))
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
    """按主城坐标猜「本局是哪张图」。

    依据：主城周围必有一片建筑占地（land.type=6）。玩家这局的真实主城坐标
    是已知的（state.home），拿它到每张候选图上数建筑格，哪张图在那一带有
    大片建筑，哪张就是本局用的图。分数接近时**不给结论**，让用户自己选。
    """
    best = []
    for name in sorted(load_maps()):
        m = load_maps()[name]
        if not (0 <= x < m.w and 0 <= y < m.h):
            continue
        best.append({"name": name, "building": m.building_around(x, y, radius)})
    best.sort(key=lambda e: -e["building"])
    out = {"x": x, "y": y, "radius": radius, "ranking": best, "pick": None, "why": ""}
    if not best:
        out["why"] = "主城坐标不在这几张图的范围内。"
        return out
    top = best[0]
    second = best[1]["building"] if len(best) > 1 else 0
    # 只在大幅领先时才下结论，避免「三张图都差不多」时瞎猜
    if top["building"] >= 10 and top["building"] >= second * 3:
        out["pick"] = top["name"]
        out["why"] = "主城 (%d, %d) 周围有 %d 格建筑，其他图最多 %d 格。" % (
            x, y, top["building"], second)
    else:
        out["why"] = "主城周围建筑格差别不大（%s），无法判断，请手动选。" % (
            "、".join("%s=%d" % (e["name"], e["building"]) for e in best))
    return out
