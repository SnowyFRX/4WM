"""暂停闸：暂停期间脚本不向模拟器发任何输入，用户可以先手动操作。

为什么要单独一个模块：
  脚本和用户抢同一个模拟器。用户想临时点两下（看建筑、翻任务、拖地图），
  脚本正好也在点，就会互相打断、点错位置。
  暂停不等于停止：进程、网页、状态都还在，只是不再「动手」。

拦截点分两层：
  1. bot.loop 每轮开头 —— 暂停时停在这里，不进入任务；
  2. Device.shell 里凡是 input / monkey / am force-stop 这类会动的命令 ——
     兜底，防止某个长任务进行到一半还在点。

截图**不拦**：只读，不干扰用户操作，网页还能继续显示画面。
"""
import threading

# 允许运行 = set；暂停 = clear。默认运行。
_RUNNING = threading.Event()
_RUNNING.set()


def paused():
    return not _RUNNING.is_set()


def set_paused(on):
    """True=暂停，False=恢复。返回设置后的暂停状态。"""
    if on:
        _RUNNING.clear()
    else:
        _RUNNING.set()
    return paused()


def wait(timeout=None):
    """暂停时阻塞，直到恢复（或超时）。返回 True 表示现在可以继续。"""
    return _RUNNING.wait(timeout)


def toggle():
    return set_paused(not paused())
