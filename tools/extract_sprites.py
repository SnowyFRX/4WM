"""从九万亩 APK 中提取精灵帧 (SpriteFrame) 为独立 PNG，用作识别模板。

用法:
    python tools/extract_sprites.py <apk路径> <输出目录> [路径前缀过滤...]

例:
    python tools/extract_sprites.py apk/jiuwanmu-4.5.0.apk assets/sprites tmp/image/land_
"""
import io
import json
import os
import sys
import zipfile

from PIL import Image

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
BASE = "assets/assets/resources/"


def decode_uuid(u):
    """Cocos Creator 2.x 压缩 uuid (22 位) -> 标准 uuid。其他长度原样返回。"""
    if len(u) != 22:
        return u
    h = u[:2]
    s = u[2:]
    for i in range(0, len(s), 2):
        a = B64.index(s[i])
        b = B64.index(s[i + 1])
        h += "%x%x%x" % (a >> 2, ((a & 3) << 2) | (b >> 4), b & 15)
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def main():
    apk, out_dir = sys.argv[1], sys.argv[2]
    prefixes = sys.argv[3:] or [""]
    z = zipfile.ZipFile(apk)
    names = set(z.namelist())
    cfg = json.loads(z.read(BASE + "config.json"))
    uuids = cfg["uuids"]
    sf_type = cfg["types"].index("cc.SpriteFrame")

    # 资源索引 -> 路径
    wanted = {}
    for k, v in cfg["paths"].items():
        if v[1] == sf_type and any(v[0].startswith(p) for p in prefixes):
            wanted[int(k)] = v[0]
    idx2pack = {}
    for pid, lst in cfg["packs"].items():
        for pos, i in enumerate(lst):
            idx2pack.setdefault(i, (pid, pos))

    textures = {}

    def texture(tex_uuid):
        if tex_uuid not in textures:
            u = decode_uuid(tex_uuid)
            f = BASE + "native/%s/%s.png" % (u[:2], u)
            if f not in names:
                textures[tex_uuid] = None
            else:
                textures[tex_uuid] = Image.open(io.BytesIO(z.read(f))).convert("RGBA")
        return textures[tex_uuid]

    packs = {}
    done = 0
    for idx, path in sorted(wanted.items(), key=lambda x: x[1]):
        if idx not in idx2pack:
            continue
        pid, pos = idx2pack[idx]
        if pid not in packs:
            packs[pid] = json.loads(z.read(BASE + "import/%s/%s.json" % (pid[:2], pid)))
        pk = packs[pid]
        shared_uuids = pk[1]
        entry = pk[5][pos]
        meta = entry[0][0]
        tex = texture(shared_uuids[entry[1][0]] if isinstance(entry[1], list) else shared_uuids[0])
        if tex is None:
            continue
        x, y, w, h = meta["rect"]
        rotated = meta.get("rotated", 0)
        if rotated:
            crop = tex.crop((x, y, x + h, y + w)).rotate(90, expand=True)
        else:
            crop = tex.crop((x, y, x + w, y + h))
        ow, oh = meta["originalSize"]
        ox, oy = meta.get("offset", [0, 0])
        canvas = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
        # cocos offset: 以中心为原点、y 轴向上
        px = int(round((ow - w) / 2 + ox))
        py = int(round((oh - h) / 2 - oy))
        canvas.paste(crop, (px, py))
        dst = os.path.join(out_dir, path.replace("tmp/image/", "") + ".png")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        canvas.save(dst)
        done += 1
    print("extracted", done, "sprites to", out_dir)


if __name__ == "__main__":
    main()
