"""九万亩自动脚本入口。

用法:
    python run.py                 # 按 config.yaml 循环执行所有任务
    python run.py once expand     # 只执行一次某个任务 (expand/chests/build/recruit/quests)
    python run.py map             # 解析当前地图画面，输出调试图 logs/map_debug.png
    python run.py plan            # 不出兵，画出开荒计划 logs/plan.png（圈数、尝试顺序）
    python run.py screen          # 识别当前界面并打印 OCR 结果
"""
import faulthandler
import logging
import os
import sys

# 原生库（onnxruntime）崩溃时 Python 不会打 traceback，进程会直接消失。
# 打开 faulthandler 后至少能在 run_live.log 里留下崩溃时的线程栈。
faulthandler.enable(all_threads=True)

from jwm.bot import Bot, load_config
from jwm.vision import ROOT


def setup_logging():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(os.path.join(ROOT, "logs", "bot.log"), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("RapidOCR", "rapidocr", "onnxruntime"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_lock = None


def single_instance():
    """同一时间只允许一个脚本操作模拟器（占用本机一个端口当锁，进程退出自动释放）。"""
    import socket

    global _lock
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", 47391))
    except OSError:
        print("已经有一个九万亩脚本在运行了，先关掉它再启动。")
        sys.exit(1)


def main():
    single_instance()
    setup_logging()
    bot = Bot(load_config())
    args = sys.argv[1:]
    if not args:
        if bot.route:
            # 由控制台 console.py 拉起时，用 8766 且不自动开浏览器（控制台占着 8765）
            web = bot.cfg.get("web") or {}
            port = int(web.get("port") or 8765)
            if os.environ.get("DSH_JWM_NO_WEB"):
                port = int(web.get("child_port") or port + 1)
            with bot.route.serve(port=port, path="/jwm/",
                                 open_browser=not os.environ.get("DSH_JWM_NO_WEB")):
                bot.loop()
        else:
            bot.loop()
    elif args[0] == "once":
        if args[1] == "expand" and bot.route:
            print("请运行 start.bat，在网页地图上选点并启动开荒。")
        elif args[1] in ("build", "recruit") and bot.operations:
            bot.scan_route_map(prefetch=False)
            if bot.route.home:
                print(bot.run_task(args[1]))
            else:
                print(bot.route.snapshot()["mapError"])
        else:
            print(bot.run_task(args[1]))
    elif args[0] == "map":
        import cv2
        from jwm import mapview as mv

        img = bot.dev.screenshot()
        g = mv.MapGrid(img)
        p = os.path.join(ROOT, "logs", "map_debug.png")
        cv2.imencode(".png", g.debug_image())[1].tofile(p)
        print("领地 %d 格，可攻占边界 %d 格，调试图: %s" % (len(g.owned_tiles()), len(g.frontier()), p))
    elif args[0] == "plan":
        import cv2

        img = bot.expand.plan_image()
        p = os.path.join(ROOT, "logs", "plan.png")
        cv2.imencode(".png", img)[1].tofile(p)
        print("开荒计划图（数字=尝试顺序，x=近期打不动，r=圈数）:", p)
    elif args[0] == "screen":
        sc = bot.ui.grab()
        print("界面:", bot.ui.classify(sc))
        for b in sc.boxes:
            print("  %-20s %s" % (b.text, b.box))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
