"""采集兵种头像模板：把军队面板里每种不同的头像各存一份到 assets/unit_icons/。

兵种靠像素模板识别（不再按颜色猜：实测同兵种内部差异≈0，不同兵种差异 59~71，
用颜色阈值区分根本分不开 —— 这就是之前「兵种构成都不对」的原因）。

用法：python tools/capture_unit_icons.py
会聚类出几种不同头像，每种存一张，并让你确认名字。
"""
import logging
import os
import sys
from pathlib import Path

proj = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(proj))
os.chdir(proj)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import cv2
import numpy as np

from jwm.army_panel import UNIT_MAX, avatar_at
from jwm.bot import Bot, load_config

OUT = proj / "assets" / "unit_icons"
OUT.mkdir(parents=True, exist_ok=True)

bot = Bot(load_config())
bot.ensure_game()
sc = bot.chests.open_army_panel()

# 抓所有编队的所有头像
samples = []
for b in sc.boxes:
    name = b.text.strip()
    if not name.startswith("编队") or b.center[1] >= 1100:
        continue
    for i in range(UNIT_MAX):
        a = avatar_at(sc.img, i, b.center[1])
        if a is None:
            break
        x, y, roi = a
        if cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2].std() < 12:
            continue
        g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        samples.append((name, i, cv2.resize(g, (32, 30), interpolation=cv2.INTER_AREA).astype(np.int16)))

print("共采到 %d 个头像，来自 %d 支编队" % (len(samples), len({s[0] for s in samples})))

# 按像素差聚类（阈值 12：同兵种 <13，不同兵种 >59）
clusters = []  # [(代表图, [(编队, 序号, 图)...])]
for name, i, im in samples:
    for rep, members in clusters:
        if np.abs(rep - im).mean() < 12:
            members.append((name, i, im))
            break
    else:
        clusters.append((im, [(name, i, im)]))

print("聚出 %d 种不同头像" % len(clusters))
for idx, (rep, members) in enumerate(clusters, 1):
    who = {}
    for n, _, _ in members:
        who[n] = who.get(n, 0) + 1
    print("  类型%d：%d 个，出现在 %s" % (idx, len(members), who))
    cv2.imencode(".png", rep.astype(np.uint8))[1].tofile(str(OUT / ("unit_type%d.png" % idx)))

bot.ui.to_map()
print("\n模板已写入:", OUT)
print("请告诉脚本每种类型叫什么（长枪兵/连弩兵/…），改名即可：unit_type1.png -> unit_长枪兵.png")
