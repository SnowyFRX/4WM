"""等扫描帧产出并验证格子间距是否严格等于 tile。"""
import json
import time
import urllib.request
import sys
from collections import defaultdict

BASE = "http://127.0.0.1:8766/jwm/"


def state():
    return json.loads(urllib.request.urlopen(BASE + "state", timeout=10).read().decode("utf-8"))


def spacing_report(st):
    frames = st.get("frames") or []
    tile = (st.get("view") or {}).get("tile")
    if not frames or not tile:
        return None
    tile = float(tile)
    dxs, dys = [], []
    for f in frames:
        ts = f.get("tiles") or []
        by = defaultdict(dict)
        for t in ts:
            by[t["y"]][t["x"]] = t
        for y, row in by.items():
            xs = sorted(row)
            for a, b in zip(xs, xs[1:]):
                if b - a == 1:
                    dxs.append(round(row[b]["left"] - row[a]["left"], 4))
        byx = defaultdict(dict)
        for t in ts:
            byx[t["x"]][t["y"]] = t
        for x, col in byx.items():
            ys = sorted(col)
            for a, b in zip(ys, ys[1:]):
                if b - a == 1:
                    dys.append(round(col[a]["top"] - col[b]["top"], 4))
    return tile, dxs, dys


print("等待扫描产出帧…")
st = None
for i in range(24):
    time.sleep(5)
    try:
        st = state()
    except Exception as e:
        print("  读取失败:", e)
        continue
    n = len(st.get("frames") or [])
    err = st.get("mapError")
    print("  %2ds 帧数=%d %s" % ((i + 1) * 5, n, ("err=" + str(err)[:40]) if err else ""))
    if n and not err:
        break

if not st or not st.get("frames"):
    print("没有拿到帧，结束")
    sys.exit(1)

r = spacing_report(st)
if not r:
    print("没有可比对的间距")
    sys.exit(1)

tile, dxs, dys = r
print()
print("view.tile = %.4f" % tile)
print("水平间距 唯一值:", sorted(set(dxs))[:6], "(n=%d)" % len(dxs))
print("垂直间距 唯一值:", sorted(set(dys))[:6], "(n=%d)" % len(dys))

okx = dxs and all(abs(v - tile) < 0.01 for v in dxs)
oky = dys and all(abs(v - tile) < 0.01 for v in dys)
print()
print("水平全部等于 tile:", okx)
print("垂直全部等于 tile:", oky)
print("✅ 间距严格等于 tile（对齐条件成立）" if (okx and oky) else "❌ 间距仍不等于 tile")
