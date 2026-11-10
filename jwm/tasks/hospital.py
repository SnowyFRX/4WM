"""医馆：治疗伤兵，让它们回到编队继续作战。

界面实测（点城里医馆 → 第二个标签「治疗伤兵」）：
    治疗伤兵            (515,212)   标签页
    选择军队             (109,292)   编队卡片：编队5 / 编队2 / 编队6 ...
    6/8                 (651,292)   这个编队已选几个兵 / 上限
    选择伤兵(1/200)      (142,551)   总共还有几个伤兵等着治
    按士兵等级排序        (549,550)   列表排序
    治疗费 / 治疗        (595,794)   确认按钮
    治疗队列(0/6)        (135,888)   治疗队列

机制（buildText.desc_2016）：士兵战败后有几率回到医馆，治疗后可再次作战。
医馆等级越高治疗时间越短 —— 所以治好的兵回的是**原来的编队**，
不用自己判断兵种归属（游戏已经记住它原来是哪支队伍的兵）。
"""
import logging
import re
import time

log = logging.getLogger(__name__)

TAB_HOSPITAL = (470, 190, 570, 235)  # 「治疗伤兵」标签
R_WAIT = r"选择伤兵"
R_QUEUE = r"治疗队列"
BTN_HEAL = (540, 770, 660, 820)


class Hospital:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui
        self.cfg = (bot.cfg or {}).get("hospital") or {}

    # ---------- 读界面 ----------
    @staticmethod
    def wounded(sc):
        """还有几个伤兵待治疗（读“选择伤兵(1/200)”里的 1）。"""
        b = sc.text(R_WAIT)
        if not b:
            return None
        m = re.search(r"\((\d+)\s*/\s*(\d+)\)", b.text)
        return int(m.group(1)) if m else None

    @staticmethod
    def queue(sc):
        """治疗队列 (已用, 上限)。"""
        b = sc.text(R_QUEUE)
        if not b:
            return None
        m = re.search(r"\((\d+)\s*/\s*(\d+)\)", b.text)
        return (int(m.group(1)), int(m.group(2))) if m else None

    @staticmethod
    def cards(sc):
        """编队卡片 [(名字, 中心x)]。"""
        out = []
        for b in sc.boxes:
            if re.fullmatch(r"编队\s*\d+", b.text.strip()) and 300 < b.center[1] < 400:
                out.append((b.text.strip(), b.center[0]))
        return out

    # ---------- 主流程 ----------
    def open_hospital(self):
        """进城打开医馆的「治疗伤兵」页；失败返回 None。"""
        pos = (self.bot.state.data.get("building_pos") or {}).get("医馆")
        if not pos:
            log.info("还没有医馆的位置（先让 build 任务扫一遍城内建筑）")
            return None
        self.ui.to_city()
        # 城里有加载动画，点太早会“点空”。点一次没反应就等一下再点一次。
        sc = None
        for attempt in range(2):
            self.dev.tap(pos[0], pos[1] + 45, delay=1.4)
            sc = self.ui.grab()
            if (sc.text(r"^医馆$") or sc.text(r"治疗伤兵") or sc.text(r"士兵战败")
                    or self.ui.classify(sc) == "building_panel"):
                break
            if attempt == 0:
                time.sleep(1.2)
                sc = None
        if sc is None:
            log.debug("  (%.0f,%.0f) 点了两次也没打开建筑面板", pos[0], pos[1] + 45)
            self.ui.to_map()
            return None
        if not sc.text(R_WAIT):
            # 面板会记住上次页签，切到「治疗伤兵」
            self.ui.tap_text(sc, r"治疗伤兵", TAB_HOSPITAL, delay=1.0)
            sc = self.ui.grab()
        if not sc.text(R_WAIT):
            self.ui.to_map()
            return None
        return sc

    def run(self):
        if not self.cfg.get("enabled", True):
            return 0
        sc = self.open_hospital()
        if sc is None:
            log.debug("医馆没打开（可能还没修），跳过")
            return 0
        n = self.wounded(sc)
        if n is None:
            self.ui.to_map()
            return 0
        if n <= 0:
            log.debug("没有需要治疗的伤兵")
            self.ui.to_map()
            return 0
        q = self.queue(sc)
        if q and q[0] >= q[1]:
            log.info("治疗队列已满 (%d/%d)，等它消化", q[0], q[1])
            self.ui.to_map()
            return 0
        log.info("医馆有 %d 个伤兵待治疗", n)
        healed = 0
        # 逐个编队试：哪个编队有伤兵就治哪个（伤兵会回到它原来的编队）
        for name, cx in self.cards(sc):
            left = self.wounded(self.ui.grab())
            if left is not None and left <= 0:
                break
            q = self.queue(self.ui.grab())
            if q and q[0] >= q[1]:
                log.info("治疗队列满了 (%d/%d)，这轮先到这", q[0], q[1])
                break
            self.dev.tap(cx, 380, delay=1.0)
            sc = self.ui.grab()
            now = self.wounded(sc)
            if not now:
                continue
            if self._heal_one(sc):
                healed += 1
                log.info("  %s 的伤兵送进治疗队列", name)
                self.bot.state.inc("healed")
        self.ui.to_map()
        if healed:
            log.info("医馆治疗：送了 %d 批伤兵", healed)
        return healed

    def _heal_one(self, sc):
        """点「治疗」。没伤兵可选时游戏不会给按钮 / 灰掉，这里按文本点。"""
        if self.ui.tap_text(sc, r"^治疗$", BTN_HEAL, delay=1.5):
            s2 = self.ui.grab()
            # 真机实测：编队不在主城时，游戏弹「军队不在主城内，无法安置治疗的伤兵」。
            # 必须认出并关掉它，否则会卡在提示弹窗，后续编队全点歪。
            if s2.text(r"军队不在主城|无法安置治疗|移回主城"):
                log.info("  编队不在主城，治好的伤兵没处安置，等队伍回城再治")
                self.ui.close(s2)
                return False
            if s2.text(r"治疗中|治疗队列"):
                return True
            # 有时会弹“费用确认”，点确定
            if self.ui.tap_text(s2, r"^确定$", delay=1.2):
                return True
            # 没读到预期反馈，可能停在某个弹窗，收一下避免卡住
            log.debug("  治疗后界面未确认，截图排查")
            self.ui.close(s2)
            return False
        return False
