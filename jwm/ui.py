"""界面识别与导航。

已知规律（实测）：
  - 大地图左下角是“进城”按钮；镜头离开主城时底部会多一个“回城定位”按钮。
  - 城内左下角是绿色“返回”箭头。
  - 返回键不可靠：有时能关弹窗，有时游戏完全不响应；地图上什么都没开时按返回键，
    镜头还会乱跳并选中别的地块。所以优先用点击关闭（见 close()），返回键只作兜底。
  - 新手引导对话框在底部 (y≈1060~1200)，点一下继续。
"""
import logging
import time

import cv2
import numpy as np

from . import vision as V
from .vision import OCR, find_text

log = logging.getLogger(__name__)

# 区域
R_BOTTOM_LEFT = (0, 1150, 110, 1260)
R_BOTTOM_HOME = (78, 1120, 220, 1275)
R_BOTTOM_RIGHT = (600, 1150, 720, 1260)
R_DIALOG = (60, 1040, 640, 1210)

# 面板/弹窗关键词 -> 界面名
PANEL_KEYWORDS = [
    ("选择要出战的军队", "march_select"),
    ("选择要前往的军队", "move_select"),
    ("选择要屯田的军队", "tonden_select"),
    ("战斗预测", "forecast_dialog"),
    ("所有军队", "army_panel"),
    ("当前区域的军队", "army_panel"),
    ("建筑信息", "building_panel"),
    ("建筑列表", "build_list"),
    ("招募士兵", "building_panel"),
    ("第.章", "quest_panel"),
    ("攻占成功", "battle_result"),
    ("点击任意位置继续", "tap_continue"),
    ("通过微信登录", "login"),
    ("TapTap", "login"),
    ("获取更新信息|正在更新", "updating"),
    ("用户注册服务协议", "agreement"),
    ("重新连接|重连|网络异常|连接断开|断开连接", "reconnect"),
    ("退出游戏|确定要退出|是否退出", "exit_dialog"),
]


class Screen:
    """一次截图 + 懒加载的 OCR 结果。"""

    def __init__(self, img):
        self.img = img
        self._boxes = None
        self._regions = {}

    @property
    def boxes(self):
        if self._boxes is None:
            self._boxes = OCR.read(self.img)
        return self._boxes

    def read(self, region):
        """只识别一小块区域（比整屏 OCR 快很多）；整屏已识别过就直接过滤。"""
        if self._boxes is not None:
            x1, y1, x2, y2 = region
            return [b for b in self._boxes if x1 <= b.center[0] <= x2 and y1 <= b.center[1] <= y2]
        if region not in self._regions:
            self._regions[region] = OCR.read(self.img, region)
        return self._regions[region]

    def text(self, pattern, region=None):
        for b in self.boxes:
            if region and not (region[0] <= b.center[0] <= region[2] and region[1] <= b.center[1] <= region[3]):
                continue
            if find_text([b], pattern):
                return b
        return None

    def has(self, tpl, region=None, threshold=0.85, bright=True):
        """找模板；bright=True 时还要求亮度和模板相近（被弹窗遮罩压暗的不算）。"""
        m = V.match(self.img, tpl, region, threshold)
        if m is None or not bright:
            return m
        t, _ = V.template(tpl)
        x1, y1 = m.x - m.w // 2, m.y - m.h // 2
        roi = self.img[y1:y1 + m.h, x1:x1 + m.w]
        if roi.size and roi.mean() < 0.85 * t.mean():
            return None
        return m


