"""持久化状态（data/state.json）：地块记录、统计、战绩学习等。"""
import json
import os
import re
import threading
import time

from .vision import ROOT

PATH = os.path.join(ROOT, "data", "state.json")


class State:
    def __init__(self, path=PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"tiles": {}, "stats": {}, "timers": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with self.lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            # OneDrive / 杀毒软件会短暂锁住文件，os.replace 抛 WinError 5。
            # 原来一次失败就把整个任务打成 ERROR，实测在训练任务里直接中断。
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 4:
                        break
                    time.sleep(0.15 * (attempt + 1))
            # 实在换不了，只能直接覆盖目标文件（牺牲原子性，但别让任务崩掉）
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ---- 地块 ----
    def tile(self, x, y):
        return self.data["tiles"].get("%d,%d" % (x, y))

    def set_tile(self, x, y, **kw):
        k = "%d,%d" % (x, y)
        rec = self.data["tiles"].setdefault(k, {})
        rec.update(kw)
        rec["t"] = int(time.time())
        self.save()

    def tile_blocked(self, x, y, retry_hours):
        """该地块最近是否判定为打不动/不可攻占。"""
        rec = self.tile(x, y)
        if not rec:
            return False
        st = rec.get("status")
        age = time.time() - rec.get("t", 0)
        if rec.get("name") == "主城":
            return True
        if st in ("owned", "invalid"):
            return age < 24 * 3600
        if st == "hard":
            # 打不动分两种：兵力不足（升级兵种后就能打，几小时后再试）
            # 和守军太强（短期没戏，别反复拿体力去撞）。由 _mark 记下 cool_h。
            hours = rec.get("cool_h") or retry_hours
            return age < hours * 3600
        if st == "attacking":
            return age < 600
        if st == "noattack":
            # 优先级：cool_h（小时，用于“免战保护中”这种长冷却）
            #        → cool_s（秒，用于“点不中，5 分钟后再试”）
            #        → 老规则 30 分钟
            if rec.get("cool_h"):
                return age < float(rec["cool_h"]) * 3600
            return age < float(rec.get("cool_s") or 1800)
        return False

    # ---- 战绩学习 ----
    def learn_key(self, res, level, tier):
        """一类地的标识：资源+等级+难度。同类的成功率可以互相参考。"""
        return "%s%d|%s" % (res or "荒地", int(level or 1), "?" if tier is None else int(tier))

    def learn_get(self, key):
        return (self.data.get("learn") or {}).get(key) or {"try": 0, "ok": 0}

    def learn_add(self, key, ok):
        """记一次出征结果（ok=预测兵不血刃并成功派出）。"""
        box = self.data.setdefault("learn", {})
        rec = box.setdefault(key, {"try": 0, "ok": 0})
        rec["try"] = int(rec.get("try", 0)) + 1
        if ok:
            rec["ok"] = int(rec.get("ok", 0)) + 1
        rec["t"] = int(time.time())
        self.save()

    def learn_rate(self, key, prior=0.5, weight=1.0):
        """这类地的成功率，用贝叶斯平滑：样本少时靠近先验，样本多了才信实测。

        weight 不能太大：以前是 3.0，导致「0 成 1 败」也算出 38% 成功率，
        再乘上 5 级地 14 的产出，期望收益虚高到 5.2，把真正打得过的地挤到后面。
        1.0 的意思是：1 次实测就能明显压过先验。
        """
        rec = self.learn_get(key)
        try_n, ok_n = int(rec.get("try", 0)), int(rec.get("ok", 0))
        return (ok_n + prior * weight) / (try_n + weight)

    # ---- 编队组合学习：哪几支编队能把这类地打出「兵不血刃」 ----
    def combo_get(self, key):
        """这类地历史上成功过的编队组合，按用得少的优先（试错覆盖）。

        记录形如 {"armies": ["编队2","编队4"], "ok": 3, "try": 4, "t": ts}。
        返回列表，最近的在前。
        """
        box = (self.data.get("combos") or {}).get(key) or {}
        rows = [dict(v) for v in box.values() if v.get("armies")]
        # 成功率高的排前面；同成功率下用得少的优先（多试几种，别死盯一套）
        rows.sort(key=lambda r: (-(r.get("ok", 0) / max(1, r.get("try", 1))), r.get("try", 99)))
        return rows

    def combo_add(self, key, armies, ok):
        """记一次编队组合尝试。armies 必须已排序，保证同一套组合落在同一个 key。"""
        if not armies:
            return
        name = "|".join(sorted(armies))
        box = self.data.setdefault("combos", {}).setdefault(key, {})
        rec = box.setdefault(name, {"armies": sorted(armies), "ok": 0, "try": 0})
        rec["try"] = int(rec.get("try", 0)) + 1
        if ok:
            rec["ok"] = int(rec.get("ok", 0)) + 1
        rec["t"] = int(time.time())
        # 每类地最多保留 12 套组合，避免状态文件膨胀
        if len(box) > 12:
            worst = min(box, key=lambda k: (box[k].get("ok", 0) / max(1, box[k].get("try", 1)), box[k].get("t", 0)))
            box.pop(worst, None)
        self.save()

    # ---- 卡格诊断 ----
    def hard_tiles(self, route_home, retry_hours):
        """当前主城周边被判「打不动」的地块坐标集合（用于自动绕行）。"""
        out = set()
        for k, rec in (self.data.get("tiles") or {}).items():
            if rec.get("status") != "hard":
                continue
            if rec.get("route_home") and route_home and rec["route_home"] != list(route_home):
                continue
            if time.time() - rec.get("t", 0) < retry_hours * 3600:
                try:
                    x, y = (int(v) for v in k.split(","))
                except ValueError:
                    continue
                out.add((x, y))
        return out

    def learn_report(self, min_try=1):
        """战绩报表：每类地的样本数、成功率、期望收益，按期望收益从高到低。

        期望收益 = 成功率 × 每小时产出（荒地算 3：1粮+1木+1石）。
        控制台用它一眼看出「哪类地赚、哪类地亏」。
        """
        prod = {1: 1, 2: 4, 3: 7, 4: 10, 5: 14}
        out = []
        for key, rec in (self.data.get("learn") or {}).items():
            try_n = int(rec.get("try", 0))
            ok_n = int(rec.get("ok", 0))
            if try_n < min_try:
                continue
            res, level, tier = key, 1, "?"
            try:
                head, tier = key.split("|", 1)
                m = re.match(r"(.+?)(\d+)$", head)
                if m:
                    res, level = m.group(1), int(m.group(2))
                else:
                    res = head
            except Exception:
                pass
            rate = self.learn_rate(key)
            p = 3 if res == "荒地" else prod.get(level, 1)
            out.append({"key": key, "res": res, "level": level, "tier": tier,
                        "try": try_n, "ok": ok_n,
                        "rate": round(rate, 3), "expect": round(rate * p, 2),
                        # 一次没成过 + 样本够 = 脚本会直接跳过（跟 expand 的判据一致）
                        "skip": ok_n == 0 and try_n >= 3})
        out.sort(key=lambda r: (r["skip"], -r["expect"]))  # 能打的排前面
        return out

    def reset_hard(self):
        """兵力变强后（招了兵/建了新编队），之前打不动的地块立即允许重试。"""
        n = 0
        for rec in self.data["tiles"].values():
            if rec.get("status") == "hard":
                rec["t"] = 0
                n += 1
        if n:
            self.save()
        return n

    # ---- 计时器 ----
    def due(self, name, interval_s):
        last = self.data["timers"].get(name, 0)
        return time.time() - last >= interval_s

    def next_at(self, name, interval_s):
        """下次到点的时间戳（0 = 还没跑过，随时可跑）。"""
        last = self.data["timers"].get(name, 0)
        return last + interval_s if last else 0

    def touch(self, name):
        self.data["timers"][name] = int(time.time())
        self.save()

    # ---- 暂停 / 继续（停止后重新开始要接着上次的进度，不是从头轮一遍） ----
    def pause(self):
        """控制台点停止时记一笔，回来后按这个把暂停时间扣掉。"""
        self.data["paused_at"] = time.time()
        self.save()

    def resume(self):
        """把暂停期间的时间从各任务计时器里扣掉，回到上次的节奏。返回暂停了多少秒。

        注意补回的量要封顶：暂停了 176 分钟就把所有任务推迟 176 分钟是灾难 ——
        控制台一开一关，expand（60 秒间隔）要等 3 小时才跑第一轮，看起来像“脚本不动了”。
        正常重启最多补 10 分钟，超过就当作“该跑了”。
        """
        at = float(self.data.pop("paused_at", 0) or 0)
        if not at:
            return 0
        gap = max(0.0, time.time() - at)
        if gap > 60:  # 只是重启一下就不用折腾了
            back = min(gap, 600.0)  # 最多补 10 分钟
            for k, v in (self.data.get("timers") or {}).items():
                if v:
                    self.data["timers"][k] = int(v + back)
        self.save()
        return gap

    def inc(self, key, n=1):
        self.data["stats"][key] = self.data["stats"].get(key, 0) + n
        self.save()

    # ---- 空转退避 ----
    def streak(self, name):
        """连续几轮“白跑”（比如开荒一轮一块地都没打下来）。"""
        return int((self.data.get("streak") or {}).get(name, 0))

    def set_streak(self, name, n):
        box = self.data.setdefault("streak", {})
        if n <= 0:
            box.pop(name, None)
        else:
            box[name] = int(n)
        self.save()

    def effective_interval(self, name, base_s, cap_s=None):
        """把“连续白跑”折进间隔：越白跑越慢，别拿截图和 OCR 空烧。

        开荒一轮要多次截图 + OCR + 点地块，周围的地都进了冷却之后，
        每 60 秒扫一遍纯属浪费。这里按连续白跑次数线性退避，封顶 cap_s。
        """
        n = self.streak(name)
        if n <= 0:
            return base_s
        out = base_s * (1 + n)
        return min(out, cap_s or base_s * 8)
