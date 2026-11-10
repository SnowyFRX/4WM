"""领取各编队背包里的宝箱（士兵阵亡宝箱会丢，要及时领）。

- 军队按钮右上角出现金色宝箱角标 = 有宝箱可领，主循环随时检查，一出现就领；
- 领取时如果提示“库房容量不足”，点“升级仓库/升级粮仓”直接升级，绝不点“继续领取”（会丢资源）。
"""
import logging
import time

log = logging.getLogger(__name__)

R_BADGE = (682, 1155, 716, 1190)  # 军队按钮右上角的宝箱角标
R_POPUP_BTN = (480, 300, 720, 1000)  # 宝箱弹窗里“打开/领取”按钮所在的列


def has_chests(img):
    x1, y1, x2, y2 = R_BADGE
    roi = img[y1:y2, x1:x2].astype(int)
    gold = (roi[:, :, 2] > 200) & (roi[:, :, 1] > 150) & (roi[:, :, 0] < 90)
    return int(gold.sum()) >= 60


class Chests:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui
        self.blocked_until = 0  # 库房满且升级不了时，暂停一会儿再领

    def pending(self, img=None):
        if time.time() < self.blocked_until:
            return False
        img = img if img is not None else self.dev.screenshot()
        return has_chests(img)

    def open_army_panel(self):
        sc = self.ui.to_map()
        m = sc.has("btn_army", (600, 1150, 720, 1260), 0.8)
        if not m:
            raise RuntimeError("找不到军队按钮")
        self.dev.tap(*m.center, delay=1.2)
        sc = self.ui.wait_text(r"所有军队|拥有军队|当前区域的军队", timeout=5)
        if sc is None:
            raise RuntimeError("军队面板没打开")
        b = sc.text(r"^所有军队$")
        if b:
            self.dev.tap(*b.center, delay=0.8)
            sc = self.ui.grab()
        return sc

    def run(self):
        """返回领取的宝箱个数。"""
        sc = self.open_army_panel()
        claimed = 0
        for _ in range(20):
            b = sc.text(r"获得宝箱\s*[xX×]\s*\d+")
            if not b:
                break
            self.dev.tap(*b.center, delay=1.0)
            n, stop = self._drain()
            claimed += n
            if stop:
                break
            sc = self.ui.grab()
            if not sc.text(r"所有军队|拥有军队"):
                sc = self.open_army_panel()
        log.info("领取宝箱 %d 个", claimed)
        if claimed:
            self.bot.state.inc("chests", claimed)
        self.ui.to_map()
        return claimed

    def _drain(self):
        """宝箱弹窗里反复点“打开”/“领取”。返回 (领取数, 是否因库房满而停止)。"""
        n = 0
        for _ in range(60):
            sc = self.ui.grab()
            if sc.text(r"容量不足"):
                return n, self._storage_full(sc)
            b = None
            for t in sc.read(R_POPUP_BTN):
                if t.text.strip() in ("领取", "打开"):
                    b = t
                    break
            if not b:
                break
            if b.text.strip() == "领取":
                n += 1
            self.dev.tap(*b.center, delay=0.8)
        return n, False

    def _storage_full(self, sc):
        """库房满：去升级仓库/粮仓。返回 True 表示本轮停止领宝箱。"""
        b = sc.text(r"^升级(仓库|粮仓)")
        if not b:
            log.warning("库房容量不足，但没找到升级按钮")
            self.blocked_until = time.time() + 300
            return True
        what = "粮仓" if "粮仓" in b.text else "仓库"
        if self.bot.operations:
            log.info("%s已满，等待网页建筑升级队列处理", what)
            self.blocked_until = time.time() + 300
            self.ui.close(sc)
            return True
        log.info("领宝箱时%s满了，去升级%s", what, what)
        self.dev.tap(*b.center, delay=1.5)
        if self.bot.build.upgrade_open_panel(what):
            log.info("开始升级 %s", what)
        else:
            log.info("%s 现在升级不了（施工队列忙或资源不足），5 分钟后再领宝箱", what)
            self.blocked_until = time.time() + 300
        return True
