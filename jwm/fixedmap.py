"""用 apk 里的固定地图数据渲染整张地图，给网页用。

和旧方案的区别
--------------
旧方案：脚本一屏屏截图 → 拼接 → 每格靠 OCR 认。
  受缩放/弹窗/光照影响，覆盖率只有当前视野，拼图还会错位。

新方案：apk 里有整张 600×600 的固定地图（每格是 land 表 id），
  直接渲染成图。全图一次出，资源/等级/产量全部精确已知。

每格能读出什么（靠 land + landAttr 表）
--------------------------------------
  资源类型（粮/木/石）、等级、三项产量、是否可占领、
  血量、占地尺寸（1x1 / 2x2）、消耗体力、屯田时间、掉落
"""
import json
import os

# 地块配色（渲染用）。按资源类型区分，和游戏观感接近，便于对照。
PALETTE = {
    "water": (74, 144, 199),       # 海：蓝
    "grain": (232, 214, 122),      # 粮地：黄
    "timber": (86, 158, 92),       # 木地：绿
    "stone": (168, 168, 168),      # 石地：灰
    "building": (110, 96, 86),     # 建筑占地：深灰褐
    "decor": (150, 186, 140),      # 地形装饰位：浅绿
    "empty": (206, 200, 158),     # 荒地：土黄底色
    "other": (196, 196, 180),      # 其他地形
    "unknown": (255, 0, 255),      # 未知（醒目，方便发现问题）
}

# land 表的 type -> 资源类别
TYPE_KIND = {3: "grain", 4: "timber", 5: "stone", 6: "building",
             7: "water", 8: "decor", 9: "other"}

# 水面 id（600×600 地图四角都是 71，已实测验证）
WATER_IDS = frozenset([69, 70, 71] + list(range(631, 663)))

MAP_DIR_NAME = "maps"


def land_kind(entry, value):
    """把 land 表条目归成渲染用的类别。

    关键（实测 2026-11-13）：type=3/4/5 各自都有 lv=1..5，
    但 **lv=1 时三项产量都是 1/1/1** —— 那是普通荒地，不是资源地。
    只有 lv>=2 才是真正的资源地（粮地 lv2 产量 4，lv5 产量 14）。

    以前把 lv=1 也算成资源地，整张图渲染出来就是密集噪点，
    和游戏里「成片的森林、零散的粮田」完全对不上。
    """
    if value is None:
        return "unknown"
    if value in WATER_IDS:
        return "water"
    if not entry:
        return "unknown"
    t = entry.get("type")
    lv = entry.get("lv") or 0
    if t == 6:
        return "building"
    if t == 7:
        return "water"
    if t == 8:
        return "decor"
    if t in (3, 4, 5):
        if lv <= 1:
            return "empty"
        return TYPE_KIND[t]
    return "other"


class FixedMap:
    """一张固定地图 + land 表，负责查询与渲染。"""

    def __init__(self, name, grid, land_by_id, land_attr=None):
        self.name = name
        self.grid = grid            # 二维 list[list[int]]
        self.h = len(grid)
        self.w = len(grid[0]) if grid else 0
        self.land = land_by_id or {}
        self.land_attr = land_attr or {}

    # ---------- 查询 ----------
    def value(self, x, y):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return None
        return self.grid[y][x]

    def kind(self, v):
        """资源类别：water/grain/timber/stone/building/decor/other/unknown。"""
        if v is None:
            return "unknown"
        if v in WATER_IDS:
            return "water"
        return land_kind(self.land.get(v), v)

    def info(self, x, y):
        """一格的全部已知信息（给网页点击显示）。"""
        v = self.value(x, y)
        if v is None:
            return None
        e = self.land.get(v, {})
        attr = self.land_attr.get(v, {})
        return {
            "x": x, "y": y, "value": v,
            "kind": self.kind(v),
            "level": e.get("lv", 0),
            "occupyable": bool(e.get("occupy")),
            "yield": {"grain": e.get("cereal", 0),
                      "timber": e.get("timber", 0),
                      "stone": e.get("stone", 0)},
            "hp": attr.get("hp"),
            "size": attr.get("size"),
            "stamina": attr.get("need_stamina"),
            "tonden": attr.get("tonden_time"),
        }

    def stats(self):
        """整张图的构成统计（给网页做概览）。"""
        counts = {}
        for row in self.grid:
            for v in row:
                k = self.kind(v)
                counts[k] = counts.get(k, 0) + 1
        return counts

    def building_around(self, x, y, r=8):
        """(x, y) 周围 r 格内有多少「建筑占地」格（land.type == 6）。

        用途：判断「本局用的是哪张图」。
        主城是玩家落脚点，周围一定有一片城墙/建筑（type=6）。
        每张图的主城位置是预设的，所以在真实主城坐标处，
        正确的那张图会有一大片 type=6，别的图不会。
        """
        n = 0
        for yy in range(max(0, y - r), min(self.h, y + r + 1)):
            row = self.grid[yy]
            for xx in range(max(0, x - r), min(self.w, x + r + 1)):
                if (self.land.get(row[xx]) or {}).get("type") == 6:
                    n += 1
        return n