class UI:
    def __init__(self, dev):
        self.dev = dev

    def grab(self):
        return Screen(self.dev.screenshot())

    def grab_stable(self, timeout=2.0):
        """等画面停止变化（镜头平移/弹窗动画结束）再返回。"""
        prev = self.dev.screenshot()
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.08)
            cur = self.dev.screenshot()
            if self._same(prev, cur, tol=1.5):
                return Screen(cur)
            prev = cur
        return Screen(prev)

    # ---------- 识别 ----------
    def on_map(self, sc):
        return sc.has("map_btn_city", R_BOTTOM_LEFT, 0.8) is not None

    def in_city(self, sc):
        return sc.has("city_btn_back", R_BOTTOM_LEFT, 0.8) is not None and sc.has("city_btn_build", (500, 1150, 640, 1260), 0.8) is not None

    @staticmethod
    def brackets(sc):
        """地块选中框的四角括号 → (x1, y1, x2, y2) 或 None。"""
        a = V.match(sc.img, "bracket_tl", threshold=0.75)
        b = V.match(sc.img, "bracket_br", threshold=0.75)
        if b is None:
            return None
        x2, y2 = b.x + b.w // 2, b.y + b.h // 2
        if a:
            x1, y1 = a.x - a.w // 2, a.y - a.h // 2
            if 40 < x2 - x1 < 200 and 40 < y2 - y1 < 200:
                return x1, y1, x2, y2

        # 单格靠着主城时，屋檐可能盖住左上角。用其余三个角验证同一选框。
        h, w = sc.img.shape[:2]
        right = (max(0, b.x - 16), max(0, b.y - 80), min(w, b.x + 16), max(0, b.y - 39))
        bottom = (max(0, b.x - 80), max(0, b.y - 16), max(0, b.x - 39), min(h, b.y + 16))
        top_right = V.match(sc.img, "bracket_br", right, 0.75, flip=0)
        bottom_left = V.match(sc.img, "bracket_br", bottom, 0.72, flip=1)
        if not (top_right and bottom_left):
            return None
        if (abs(top_right.x - b.x) > 4 or abs(bottom_left.y - b.y) > 4
                or abs((b.x - bottom_left.x) - (b.y - top_right.y)) > 10):
            return None
        return bottom_left.x - bottom_left.w // 2, top_right.y - top_right.h // 2, x2, y2

    def has_tile_popup(self, sc):
        """地图上是否开着地块弹窗（有选中框）。"""
        return self.brackets(sc) is not None

    def is_dialogue(self, sc):
        """新手引导/剧情对话框（底部白框）。"""
        x1, y1, x2, y2 = R_DIALOG
        white = V.color_ratio(sc.img, (x1 + 20, y1 + 30, x2 - 20, y2 - 30), (225, 235, 240), (255, 255, 255))
        if white < 0.55:
            return False
        return any(R_DIALOG[1] < b.center[1] < R_DIALOG[3] and len(b.text) >= 6 for b in sc.boxes)

    def classify(self, sc):
        # 快速路径：左下角按钮没被遮罩压暗 = 没有面板/对话框挡着，不用 OCR
        if self.on_map(sc):
            return "map_popup" if self.has_tile_popup(sc) else "map"
        if self.in_city(sc):
            return "city"
        if sc.has("city_btn_back", R_BOTTOM_LEFT, 0.8) is not None:
            return "tile_inside"  # 地块内部视图（点“进入”后看驻军），只有返回箭头没有建造按钮
        for kw, name in PANEL_KEYWORDS:
            if sc.text(kw):
                return name
        if self.is_dialogue(sc):
            return "dialogue"
        if self.in_city(sc):
            return "city"
        if self.on_map(sc):
            return "map_popup" if self.has_tile_popup(sc) else "map"
        return "unknown"

    # ---------- 关闭弹窗 ----------
    @staticmethod
    def _same(a, b, tol=3.0):
        sa = cv2.resize(a, (90, 160), interpolation=cv2.INTER_AREA).astype(np.int16)
        sb = cv2.resize(b, (90, 160), interpolation=cv2.INTER_AREA).astype(np.int16)
        return np.abs(sa - sb).mean() < tol

    def close(self, sc=None):
        """关闭最上层的弹窗/面板。

        实测游戏有时不响应返回键，所以优先用点击：
          - 地块弹窗：再点一次选中的格子；
          - 面板：点面板外的遮罩区域；预测对话框点“取消”；
          - 点击没反应才用返回键。
        """
        sc = sc or self.grab()
        kind = self.classify(sc)
        if kind in ("map", "city", "tile_inside"):
            return sc
        if kind == "map_popup":
            br = self.brackets(sc)
            if br:
                self.dev.tap((br[0] + br[2]) // 2, (br[1] + br[3]) // 2, jitter=0)
            else:
                self.dev.back(delay=0)
        elif kind in ("dialogue", "tap_continue", "battle_result"):
            self.dev.tap(360, 1130)
        elif kind == "forecast_dialog" or sc.text(r"^取消$"):
            if not self.tap_text(sc, r"^取消$", delay=0):
                self.dev.tap(360, 1215)
        else:
            self.dev.tap(360, 1215)  # 面板外的遮罩
        sc2 = Screen(self.dev.settle(1.2))
        if self._same(sc.img, sc2.img):
            log.debug("点击关闭无效，改用返回键")
            self.dev.back(delay=1.0)
            sc2 = self.grab()
        return sc2

    # ---------- 导航 ----------
    def to_map(self, max_steps=15, auto_login=True):
        """回到干净的大地图（没有弹窗）。返回最后一帧 Screen。"""
        login_tries = 0
        for _ in range(max_steps):
            sc = self.grab()
            kind = self.classify(sc)
            log.debug("to_map: %s", kind)
            if kind == "map":
                return sc
            if kind in ("dialogue", "tap_continue", "battle_result"):
                self.dev.tap(360, 1130, delay=1.2)
            elif kind in ("city", "tile_inside"):
                m = sc.has("city_btn_back", R_BOTTOM_LEFT, 0.8)
                self.dev.tap(*m.center, delay=2.0)
            elif kind == "updating":
                time.sleep(5)
            elif kind == "reconnect":
                if not self.tap_text(sc, r"重新连接|重连|确定|重试", delay=5):
                    self.close(sc)
            elif kind == "agreement":
                self.tap_text(sc, r"^同意$", delay=3)
            elif kind == "exit_dialog":
                if not self.tap_text(sc, r"^取消$", delay=1.5):
                    self.close(sc)
            elif kind == "login":
                login_tries += 1
                if not auto_login or login_tries > 3:
                    raise RuntimeError("游戏停在登录界面，请手动登录")
                log.warning("检测到登录界面，尝试 TapTap 登录（第 %d 次）", login_tries)
                b = sc.text(r"TapTap")
                if b:
                    self.dev.tap(*b.center, delay=8)
                    s2 = self.grab()
                    # TapTap 授权页
                    self.tap_text(s2, r"确认登录|授权|同意|登录", delay=8)
                else:
                    time.sleep(5)
            else:
                # 面板、弹窗、地图上的地块弹窗
                self.close(sc)
        raise RuntimeError("无法回到大地图")

    def to_home(self):
        """回到大地图并把镜头对准主城。"""
        return self.use_home_shortcut(self.to_map())[0]

    def use_home_shortcut(self, sc):
        """点击当前地图左下角的回主城按钮，并确认定位动画结束。"""
        clicked = False
        for _ in range(2):
            button = sc.has("map_btn_home", R_BOTTOM_HOME, 0.8)
            if button is None:
                return sc, clicked
            log.debug("点击左下角回主城快捷键")
            self.dev.tap(*button.center, jitter=0, delay=1.5)
            sc = self.grab_stable(timeout=0.8)
            if not self.on_map(sc) or self.has_tile_popup(sc):
                return sc, False
            clicked = True
        return sc, sc.has("map_btn_home", R_BOTTOM_HOME, 0.8) is None

    def to_city(self):
        sc = self.grab()
        if self.in_city(sc):
            return sc  # 已在城内主视图，不用出城再进城
        # 城内某个面板开着（建筑信息/兵营/修建列表）→ 关掉回主视图，同样不出城
        if sc.text(r"建筑信息|选择军队|建筑列表|内政|招募士兵"):
            self.close(sc)
            sc = self.grab()
            if self.in_city(sc):
                return sc
        sc = self.to_map()
        m = sc.has("map_btn_city", R_BOTTOM_LEFT, 0.8)
        self.dev.tap(*m.center, delay=2.5)
        for _ in range(5):
            sc = self.grab()
            if self.in_city(sc):
                return sc
            if self.is_dialogue(sc):
                self.dev.tap(360, 1130, delay=1.2)
            else:
                time.sleep(1)
        raise RuntimeError("进城失败")

    def tap_text(self, sc, pattern, region=None, delay=1.2):
        b = sc.text(pattern, region)
        if b is None:
            return False
        self.dev.tap(*b.center, delay=delay)
        return True

    def wait_text(self, pattern, timeout=6.0, region=None, interval=0.2):
        end = time.time() + timeout
        while time.time() < end:
            sc = self.grab()
            if sc.text(pattern, region):
                return sc
            time.sleep(interval)
        return None
