"""地图匹配 v2：用「具体资源值」而不是「水/陆」做签名。

v1 的教训：主城周边一片全是陆地，三张图 100% 全中，无法区分。
真正有区分力的是**每格的具体 land id**（粮地几级、木地几级…），
因为这些是随机分布的地形细节，不同地图几乎不可能撞车。

签名构造：
  从游戏实时画面拿到若干格子，识别出「资源类型 + 等级」，
  映射成候选 land id 集合，再与各图对应坐标的值比对。

注意：不要求 OCR 一定成功。策略是
  1. 先用能确认的格子（水/陆，颜色判断，最稳）
  2. 再叠加资源类型+等级（OCR，但允许失败）
  3. 样本越多越准
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jwm.apkmap import ApkReader

WATER_IDS = frozenset([69, 70, 71] + list(range(631, 663)))

# land 表 type -> 资源键名
TYPE_RES = {3: "grain", 4: "timber", 5: "stone"}


class LandTable:
    """land 表：id -> (type, lv, 产量)，以及反向索引 (资源, 等级) -> 候选 id。"""

    def __init__(self, land_rows):
        self.by_id = {}
        self.by_res_lv = defaultdict(set)
        for e in land_rows:
            if "id" not in e:
                continue
            i = e["id"]
            t = e.get("type")
            lv = e.get("lv", 0) or 0
            self.by_id[i] = e
            res = TYPE_RES.get(t)
            if res:
                self.by_res_lv[(res, lv)].add(i)

    def is_water(self, v):
        return v in WATER_IDS

    def describe(self, v):
        """给网页/日志看的一句话。"""
        e = self.by_id.get(v)
        if not e:
            return "未知(%s)" % v
        if self.is_water(v):
            return "水域"
        t = e.get("type")
        res = TYPE_RES.get(t)
        if res:
            name = {"grain": "粮", "timber": "木", "stone": "石"}[res]
            return "%s地 Lv%s" % (name, e.get("lv"))
        if t == 6:
            return "建筑占地"
        return "地形(type=%s)" % t

    def candidates(self, res, lv):
        return self.by_res_lv.get((res, lv), set())


def load_all(apk_path):
    """返回 (maps, land_table)。"""
    r = ApkReader(apk_path)
    maps = {}
    for name in r.names():
        if not name.startswith("maps_"):
            continue
        raw = r.load(name)
        if not isinstance(raw, list) or not raw:
            continue
        w = int(len(raw) ** 0.5)
        if w * w == len(raw):
            w = int(round(len(raw) ** 0.5))
        if w * w != len(raw):
            continue
        maps[name] = {"w": w, "raw": raw}
    land = LandTable(r.load("land") or [])
    return maps, land


def score(maps, name, samples, land):
    """samples: [{x, y, water?, res?, lv?}]

    返回 (一致数, 可判定数, 一致率)。
    只统计「这条样本能给出确定判断」的情况，避免把没识别出的格子算成失败。
    """
    m = maps[name]
    w, raw = m["w"], m["raw"]
    hit = 0
    judged = 0
    for s in samples:
        x, y = s["x"], s["y"]
        if not (0 <= x < w and 0 <= y < w):
            continue
        v = raw[y * w + x]
        if s.get("water") is not None:
            judged += 1
            if land.is_water(v) == s["water"]:
                hit += 1
        elif s.get("res") and s.get("lv") is not None:
            cands = land.candidates(s["res"], s["lv"])
            if not cands:
                continue
            judged += 1
            if v in cands:
                hit += 1
    rate = (hit / judged) if judged else 0.0
    return hit, judged, rate


def identify(maps, samples, land, min_judged=8, min_margin=0.12):
    ranking = {}
    for name in maps:
        hit, judged, rate = score(maps, name, samples, land)
        if judged:
            ranking[name] = {"hit": hit, "judged": judged, "rate": round(rate, 4)}

    if not ranking:
        return {"name": None, "ranking": {}, "reason": "没有任何格子能用于比对"}
    order = sorted(ranking.items(), key=lambda kv: kv[1]["rate"], reverse=True)
    best_name, best = order[0]
    if best["judged"] < min_judged:
        return {"name": None, "ranking": ranking,
                "reason": "可判定样本只有 %d 个（需要 %d 个以上）" % (best["judged"], min_judged)}
    second = order[1][1]["rate"] if len(order) > 1 else 0.0
    if best["rate"] < 0.85:
        return {"name": None, "ranking": ranking,
                "reason": "最高一致率仅 %.0f%%" % (best["rate"] * 100)}
    if len(order) > 1 and best["rate"] - second < min_margin:
        return {"name": None, "ranking": ranking,
                "reason": "前两名太接近（%.0f%% vs %.0f%%），需要更多样本"
                          % (best["rate"] * 100, second * 100)}
    return {"name": best_name, "ranking": ranking,
            "reason": "一致率 %.0f%%，领先 %.0f 个百分点"
                      % (best["rate"] * 100, (best["rate"] - second) * 100)}


if __name__ == "__main__":
    apk = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu\apk\jiuwanmu.apk"
    maps, land = load_all(apk)
    print("地图:", {k: "%dx%d" % (v["w"], v["w"]) for k, v in maps.items()})
    print("land 表:", len(land.by_id), "条")

    # 用 maps_14 造「资源型样本」自检：取 30 个资源地格子
    W = maps["maps_14"]["w"]
    raw = maps["maps_14"]["raw"]
    samples = []
    for dy in range(-20, 21):
        for dx in range(-20, 21):
            x, y = 243 + dx, 291 + dy
            v = raw[y * W + x]
            e = land.by_id.get(v)
            if not e:
                continue
            t = e.get("type")
            res = TYPE_RES.get(t)
            if res:
                samples.append({"x": x, "y": y, "res": res, "lv": e.get("lv", 0)})
    samples = samples[:60]
    print("自检样本（来自 maps_14 的资源地）:", len(samples))
    res = identify(maps, samples, land)
    print("  识别结果:", res["name"], "|", res["reason"])
    for k, v in res["ranking"].items():
        print("    %-8s %d/%d = %.1f%%" % (k, v["hit"], v["judged"], v["rate"] * 100))
