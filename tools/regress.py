"""真机回归工具：直接调用 Bot 的各任务，验证功能（在 tools/ 下运行）。

用法（先设好 adb PATH、TMP 指向可写目录）：
    python tools/regress.py scan            # 只读：进城扫建筑，记录 building_pos
    python tools/regress.py armies          # 读编队状态 + home_idle_armies
    python tools/regress.py hospital        # 跑一次医馆治疗
    python tools/regress.py hospital_debug  # 医馆界面 OCR + 卡片/队列
    python tools/regress.py hospital_heal   # 点治疗看弹什么（诊断用）
    python tools/regress.py drill           # 跑一次校场练兵
    python tools/regress.py drill_debug     # 校场界面 OCR + 卡片识别
"""
import logging
import os
import sys
from pathlib import Path

proj = Path(__file__).resolve().parent.parent  # tools/ 的上一级 = 项目根
sys.path.insert(0, str(proj))
os.chdir(proj)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

from jwm.bot import Bot, load_config

bot = Bot(load_config())
which = sys.argv[1] if len(sys.argv) > 1 else "scan"
print("=== 任务列表 ===", list(bot.tasks.keys()))

if which == "scan":
    bot.build.scan()
    pos = bot.state.data.get("building_pos") or {}
    print("=== building_pos ===")
    for name, p in sorted(pos.items()):
        print("  %s -> %s" % (name, p))
    print("医馆:", pos.get("医馆"), " 校场:", pos.get("校场"))
elif which == "armies":
    bot.army_snapshot(force=True)
    for name, info in (bot._armies_cache or {}).items():
        print("  ", name, info)
    print("home_idle:", bot.home_idle_armies())
elif which == "hospital":
    bot.ensure_game()
    print("hospital 治疗批数:", bot.hospital.run())
elif which == "hospital_debug":
    bot.ensure_game()
    sc = bot.hospital.open_hospital()
    if sc is None:
        print("医馆没打开")
    else:
        print("wounded:", bot.hospital.wounded(sc), "queue:", bot.hospital.queue(sc))
        print("cards:", bot.hospital.cards(sc))
        for b in sorted(sc.boxes, key=lambda b: b.center[1]):
            print("  (%3d,%4d) %s" % (b.center[0], b.center[1], b.text))
elif which == "hospital_heal":
    bot.ensure_game()
    sc = bot.hospital.open_hospital()
    cards = bot.hospital.cards(sc)
    if cards:
        bot.dev.tap(cards[0][1], 380, delay=1.2)
        bot.ui.tap_text(bot.ui.grab(), r"^治疗$", (540, 770, 660, 820), delay=1.5)
        for b in sorted(bot.ui.grab().boxes, key=lambda b: b.center[1]):
            print("  (%3d,%4d) %s" % (b.center[0], b.center[1], b.text))
elif which == "drill":
    bot.ensure_game()
    bot.army_snapshot(force=True)
    print("home_idle:", bot.home_idle_armies())
    print("drill 训练兵数:", bot.drill.run())
elif which == "drill_debug":
    bot.ensure_game()
    bot.army_snapshot(force=True)
    print("home_idle:", bot.home_idle_armies())
    sc = bot.drill.open_drill()
    if sc is None:
        print("校场没打开")
    else:
        print("queue:", bot.drill.queue(sc), "cards:", bot.drill.cards(sc))
        print("avatar_centers:", bot.drill.avatar_centers(sc.img))
        bot.ui.to_map()
