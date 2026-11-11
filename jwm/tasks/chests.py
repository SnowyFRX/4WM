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
    MAX_PAGES = 8          # 编队上限 9 支，一屏 4 张，8 屏足够覆盖且有上限保护
    RETRY_EMPTY = 180      # 角标在但找不到可领入口时，隔 3 分钟再看（别每个周期都空转）

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
        """返回领取的宝箱个数。

        军队面板一屏只放 4 张卡，第 5 支往下的宝箱要往下翻才看得到。
        以前只看当前屏，所以经常「明明有角标却领取 0 个」。
        """
        from ..scroll import walk

        box = {"sc": self.open_army_panel(), "claimed": 0, "stop": False}

        def page(screen):
            box["sc"] = screen
            for _ in range(20):
                b = screen.text(r"获得宝箱\s*[xX×]\s*\d+")
                if not b:
                    return True          # 这一屏领完了，交给 walk 翻下一页
                self.dev.tap(*b.center, delay=1.0)
                n, stop = self._drain()
                box["claimed"] += n
                if stop:
                    box["stop"] = True
                    return False     # 库房满了，别再翻
                screen = self.ui.grab()
                box["sc"] = screen
                if not screen.text(r"所有军队|拥有军队"):
                    screen = self.open_army_panel()
                    box["sc"] = screen
            return not box["stop"]

        pages = walk(lambda: box["sc"],
                     lambda: self.dev.swipe(370, 900, 370, 724, 420, delay=0.7),
                     page, max_pages=self.MAX_PAGES)
        claimed = box["claimed"]
        log.info("领取宝箱 %d 个（看了 %d 屏）", claimed, pages)
        if claimed:
            self.bot.state.inc("chests", claimed)
        else:
            # 真机实测（2026-11-11 20:07）：军队按钮右上角明明有金色宝箱角标，
            # 但「所有军队」5 张卡翻到底、连「战报」页签也翻过，都没有「获得宝箱」入口。
            # 以前这里什么都不记，于是每个任务周期都开一次面板、空转一轮、刷一条日志。
            # 现在如实上报并短期退避，等游戏把入口放出来再看。
            self.blocked_until = time.time() + self.RETRY_EMPTY
            log.warning("宝箱角标亮着，但军队面板/战报里都没有可领入口；%d 分钟后再看",
                        self.RETRY_EMPTY // 60)
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
