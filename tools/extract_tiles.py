"""把 apk 里所有地块/装饰图标提取成 data/tiles/<icon>.png。

为什么需要：
  之前「画地图」只是给每格涂一个颜色，看着像撒了一地纸屑，和游戏画面完全不像。
  游戏里每格其实是一张贴图：草地底、树、石头、粮田小方块、湖泊……
  这些图就在 apk 的 tmp/image/land_0..3/ 下，本脚本按 icon 名提取出来。

用法：
    python tools/extract_tiles.py apk/jiuwanmu.apk data/tiles
"""
import io
import json
import os
import sys
import zipfile
from collections import defaultdict

from PIL import Image

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
BASE = "assets/assets/resources/"


def decode_uuid(u):
    if len(u) != 22:
        return u
    h = u[:2]
    s = u[2:]
    for i in range(0, len(s), 2):
        a = B64.index(s[i])
        b = B64.index(s[i + 1])
        h += "%x%x%x" % (a >> 2, ((a & 3) << 2) | (b >> 4), b & 15)
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def load_table(apk, z, name):
    """按表名找 JsonAsset（沿用 apkmap 的做法）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from jwm.apkmap import ApkReader
    r = ApkReader(apk)
    rows = r.load(name) or []
    r.close()
    return rows


def main():
    apk = sys.argv[1] if len(sys.argv) > 1 else "apk/jiuwanmu.apk"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "data/tiles"
    os.makedirs(out_dir, exist_ok=True)

    # 需要哪些 icon：地块的 icon + 装饰的 icon
    icons = set()
    for row in load_table(apk, None, "land"):
        if row.get("icon"):
            icons.add(row["icon"])
    for row in load_table(apk, None, "decoration"):
        if row.get("icon"):
            icons.add(row["icon"])
    print("需要 %d 个图标" % len(icons))

    z = zipfile.ZipFile(apk)
    names = set(z.namelist())
    cfg = json.loads(z.read(BASE + "config.json"))
    uuids = cfg["uuids"]
    sf_type = cfg["types"].index("cc.SpriteFrame")

    # basename -> [(主题, 完整路径, 索引)]，优先 land_3 主题
    by_name = defaultdict(list)
    for k, v in cfg["paths"].items():
        path = v[0]
        if v[1] != sf_type:
            continue
        base = os.path.basename(path)
        theme = path.split("/")[2] if path.startswith("tmp/image/") else ""
        if base in icons:
            by_name[base].append((theme, path, int(k)))

    # 四套地形主题：land_0 草地绿（和游戏画面一致）、land_1 相近、
    # land_2 秋季偏橙、land_3 雪地偏青白。
    # 之前默认挑了 land_3，整张图渲染出来一片灰白，看着根本不像游戏。
    def theme_rank(t):
        return {"land_0": 0, "land_1": 1, "land_2": 2, "land_3": 3}.get(t, 9)

    idx2pack = {}
    for pid, lst in cfg["packs"].items():
        for pos, i in enumerate(lst):
            idx2pack[i] = (pid, pos)

    textures = {}

    def texture(tex_uuid):
        if tex_uuid not in textures:
            u = decode_uuid(tex_uuid)
            f = BASE + "native/%s/%s.png" % (u[:2], u)
            textures[tex_uuid] = (Image.open(io.BytesIO(z.read(f))).convert("RGBA")
                                  if f in names else None)
        return textures[tex_uuid]

    packs = {}
    done = 0
    missing = []
    for icon in sorted(icons):
        cands = sorted(by_name.get(icon, []), key=lambda x: theme_rank(x[0]))
        if not cands:
            missing.append(icon)
            continue
        theme, path, idx = cands[0]
        if idx not in idx2pack:
            missing.append(icon)
            continue
        pid, pos = idx2pack[idx]
        if pid not in packs:
            packs[pid] = json.loads(z.read(BASE + "import/%s/%s.json" % (pid[:2], pid)))
        entry = packs[pid][5][pos]
        shared = packs[pid][1]
        tex = texture(shared[entry[1][0]] if isinstance(entry[1], list) else shared[0])
        if tex is None:
            missing.append(icon)
            continue
        meta = entry[0][0]
        x, y, w, h = meta["rect"]
        if meta.get("rotated", 0):
            crop = tex.crop((x, y, x + h, y + w)).rotate(90, expand=True)
        else:
            crop = tex.crop((x, y, x + w, y + h))
        ow, oh = meta["originalSize"]
        ox, oy = meta.get("offset", [0, 0])
        canvas = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
        canvas.paste(crop, (int(round((ow - w) / 2 + ox)), int(round((oh - h) / 2 - oy))))
        canvas.save(os.path.join(out_dir, icon + ".png"))
        done += 1

    print("提取 %d 个图标到 %s" % (done, out_dir))
    if missing:
        print("缺 %d 个（表里有 icon 但 apk 里找不到图）：%s"
              % (len(missing), missing[:10]))


if __name__ == "__main__":
    main()
