"""从 apk 解析固定地图，输出结构化数据。

数据已确认（见 tools/PROBE_MAPS.md）：
  maps_N     : 600x600（maps_0 是 50x50）的整数数组，每格一个地形编码
  decorations_N : [x, y, spriteId] 或 [x, y1, y2, spriteId] 的装饰物列表

坐标系：数组下标 = y * WIDTH + x，与游戏内坐标一致（主城 (243,291) 已验证）。

本模块只负责「读 apk → 结构化」，不猜语义；语义映射留给 terrain_semantics。
"""
import json
import os
import re
import zipfile

# Cocos 资源在 apk 内的路径
APP_PREFIX = "assets/assets/app/import/"

# 已知地图尺寸
KNOWN_SIZES = {50: "maps_0", 600: "maps_13/14/15"}


class ApkReader:
    """按资源名读 Cocos JsonAsset；解析结果缓存。"""

    def __init__(self, apk_path):
        if not os.path.exists(apk_path):
            raise FileNotFoundError(apk_path)
        self.path = apk_path
        self._zip = zipfile.ZipFile(apk_path)
        self._names = [n for n in self._zip.namelist() if n.endswith(".json")]
        self._name_index = None      # 资源名 -> 内部路径
        self._cache = {}

    def _build_index(self):
        """扫描一遍，建立「资源名 → 文件路径」索引（只做一次）。"""
        if self._name_index is not None:
            return
        idx = {}
        pat = re.compile(rb'\[\[0,"([^"]{1,40})",')
        for n in self._names:
            try:
                head = self._zip.read(n)[:400]
            except Exception:
                continue
            if b'cc.JsonAsset' not in head:
                continue
            m = pat.search(head)
            if m:
                idx[m.group(1).decode("utf-8", "ignore")] = n
        self._name_index = idx

    def names(self):
        self._build_index()
        return sorted(self._name_index)

    def load(self, asset_name):
        """读一个 JsonAsset 的 json 值；没有返回 None。"""
        if asset_name in self._cache:
            return self._cache[asset_name]
        self._build_index()
        n = self._name_index.get(asset_name)
        if n is None:
            return None
        txt = self._zip.read(n).decode("utf-8", "ignore")
        marker = '[[0,"%s",' % asset_name
        i = txt.find(marker)
        if i < 0:
            return None
        val, _ = json.JSONDecoder().raw_decode(txt[i + len(marker):])
        self._cache[asset_name] = val
        return val

    def close(self):
        try:
            self._zip.close()
        except Exception:
            pass


def map_names(reader):
    """apk 里所有 maps_N 资源名。"""
    return [n for n in reader.names() if re.fullmatch(r"maps_\d+", n)]


def grid_of(raw):
    """把扁平数组还原成 (width, height, 二维 list)；长度不是完全平方数时返回 None。"""
    if not isinstance(raw, list) or not raw:
        return None          # 空数组不是 0x0 地图
    n = len(raw)
    w = int(n ** 0.5)
    if w * w != n:
        return None
    return w, w, [raw[y * w:(y + 1) * w] for y in range(w)]


def parse_map(reader, name):
    """解析一张地图为结构化 dict。"""
    raw = reader.load(name)
    if raw is None:
        return None
    g = grid_of(raw)
    if g is None:
        return {"name": name, "error": "不是方形网格", "length": len(raw)}
    w, h, rows = g
    counts = {}
    for v in raw:
        counts[v] = counts.get(v, 0) + 1
    return {
        "name": name,
        "width": w,
        "height": h,
        "cells": rows,
        "valueCounts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "distinct": len(counts),
    }


def parse_decorations(reader, name):
    """解析装饰物层。

    实测格式（decorations_14，93530 条）：
      [起点索引, 装饰id, 终点索引]  —— 表示从哪格到哪格画这个装饰
      [起点索引, 装饰id]           —— 单格
    索引 = y * WIDTH + x，与地图坐标一致。
    装饰 id 对应 decoration 表的 1..150（100% 命中）。

    注意：装饰物只是**纯视觉**元素（阴影/草地/湖泊/山丘），
    不影响资源、等级、归属，脚本决策用不到。
    """
    raw = reader.load(name)
    if raw is None:
        return None
    items = []
    for e in raw:
        if not isinstance(e, list) or len(e) < 2:
            continue
        items.append({"start": e[0], "decoration": e[1],
                      "end": e[2] if len(e) > 2 else e[0]})
    return {"name": name, "count": len(items), "items": items}


def export_all(apk_path, out_dir, with_decorations=False):
    """把 apk 里的地图（可选装饰层）导出成 json 文件。

    with_decorations 默认关：装饰物是纯视觉元素（阴影/草/湖/山），
    对脚本决策没有用处，导出后单个文件就有 3MB 以上。
    """
    os.makedirs(out_dir, exist_ok=True)
    reader = ApkReader(apk_path)
    result = {"maps": [], "decorations": []}

    for name in map_names(reader):
        m = parse_map(reader, name)
        if m:
            p = os.path.join(out_dir, name + ".json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False)
            result["maps"].append({"name": name, "width": m.get("width"),
                                   "height": m.get("height"),
                                   "distinct": m.get("distinct"),
                                   "file": os.path.basename(p)})

    if with_decorations:
        deco = [n for n in reader.names() if re.fullmatch(r"decorations_\d+", n)]
        for name in deco:
            d = parse_decorations(reader, name)
            if d and d["count"]:
                p = os.path.join(out_dir, name + ".json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
                result["decorations"].append({"name": name, "count": d["count"],
                                              "file": os.path.basename(p)})
        # 装饰 id → icon 的对照表。没有它，decorations_N 里那堆数字没法还原成
        # 「草/丘/山/湖」，网页就没法按地形上色。体积很小，一起导出。
        table = {e["id"]: {"icon": e.get("icon"), "type": e.get("type")}
                 for e in (reader.load("decoration") or []) if "id" in e}
        if table:
            p = os.path.join(out_dir, "decoration_table.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(table, f, ensure_ascii=False)
            result["decorationTable"] = {"count": len(table),
                                         "file": os.path.basename(p)}

    reader.close()
    return result


if __name__ == "__main__":
    import sys
    apk = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu\apk\jiuwanmu.apk"
    out = sys.argv[2] if len(sys.argv) > 2 else \
        r"C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu\data\maps"
    # 第三个参数带 --with-decorations 才导出装饰层（纯视觉，体积大）
    with_deco = "--with-decorations" in sys.argv
    r = export_all(apk, out, with_decorations=with_deco)
    print("地图:")
    for m in r["maps"]:
        print("  %-10s %dx%d  值种类 %s" % (m["name"], m["width"], m["height"], m["distinct"]))
    if r["decorations"]:
        print("装饰层:")
        for d in r["decorations"]:
            print("  %-16s %d 条" % (d["name"], d["count"]))
    else:
        print("装饰层: 未导出（加 --with-decorations 可导出，纯视觉用途）")
    print("输出目录:", out)
