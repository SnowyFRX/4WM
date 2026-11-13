"""主循环：按优先级和时间间隔轮流执行各项任务，出错自动恢复。"""
import datetime
import logging
import os
import time
import traceback

import cv2
import yaml

from . import pause
from .device import Device
from .operations import REFRESH_SECONDS
from .state import State
from .ui import UI
from .vision import ROOT

log = logging.getLogger(__name__)
TASK_PHASES = {
    "chests": "正在检查编队宝箱",
    "quests": "正在检查任务奖励",
    "expand": "正在检查开荒路线",
    "build": "正在检查施工和升级队列",
    "recruit": "正在检查招募队列",
    "drill": "正在校场训练士兵",
    "hospital": "正在医馆治疗伤兵",
}


def load_config(path=None):
    path = path or os.path.join(ROOT, "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        dcfg = cfg.get("device", {})
        self.dev = Device(serial=dcfg.get("serial"), adb=dcfg.get("adb", "adb"))
        self.ui = UI(self.dev)
        self.state = State()
        self.route = None
        self.operations = None
        self.route_waiting_for_game = False
        if cfg.get("expand", {}).get("strategy") == "route":
            from .operations import Operations
            from .route_map import RouteMap

            self.operations = Operations()
            self.route = RouteMap(self.operations)
            self.route_waiting_for_game = True
        from .tasks.build import Build
        from .tasks.chests import Chests
        from .tasks.drill import Drill
        from .tasks.expand import Expand
        from .tasks.hospital import Hospital
        from .tasks.quests import Quests
        from .tasks.recruit import Recruit

        self.chests = Chests(self)
        self.expand = Expand(self)
        # 让网页能读写「打不动地块怎么办」这个开关
        if self.route is not None:
            self.route.expand = self.expand
            self.route.route_mode = str(self.cfg.get("expand", {}).get("route_mode", "strict"))
        self.build = Build(self)
        self.recruit = Recruit(self)
        self.quests = Quests(self)
        self.drill = Drill(self)
        self.hospital = Hospital(self)
        self._armies_cache = {}
        self._army_age = 0
        self.tasks = {
            "chests": self.chests.run,
            "expand": self.expand.run,
            "build": self.build.run,
            "recruit": self.recruit.run,
            "quests": self.quests.run,
            "drill": self.drill.run,
            "hospital": self.hospital.run,
        }

    def army_snapshot(self, force=False):
        """读一次编队状态（默认 20 秒缓存），供练兵/治疗/开荒判断谁“在主城待机”。

        以前缓存 90 秒，网页上的「待机中」能压住一整分半——队伍早就出门了还显示待机，
        所以用户看到的状态总是不实时。现在默认 20 秒，且可在 config 里改。
        """
        ttl = float((self.cfg.get("army") or {}).get("snapshot_seconds", 20))
        if not force and self._armies_cache and time.time() - self._army_age < ttl:
            return
        try:
            self._armies_cache = self.recruit.read_armies() or {}
            self._army_age = time.time()
            # 把读取时间也交给网页：状态到底是「刚刚」还是「两分钟前」要说得清
            if self.operations:
                self.operations.observe(armyAt=self._army_age, snapshotSeconds=REFRESH_SECONDS)
        except Exception as e:
            log.debug("编队状态读取失败: %s", e)

    def home_idle_armies(self):
        """当前“在主城且待机”的编队名（练兵/补兵只能在主城做，先筛一遍）。"""
        out = set()
        for name, info in (self._armies_cache or {}).items():
            st = (info or {}).get("state") or ""
            loc = (info or {}).get("location") or ""
            if "待机" in st and "主城" in loc:
                out.add(name)
        return out

    def dump(self, tag):
        d = os.path.join(ROOT, "logs", "shots")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s_%s.png" % (datetime.datetime.now().strftime("%m%d_%H%M%S"), tag))
        try:
            cv2.imencode(".png", self.dev.screenshot())[1].tofile(p)
        except Exception:
            pass
        return p

    def phase(self, detail, task=None, mode="running"):
        if self.route:
            self.route.phase(detail, task, mode)

    def ensure_game(self):
        if self.dev.game_running():
            return
        self.phase("正在启动游戏")
        log.warning("游戏不在前台，正在启动...")
        self.dev.start_game()
        # 等加载完：出现大地图/城内/登录界面
        for _ in range(30):
            time.sleep(4)
            kind = self.ui.classify(self.ui.grab())
            if kind in ("map", "map_popup", "city", "login", "agreement", "dialogue"):
                break

    def in_quiet_hours(self):
        q = self.cfg.get("schedule", {}).get("pause_expand_hours")
        if not q:
            return False
        h = datetime.datetime.now().hour
        start, end = q
        return start <= h < end if start < end else (h >= start or h < end)

    def run_task(self, name):
        log.info("==== 任务: %s ====", name)
        self.phase(TASK_PHASES.get(name, "正在执行任务"), name)
        route_step = self.route.next() if name == "expand" and self.route else None
        job = self.operations.head(name) if self.operations and name in ("build", "recruit", "drill") else None
        try:
            self.ensure_game()
            r = self.tasks[name]()
            self.state.touch(name)
            if route_step:
                self.phase(self.route.status, name, "done" if r or self.route.next() != route_step else "waiting")
            elif job:
                message = self.operations.task_message(name)
                if message:
                    head = self.operations.head(name)
                    done = (name in ("recruit", "drill") and r) or not head or head["id"] != job["id"]
                    self.phase(message, name, "done" if done else "waiting")
            self.quick_checks(skip=name)
            return r
        except Exception as e:
            self.phase("任务遇到错误，正在恢复游戏界面。", name, "error")
            log.error("任务 %s 出错: %s", name, e)
            log.debug(traceback.format_exc())
            log.error("截图已保存: %s", self.dump("err_" + name))
            try:
                self.ui.to_map()
            except Exception:
                pass
            self.state.touch(name)  # 出错也推迟，避免死循环
            return None

    def quick_checks(self, skip=None):
        """不等定时：军队按钮有宝箱角标就领宝箱，任务卷轴有红点就领奖。只看一张截图，很快。"""
        try:
            img = self.dev.screenshot()
            if not self.ui.on_map(self.ui.grab()):
                return
            if skip != "chests" and self.chests.pending(img):
                self.phase("正在领取编队宝箱", "chests")
                log.info("有宝箱可领，立即领取")
                self.chests.run()
                self.state.touch("chests")
                img = self.dev.screenshot()
            if skip != "quests" and self.quests.pending():
                self.phase("正在领取任务奖励", "quests")
                log.info("任务卷轴有红点，立即领取奖励")
                self.quests.run()
                self.state.touch("quests")
        except Exception as e:
            self.phase("奖励检查遇到错误，正在恢复游戏界面。", "game", "error")
            log.error("快速检查出错: %s", e)
            log.debug(traceback.format_exc())
            try:
                self.ui.to_map()
            except Exception:
                pass

    def resume_reset(self):
        """从暂停里回来：把 adb 连接和游戏界面都拉回脚本假设的样子。

        暂停期间用户可能进了建筑详情、编队面板，甚至把游戏切到后台；
        脚本要是直接接着干，就会按「现在正停在大地图」的假设去点，
        结果点歪、等不到预期画面、一路超时 —— 手感就是「恢复后特别迟钝」。
        恢复后先重建 adb 连接（空闲过的管道第一条命令容易超时），
        再把界面拉回大地图，然后才允许进入任务。
        """
        log.info("恢复后先复位：重建 adb 连接、回到大地图")
        try:
            self.dev.reset_link()
        except Exception:
            log.debug("重建 adb 连接失败", exc_info=True)
        try:
            self.ui.to_map()
        except Exception:
            log.warning("恢复后没能回到大地图，下一轮任务会再试", exc_info=True)

    def loop(self):
        sched = self.cfg.get("schedule", {})
        intervals = sched.get("intervals", {})
        enabled = sched.get("tasks", ["chests", "quests", "build", "recruit", "expand"])
        idle = sched.get("idle_seconds", 20)
        log.info("开始运行，任务: %s", ", ".join(enabled))
        while True:
            # 暂停：停在这里不动手，等用户点「继续」。进程、网页、状态都还在。
            if pause.paused():
                self.phase("脚本已暂停，模拟器交给你手动操作", "idle", "paused")
                pause.wait()
                self.resume_reset()
                self.phase("已恢复，继续按计划执行", "idle", "running")
            # 地图扫描是有状态的慢活（要回城、拖镜头、拼图），一旦开始就做完，
            # 不再像以前那样在每个任务前插一次、把一次扫描切成几段（那样看起来在做一半就跑去干别的）。
            if self.route:
                self.route.wake.clear()
                self.run_map_scan()
                if self.route_waiting_for_game:
                    self.route.wake.wait(idle)
                    continue
                self.refresh_operations()
                self.observe_resources()
            ran = False
            for name in enabled:
                # 网页同步是用户正在等的即时指令，优先于普通轮询任务。
                if self.route and self.route.take_scan_peek() is not None:
                    self.run_map_scan()
                    ran = True
                    break
                if name == "expand" and self.in_quiet_hours():
                    continue
                new_route = name == "expand" and self.expand.route_pending()
                changed = self.operations and self.operations.take_changed(name)
                interval = intervals.get(name, 600)
                if self.operations and name in ("build", "recruit", "drill") and self.operations.head(name):
                    # 队列有活干就快跑；但资源守卫冷却中就按普通间隔，别空转
                    if self.operations.guard_ready(name):
                        interval = min(interval, 25)
                    else:
                        interval = min(interval, 60)
                if new_route or changed or self.state.due(name, interval):
                    self.run_task(name)
                    ran = True
            if not ran:
                if not self.route_waiting_for_game:
                    self.quick_checks()
                if self.route:
                    if not self.route_waiting_for_game:
                        self.phase("等待下一轮任务或网页指令", "idle", "idle")
                    self.route.wake.wait(idle)
                else:
                    time.sleep(idle)

    def run_map_scan(self):
        """把网页请求的地图扫描一口气做完（含预读四角），中途不切去别的任务。"""
        while True:
            if not self.scan_route_map():
                return
            # 扫描过程中网页可能又提了新请求（比如拖动了视野），继续做完再返回
            if self.route and self.route.take_scan_peek() is None:
                return

    def scan_route_map(self, prefetch=True):
        from .route_camera import GameNotReady, HomeUnavailable

        request = self.route.take_scan()
        if request is None:
            return False
        target, warm = request
        self.phase(f"正在扫描视野 {target}" if target else "正在读取主城周边地图", "map")
        try:
            if not self.dev.game_running():
                self.phase("正在启动游戏", "game")
                self.dev.start_game()
                raise GameNotReady("游戏正在启动，请在进入大地图后读取。")
            self.expand.route_camera.publish(self.route, target, warm=warm and prefetch)
        except GameNotReady as exc:
            self.phase(str(exc), "game", "waiting")
            self.route_waiting_for_game = True
            self.route.scan_failed(str(exc), retry=True, target=target)
        except HomeUnavailable as exc:
            self.phase(str(exc), "map", "waiting")
            self.route_waiting_for_game = False
            log.debug("网页地图自动回城失败，本轮稍后重试", exc_info=True)
            self.route.scan_failed(str(exc), retry=True, target=target)
        except Exception as exc:
            self.phase("地图读取遇到错误，详情见地图提示。", "map", "error")
            self.route_waiting_for_game = False
            log.debug("网页地图读取失败", exc_info=True)
            self.route.scan_failed(str(exc), retry=self.route.view is None)
        else:
            self.route_waiting_for_game = False
            self.phase("地图视野已更新", "map", "done")
        return True

    def observe_resources(self, sc=None):
        """读一次顶栏资源，记进 operations 供网页显示 + 资源判断。每 60 秒最多一次。"""
        if not self.operations:
            return
        now = time.time()
        if getattr(self, "_res_age", 0) and now - self._res_age < 60:
            return
        try:
            from . import resources as R
            sc = sc or self.ui.grab()
            # 资源条只在大地图上完整可见：城里面板会挡住左侧那列（特殊资源全读不到）。
            # 不在大地图就先回地图再读；回不去就这轮不读，别用错画面的数据覆盖。
            if not self.ui.on_map(sc):
                try:
                    sc = self.ui.to_map(auto_login=False)
                except Exception:
                    log.debug("读资源前回地图失败，本轮跳过")
                    return
            if not self.ui.on_map(sc):
                return
            snap = R.snapshot(sc)
            amounts = snap["amounts"]
            # 自检：粮木石必须都读到，否则说明抓错画面了（例如停在弹出提示上）
            if not all(amounts.get(k) for k in ("grain", "wood", "stone")):
                log.debug("资源读数不完整，本轮跳过：%s", amounts)
                return
            self.operations.set_resources(amounts, snap["fill"], snap.get("points"))
            self._res_age = now
            log.info("资源：%s", amounts)
        except Exception:
            log.debug("资源读取失败", exc_info=True)

    def refresh_operations(self):
        if not self.operations.take_refresh():
            return
        # 步骤带序号，网页上一眼能看出现在做到第几步、还剩几步
        self.phase("刷新 1/4：正在读军队面板（编队兵力和位置）", "refresh")
        error = ""
        armies = {}
        try:
            armies = self.recruit.read_armies()
            # 这次读取同时当「编队快照」用：时间戳跟上（以前只在练兵那边写，
            # 练兵一停，网页上的「读取于 N 秒前」就一路涨到几百秒），
            # 缓存也一并填好，army_snapshot() 直接复用，不必再开一次面板。
            self._armies_cache = armies or {}
            self._army_age = time.time()
            self.operations.observe(armies=armies, armyAt=self._army_age,
                                    snapshotSeconds=REFRESH_SECONDS)
        except Exception:
            log.debug("网页军队刷新失败", exc_info=True)
            error = "军队面板读取未完成，可手动刷新。"
        try:
            self.phase("刷新 2/4：正在读取建筑等级", "refresh")
            # 建筑等级只首次读一遍并缓存；以后启动直接用缓存。
            # 用户下达升级命令时，queued_upgrade 只核对那一座，不再全城逐座点。
            if not self.build.load_cache():
                self.phase("刷新 2/4：首次读取建筑等级（只读这一次）", "refresh")
                self.build.scan()
            self.phase("刷新 3/4：正在核对兵营和可用兵种", "refresh")
            self.recruit.refresh(armies)
        except Exception:
            log.debug("网页建筑和军队刷新失败", exc_info=True)
            error = "游戏状态读取未完成，可手动刷新。"
        finally:
            self.operations.refreshed(error)
            self.autotune()
            self.phase("刷新 4/4：" + (error or "全部完成，状态已更新"), "refresh",
                       "error" if error else "done")

    def autotune(self):
        """按账号现状自动调整参数（主城等级、编队兵力变了就跟着变）。

        只覆盖 config 里为 null 的项，你自己写的数字不动。
        """
        if not self.operations:
            return
        try:
            from . import autotune as AT
            changed, profile = AT.apply(self.cfg, self.operations.facts)
            if profile:
                self.operations.observe(autotune=profile)
            if changed:
                self.operations.observe(
                    autotuneChanged=["%s=%s" % (p.split(".")[-1], v) for p, v in changed])
        except Exception:
            log.debug("自动调整参数失败", exc_info=True)
