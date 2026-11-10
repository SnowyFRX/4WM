"""把当前可见的储备资源图标裁下来存成模板，供后续按外形识别。

为什么必须按外形识别：
  这四个特殊资源（经验书/铁/卷轴/钉子）是竖排显示的，但数量为 0 的那一行
  游戏直接不画 —— 下面的行会整体上移。所以不能按固定 y 或“第几行”去对应，
  必须先认出这个图标是什么，再取它旁边的数字。

用法：python tools/capture_res_icons.py
会在 assets/res_icons/ 下生成 icon_<name>.png，同时打印每行识别到的数字。
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

from jwm import resources as R
from jwm.bot import Bot, load_config
from jwm.vision import OCR

# 按界面从上到下的固定顺序（用户确认）：经验书、铁、卷轴、钉子
ORDER = ["book", "iron", "scroll", "nail"]

bot = Bot(load_config())
bot.ensure_game()
sc = bot.ui.to_map()

rows = R.stock_rows(sc.img)
print("检测到 %d 行：%s" % (len(rows), rows))
out_dir = proj / "assets" / "res_icons"
out_dir.mkdir(parents=True, exist_ok=True)

for i, (y1, y2) in enumerate(rows):
    cy = (y1 + y2) // 2
    name = ORDER[i] if i < len(ORDER) else "extra%d" % i
    crop = sc.img[y1:y2, 4:52]
    cv2.imencode(".png", crop)[1].tofile(str(out_dir / ("icon_%s.png" % name)))
    # 读这一行的数字
    num_crop = sc.img[max(0, cy - 20):cy + 20, 45:200]
    big = cv2.resize(num_crop, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    inv = 255 - cv2.inRange(gray, 170, 255)
    texts = [e.text for e in OCR.read(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR))]
    print("  第%d行 y=%d~%d -> 存为 %s.png，数字 %s" % (i + 1, y1, y2, name, texts))

print("\n模板已写入:", out_dir)
print("注意：只保存了当前可见的行。要到齐四种，需等对应资源不为 0 时再跑一次。")
