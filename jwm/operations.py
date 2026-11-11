"""网页上的脚本指令队列，与游戏内的施工和招募队列分别记录。"""
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from .vision import ROOT

log = logging.getLogger(__name__)

MAX_JOBS = 1000

# 用户自定义开荒小队：只记录「小队名 + 哪几支编队」。
# 不再按资源类型/等级/难度做规则匹配 —— 用户选哪几支，开荒就只派哪几支。
# 结构：{"squads": [{"name": "一队", "armies": ["编队2", "编队4"], "on": true}],
#        "active": "一队"}


class Operations:
    def __init__(self, path=Path(ROOT, "data", "operation_queues.json")):
        self.lock = threading.Lock()
        self.path = Path(path)
        # 编队组合预设单独存，跟队列分开，方便单独备份/交接
        self.squad_path = Path(ROOT, "data", "army_combos.json")
        self.squads = []          # [{name, armies:[...], on:bool}]
        self.active_squad = None  # 这次开荒用哪个小队；None = 不启用预设
        self._load_squads()
        self.home = None
        self.queues = {"build": [], "recruit": [], "drill": []}
        self.messages = {"build": "", "recruit": "", "drill": ""}
        self.changed = set()
        self.blocked = {}  # kind -> monotonic 截止时间（资源不足时暂停该队列）
        self.facts = {}
        self.next_refresh = 0
        self.refreshing = False
        self.refresh_error = ""

    # ---------- 用户自定义开荒小队 ----------
    def _load_squads(self):
        try:
            data = json.loads(self.squad_path.read_text(encoding="utf-8"))
            items = data.get("squads") if isinstance(data, dict) else data
            if not isinstance(items, list) and isinstance(data, dict):
                items = data.get("combos") or []   # 兼容旧文件，但丢掉旧的 match 规则
            if not isinstance(items, list):
                items = []
            self.squads = [{"name": str(c.get("name") or "小队%d" % (i + 1))[:20],
                            "armies": [str(a) for a in c.get("armies") or [] if str(a).strip()][:9],
                            "on": bool(c.get("on", True)),
                            "route": str(c.get("route") or "")[:64]}
                           for i, c in enumerate(items)
                           if isinstance(c, dict) and c.get("armies")]
            active = data.get("active") if isinstance(data, dict) else None
            self.active_squad = active if any(s["name"] == active for s in self.squads) else None
        except (OSError, ValueError):
            self.squads = []
            self.active_squad = None

    def _save_squads(self):
        try:
            self.squad_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.squad_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"squads": self.squads, "active": self.active_squad},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.squad_path)
        except OSError:
            log.warning("保存开荒小队失败", exc_info=True)

    def squad_snapshot(self):
        with self.lock:
            return [dict(s) for s in self.squads]

    def squad_set(self, squads, active=None):
        """整表覆盖。小队只存名字、成员，以及可选的路线绑定；不存任何地块匹配规则。"""
        if not isinstance(squads, list):
            raise ValueError("小队数据格式不对。")
        clean = []
        for c in squads[:50]:
            if not isinstance(c, dict):
                continue
            armies = c.get("armies")
            if not isinstance(armies, list) or not armies:
                continue
            armies = [str(a) for a in armies if str(a).strip()][:9]
            if not armies:
                continue
            name = str(c.get("name") or "").strip()[:20] or "小队%d" % (len(clean) + 1)
            while any(s["name"] == name for s in clean):
                name = (name + "2")[:20]
            clean.append({"name": name, "armies": armies, "on": bool(c.get("on", True)),
                          "route": str(c.get("route") or "")[:64]})
        with self.lock:
            self.squads = clean
            names = [s["name"] for s in clean]
            self.active_squad = active if active in names else (names[0] if names else None)
            self._save_squads()
            self.changed.add("expand")
        return clean

    def set_active_squad(self, name):
        """选择这次开荒用哪个小队；None/空 = 不启用预设，脚本自动挑。"""
        with self.lock:
            if name in (None, "", "auto"):
                self.active_squad = None
            elif any(s["name"] == name for s in self.squads):
                self.active_squad = name
            else:
                raise ValueError("没有这个小队。")
            self._save_squads()
            self.changed.add("expand")
            return self.active_squad

    def squad_pick(self, route_key=None):
        """挑这次开荒用哪个小队。

        - 用户选了某个小队，就按用户选的；
        - 该小队绑定了某条路线（route_key），只在打那条路线时生效；
        - 没有绑定路线的小队当作通用小队，任何路线都能用；
        - 没有可用小队返回 None（脚本才回到自动挑兵）。
        """
        with self.lock:
            usable = [s for s in self.squads if s.get("on", True)]
            if not usable:
                return None
            ordered = ([s for s in usable if s["name"] == self.active_squad]
                       + [s for s in usable if s["name"] != self.active_squad])
            for s in ordered:
                bound = s.get("route") or ""
                if bound and route_key and bound != route_key:
                    continue   # 这条小队属于别的路线，别插手
                if bound and not route_key:
                    continue   # 绑了路线但当前不知道是哪条，谨慎起见不抢
                return dict(s)
            return None

    def squad_routes(self):
        """小队 -> 它绑定的路线（给网页显示用）。"""
        with self.lock:
            return {s["name"]: s.get("route") or "" for s in self.squads}

    def identify(self, home):
        with self.lock:
            if self.home == home:
                return
            self.home = home
            self.queues = {"build": [], "recruit": [], "drill": []}
            self.messages = {"build": "", "recruit": "", "drill": ""}
            self.facts = {}
            self.next_refresh = 0
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and saved.get("home") == list(home):
                    queues = saved["queues"]
                    if all(isinstance(queues[k], list) and all(isinstance(j, dict) and "id" in j for j in queues[k])
                           for k in self.queues):
                        self.queues = queues
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.changed = {k for k, q in self.queues.items() if q}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"home": self.home, "queues": self.queues}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self):
        from .tasks.build import BUILDING_NAMES

        with self.lock:
            return dict(queues={k: [dict(j) for j in q] for k, q in self.queues.items()},
                        messages=dict(self.messages), facts=dict(self.facts),
                        squads=[dict(s) for s in self.squads], activeSquad=self.active_squad,
                        blocked={k: max(0, int(v - time.monotonic())) for k, v in self.blocked.items()},
                        refreshing=self.refreshing, refreshError=self.refresh_error, buildingNames=BUILDING_NAMES)

    def command(self, data):
        from .tasks.build import BUILDING_NAMES

        op, kind = data.get("op"), data.get("kind")
        # 小队设置要在取锁之前处理：squad_set 自己会加同一把锁
        if op == "save_squads":
            self.squad_set(data.get("squads") or [], data.get("active"))
            return
        if op == "set_squad":
            self.set_active_squad(data.get("name"))
            return
        with self.lock:
            if op == "refresh":
                if not self.refreshing:
                    self.next_refresh = 0
                return
            if not self.home or kind not in ("build", "recruit", "drill"):
                raise ValueError("请先读取主城。")
            queue = self.queues[kind]
            if op == "add":
                count = data.get("count", 1)
                if type(count) is not int or not 1 <= count <= MAX_JOBS:
                    raise ValueError("数量须为 1–1000。")
                added = count if kind == "build" else 1
                if len(queue) + added > MAX_JOBS:
                    raise ValueError("每种脚本队列最多 1000 项。")
                if kind == "build":
                    name = data.get("name")
                    if name not in BUILDING_NAMES:
                        raise ValueError("请选择建筑。")
                    queue.extend({"id": secrets.token_hex(8), "name": name} for _ in range(added))
                elif kind == "drill":
                    army = data.get("army")
                    if not isinstance(army, str) or army not in self.facts.get("armies", {}):
                        raise ValueError("请选择已读取的编队。")
                    count = max(1, min(count, 9))
                    slot = data.get("slot", 0)
                    if slot not in (0, 1, 2, 3, 4, 5):
                        raise ValueError("兵种格只能是 1–5。")
                    queue.append(dict(id=secrets.token_hex(8), army=army, count=count,
                                      slot=int(slot)))
                else:
                    army, slot = data.get("army"), data.get("slot")
                    if (not isinstance(army, str) or army not in self.facts.get("armies", {})
                            or type(slot) is not int or not 1 <= slot <= 5):
                        raise ValueError("请选择已读取的编队和兵种格。")
                    queue.append(dict(id=secrets.token_hex(8), army=army, slot=slot, count=count))
            else:
                index = next((i for i, job in enumerate(queue) if job["id"] == data.get("id")), None)
                if index is None:
                    raise ValueError("该条指令已变化，请刷新后重试。")
                if op == "remove":
                    queue.pop(index)
                elif op in ("up", "down"):
                    other = index + (-1 if op == "up" else 1)
                    if 0 <= other < len(queue):
                        if kind == "build" and ("base" in queue[index] or "base" in queue[other]):
                            raise ValueError("这项升级正在核对进度，完成后才能调整顺序。")
                        queue[index], queue[other] = queue[other], queue[index]
                elif op == "retry" and kind == "recruit" and queue[index].get("verify"):
                    queue[index].pop("verify")
                else:
                    raise ValueError("无效的队列操作。")
            self._save()
            self.changed.add(kind)
            self.messages[kind] = ""

    def take_changed(self, kind):
        with self.lock:
            if kind in self.changed:
                self.changed.remove(kind)
                return True
            return False

    def take_refresh(self):
        with self.lock:
            if not self.home or time.monotonic() < self.next_refresh:
                return False
            self.refreshing = True
            self.refresh_error = ""
            return True

    def refreshed(self, error=""):
        with self.lock:
            self.refreshing = False
            self.refresh_error = error
            self.next_refresh = time.monotonic() + 240

    def observe(self, **facts):
        with self.lock:
            self.facts.update(facts)
            self.facts["updated"] = time.time()

    def building(self, info):
        if info["name"] and info["level"] is not None:
            with self.lock:
                previous = self.facts.get("buildings", {})
                self.facts["buildings"] = {**previous, info["name"]: {
                    "level": info["level"], "busy": info["busy"], "at": time.time()}}

    def head(self, kind):
        with self.lock:
            queue = self.queues[kind]
            return dict(queue[0]) if queue else None

    def task_message(self, kind):
        with self.lock:
            return self.messages.get(kind, "")

    def message(self, kind, job, message):
        with self.lock:
            queue = self.queues[kind]
            if queue and queue[0]["id"] == job["id"]:
                self.messages[kind] = message

    def attempt(self, kind, job, click, **before_click):
        with self.lock:
            queue = self.queues[kind]
            if not queue or queue[0]["id"] != job["id"]:
                return False
            queue[0].update(before_click)
            self._save()
            click()
            return True

    def complete(self, kind, job, spent=1):
        """完成一项。造兵/练兵按人数递减 count，减完才出队。"""
        with self.lock:
            queue = self.queues[kind]
            for index, current in enumerate(queue):
                if current["id"] != job["id"]:
                    continue
                if kind in ("recruit", "drill") and current["count"] > spent:
                    current["count"] -= spent
                    current.pop("verify", None)
                else:
                    queue.pop(index)
                self._save()
                self.changed.add(kind)
                if index == 0:
                    self.messages[kind] = "上一项已执行。" if queue else "脚本队列已完成。"
                return

    # ---------- 资源守卫：资源不够就别硬试 ----------
    def guard(self, kind, seconds, why):
        """把某个队列暂停一段时间（资源不足/条件不满足），期间不再点它。"""
        with self.lock:
            self.blocked[kind] = time.monotonic() + max(30, int(seconds))
            self.messages[kind] = why
            self.changed.add(kind)

    def guard_left(self, kind):
        """该队列还剩多少秒的冷却（0 = 可以做）。"""
        with self.lock:
            return max(0, int(self.blocked.get(kind, 0) - time.monotonic()))

    def guard_ready(self, kind):
        return self.guard_left(kind) == 0

    def clear_guard(self, kind):
        with self.lock:
            if self.blocked.pop(kind, None):
                self.changed.add(kind)

    def set_resources(self, amounts, fill, points=None):
        """记录最近一次读到的资源，供网页显示和判断。

        注意：资源占比是 numpy.float64（cv2 算出来的），json 不能直接序列化，
        必须转成内置 float，否则整个 snapshot 会失败、网页拿不到任何数据。
        """
        with self.lock:
            clean_fill = {}
            for k, v in (fill or {}).items():
                try:
                    clean_fill[k] = round(float(v), 3)
                except (TypeError, ValueError):
                    continue
            clean_amounts = {}
            for k, v in (amounts or {}).items():
                try:
                    clean_amounts[k] = int(v) if v is not None else None
                except (TypeError, ValueError):
                    clean_amounts[k] = None
            self.facts["resources"] = {"amounts": clean_amounts, "fill": clean_fill,
                                       "points": int(points) if points is not None else None,
                                       "at": time.time()}
