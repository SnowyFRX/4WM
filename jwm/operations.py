"""网页上的脚本指令队列，与游戏内的施工和招募队列分别记录。"""
import json
import secrets
import threading
import time
from pathlib import Path

from .vision import ROOT

MAX_JOBS = 1000


class Operations:
    def __init__(self, path=Path(ROOT, "data", "operation_queues.json")):
        self.lock = threading.Lock()
        self.path = Path(path)
        self.home = None
        self.queues = {"build": [], "recruit": [], "drill": []}
        self.messages = {"build": "", "recruit": "", "drill": ""}
        self.changed = set()
        self.blocked = {}  # kind -> monotonic 截止时间（资源不足时暂停该队列）
        self.facts = {}
        self.next_refresh = 0
        self.refreshing = False
        self.refresh_error = ""

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
                        blocked={k: max(0, int(v - time.monotonic())) for k, v in self.blocked.items()},
                        refreshing=self.refreshing, refreshError=self.refresh_error, buildingNames=BUILDING_NAMES)

    def command(self, data):
        from .tasks.build import BUILDING_NAMES

        op, kind = data.get("op"), data.get("kind")
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
                    queue.append(dict(id=secrets.token_hex(8), army=army, count=count))
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