def _load_land_table(apk_reader):
    """读 land / landAttr 表，转成 {id: row} 方便查。"""
    land = {e["id"]: e for e in (apk_reader.load("land") or []) if "id" in e}
    attr = {e["id"]: e for e in (apk_reader.load("landAttr") or []) if "id" in e}
    return land, attr


def load_map_from_json(path, land, land_attr):
    """从 data/maps/*.json 读一张已导出的地图。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if "cells" not in d:
        raise ValueError("%s 不是地图文件" % path)
    return FixedMap(d.get("name") or os.path.basename(path), d["cells"], land, land_attr)


def load_land_from_apk(apk_path):
    """只取 land / landAttr 两张表（不需要整包解析）。"""
    from .apkmap import ApkReader
    r = ApkReader(apk_path)
    land, attr = _load_land_table(r)
    r.close()
    return land, attr


def load_all(map_dir, apk_path=None, land=None, land_attr=None):
    """载入 data/maps 下所有地图。

    land 表优先从 apk 读（原始、最全）；apk 不在就退化成「只有类别、没有等级」。
    """
    if land is None:
        if apk_path and os.path.exists(apk_path):
            land, land_attr = load_land_from_apk(apk_path)
        else:
            land, land_attr = {}, {}
    out = {}
    if not os.path.isdir(map_dir):
        return out
    for fn in sorted(os.listdir(map_dir)):
        if not (fn.startswith("maps_") and fn.endswith(".json")):
            continue
        try:
            m = load_map_from_json(os.path.join(map_dir, fn), land, land_attr or {})
        except Exception:
            continue
        out[m.name] = m
    return out


# ---------------- 渲染 ----------------

def render_array(fixed, region=None, scale=1, flip=False):
    """把地图渲染成 HxWx3 的 numpy 数组（RGB）。

    region: (x0, y0, x1, y1) 闭开区间；None 表示整张图。
    scale: 每格放大倍数（整数）。
    flip:  y 轴方向。False = 数组行序直接当屏幕行（行 0 在最上面）；
           True = 游戏坐标 y 向上，屏幕行 s 对应地图行 h-1-s。

    为什么要有 flip：游戏里 world() 是 (home.y - y) * tile，说明 y 越大越靠上。
    但 maps_N 的行序到底是「屏幕行」还是「游戏 y」，apk 里没有明说。
    两种都渲染一次，让用户在地图上对照主城周边一眼确认，比猜稳。
    """
    import numpy as np

    x0, y0, x1, y1 = region or (0, 0, fixed.w, fixed.h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(fixed.w, x1), min(fixed.h, y1)
    w, h = max(0, x1 - x0), max(0, y1 - y0)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if w == 0 or h == 0:
        return img
    for yy in range(h):
        s = y0 + yy                       # 屏幕行
        row = fixed.grid[fixed.h - 1 - s] if flip else fixed.grid[s]
        for xx in range(w):
            kind = fixed.kind(row[x0 + xx])
            img[yy, xx] = PALETTE.get(kind, PALETTE["unknown"])
    if scale > 1:
        img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
    return img


def render_png(fixed, region=None, scale=1, flip=False):
    """渲染成 PNG 字节（给 HTTP 直接下发）。"""
    import cv2
    arr = render_array(fixed, region=region, scale=scale, flip=flip)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return buf.tobytes()


def region_meta(fixed, region=None):
    """渲染区域对应的元信息，前端用来做坐标换算。"""
    x0, y0, x1, y1 = region or (0, 0, fixed.w, fixed.h)
    return {"name": fixed.name, "x0": x0, "y0": y0,
            "x1": min(x1, fixed.w), "y1": min(y1, fixed.h),
            "mapWidth": fixed.w, "mapHeight": fixed.h}


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    maps = load_all(os.path.join(root, "data", "maps"),
                    apk_path=os.path.join(root, "apk", "jiuwanmu.apk"))
    print("载入地图:", list(maps))
    for name, m in maps.items():
        st = m.stats()
        print("  %-8s %dx%d  %s" % (name, m.w, m.h,
                                    " ".join("%s=%d" % kv for kv in
                                             sorted(st.items(), key=lambda kv: -kv[1])[:5])))
    if len(sys.argv) > 1:
        name = sys.argv[1]
        m = maps[name]
        png = render_png(m, region=(0, 0, 200, 200), scale=4)
        out = os.path.join(root, "logs", "fixedmap_%s.png" % name)
        with open(out, "wb") as f:
            f.write(png)
        print("样例渲染:", out, len(png), "字节")
