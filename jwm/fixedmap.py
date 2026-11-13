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

# 地块配色（渲染用）。按游戏画面取色，不是随便挑的：
#   地面（lv=1 荒地）在游戏里是**草地绿**，以前画成米色土黄，
#   整张图看着像撒了一地纸屑，和游戏里对不上。
PALETTE = {
    "water": (74, 144, 199),       # 海：蓝
    "grain": (232, 214, 122),      # 粮地：浅黄方块
    "timber": (66, 126, 70),       # 木地：深绿（树冠）
    "stone": (176, 176, 166),      # 石地：灰白（石头）
    "building": (120, 96, 86),     # 建筑占地：灰褐
    "obstacle": (138, 140, 126),   # 地形障碍（山体/丘陵，不可通行）
    "decor": (126, 168, 104),      # 地形装饰位：浅绿
    "empty": (124, 158, 92),       # 空地：草地绿（游戏底色）
    "other": (140, 152, 112),      # 其他地形
    "unknown": (255, 0, 255),      # 未知（醒目，方便发现问题）
}

# land 表的 type -> 资源类别
# 注意 type=6：实测 id=16 / id=93 都是 occupy=0、icon 为空、满地图各几千格，
# 是**不可通行的地形障碍**（apk 里的文本键 ui.land_111 / ui.land_112）。
# 以前一律叫「建筑」是错的 —— 建筑是玩家对象，不在固定地形里；
# 把它当主城更是错的（主城是动态对象，会覆盖固定地形）。
TYPE_KIND = {3: "grain", 4: "timber", 5: "stone", 6: "obstacle",
             7: "water", 8: "decor", 9: "other"}

# 水面 id（600×600 地图四角都是 71，已实测验证）
WATER_IDS = frozenset([69, 70, 71] + list(range(631, 663)))

# 装饰物（decorations_N）的五大类，按 icon 前缀分。
# 阴影（shadow，5 万条）是纯投影，画上去只会把整张图糊成脏灰色，直接丢掉。
DECO_NONE, DECO_GRASS, DECO_HILL, DECO_MOUNTAIN, DECO_LAKE = 0, 1, 2, 3, 4
DECO_BY_PREFIX = {"grass": DECO_GRASS, "hill": DECO_HILL,
                  "mountain": DECO_MOUNTAIN, "lake": DECO_LAKE}

# 装饰色。资源格不换色（资源类型才是要看的），只按地形压一点明暗；
# 荒地和装饰位则直接照地形上色 —— 这样「成片的林子/山/湖」才看得出来。
DECO_COLOR = {
    DECO_GRASS: (108, 148, 92),
    DECO_HILL: (150, 158, 122),
    DECO_MOUNTAIN: (128, 130, 122),
    DECO_LAKE: (86, 152, 196),
}
# 资源格上叠地形时乘的系数：山丘下压暗一点，草地提亮一点
DECO_SHADE = {DECO_GRASS: 1.05, DECO_HILL: 0.93, DECO_MOUNTAIN: 0.86}

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
        return "obstacle"
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

    def __init__(self, name, grid, land_by_id, land_attr=None, deco=None, deco_rects=None):
        self.name = name
        self.grid = grid            # 二维 list[list[int]]
        self.h = len(grid)
        self.w = len(grid[0]) if grid else 0
        self.land = land_by_id or {}
        self.land_attr = land_attr or {}
        self.deco = deco            # 二维 list[list[int]]，装饰大类；没有就是 None
        self.deco_rects = deco_rects or []   # [(x0,y0,x1,y1,icon)]，贴图渲染用

    # ---------- 查询 ----------
    def value(self, x, y):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return None
        return self.grid[y][x]

    def deco_at(self, x, y):
        """这一格的装饰大类（草/丘/山/湖/无）。"""
        if not self.deco:
            return DECO_NONE
        if not (0 <= x < self.w and 0 <= y < self.h):
            return DECO_NONE
        return self.deco[y][x]

    def kind(self, v):
        """资源类别：water/grain/timber/stone/building/decor/other/unknown。"""
        if v is None:
            return "unknown"
        if v in WATER_IDS:
            return "water"
        return land_kind(self.land.get(v), v)

    def color_at(self, x, y):
        """一格该画什么颜色（RGB）。

        规则：
          - 湖（装饰）压过一切：湖里不会是资源地。
          - 资源格（粮/木/石）保留资源色，只按地形压一点明暗，
            否则「木地」被草地色盖掉，就分不出哪格能占。
          - 建筑占地不动（静态建筑，装饰对它没意义）。
          - 其余（荒地/装饰位/其他）按地形上色，这样成片的林子、山、湖才看得见。
        """
        kind = self.kind(self.grid[y][x])
        base = PALETTE.get(kind, PALETTE["unknown"])
        d = self.deco_at(x, y)
        if d == DECO_LAKE:
            return DECO_COLOR[DECO_LAKE]
        if kind in ("grain", "timber", "stone"):
            f = DECO_SHADE.get(d, 1.0)
            if f == 1.0:
                return base
            return (min(255, int(base[0] * f)),
                    min(255, int(base[1] * f)),
                    min(255, int(base[2] * f)))
        if kind in ("building", "obstacle"):
            return base
        if d in DECO_COLOR:
            return DECO_COLOR[d]
        return base

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
            "deco": self.deco_at(x, y),
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
        """**已废弃**：曾经用来「按主城周围建筑密度猜本局是哪张图」。

        前提错了：type=6 的 id=16 / id=93 是不可通行的地形障碍，
        不是建筑，更不是主城（主城是动态对象，会覆盖固定地形）。
        这个启发式因此不成立，留着只会误导，接口保留只为兼容旧调用。
        """
        return 0


