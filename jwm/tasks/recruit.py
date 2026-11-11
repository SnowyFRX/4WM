"""兵营招兵：给在主城、未满员的编队补兵；都在外面时新建编队。

实测规律：
  - 只能给在主城的编队招兵，标着“外出”的编队招募按钮是灰的；
  - 每队人数上限由主城等级决定（1 级 3 人、2 级 5 人……最多 9 人），面板里用剪影显示空位；
  - 再次点击已选中的兵种会弹出士兵详情，所以已选中就不要再点。
"""
import base64
import logging
import re
import time

import cv2
import numpy as np

from .build import find_badges

log = logging.getLogger(__name__)

SLOT_X = [103, 215, 327, 438, 550]  # “选择士兵”一排的 5 个兵种格
SLOT_Y = 632
BTN = (575, 795)  # 招募按钮


class Recruit:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui
        self.cfg = bot.cfg.get("recruit", {})

    def open_barracks(self):
        sc = self.ui.to_city()
        pos = self.bot.state.data.get("building_pos", {}).get("兵营")
        cands = [tuple(pos)] if pos else []
        cands += [p for p in find_badges(sc.img) if not pos or p != tuple(pos)]
        for bx, by in cands:
            self.dev.tap(bx, by + 45, delay=1.3)
            sc = self.ui.grab()
            if not (sc.text(r"选择军队") and sc.text(r"选择士兵")):
                if sc.text(r"^兵营$", (100, 250, 400, 360)):
                    self.ui.tap_text(sc, r"^招募士兵$", delay=1.2)
                    sc = self.ui.grab()
                elif sc.text(r"建筑信息|内政"):
                    self.ui.close()
                    continue
                else:
                    self.ui.to_city()
                    continue
            if sc.text(r"选择军队"):
                self.bot.state.data.setdefault("building_pos", {})["兵营"] = [bx, by]
                self.bot.state.save()
                return sc
        return None

    def cards(self, sc):
        """[(名字框, 士兵数, 是否外出)]"""
        out = []
        names = [b for b in sc.boxes if 320 < b.center[1] < 372 and b.box[0] > 200]
        for nb in names:
            x1, x2 = nb.box[0] - 10, nb.box[0] + 180
            n, away = None, False
            for b in sc.boxes:
                if not (372 <= b.center[1] < 410 and x1 <= b.center[0] <= x2):
                    continue
                t = b.text.strip()
                if "外出" in t:
                    away = True
                m = re.match(r"^\D?(\d+)", t)
                if m and n is None and "外出" not in t:
                    n = int(m.group(1))
            if n is not None:
                out.append((nb, n, away))
        return out

    def army_slots(self, sc):
        b = sc.text(r"^\d+/\d+$", (560, 260, 700, 320))
        if b:
            a, c = b.text.split("/")
            return int(a), int(c)
        return None

    @staticmethod
    def button_active(img):
        r, g, b = [int(v) for v in img[BTN[1] - 10, BTN[0] - 55][::-1]]
        return r > 245 and b < 135

    @staticmethod
    def _index(name):
        """编队名里的序号 -> 0-based（编队1=0）。决定它该造近战还是远程。"""
        m = re.search(r"(\d+)", name or "")
        return int(m.group(1)) - 1 if m else 99

    @staticmethod
    def is_unlocked(img, slot):
        """“选择士兵”一排某个兵种格是否已解锁（锁着的格子有金色锁图标）。"""
        x = SLOT_X[slot - 1]
        roi = img[SLOT_Y - 35:SLOT_Y + 35, x - 35:x + 35].astype(int)
        r, g, b = roi[..., 2], roi[..., 1], roi[..., 0]
        gold = ((r > 190) & (g > 150) & (b < 110)).mean()
        return gold < 0.04

    @staticmethod
    def slot_selected(img, slot):
        x = SLOT_X[slot - 1]
        roi = img[SLOT_Y - 52:SLOT_Y + 52, x - 52:x + 52].astype(int)
        green = (roi[:, :, 1] > 150) & (roi[:, :, 2] < 150) & (roi[:, :, 0] < 120)
        return green.mean() > 0.03

    def queue(self, sc):
        b = sc.text(r"招募队列")
        if b:
            m = re.search(r"(\d+)\s*/\s*(\d+)", b.text)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def capacity(img):
        """选中编队后上方那排人像：实心=已有士兵，淡色剪影=空位。返回 (已有, 上限)。"""
        roi = img[455:510, 60:660]
        bg = np.median(roi.reshape(-1, 3), axis=0)
        diff = np.abs(roi.astype(int) - bg).sum(axis=2) > 40
        xs = np.nonzero(diff.sum(axis=0) > 3)[0]
        groups = []
        for x in xs:
            if groups and x - groups[-1][-1] <= 8:
                groups[-1].append(x)
            else:
                groups.append([x])
        groups = [g for g in groups if g[-1] - g[0] >= 20]
        if not groups:
            return None
        v = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2]
        filled = sum(1 for g in groups if v[:, g[0]:g[-1] + 1][diff[:, g[0]:g[-1] + 1]].mean() < 180)
        return filled, len(groups)

    def all_cards(self, sc):
        """编队卡片一排可能放不下，往左滑动收集全部。返回 {名字: (名字框, 人数, 外出)}。"""
        found = {}
        for _ in range(3):
            for c in self.cards(sc):
                found.setdefault(c[0].text, c)
            slots = self.army_slots(sc)
            if not slots or len(found) >= slots[0]:
                break
            self.dev.swipe(600, 368, 250, 368, 400, delay=0.8)
            sc = self.ui.grab()
        return found, sc

    def select(self, name):
        """在卡片排里找到并选中某个编队（必要时左右滑动）。"""
        for direction in ("right", "right", "left", "left", "left", None):
            sc = self.ui.grab()
            for nb, n, away in self.cards(sc):
                if nb.text == name:
                    self.dev.tap(nb.center[0], nb.center[1] + 15, delay=0.8)
                    return self.ui.grab()
            if direction == "right":
                self.dev.swipe(250, 368, 650, 368, 400, delay=0.8)
            elif direction == "left":
                self.dev.swipe(600, 368, 250, 368, 400, delay=0.8)
        return None

    def read_armies(self):
        """从军队面板读编队状态/位置/兵力/血量/兵种/行军倒计时（用 army_panel.parse_armies）。

        返回 {名字: {state, location, hp, soldiers, mix, timer, chests, units}}。
        units 是士兵头像的 base64 PNG，网页直接显示——比猜兵种名可靠。
        """
        import base64

        import cv2

        from ..army_panel import parse_armies

        self.bot.phase("正在打开所有军队面板")
        armies = {}
        complete = {}         # 该队的士兵区是否完整进入过视野
        sc = self.bot.chests.open_army_panel()
        per_page = 4          # 军队面板一屏固定放 4 张卡（实测）
        card_h = 176          # 卡片行高（实测）
        pages = 1
        no_new = 0            # 连续几屏没出现新编队（用来判断真的到底了）
        for page in range(1, 25):
            self.bot.phase(f"正在读取军队面板第 {page} 屏")
            # 关键：解析和裁图必须用同一张截图。翻页后 ny 会变，跨屏混用会截到别的卡片。
            items, owned, _cap = parse_armies(sc)
            fresh = 0
            for it in items:
                if it["name"] not in armies:
                    fresh += 1
                # 卡片贴屏幕底边时，士兵图会被截断/截不到。只有 y2 在屏幕内才算完整。
                # 之前只看“名字都读到了”就 break，编队4刚好在底边，于是永远是 0/9 没图。
                name = it["name"]
                complete[name] = bool(complete.get(name)) or (it["ny"] + 95 <= 1080)
                strip = it.get("units_img")
                png = ""
                if strip is not None and strip.size:
                    ok, buf = cv2.imencode(".png", strip)
                    if ok:
                        png = base64.b64encode(buf).decode("ascii")
                prev = armies.get(it["name"]) or {}
                # 这一屏卡片不完整（贴屏幕底边）时，兵力/血量/构成会读错或读成 0。
                # 那时保留之前读到的值，别用坏数据覆盖好数据。
                bad = not it["soldiers"]
                armies[it["name"]] = {
                    "state": it["state"] or prev.get("state", ""),
                    "location": it["pos"] or prev.get("location", ""),
                    "hp": it["hp"] if it["hp"] is not None else prev.get("hp"),
                    "soldiers": prev.get("soldiers", 0) if bad else it["soldiers"],
                    "mix": it["mix"] or prev.get("mix", {}),
                    "timer": it["timer"] or prev.get("timer", ""),
                    "chests": it["chests"] or prev.get("chests", 0),
                    "units": png or prev.get("units", ""),
                }
            seen_all = owned is not None and len(armies) >= owned
            # 名字见全了还不够：每一队的士兵区都得完整出现过。
            # 只要有一队还贴在屏幕底边，就继续往下滑一屏再读。
            if seen_all and all(complete.get(n) for n in armies):
                break
            # 连续两屏都没出现新编队 = 真的到底了。
            # 以前只靠「页面完全没变化」判停，滑到尽头后同一批卡片被反复解析，
            # 于是出现「6 支（拥有 5）」这种自相矛盾、还带假条目的读数。
            no_new = 0 if fresh else no_new + 1
            if no_new >= 2:
                break
            # 每次只滑一张卡的高度：一屏 4 张，9 支就要滑 8 次。
            # 滑太多会把夹在屏与屏之间的那张整个跳过（「挨揍」就是这么丢的）。
            old = sc
            self.dev.swipe(370, 900, 370, 900 - card_h, 420, delay=0.6)
            sc = self.ui.grab()
            if self.ui._same(old.img, sc.img, tol=0.8):
                break
        # 读到的条目比「拥有 N 支」还多，说明有伪造条目混进来：只保留有名字且有内容的
        if owned is not None and len(armies) > owned:
            log.info("读到 %d 支多于编队总数 %d，丢弃没有位置/兵力的伪条目", len(armies), owned)
            real = {n: v for n, v in armies.items()
                    if (v.get("location") or v.get("soldiers") or v.get("units"))}
            while len(real) > owned:
                real.popitem()
            if real:
                armies = real
        missing = [n for n in armies if not complete.get(n)]
        log.info("读取编队完成：%d 支（拥有 %s）%s", len(armies), owned,
                 "，未读全：" + "、".join(missing) if missing else "")
        self.ui.to_map()
        return armies

    def refresh(self, armies):
        armies = {name: dict(info) for name, info in armies.items()}
        sc = self.open_barracks()
        if sc is None:
            self.bot.operations.observe(armies=armies)
            return
        cards, sc = self.all_cards(sc)
        for name, (_, count, away) in cards.items():
            item = armies.setdefault(name, {})
            item.update(count=count, away=away)
            # 兵营卡片只知道「在队里/外出」，不知道行军中还是驻守中。
            # 以前这里直接写「在主城/外出」，把刚读到的真实状态盖掉了，
            # 网页于是一直显示笼统的「在主城」，看着像不同步。
            if not item.get("state"):
                item["state"] = "外出" if away else "在主城"
        ok, strip = cv2.imencode(".jpg", sc.img[SLOT_Y-42:SLOT_Y+42, 58:596])
        self.bot.operations.observe(armies=armies, armySlots=self.army_slots(sc),
                                    gameRecruit=self.queue(sc),
                                    unlocked=[i for i in range(1, 6) if self.is_unlocked(sc.img, i)],
                                    soldierStrip=base64.b64encode(strip).decode("ascii") if ok else "")
        self.ui.to_city()

    def queued_recruit(self, job):
        ops = self.bot.operations
        name, slot = job["army"], job["slot"]
        if job.get("verify"):
            ops.message("recruit", job, "上次点击后未核实是否受理。请核对游戏，再决定是否重试。")
            return 0
        self.bot.phase(f"正在打开兵营，核对{name}和第 {slot} 格兵种")
        if self.open_barracks() is None:
            ops.message("recruit", job, "还未识别到兵营。")
            return 0
        try:
            sc = self.select(name)
            card = next((c for c in self.cards(sc) if c[0].text.strip() == name), None) if sc else None
            if not card:
                ops.message("recruit", job, f"未识别到{name}。")
                return 0
            if card[2]:
                ops.message("recruit", job, f"等待{name}回到主城。")
                return 0
            if not self.is_unlocked(sc.img, slot):
                # 兵种没解锁 = 这个任务当前根本做不到。
                # 以前只是 return 0，任务永远留在队列里，每 120 秒重试一次、每次都白跑，
                # 表现就是「recruit 一直在跑却什么都不做」。现在明确暂停并告诉你。
                ops.message("recruit", job, f"第 {slot} 格兵种尚未解锁，该任务已暂停（解锁后会自动继续）。")
                ops.guard("recruit", 3600, f"{name} 的第 {slot} 格兵种未解锁。")
                return 0
            if not self.slot_selected(sc.img, slot):
                self.dev.tap(SLOT_X[slot - 1], SLOT_Y, delay=0.8)
                sc = self.ui.grab()
            q = self.queue(sc)
            cap = self.capacity(sc.img)
            self.bot.phase(f"正在检查{name}人数和游戏招募位")
            ops.observe(gameRecruit=q)
            if q and q[0] >= q[1]:
                ops.message("recruit", job, "等待游戏招募队列空位。")
                return 0
            if cap and cap[0] >= min(cap[1], self.cfg.get("max_per_army", 9)):
                ops.message("recruit", job, f"{name}已满员。")
                return 0
            button = sc.text(r"^招募$", (470, 730, 690, 850))
            if not (q and cap and button and self.slot_selected(sc.img, slot)
                    and self.button_active(sc.img)):
                # 招募按钮不亮 = 资源不足 或 兵营/空位条件不满足。别每 25 秒硬点，
                # 暂停队列等资源恢复（resource_wait_seconds），避免无意义尝试。
                ops.message("recruit", job, "等待资源或招募位，已暂停本队列。")
                ops.guard("recruit", self.cfg.get("resource_wait_seconds", 600), "资源不足，暂停招募。")
                return 0
            self.bot.phase(f"正在提交{name}的招募指令并检查回执")
            if not ops.attempt("recruit", job, lambda: self.dev.tap(*button.center), verify=True):
                return 0
            after = self.ui.grab_stable()
            new_q = self.queue(after)
            new_cap = self.capacity(after.img)
            ops.observe(gameRecruit=new_q)
            if (new_q and new_q[0] > q[0]) or (new_cap and new_cap[0] > cap[0]):
                ops.complete("recruit", job)
                self.bot.state.inc("recruited")
                self.bot.state.reset_hard()
                return 1
            ops.message("recruit", job, "上次点击后未核实是否受理。请核对游戏，再决定是否重试。")
            return 0
        finally:
            self.ui.to_city()

    def _away(self, sc, name):
        for nb, n, away in self.cards(sc):
            if nb.text == name:
                return away
        return False

    def fill(self, name, slot):
        """给当前选中的编队补兵，直到满员/队列满/资源不足。返回招募人数。"""
        sc = self.ui.grab()
        if not self.slot_selected(sc.img, slot):
            self.dev.tap(SLOT_X[slot - 1], SLOT_Y, delay=0.8)
        n = 0
        for _ in range(12):
            sc = self.ui.grab()
            q = self.queue(sc)
            if q and q[0] >= q[1]:
                self.queue_full = True
                break
            if not self.button_active(sc.img):
                break
            self.dev.tap(*BTN, delay=0.8)
            n += 1
        if n:
            log.info("为 %s 招募了 %d 名士兵", name, n)
        return n

    def run(self):
        if self.bot.operations:
            job = self.bot.operations.head("recruit")
            if not job:
                return 0
            if not self.bot.operations.guard_ready("recruit"):
                return 0  # 资源不足冷却中，不硬试
            return self.queued_recruit(job)
        per_army = self.cfg.get("max_per_army", 9)
        melee_count = self.cfg.get("melee_count", 2)
        melee_slot = self.cfg.get("melee_slot", 1)
        ranged_slot = self.cfg.get("ranged_slot", self.cfg.get("soldier_slot", 2))
        focus = self.cfg.get("focus_slot")

        def want_slot(index):
            # 前 melee_count 支编队造近战，其余造远程；focus_slot 非空则一律用它（你指定重点兵种）
            if focus:
                return focus
            return melee_slot if index < melee_count else ranged_slot

        sc = self.open_barracks()
        if sc is None:
            log.info("没找到兵营")
            self.ui.to_map()
            return 0
        self.queue_full = False
        cards, sc = self.all_cards(sc)
        unlocked = {s for s in range(1, 6) if self.is_unlocked(sc.img, s)}
        if not unlocked:
            log.info("没有已解锁的兵种，跳过招兵")
            self.ui.to_map()
            return 0
        total = 0
        understrength = []
        cap = None
        for name, (nb, have, away) in sorted(cards.items(), key=lambda kv: kv[1][1]):
            sc = self.select(name)
            if sc is None:
                continue
            c = self.capacity(sc.img)
            if c:
                have, cap = c
            limit = min(per_army, cap or per_army)
            if have >= limit:
                continue
            if away:
                understrength.append(name)  # 在外面没法补，记下来让开荒任务召回
                continue
            if not self.queue_full:
                ws = want_slot(self._index(name))
                if ws in unlocked:
                    total += self.fill(name, ws)
                else:
                    log.info("%s 想招的兵种(槽%d)还没解锁，跳过", name, ws)
        # 还有编队名额就新建编队（但想造的兵种没解锁就留空位，不硬造别的兵种）
        slots = self.army_slots(self.ui.grab())
        while (self.cfg.get("create_armies", True) and slots and slots[0] < slots[1]
               and not self.queue_full):
            ws = want_slot(slots[0])  # 新队序号 = 当前已有队数
            if ws not in unlocked:
                log.info("第%d支编队想造的兵种(槽%d)还没解锁，留空位等你解锁", slots[0] + 1, ws)
                break
            log.info("新建一支编队 (%d/%d)", *slots)
            self.dev.swipe(250, 368, 650, 368, 400, delay=0.8)  # “+” 在最左边
            self.dev.tap(132, 368, delay=1.0)
            if not self.ui.tap_text(self.ui.grab(), r"^创建$", delay=1.2):
                break
            n = self.fill("新编队", ws)
            total += n
            new_slots = self.army_slots(self.ui.grab())
            if not n or new_slots == slots:
                break
            slots = new_slots
        # 在外面的缺员编队：先调回主城（只能给在主城的编队招兵），到了再补满
        # 注意：正在战斗/行军中的绝不召回 —— 那是打断出征，不是补兵。
        if understrength and not self.queue_full:
            busy = set(self.bot.expand._busy_armies())
            hold = [n for n in understrength if n in busy]
            understrength = [n for n in understrength if n not in busy]
            if hold:
                log.info("这些编队正在战斗/行军，先不召回补兵：%s", "、".join(hold))
        if understrength and not self.queue_full:
            log.info("在外面缺员且空闲的编队：%s，调回主城补兵", "、".join(understrength))
            self.ui.to_map()
            back = self.bot.expand.recall(names=understrength)
            wait_max = self.cfg.get("recall_wait_max", 90)
            near = [(n, s) for n, s in back if s <= wait_max]
            if near:
                wait = max(s for _, s in near) + 3
                log.info("等编队回城 %d 秒", wait)
                time.sleep(wait)
                if self.open_barracks() is not None:
                    for name, _ in near:
                        sc = self.select(name)
                        if sc is None:
                            continue
                        c = self.capacity(sc.img)
                        if c and c[0] >= min(per_army, c[1]):
                            continue
                        if self._away(sc, name):
                            log.info("%s 还没回到主城，下次再补", name)
                            continue
                        ws = want_slot(self._index(name))
                        if ws in unlocked:
                            total += self.fill(name, ws)
                        if name in understrength:
                            understrength.remove(name)
            # 太远的（行军超过 recall_wait_max）下次招兵时再补
        self.bot.state.data["understrength"] = understrength
        self.bot.state.save()
        if total:
            self.bot.state.inc("recruited", total)
            k = self.bot.state.reset_hard()
            if k:
                log.info("兵力增加，%d 块之前打不动的地允许重试", k)
        elif not understrength:
            log.info("没有需要补兵的编队")
        self.ui.to_map()
        return total
