"""校场：训练士兵升级。

界面结构（真机量出来的）：
  - 上面一排编队卡片（可以左右滑，“外出”的编队不能训练）；
  - 选中编队后，下面是一排**兵种头像**（每个兵一个头像，可左右滑，点哪个选哪个）；
  - 点头像 → 升级效果显示“血量+X / 攻击+Y / 等级+1”，再点“训练”排进队列；
  - 兵营等级不够时，升级效果那里写“需要兵营15级”，而且“训练”按钮根本不出现。

所以“训练一支编队”= 逐个点它的每个兵、能训的就训一个，而不是只点第一个。
训练顺序由你定（config 的 drill.priority），**只训你填进去的编队**，没填就一个都不训。
"""
import logging
import re
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)

CARD_Y = (320, 380)     # 编队卡片那一行
CARD_TAP_Y = 365        # 点卡片的 y
AVATAR_Y = 494          # 兵种头像行的中心 y
AVATAR_DX = 112         # 头像间距
AVATAR_X0 = 105         # 第一个头像中心 x（滑过之后会变，实际按检测结果走）


class Drill:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui

    @property
    def cfg(self):
        return self.bot.cfg.get("drill") or {}

    # ---------------- 打开校场 ----------------
    def open_drill(self):
        from .build import find_badges
        sc = self.ui.to_city()
        pos = (self.bot.state.data.get("building_pos") or {}).get("校场")
        cands = [tuple(pos)] if pos else []
        cands += [p for p in find_badges(sc.img) if tuple(p) not in cands]
        # 施工中的建筑头上是倒计时、没有盾牌，也补一份候选位置
        for b in sc.boxes:
            t = b.text.strip()
            if re.fullmatch(r"\d+:\d{2}(:\d{2})?", t) and b.center[0] < 620 and 160 < b.center[1] < 1100:
                cands.append((int(b.center[0]), int(b.center[1]) + 40))
        for bx, by in cands:
            self.dev.tap(bx, by + 45, delay=1.4)
            sc = self.ui.grab()
            # 服务器横幅（“XX 在点将台获得了…”）经常盖住弹窗标题，
            # 所以“选择军队/训练队列”也算数，别只认“训练士兵”四个字。
            if not (sc.text(r"训练士兵") or sc.text(r"选择军队|训练队列")):
                self.ui.close()
                continue
            if not sc.text(r"选择军队"):
                if self.ui.tap_text(sc, r"^训练士兵$", delay=1.2):
                    sc = self.ui.grab()
            if sc.text(r"选择军队"):
                self.bot.state.data.setdefault("building_pos", {})["校场"] = [bx, by]
                self.bot.state.save()
                return sc
            self.ui.close()
        return None

    # ---------------- 读界面 ----------------
    @staticmethod
    def cards(sc):
        """编队卡片：[(名字, 中心x, 是否外出, 卡片下方的数字)]。

        卡片下方那个数字（y≈384 那一行）越大，说明这支队伍练得越深。
        智能练兵顺序就靠它排：等级高的优先练（练上去收益最大）。
        """
        out = []
        for b in sc.boxes:
            t = b.text.strip()
            if not re.fullmatch(r"编队\s*\d+", t) or not (CARD_Y[0] < b.center[1] < CARD_Y[1]):
                continue
            away = False
            for o in sc.boxes:
                if "外出" not in o.text:
                    continue
                if abs(o.center[1] - b.center[1] - 33) < 22 and abs(o.center[0] - b.center[0]) < 90:
                    away = True
                    break
            lv = 0
            for o in sc.boxes:
                if not re.fullmatch(r"\d{1,4}", o.text.strip()):
                    continue
                if 0 < o.center[1] - b.center[1] < 55 and abs(o.center[0] - b.center[0]) < 60:
                    lv = max(lv, int(o.text.strip()))
            out.append((t, b.center[0], away, lv))
        return out

    def _smart_order(self, cards):
        """没填顺序时自己排：正在训练的优先，然后按等级（卡片下方数字）从高到低。

        cards: [(名字, x, 是否外出, 数字)]
        """
        # 训练队列里有兵在训，就先把“正在训的那支”找出来接着练 —— 游戏里
        # 训练队列是按编队排的，中途换编队会打断进度。
        training = self.bot.state.data.get("drill_training")
        rows = list(cards)
        rows.sort(key=lambda c: (-(c[3] or 0), c[0]))
        order = [c[0] for c in rows]
        if training in order:
            order.remove(training)
            order.insert(0, training)
            log.debug("接着练上次训练中的 %s", training)
        return order

    @staticmethod
    def queue(sc):
        b = sc.text(r"训练队列")
        if b:
            m = re.search(r"(\d+)\s*/\s*(\d+)", b.text)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def avatar_centers(img):
        """这一屏能看到几个兵，各自的中心 x。

        头像有棕色/银灰各种颜色，所以不用颜色区分，用“有饱和度或明显偏暗”来找，
        背景是均匀的米黄（S≈45、V≈239），一挡就出来了。
        """
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        band = hsv[AVATAR_Y - 39:AVATAR_Y + 41, :]
        s = band[:, :, 1].astype(int)
        v = band[:, :, 2].astype(int)
        m = ((s >= 70) | (v < 180)).astype(np.uint8)
        col = m.mean(axis=0)
        runs = []
        st = None
        for i, val in enumerate(col > 0.12):
            if val and st is None:
                st = i
            if not val and st is not None:
                if i - st > 45:
                    runs.append((st + i - 1) // 2)
                st = None
        if st is not None and len(col) - st > 45:
            runs.append((st + len(col) - 1) // 2)
        return runs

    # ---------------- 选中编队 ----------------
    def select_army(self, name):
        """左右滑编队卡片，找到并选中指定编队。返回 (sc, 说明)。

        卡片一行放不下所有编队（6 队只能看到 3~4 张），而且默认可能停在中间，
        所以先往回滑到最左，再一张张往右找。
        """
        def names(sc):
            return [c[0] for c in self.cards(sc)]

        # 先滑到最左
        for _ in range(5):
            before = names(self.ui.grab())
            self.dev.swipe(150, CARD_TAP_Y, 650, CARD_TAP_Y, 350, delay=0.6)
            if names(self.ui.grab()) == before:
                break
        # 再从左往右找
        for _ in range(8):
            sc = self.ui.grab()
            cards = self.cards(sc)
            for card in cards:
                nm, cx, away = card[0], card[1], card[2]
                if nm != name:
                    continue
                if away:
                    return None, "编队在外面，游戏不让训练"
                self.dev.tap(cx, CARD_TAP_Y, delay=1.0)
                return self.ui.grab(), ""
            before = [c[0] for c in cards]
            self.dev.swipe(650, CARD_TAP_Y, 150, CARD_TAP_Y, 400, delay=0.7)
            if [c[0] for c in self.cards(self.ui.grab())] == before:
                break  # 卡片滑不动了 = 已经到头
        return None, "校场里没找到这张编队卡片"

    # ---------------- 训练 ----------------
    def train_soldier(self, x):
        """选中第 x 个兵并训练。返回 (ok, 说明)。"""
        self.dev.tap(x, AVATAR_Y, delay=0.6)
        sc = self.ui.grab()
        # 兵营等级不够 / 资源不够 / 已满级时，“训练”按钮根本不会出现
        btn = sc.text(r"^训练$")
        if not btn:
            # 点到第一个头像（英雄位）会打开「兵种详情」而不是选兵面板，
            # 详情里有“出手速度 / 等级N / x/y”这些字样。这种要关掉，别卡在详情页里
            # 一直往下点（否则后面每个兵都在详情页上乱点）。
            if sc.text(r"出手速度|攻击范围") or sc.text(r"^等级\d+$"):
                log.debug("    兵@%d 打开的是兵种详情，关掉继续", x)
                self.ui.close(sc)
                return False, "点开的是兵种详情（英雄位）"
            eff = " ".join(b.text for b in sc.boxes if 590 < b.center[1] < 700).strip()
            return False, eff or "这个兵现在训不了"
        self.dev.tap(*btn.center, delay=1.6)
        sc = self.ui.grab()
        if sc.text(r"元宝|立即完成|购买"):
            self.ui.close(sc)  # 不花元宝
            return False, "游戏提示花元宝，已取消"
        return True, ""

    def queued_drill(self, job):
        """按网页队列训练指定编队。返回训练了几个兵。

        资源不足/编队不在主城时不硬试：记一条消息并把队列暂停一会儿，
        等资源恢复或编队回城再继续（避免每 25 秒反复点同一处被拒绝）。
        """
        ops = self.bot.operations
        name = job["army"]
        want = int(job.get("count") or 1)
        self.bot.phase(f"正在校场训练{name}（队列 {want} 个）")
        try:
            self.bot.army_snapshot()
        except Exception:
            pass
        home_idle = self.bot.home_idle_armies()
        known = bool(getattr(self.bot, "_armies_cache", None))
        if known and name not in home_idle:
            ops.message("drill", job, f"等待{name}回到主城（当前位置不在主城）。")
            ops.guard("drill", 120, f"等待{name}回到主城。")
            return 0
        sc = self.open_drill()
        if sc is None:
            ops.message("drill", job, "未找到校场，先让城建扫描一遍城内建筑。")
            ops.guard("drill", 300, "未找到校场。")
            return 0
        q = self.queue(sc)
        if q and q[0] >= q[1]:
            ops.message("drill", job, "等待游戏训练队列空位。")
            ops.guard("drill", 180, "游戏训练队列已满。")
            self.ui.to_map()
            return 0
        picked, why = self.select_army(name)
        if picked is None:
            picked, why = self.select_army(name)  # 界面可能停在详情，再试一次
        if picked is None:
            ops.message("drill", job, f"未选中{name}：{why}")
            self.ui.to_map()
            return 0
        got, blocked = self._train_army_checked(name, limit=want)
        if got:
            self.bot.state.inc("trained", got)
            self.bot.state.data["drill_last_ok"] = time.time()
            self.bot.state.data["drill_training"] = name
            self.bot.state.save()
            k = self.bot.state.reset_hard()
            ops.complete("drill", job, spent=got)
            ops.message("drill", job, f"{name} 训练了 {got} 个兵。")
            if k:
                log.info("  兵练上了，之前打不动的 %d 块地解除冷却", k)
        elif blocked == "resource":
            # 资源不足：这是“无意义尝试”的典型来源，暂停队列等资源恢复
            ops.message("drill", job, f"{name} 资源不足，暂停练兵等资源恢复。")
            ops.guard("drill", self.cfg.get("resource_wait_seconds", 600), "资源不足，暂停练兵。")
        elif blocked == "queue":
            ops.message("drill", job, "训练队列已满，稍后继续。")
            ops.guard("drill", 180, "训练队列已满。")
        else:
            ops.message("drill", job, f"{name} 暂时没有可训练的兵。")
            ops.guard("drill", 300, "没有可训练的兵。")
        self.ui.to_map()
        return got

    def _train_army_checked(self, name, limit=9):
        """训练到 limit 个为止。返回 (训练数, 阻断原因)。

        阻断原因：resource=资源不足（训练按钮不出现且提示费用不足）、
        queue=训练队列满、None=正常结束。
        """
        got = 0
        last_slide = None
        for _round in range(4):
            sc = self.ui.grab()
            centers = self.avatar_centers(sc.img)
            for x in centers:
                if got >= limit:
                    return got, None
                if x < 60 or x > 690:
                    continue
                ok, why = self.train_soldier(x)
                if not ok:
                    if why and ("资源" in why or "不足" in why):
                        return got, "resource"
                    log.debug("    兵@%d 没训：%s", x, why)
                    continue
                got += 1
                q = self.queue(self.ui.grab())
                if q and q[0] >= q[1]:
                    return got, ("queue" if got < limit else None)
            before = tuple(centers)
            if before == last_slide:
                break
            last_slide = before
            self.dev.swipe(640, AVATAR_Y, 120, AVATAR_Y, 400, delay=0.8)
        return got, None

    def run(self):
        if not self.cfg.get("enabled", True):
            return 0
        # 网页队列优先：用户在控制台安排了练兵任务就按队列执行
        if self.bot.operations:
            job = self.bot.operations.head("drill")
            if job:
                if not self.bot.operations.guard_ready("drill"):
                    return 0  # 资源不足/队列满，冷却中不硬试
                return self.queued_drill(job)
        # 练兵顺序（三级优先）：
        #   1. 用户在控制台填了顺序 → 严格按用户填的来
        #   2. 没填 → 先接着练“用户正在训练的那支”（游戏里各卡片谁是训练中）
        #   3. 再按卡片下方数字从大到小（等级练得最深的优先）
        order = [x for x in (self.cfg.get("priority") or []) if x]
        user_set = bool(order)
        try:
            self.bot.army_snapshot()  # 先刷一次编队状态，好判断谁在主城
        except Exception:
            pass
        sc = self.open_drill()
        if sc is None:
            log.info("城里没找到校场（可能还没修）")
            return 0
        cards = self.cards(sc)
        if not cards:
            self.ui.close()
            return 0
        if not user_set:
            order = self._smart_order(cards)
            log.info("练兵顺序没填，按智能顺序来：%s", "、".join(order))
        home_idle = self.bot.home_idle_armies()  # 实时状态里“在主城待机”的编队
        # known = 成功读到了编队状态（区别于“没读到”）。真机发现：全部编队都在外待机时
        # home_idle 是空集（falsy），若用 home_idle 判断，away 永远为空 → 不召回、空点训练。
        known = bool(getattr(self.bot, "_armies_cache", None))
        # 练兵要求编队在城里待机。可 expand 会把这些编队一直派出去打地，结果
        # “编队在外面，游戏不让训练” → 兵种等级永远升不上去 → 又打不动高级地，
        # 死锁。所以先把要练的编队召回，练完再让它们出去。
        away = [n for n in order if known and n not in home_idle]
        if away:
            # 别每次都召回：编队刚被派出去打地就被拉回来，来回白跑。
            # 只有在“确实很久没练成过兵”时才打断它们。
            last_ok = float(self.bot.state.data.get("drill_last_ok") or 0)
            quiet_h = float(self.cfg.get("recall_for_drill_hours", 2))
            if time.time() - last_ok >= quiet_h * 3600:
                log.info("要练兵的编队在外面（%s），先召回（已经 %.1f 小时没练成过）",
                         "、".join(away), (time.time() - last_ok) / 3600 if last_ok else 999)
                try:
                    self.bot.expand.recall(names=away, thr=1.0)
                    self.bot.army_snapshot()
                    home_idle = self.bot.home_idle_armies()
                except Exception as e:
                    log.warning("  召回失败：%s", e)
                # recall 会离开校场、去大地图点「行军」选编队，结束后停在地图。
                # 必须重新打开校场，否则后面 select_army 在地图上找不到编队卡片（真机踩过）。
                sc = self.open_drill()
                if sc is None:
                    log.info("召回后没能重新打开校场，本轮先到这")
                    self.ui.to_map()
                    return 0
            else:
                log.debug("要练的编队在外面，但最近练过，先不打断它们")
        n = 0
        for name in order:
            if known and name not in home_idle:
                log.info("  %s 不在主城，跳过训练（按实时状态）", name)
                continue
            if sc is None:  # 上一轮 select_army 失败丢了界面状态，重开校场兜底
                sc = self.open_drill()
                if sc is None:
                    break
            q = self.queue(sc)
            if q and q[0] >= q[1]:
                log.info("训练队列满了 (%d/%d)，这轮先到这", q[0], q[1])
                break
            picked, why = self.select_army(name)
            if picked is None:
                # 连续训练多支时，上一支练完界面可能停在兵种详情/卡片已滑动，
                # 导致下一支定位不到。重开校场回到主界面再试一次。
                sc = self.open_drill()
                if sc is not None:
                    picked, why = self.select_army(name)
            if picked is None:
                log.info("  %s：%s", name, why)
                sc = self.ui.grab()  # 补一帧，别让 sc 被置空导致下一轮 queue(None) 崩溃
                continue
            sc = picked
            got = self._train_army(name)
            n += got
            if got:
                self.bot.state.inc("trained", got)
                self.bot.state.data["drill_last_ok"] = time.time()
                self.bot.state.data["drill_training"] = name  # 记下正在练哪支，下轮接着练
                self.bot.state.save()
                log.info("  %s：排了 %d 个兵进训练队列", name, got)
                # 练兵 = 兵种等级提升 = 战力变强，之前判定“打不动”的地应该立刻能重试，
                # 别让它们白等 12 小时冷却（招兵那边已经有这个联动，练兵漏了）。
                k = self.bot.state.reset_hard()
                if k:
                    log.info("  兵练上了，之前打不动的 %d 块地解除冷却", k)
            else:
                log.debug("  %s：没有能训的兵", name)
            sc = self.ui.grab()
        self.ui.to_map()
        return n

    def _train_army(self, name):
        """把当前选中编队里能训的兵逐个排进队列。返回排了几个。"""
        got = 0
        last_slide = None
        for _round in range(4):
            progressed = False
            sc = self.ui.grab()
            centers = self.avatar_centers(sc.img)
            for x in centers:
                if x < 60 or x > 690:
                    continue  # 被面板边缘切掉一半，点了也不准
                ok, why = self.train_soldier(x)
                if not ok:
                    log.debug("    兵@%d 没训：%s", x, why)
                    continue
                got += 1
                progressed = True
                q = self.queue(self.ui.grab())
                if q and q[0] >= q[1]:
                    log.info("    训练队列满了 (%d/%d)", q[0], q[1])
                    return got
            # 这一屏的都看过了，兵多的时候横向滑一屏继续
            before = tuple(centers)
            if before == last_slide:
                break  # 滑到头了，位置没变
            last_slide = before
            self.dev.swipe(640, AVATAR_Y, 120, AVATAR_Y, 400, delay=0.8)
        return got