def _load_land_table(apk_reader):
    """读 land / landAttr 表，转成 {id: row} 方便查。"""
    land = {e["id"]: e for e in (apk_reader.load("land") or []) if "id" in e}
    attr = {e["id"]: e for e in (apk_reader.load("landAttr") or []) if "id" in e}
    return land, attr


def load_decorations(path, w, h, deco_table=None, want_rects=False):
    """把 decorations_N.json 展开成 w×h 的装饰大类矩阵（want_rects 时连同矩形一起给）。

    格式：items = [{"start": 格索引, "decoration": 装饰id, "end": 格索引}, ...]
    格索引 = y * 宽度 + x。

    start / end 是这块装饰在**地图上的两个角**（对角），要先各自拆成 (x, y)
    再取矩形范围，不能当成「线性区间」直接 range(start, end+1) —— 那样跨行的
    记录会从行尾铺到下一行行首，整张图会变成一条一条的横条纹。

    rects 里保留 icon 名，贴图渲染要用（只留大类码的话只能涂颜色）。
    阴影类直接跳过（十三万格纯投影，画上去只是把图弄脏）。
    """
    if not os.path.exists(path):
        return (None, []) if want_rects else None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return (None, []) if want_rects else None
    items = data.get("items") if isinstance(data, dict) else data
    if not items:
        return (None, []) if want_rects else None

    grid = [[DECO_NONE] * w for _ in range(h)]
    icon_grid = [[None] * w for _ in range(h)]
    anchor_grid = [[None] * w for _ in range(h)]
    table = deco_table or {}
    for rec in items:
        did = rec.get("decoration")
        entry = table.get(did) or table.get(str(did))
        if not entry:
            continue
        icon = str(entry.get("icon") or "")
        code = DECO_BY_PREFIX.get(icon.split("_")[0])
        if not code:
            continue                      # shadow 之类，跳过
        a = rec.get("start")
        b = rec.get("end")
        if a is None:
            continue
        y1, x1 = divmod(int(a), w)
        y2, x2 = divmod(int(b if b is not None else a), w)
        x0_, x1_ = (x1, x2) if x1 <= x2 else (x2, x1)
        y0_, y1_ = (y1, y2) if y1 <= y2 else (y2, y1)
        x0_ = max(0, x0_)
        y0_ = max(0, y0_)
        x1_ = min(w - 1, x1_)
        y1_ = min(h - 1, y1_)
        if x1_ < x0_ or y1_ < y0_:
            continue
        for yy in range(y0_, y1_ + 1):
            row = grid[yy]
            irow = icon_grid[yy]
            arow = anchor_grid[yy]
            for xx in range(x0_, x1_ + 1):
                row[xx] = code
                irow[xx] = icon
                arow[xx] = (x2, y2)

    if not want_rects:
        return grid

    # 按「装饰锚点」分块：同一条装饰会在它覆盖的每一格都记一条，
    # 但 end 字段是**同一个锚点**（实测一片 4×4 的山，16 条记录的 end 都是
    # (311,242)）。所以以 (icon, end) 分组，一组的格子范围就是这一张图的覆盖区。
    #
    # 以前按「相邻同图」合并，会把挨着的几块并成一大块（12×8 之类），
    # 再拿 4×4 的图去平铺，画面就成了贴瓷砖；只画一次则周围全空。
    blocks = {}
    for y in range(h):
        irow = icon_grid[y]
        for x in range(w):
            icon = irow[x]
            if not icon:
                continue
            anchor = anchor_grid[y][x]
            if anchor is None:
                anchor = (x, y)
            key = (icon, anchor)
            box = blocks.get(key)
            if box is None:
                blocks[key] = [x, y, x, y]
            else:
                if x < box[0]:
                    box[0] = x
                elif x > box[2]:
                    box[2] = x
                if y < box[1]:
                    box[1] = y
                elif y > box[3]:
                    box[3] = y
    return grid, [(b[0], b[1], b[2], b[3], k[0]) for k, b in blocks.items()]


