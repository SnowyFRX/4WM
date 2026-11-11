"""城内建筑升级（只升级已有建筑，不打开修建列表）。

识别方式：
  - 城内每个建筑头上有白色等级小盾牌，点它下方的建筑打开面板；
  - 面板里 OCR 读建筑名/等级，“升级费用”或“升级条件”出现红字 = 资源不足或条件未满足；
  - 正在施工的建筑头上显示倒计时，用来判断施工队列是否已满。

为了快：第一次把每个建筑点一遍，记住“建筑名 → 位置”（data/state.json 的 building_pos），
之后按优先级直接点要升级的那个；位置对不上时再重新扫描。
资源快满时（顶部资源条 ≥95%）优先升级仓库（木/石）或粮仓（粮），避免资源溢出浪费。
"""
import logging
import re
import time

import cv2
import numpy as np

from ..vision import OCR

log = logging.getLogger(__name__)

BUILDING_NAMES = ["城墙", "主城", "粮仓", "仓库", "兵营", "大使馆", "自由市场", "研究所", "铁匠铺", "马厩", "工厂",
                  "校场", "里亭属", "边塞营", "联盟市场", "英雄殿", "医馆", "铁牢"]
R_UPGRADE_BTN = (200, 960, 520, 1120)
# 顶部资源条（y 坐标），条子在 x 57~219
RES_BARS = {"grain": 44, "wood": 94, "stone": 145}
FULL_RATIO = 0.95


def red_ratio(img, region):
    x1, y1, x2, y2 = [int(v) for v in region]
    roi = img[max(0, y1):y2, max(0, x1):x2]
    if roi.size == 0:
        return 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    m = ((h <= 10) | (h >= 170)) & (s >= 100) & (v >= 100)
    return float(m.mean())


def resource_fill(img):
    """顶部三条资源条的填充比例 {grain, wood, stone}（蓝色=存量，满了变红）。"""
    out = {}
    for name, y in RES_BARS.items():
        best = None
        for yy in range(y - 3, y + 4):
            row = img[yy, 57:220].astype(int)
            filled = ((row[:, 0] > 180) & (row[:, 2] < 140)) | ((row[:, 2] > 180) & (row[:, 1] < 120) & (row[:, 0] < 120))
            empty = (abs(row[:, 2] - 219) < 20) & (abs(row[:, 1] - 190) < 20) & (abs(row[:, 0] - 166) < 20)
            if filled.sum() + empty.sum() > 120:
                ratio = filled.sum() / float(filled.sum() + empty.sum())
                best = ratio if best is None else max(best, ratio)
        out[name] = best
    return out


def find_badges(img):
    """城内建筑等级盾牌的位置 [(cx, cy)]。"""
    m = (img.min(axis=2) >= 245).astype(np.uint8)
    num, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, num):
        x, y, w, h, a = stats[i]
        if not (20 <= w <= 34 and 20 <= h <= 34 and 300 <= a <= 1000 and 160 < y < 1120):
            continue
        if x < 90 and y > 1040:  # 左下角聊天按钮上的白色气泡，不是建筑盾牌
            continue
        ring = img[max(0, y - 3):y + h + 3, max(0, x - 3):x + w + 3]
        if (ring.max(axis=2) < 110).mean() < 0.18:
            continue
        out.append((int(x + w / 2), int(y + h / 2)))
    return out


