"""领取任务奖励（右下角任务卷轴）。只点“领取/完成”，不点“前往”。"""
import logging

log = logging.getLogger(__name__)

R_SCROLL = (620, 1050, 720, 1150)
R_DOT = (675, 1070, 715, 1105)  # 卷轴右上角的红点（有奖励可领时出现）


def has_reward(img):
    x1, y1, x2, y2 = R_DOT
    roi = img[y1:y2, x1:x2].astype(int)
    red = (roi[:, :, 2] > 200) & (roi[:, :, 1] < 90) & (roi[:, :, 0] < 90)
    return int(red.sum()) >= 40


class Quests:
    def __init__(self, bot):
        self.bot = bot
        self.dev = bot.dev
        self.ui = bot.ui

    def pending(self):
        return has_reward(self.dev.screenshot())

    def run(self):
        sc = self.ui.to_map()
        m = sc.has("btn_quest", R_SCROLL, 0.75)
        if not m:
            log.info("没找到任务按钮")
            return 0
        self.dev.tap(*m.center, delay=1.5)
        n = 0
        tapped = set()
        for _ in range(15):
            sc = self.ui.grab()
            # 先领小任务，最后再点章节“完成”；点过没反应的（灰按钮）跳过
            btns = [b for b in sc.boxes if b.text.strip() == "领取" and 400 < b.center[0] and 150 < b.center[1] < 1150]
            btns += [b for b in sc.boxes if b.text.strip() == "完成" and 400 < b.center[0] and 150 < b.center[1] < 1150]

            def key(btn):
                # 用同一行左侧的任务标题区分（领完后列表会重排）
                row = [t for t in sc.boxes if t.box[2] < 380 and abs(t.center[1] - btn.center[1] + 12) < 30]
                return btn.text + "|" + (row[0].text if row else str(btn.center[1] // 20))

            btns = [b for b in btns if key(b) not in tapped]
            if not btns:
                break
            b = btns[0]
            tapped.add(key(b))
            self.dev.tap(*b.center, delay=1.5)
            n += 1
            # 领奖后可能弹出奖励展示，点空白处继续
            sc2 = self.ui.grab()
            if sc2.text(r"点击任意位置|恭喜|获得"):
                self.dev.tap(360, 1180, delay=1.0)
        log.info("领取任务奖励 %d 次", n)
        # 记录当前章节和剩余任务，方便看教程进度
        sc = self.ui.grab()
        chapter = sc.text(r"^第.章$")
        todo = [b.text for b in sc.boxes if b.box[0] < 120 and 600 < b.center[1] < 1050
                and not b.text.startswith(("奖励", "升级效果")) and len(b.text) >= 4]
        log.info("任务进度 %s: %s", chapter.text if chapter else "?", " / ".join(todo))
        self.ui.to_map()
        return n