def deco_path_for(map_dir, name):
    """maps_14 → decorations_14.json"""
    tail = name.split("_", 1)[-1] if "_" in name else name
    return os.path.join(map_dir, "decorations_%s.json" % tail)


def load_map_from_json(path, land, land_attr, deco=None):
    """从 data/maps/*.json 读一张已导出的地图。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if "cells" not in d:
        raise ValueError("%s 不是地图文件" % path)
    return FixedMap(d.get("name") or os.path.basename(path), d["cells"],
                    land, land_attr, deco)


def load_land_from_apk(apk_path):
    """只取 land / landAttr 两张表（不需要整包解析）。"""
    from .apkmap import ApkReader
    r = ApkReader(apk_path)
    land, attr = _load_land_table(r)
    r.close()
    return land, attr


def load_deco_table(map_dir, apk_path=None):
    """装饰 id → icon 对照表：优先读导出的 decoration_table.json，没有再去 apk 里取。

    有这个表，decorations_N 里的数字才能还原成「草/丘/山/湖」。
    两样都没有就返回空表 —— 那就不上地形色，不影响资源显示。
    """
    p = os.path.join(map_dir, "decoration_table.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                # JSON 的键一定是字符串，装饰 id 在 decorations_N 里是数字，
                # 这里统一转回 int，否则一条都对不上（曾经整层装饰全是「无」）
                return {int(k): v for k, v in data.items()}
        except (OSError, ValueError):
            pass
    if apk_path and os.path.exists(apk_path):
        try:
            from .apkmap import ApkReader
            r = ApkReader(apk_path)
            table = {e["id"]: {"icon": e.get("icon"), "type": e.get("type")}
                     for e in (r.load("decoration") or []) if "id" in e}
            r.close()
            return table
        except Exception:
            return {}
    return {}


def load_all(map_dir, apk_path=None, land=None, land_attr=None, with_decor=True):
    """载入 data/maps 下所有地图。

    land 表优先从 apk 读（原始、最全）；apk 不在就退化成「只有类别、没有等级」。
    with_decor 打开时顺带把装饰层展开进每张地图，网页按地形上色。
    """
    if land is None:
        if apk_path and os.path.exists(apk_path):
            land, land_attr = load_land_from_apk(apk_path)
        else:
            land, land_attr = {}, {}
    deco_table = load_deco_table(map_dir, apk_path) if with_decor else {}
    out = {}
    if not os.path.isdir(map_dir):
        return out
    for fn in sorted(os.listdir(map_dir)):
        if not (fn.startswith("maps_") and fn.endswith(".json")):
            continue
        try:
            name = fn[:-5]
            deco = None
            rects = []
            if deco_table:
                dpath = deco_path_for(map_dir, name)
                # 地图多宽得先知道才能把格索引拆成 x/y，所以先读 cells
                with open(os.path.join(map_dir, fn), encoding="utf-8") as f:
                    head = json.load(f)
                cells = head.get("cells") or []
                w = len(cells[0]) if cells else 0
                h = len(cells)
                if w and h:
                    deco, rects = load_decorations(dpath, w, h, deco_table, want_rects=True)
                m = FixedMap(head.get("name") or name, cells, land, land_attr or {}, deco, rects)
            else:
                m = load_map_from_json(os.path.join(map_dir, fn), land, land_attr or {})
        except Exception:
            continue
        out[m.name] = m
    return out


# ---------------- 渲染 ----------------

def render_array(fixed, region=None, scale=1, flip=False):
    """把地图渲染成 HxWx3 的 numpy 数组（RGB）——纯色块版本，作为贴图不可用时的兜底。

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
        my = fixed.h - 1 - s if flip else s
        row = img[yy]
        for xx in range(w):
            row[xx] = fixed.color_at(x0 + xx, my)
    if scale > 1:
        img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
    return img