class Build:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui
        self.cfg = bot.cfg.get("build", {})

    @property
    def positions(self):
        return self.bot.state.data.setdefault("building_pos", {})

    # ---------- 建筑等级缓存：只首次读数，后面升级时再核对 ----------
    CACHE_KEY = "building_cache"

    def load_cache(self):
        """把上次读到的建筑等级填回网页，返回 True = 不用再一座座点。"""
        ops = self.bot.operations
        if not ops or not ops.home:
            return False
        cache = self.bot.state.data.get(self.CACHE_KEY) or {}
        if cache.get("home") != list(ops.home):
            return False
        buildings = cache.get("buildings") or {}
        if not buildings:
            return False
        ops.observe(buildings={name: dict(info) for name, info in buildings.items()},
                    buildingsCachedAt=cache.get("at", 0),
                    construction=cache.get("construction"))
        log.info("沿用上次建筑等级（%s，%d 座），升级时再核对",
                 time.strftime("%m-%d %H:%M", time.localtime(cache.get("at", 0))),
                 len(buildings))
        return True

    def remember_buildings(self):
        """把当前 facts 里的建筑等级存进 state，下次启动直接复用。"""
        ops = self.bot.operations
        if not ops or not ops.home:
            return
        buildings = ops.facts.get("buildings") or {}
        if not buildings:
            return
        self.bot.state.data[self.CACHE_KEY] = {
            "home": list(ops.home),
            "at": time.time(),
            "buildings": {name: dict(info) for name, info in buildings.items()},
            "construction": ops.facts.get("construction"),
        }
        self.bot.state.save()

    # ---------- 面板解析 ----------
    def read_panel(self, sc):
        info = {"name": None, "level": None, "can_upgrade": False, "busy": False}
        for b in sc.boxes:
            t = b.text.strip()
            if info["name"] is None and t in BUILDING_NAMES and 250 < b.center[1] < 360:
                info["name"] = t
            m = re.fullmatch(r"等级\s*(\d+)", t)
            if m and info["level"] is None and b.center[1] < 400:
                info["level"] = int(m.group(1))
        btn = sc.text(r"^升级$", R_UPGRADE_BTN)
        if sc.text(r"升级中|加速|已满级|最高等级"):
            info["busy"] = True
        if btn and not info["busy"]:
            # 费用/条件区域出现红字 = 不满足
            info["can_upgrade"] = red_ratio(sc.img, (40, 820, 680, btn.box[1] - 5)) < 0.002
            info["btn"] = btn.center
        if self.bot.operations:
            self.bot.operations.building(info)
        return info

    def open_at(self, pos):
        """点开某个位置的建筑，返回面板 Screen（不是建筑面板则返回 None）。"""
        bx, by = pos
        self.dev.tap(bx, by + 45, delay=1.2)
        sc = self.ui.grab()
        if not sc.text(r"建筑信息"):
            self.ui.to_city()
            return None
        if not sc.text(r"^等级\d+$"):
            # 面板会记住上次的页签（如兵营停在“招募士兵”），切回“建筑信息”
            self.ui.tap_text(sc, r"^建筑信息$", delay=0.8)
            sc = self.ui.grab()
        return sc

    def skipped(self, name):
        until = self.bot.state.data.get("build_skip", {}).get(name, 0)
        return time.time() < until

    def skip(self, name, seconds, why):
        self.bot.state.data.setdefault("build_skip", {})[name] = int(time.time() + seconds)
        self.bot.state.save()
        log.info("  %s 升不了：%s（%d 分钟内跳过）", name, why, seconds // 60)

    def do_upgrade(self, sc, info, approve=None):
        """在已打开的建筑面板上点升级。返回 True 表示已开始升级。"""
        if not info["can_upgrade"]:
            return False
        btn = sc.text(r"^升级$", R_UPGRADE_BTN)
        if not btn:
            return False
        self.bot.phase(f"正在提交{info['name']}升级并检查游戏回执")
        if approve:
            if not approve(lambda: self.dev.tap(*btn.center)):
                return False
        else:
            self.dev.tap(*btn.center)
        # 升不了时游戏只弹一个很快消失的提示，连拍抓它
        toast = None
        for _ in range(8):
            time.sleep(0.1)
            for b in OCR.read(self.dev.screenshot(), (0, 300, 720, 1000)):
                if re.search(r"解锁更多等级|新手村|队列|不足|需要", b.text):
                    toast = b.text
                    break
            if toast:
                break
        if toast:
            self.skip(info["name"], 3600 if "新手村" in toast or "解锁" in toast else 300, toast)
            return False
        time.sleep(0.6)
        sc = self.ui.grab()
        if sc.text(r"元宝|充值|购买|队列"):
            # 不花元宝；施工队列满时游戏会提示花元宝加队列，一律取消
            log.info("升级 %s 需要元宝或施工队列已满，已取消", info["name"])
            self.ui.close(sc)
            return False
        if sc.text(r"^升级$", R_UPGRADE_BTN) and sc.text(r"建筑信息"):
            return False  # 面板还在 = 没升成
        log.info("开始升级 %s → %s 级", info["name"], (info["level"] or 0) + 1)
        self.bot.state.inc("upgrades")
        if self.bot.operations:
            # 升级命令本身就是一次核对：只更新这一座，不用回头全城再点一遍
            self.bot.operations.building({"name": info["name"],
                                          "level": (info["level"] or 0) + 1, "busy": True})
            self.remember_buildings()
        return True

    # ---------- 扫描 ----------
    def scan(self, after_panel=None):
        """把城里每个建筑点一遍，记住位置。"""
        self.bot.phase("正在进入城内读取建筑")
        sc = self.ui.to_city()
        badges = find_badges(sc.img)
        if self.bot.operations:
            busy_positions = [tuple(pos) for pos in self.positions.values()
                              if not any(abs(pos[0] - x) < 12 and abs(pos[1] - y) < 12 for x, y in badges)]
            badges += list(dict.fromkeys(busy_positions))
            self.bot.operations.observe(construction=[self.busy_count(sc), self.cfg.get("queue_size", 1)])
        log.info("扫描城内建筑（%d 个）", len(badges))
        # 合并而不是清空：施工中的建筑头上是倒计时、没有盾牌，扫不到，保留旧位置
        for index, pos in enumerate(badges, 1):
            self.bot.phase(f"正在确认第 {index}/{len(badges)} 座建筑")
            sc = self.open_at(pos)
            if sc is not None:
                info = self.read_panel(sc)
                if info["name"]:
                    self.positions[info["name"]] = list(pos)
                self.ui.close()
            if after_panel and after_panel():
                self.bot.phase("正在返回城内继续扫描建筑", "refresh")
                self.ui.to_city()
        self.bot.state.save()
        self.remember_buildings()

    def queued_upgrade(self, job):
        ops = self.bot.operations
        name = job["name"]
        self.bot.phase(f"正在确认{name}等级和施工状态")
        city = self.ui.to_city()
        busy = self.busy_count(city)
        limit = self.cfg.get("queue_size", 1)
        ops.observe(construction=[busy, limit])
        pos = self.positions.get(name)
        sc = self.open_at(pos) if pos else None
        info = self.read_panel(sc) if sc else None
        if not info or info["name"] != name:
            if sc:
                self.ui.close(sc)
            self.scan()
            self.bot.phase(f"正在重新确认{name}等级和施工状态")
            pos = self.positions.get(name)
            sc = self.open_at(pos) if pos else None
            info = self.read_panel(sc) if sc else None
        if not info or info["name"] != name or info["level"] is None:
            ops.message("build", job, f"等待识别{name}的等级。")
        elif "base" in job and info["level"] > job["base"] and (
                not info["busy"] or sc.text(r"已满级|最高等级")):
            ops.complete("build", job)
        elif busy >= limit or info["busy"]:
            ops.message("build", job, f"等待{name}完工或游戏施工位空出。")
        elif not info["can_upgrade"] or self.skipped(name):
            ops.message("build", job, f"{name}暂不满足升级条件，保留队首。")
            # 费用/条件不足：暂停这个队列，别每 25 秒重开一次建筑面板空试
            ops.guard("build", self.cfg.get("resource_wait_seconds", 600), f"{name}资源或条件不足，稍后重试。")
        else:
            level = info["level"]
            started = self.do_upgrade(sc, info, approve=lambda click: ops.attempt(
                "build", job, click, base=level))
            ops.message("build", job, f"{name} {level} → {level + 1} 级，等待游戏确认。"
                        if started else f"{name}未确认开始升级，下轮核对等级后重试。")
            self.ui.to_city()
            return int(started)
        if sc:
            self.ui.close(sc)
        return 0

    def busy_count(self, sc):
        """城内施工中的建筑数（建筑头上的倒计时；漏读时用右下角施工队列图标兜底）。"""
        timers = [b for b in sc.boxes if re.fullmatch(r"\d+:\d{2}(:\d{2})?", b.text.strip())
                  and b.center[0] < 620 and 200 < b.center[1] < 1050]
        n = len(timers)
        if n == 0:
            corner = [b for b in sc.boxes if re.fullmatch(r"\d+:\d{2}(:\d{2})?", b.text.strip())
                      and b.center[0] > 620 and 900 < b.center[1] < 1000]
            n = 1 if corner else 0
        return n

    def order(self, img):
        prio = list(self.cfg.get("upgrade_priority", []))
        fill = resource_fill(img)
        urgent = []
        if (fill.get("wood") or 0) >= FULL_RATIO or (fill.get("stone") or 0) >= FULL_RATIO:
            urgent.append("仓库")
        if (fill.get("grain") or 0) >= FULL_RATIO:
            urgent.append("粮仓")
        if urgent:
            log.info("资源快满了（%s），优先升级 %s",
                     " ".join("%s%.0f%%" % (k, (v or 0) * 100) for k, v in fill.items()), "、".join(urgent))
        return urgent + [n for n in prio if n not in urgent]

    # ---------- 升级 ----------
    def upgrade(self):
        sc = self.ui.to_city()
        busy = self.busy_count(sc)
        max_queue = self.cfg.get("queue_size", 1)
        if busy >= max_queue:
            log.info("施工队列已满 (%d/%d)", busy, max_queue)
            return 0
        order = self.order(sc.img)
        if not order:
            return 0
        if not self.positions or len(self.positions) < len(find_badges(sc.img)):
            self.scan()
        done = 0
        for name in order:
            if done >= max_queue - busy:
                break
            pos = self.positions.get(name)
            if not pos or self.skipped(name):
                continue
            sc = self.open_at(pos)
            info = self.read_panel(sc) if sc else None
            if not info or info["name"] != name:
                log.info("%s 的位置变了，重新扫描", name)
                self.ui.to_city()
                self.scan()
                break
            if self.do_upgrade(sc, info):
                done += 1
            else:
                log.debug("  %s 等级%s 暂不能升级", name, info["level"])
                self.ui.close()
            self.ui.to_city()
        return done

    def upgrade_open_panel(self, name):
        """面板已经被别处打开（例如领宝箱时点了“升级仓库”），直接尝试升级。"""
        sc = self.ui.grab()
        if not sc.text(r"建筑信息"):
            return False
        info = self.read_panel(sc)
        info["name"] = info["name"] or name
        ok = self.do_upgrade(sc, info)
        if not ok:
            self.ui.close()
        return ok

    def open_build_list(self):
        self.ui.to_city()
        self.dev.tap(583, 1197, delay=1.8)
        sc = self.ui.grab()
        if not sc.text(r"建筑列表"):
            self.ui.close()
            return None
        return sc

    @staticmethod
    def _buildable(img, box):
        """修建按钮：黄色(255,217,106)=可建，灰色(222,215,207)=条件不足。取文字框左侧采样。"""
        x = max(0, box[0] - 22)
        y = int((box[1] + box[3]) / 2)
        r, g, bl = [int(v) for v in img[y, x][::-1]]
        return r > 240 and bl < 175

    def construct(self):
        """前期把能建的新建筑都建起来（修建列表里黄色“修建”按钮）。一次建一个，占一个施工队列。"""
        self.bot.phase("正在检查施工位和可修建建筑")
        sc = self.ui.to_city()
        if self.busy_count(sc) >= self.cfg.get("queue_size", 1):
            return 0
        if self.open_build_list() is None:
            self.ui.to_city()
            return 0
        done = 0
        for _ in range(5):
            sc = self.ui.grab()
            btns = [b for b in sc.boxes if "修" in b.text and len(b.text) <= 3
                    and b.center[0] > 480 and 250 < b.center[1] < 1080]
            hit = next((b for b in sorted(btns, key=lambda b: b.center[1])
                        if self._buildable(sc.img, b.box)), None)
            if hit:
                self.dev.tap(*hit.center, delay=1.5)
                log.info("修建新建筑")
                self.bot.state.inc("builds")
                done = 1
                break
            self.dev.swipe(360, 950, 360, 350, 700, delay=0.8)
        self.ui.to_city()
        return done

    def run(self):
        if self.bot.operations:
            job = self.bot.operations.head("build")
            if job:
                if not self.bot.operations.guard_ready("build"):
                    return 0  # 资源不足冷却中，不硬试
                return self.queued_upgrade(job)
            return self.construct() if self.cfg.get("construct", False) else 0
        n = 0
        if self.cfg.get("construct", False):
            n = self.construct()
        if n == 0:
            n = self.upgrade()
        # 不主动回地图：loop 里 build 后常接 recruit（同在城内），避免来回进出城
        return n