class IconBank:
    """data/tiles 下按 icon 名命名的贴图（草/树/石/粮田/湖…）。

    游戏里每格其实是一张贴图，不是一块颜色。这些图从 apk 的
    tmp/image/land_0..3/ 提取，用草地主题 land_0。
    """

    def __init__(self, path):
        self.path = path
        self._raw = {}
        self._scaled = {}

    def raw(self, icon):
        if icon in self._raw:
            return self._raw[icon]
        from PIL import Image
        p = os.path.join(self.path, icon + ".png")
        im = None
        if os.path.exists(p):
            try:
                im = Image.open(p).convert("RGBA")
            except OSError:
                im = None
        self._raw[icon] = im
        return im

    def sized(self, icon, w, h):
        key = (icon, w, h)
        if key in self._scaled:
            return self._scaled[key]
        im = self.raw(icon)
        if im is not None:
            from PIL import Image
            im = im.resize((max(1, w), max(1, h)), Image.LANCZOS)
        self._scaled[key] = im
        return im


class _IconCache:
    """图标库的单例：进程内只扫一次目录。"""

    def __init__(self):
        self.bank = None
        self.checked = False

    def get(self):
        if not self.checked:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            d = os.path.join(root, "data", "tiles")
            self.bank = IconBank(d) if os.path.isdir(d) else None
            self.checked = True
        return self.bank


ICONS = _IconCache()

# 底色：草地绿（游戏里大半张图就是这个色）
GRASS_RGB = (124, 158, 92)
# 贴图按「80 像素 = 1 格」来换算。地块图正好 80×80，湖是 240×160（3×2 格），
# 山是 320×320（4×4 格）。以前把整张贴图硬塞进一格，湖和山的边缘就出现
# 一圈圈方格接缝 —— 游戏里的做法是按原始尺寸贴，相邻的图天然叠成一片。
TILE_PX_REF = 80.0


def render_sprite_image(fixed, region, scale, flip, bank):
    """贴图渲染：草地底 → 装饰贴图（按原始尺寸居中叠放）→ 每格资源贴图。"""
    from PIL import Image

    x0, y0, x1, y1 = region or (0, 0, fixed.w, fixed.h)
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(fixed.w, int(x1)), min(fixed.h, int(y1))
    w, h = max(0, x1 - x0), max(0, y1 - y0)
    if w <= 0 or h <= 0:
        return Image.new("RGB", (1, 1), GRASS_RGB)

    S = max(1, int(scale))
    img = Image.new("RGB", (w * S, h * S), GRASS_RGB)

    def sy_of(y):
        """地图行 → 屏幕行（flip 时上下翻转）。"""
        return fixed.h - 1 - y if flip else y

    # 1) 装饰：湖泊/草丛/小丘/山体。
    #
    # 一块装饰 = 一张图铺满它覆盖的那片格子（块由锚点确定，见 load_decorations）。
    # 画的时候按块的格子范围拉伸到正好铺满：贴图尺寸和块尺寸本来就是一致的
    # （4×4 的山对 4×4 的图、3×2 的湖对 3×2 的图），拉伸只是为了吃掉取整误差，
    # 保证块与块之间不留缝、也不会像贴瓷砖那样重复。
    for rx0, ry0, rx1, ry1, icon in fixed.deco_rects:
        if rx1 < x0 or rx0 >= x1 or ry1 < y0 or ry0 >= y1:
            continue
        # 区域在**屏幕行序**里的上下边界（flip 时地图 y 要翻过来）
        sy0, sy1 = (fixed.h - 1 - ry1, fixed.h - 1 - ry0) if flip else (ry0, ry1)
        if sy1 < y0 or sy0 >= y1:
            continue
        im = bank.sized(icon, max(1, (rx1 - rx0 + 1) * S), max(1, (sy1 - sy0 + 1) * S))
        if im is None:
            continue
        img.paste(im, ((rx0 - x0) * S, (sy0 - y0) * S), im)

    # 2) 资源：每格一张图，正好一格大
    rs = max(2, int(round(S)))
    half = (S - rs) // 2
    for sy in range(y0, y1):
        row = fixed.grid[sy_of(sy)]
        base = (sy - y0) * S + half
        for sx in range(x0, x1):
            e = fixed.land.get(row[sx]) or {}
            icon = e.get("icon") or ""
            if not icon:
                continue
            im = bank.sized(icon, rs, rs)
            if im is None:
                continue
            img.paste(im, ((sx - x0) * S + half, base), im)
    return img


def render_png(fixed, region=None, scale=1, flip=False):
    """渲染成 PNG 字节（给 HTTP 直接下发）。

    有 data/tiles 图标就贴图；没有就退回纯色块，至少还能用。
    """
    import cv2
    import numpy as np

    bank = ICONS.get()
    if bank is not None:
        img = render_sprite_image(fixed, region, scale, flip, bank)
        arr = np.array(img)[:, :, ::-1]                    # RGB → BGR
    else:
        arr = cv2.cvtColor(render_array(fixed, region=region, scale=scale, flip=flip),
                           cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".png", arr)
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
