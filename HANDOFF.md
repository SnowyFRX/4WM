# 九万亩脚本 · 开发交接

新会话/新开发者从这里读起。**只写代码里真实存在的状态，不写愿望。**

---

## 1. 这是什么

九万亩（Cocos2d-x 手游，720x1280 竖屏，雷电模拟器）的自动化脚本。
纯外挂式：`adb 截图 → OpenCV 识别 → RapidOCR 读字 → adb 点击`。
**不做内存修改、不做注入、不改包。**

项目根：`C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu`

---

## 2. 怎么跑

```powershell
# 环境
$root = 'C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu'
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'

# 语法检查 + 单元测试（每次改完必跑）
& .\.venv\Scripts\python.exe .\tools\check_syntax.py .
& .\.venv\Scripts\python.exe -m unittest discover -s tests
```

**用户入口只有一个**：双击桌面「九万亩」→ `控制台.bat` → 起 `pythonw.exe console.py` → 浏览器开
**http://127.0.0.1:8765/jwm/**

- `console.py` 是轻量启动器（8765），网页上「启动脚本/停止脚本」才拉起真正的 `run.py`。
- `run.py` 自己另开 8766 页面，通过控制台的 `/map/` 前缀代理访问。
- **对外地址永远是 8765**。`data/web_url.txt` 只由 console.py 写；`run.py` 写 `web_child_url.txt`。
- 网页 90 秒无人轮询且脚本没在跑 → 控制台自动退出，不留后台进程。

adb：`C:\Users\黄尊鹏\Downloads\platform-tools-latest-windows\platform-tools\adb.exe`
设备：`emulator-5554`

---

## 3. 六条红线（用户定的硬规则，代码里不许绕过）

1. **只有战斗预测「兵不血刃」才出兵**。轻微损失/会死人的一律不打，留给用户手动判断。
2. **绝不花元宝**。任何「立即完成/购买/加速」弹窗立即取消。
3. **绝不点未解锁的兵种/装备格**（锁图标）。
4. **只打与领地上下左右相邻的地块**。
5. **绝不点任务里的「前往」**。
6. **仓库满时绝不点「继续领取」**。

`expand.dispatch()` 里 `accept = [SAFE_LABEL]` 是第 1 条的落点，别改成"允许轻微损失"。

---

## 4. 用户明确要求过的设计原则

- **用户选的路线是命令**：`expand.route_mode` 默认 `strict`，脚本不许自动绕行、不许改路线、不许因历史记录跳过用户选的格。`flexible` 才是允许绕行。
- **自动规划只是建议**：`autoplan` 只填 `suggested`，必须用户点「用这条建议」才生效。
- **不许自己加限制**：不要再按主城等级加"难度上限"之类的门槛。能不能打由战斗预测决定。
- **开荒小队 = 用户选哪几支编队**：不按资源/等级/难度做规则匹配（这套已删除）。
- **建筑等级只读一次**：首次启动扫一遍存缓存，之后直接沿用；用户下升级命令时只核对那一座。
- **特殊资源必须靠图标认**：零值行游戏不画、下面整体上移，绝不能按第几行对应。

---

## 5. 目录结构

```
9wanmu/
├─ config.yaml          所有可调参数（写 null = 按账号自动定）
├─ run.py               脚本入口（单实例锁 47391 端口）
├─ console.py           轻量控制台启动器（8765）
├─ console.html → jwm/console.html
├─ jwm/
│  ├─ bot.py            调度主循环、任务表、刷新、autotune 挂载
│  ├─ ui.py             界面识别、close()、to_map()、has_tile_popup/has_home_popup
│  ├─ device.py         adb 封装（截图/点击/滑动/键盘）
│  ├─ vision.py         模板匹配 + RapidOCR 封装
│  ├─ mapview.py        大地图网格解析（TILE=67.5）、find_city、frontier
│  ├─ route_camera.py   主城定位、镜头校准、地图发布
│  ├─ route_map.py      网页地图服务、路线状态机、扫描队列、小队/缓存
│  ├─ route_map.html    开荒地图页（内嵌全部 JS/CSS）
│  ├─ army_panel.py     军队面板解析、unit_strip 士兵截图
│  ├─ resources.py      顶栏资源 + 四种特殊资源（图标匹配）
│  ├─ operations.py     网页指令队列、小队存储、资源守卫
│  ├─ state.py          持久化（地块/战绩学习/建筑缓存）
│  ├─ autotune.py       按账号现状推导参数（新增）
│  └─ tasks/            expand / build / recruit / drill / chests / quests / hospital
├─ tests/               单元测试（14 项，全绿）
├─ assets/res_icons/    icon_book.png、icon_iron.png（scroll/nail 缺，见待办）
└─ data/                state.json / map_cache.json / operation_queues.json / army_combos.json
```

---

## 6. 本轮已完成的改动

| 改动 | 文件 | 状态 |
|---|---|---|
| 主城弹窗识别（进入+行军+坐标，原来只认选中框→一直定位不到主城） | `route_camera.py`、`ui.py` | 真机验证过 |
| 页面离线误报（state 为空时 JS 抛错，轮询全挂→永远"等待脚本连接"） | `route_map.html` | 无头浏览器验证 |
| 子页路由忽略 `?v=N` 查询串（返回 404 被当成脚本没跑） | `route_map.py` | 验证过 |
| 地址文件被子页改写成 8766 | `route_map.py` | 验证过 |
| 控制台离线时代理回 200 HTML（网页解析失败还当在线） | `console.py` | 单测覆盖 |
| 特殊资源误读（颜色兜底把石头行认成铁，7→1） | `resources.py` | 单测覆盖 |
| 石头行混进特殊资源（读取起点 140→165） | `resources.py` | 单测覆盖 |
| 数字 OCR 漏读（只走二值化→改为先彩色，并按图标锚定行位补读） | `resources.py` | 真机验证（8/9 读对） |
| 编队4 截图缺失（名字读全就 break，第四队还贴在屏幕底边） | `recruit.py` | 真机验证（5/9 有图） |
| state.json 被 OneDrive 锁（WinError 5 直接打断任务） | `state.py` | 单测覆盖 |
| 招兵任务死循环（兵种未解锁只 return 不暂停） | `recruit.py` | 验证过 |
| 建筑等级缓存（不再每次启动逐座点） | `build.py`、`bot.py` | 真机验证日志「沿用上次建筑等级」 |
| 开荒小队替代规则匹配 | `operations.py`、`expand.py`、`route_map.html` | 后端单测覆盖，**前端未真机点过** |
| 自动调参 autotune | `autotune.py`、`bot.py` | 真机日志验证 |
| 严格/绕行模式 + 跳过记录 | `expand.py`、`route_map.py`、`route_map.html` | 后端验证 |
| 练兵可指定第 N 个兵 + 读面板费用/时长 + 灰按钮不点 | `drill.py`、`operations.py`、`route_map.html` | **未真机验证** |

### 第 2 轮（2026-11-11 晚）—— 用户提的 8 个问题

| 改动 | 文件 | 证据 |
|---|---|---|
| 列表翻页统一走 `scroll.walk`：逐屏读到底、到尽头就停、同一屏不重复计 | 新增 `jwm/scroll.py`、`chests.py`、`recruit.py` | 真机：`读取编队完成：5 支（拥有 5）`，**5/5 都有士兵图**（原 9 张只有 5 张）；原先出现「6 支（拥有 5）」 |
| 编队名兜底收紧（x<200，排除「…地/城/营」等地图名） | `army_panel.py` | 上条同一日志；伪造条目被丢弃 |
| 练兵空试不再算成功：点「训练」后读回执，`资源不足` → 失败 + 暂停队列 | `drill.py` | 真机截图 `logs/shots/1111_180223_err_drill.png`：弹「资源不足 536/580」，旧代码只认元宝 |
| 练兵点之前先比资源：面板费用 vs 粮/木/石最少那项，明显不够就不点 | `drill.py` | 新增 `parse_cost`/`shortfall` 单测；日志 `资源不够，先不点：需要 X，当前最少只有 Y` |
| 地图帧剔除固定 HUD + 动态弹窗整帧丢弃（主城展开信息不再被截） | `route_camera.py` | 真机核对 28 帧 2628 格：**0 格命中 HUD**；干净地图 `frame_overlays` 返回空，军队面板返回遮挡带 |
| 网页「框选刷新」+「同步模拟器到这里」；`scan_region` 按 6 格拆分排队 | `route_map.py`、`route_map.html` | 真机：提示「已选 (241,290)–(245,295)…」，状态「正在刷新你框选的区域（1 个视野）」 |
| 多小队 + 每队绑定一条路线；小队卡片移到开荒页「开荒进度」下方 | `operations.py`、`route_map.html` | 真机截图：小队1 显示「这条路线」+「改绑当前路线」；接口 `squads[].route` |
| 小队**同时指派**：整队一次勾上再读预测（不再一支 SAFE 就 break、其余被派去下一格） | `expand.py` `_dispatch_whole_squad` | 真机日志：`+ 编队2 … 已并入小队` / `+ 编队3 …` / `小队同时指派：编队2、编队3 → 预测: 兵不血刃` / `出兵结果: ok` |
| 打不出兵不血刃 → 真的排进补兵名单并召回（带 10 分钟冷却），召回后校验回执 | `expand.py` `_need_supply`/`mark_understrength`/`recall_accepted` | 单测 + 进程内验证：名单去重、冷却生效、回执判定 |
| 编队状态缓存 90s → 20s（可配 `army.snapshot_seconds`），网页显示「读取于 N 秒前」 | `bot.py`、`recruit.py`、`route_map.html` | 接口 `armyAt`/`snapshotSeconds` 实测存在；兵营刷新不再覆盖真实状态 |
| 路线持久化：重启不再丢你排好的路线 | `route_map.py` `_route_save/_route_load`、`data/route.json` | 真机：停→启后日志 `已恢复上次提交的路线：8 格，继续第 1 格` |
| 控制台代理不再把子服务的 4xx 吞成「脚本没在运行」 | `console.py` | 同源转发真实状态码 |
| adb 临时目录指向 `logs/adb_tmp`（原 TMP 在沙箱内，adb 写不了 adb.log → 截图全超时） | `device.py` `_adb_env()` | 修复前日志 `cannot open ...DSH~1\ascii\tmp\adb.log: Permission denied`；修复后地图正常扫描（28 帧） |
| 网页 UI 重做（配色/层次/动效/卡片/导航/新组件）+ 版本号 → `?v=10` | `route_map.html`、`console.html` | 真机截图确认渲染 |

### 第 3 轮（同一晚，收尾 + 一个真 bug）

| 改动 | 文件 | 证据 |
|---|---|---|
| **校场/医馆被搞混**：两者都叫「选择军队」，旧代码据此认校场，实际打开的是医馆「治疗伤兵」→ 一个兵都点不到、全程空试 | `drill.py` `looks_like_drill()` | 真机截图 `logs/drill_panel.png`：治疗费用 / 治疗队列 / 还没有伤兵 |
| 缓存串位：`building_pos` 里「校场」与「医馆」存成同一个坐标 `[292,680]` → 每次练兵都被带去医馆 | `drill.py`、`data/state.json` | 真机 `building_pos` 实测；现在发现是医馆就作废该校场缓存并继续找。21:22 日志确认：跳过医馆后能找到真正的校场并读到训练费用 |
| 练兵费用解析挡掉旁边的等级小数字 | `drill.py` `shortfall(min_amount=10)` | 真机日志曾出现 `面板费用/时长：[636, 1]`，那个 1 是等级 |
| 宝箱角标是真需求但无入口时不再空转（3 分钟退避） | `chests.py` | 真机：`领取宝箱 0 个（看了 1 屏）` + `宝箱角标亮着…3 分钟后再看`，之后 7 分钟没有再空转 |
| 多小队新建/删除按钮真机点过 | `route_map.html` | 真机：建「远程队」→ 页面显示「这条路线」标签；点删除 → 立即消失，配置回到只有小队1 |

### 第 4 轮（用户反馈「召回补血依旧不行」）

真机根因（不是猜的，是代码 + 日志一起对出来的）：

1. **召回是死代码**：`run_route` 在 `too_hard` 分支直接 `return 0`，下面那段
   `if self.need_heal: self.recall(...)` 永远执行不到；而 `_diagnose_stuck` 还对玩家
   写「已召回补兵」。日志证据：21:01/21:27/21:30 只有「编队带伤…回城回血后再试」，
   从来没有任何一条「召回 编队X」。
2. **假冷却**：`_need_supply` 在**没召回**的情况下就写了 `recall_at`，
   于是日志一直是「刚召回不久，这次先不打断它们」，队伍永远不会回城。
3. 召回失败静默：找不到主城/点不到「行军」/面板没出来，都 `return []` 不留日志。

修法与真机验证：

| 改动 | 证据 |
|---|---|
| 新增 `_recall_if_needed()`，`too_hard`/`no_army`/`need_heal` 都真的调用召回 | 单测 60 项；冷却只在真召回后写 |
| 召回前核对「真的选中了这一支」（`read_selected` 对比），避免点到隔壁卡片 | 真机曾把主城待机的编队一起召回 |
| 回执判定改为「面板已关闭 **或** 行军中/返回中」 | 以前点完确定面板立即关闭 → 永远误报「没读到回执」 |
| 召回失败逐条留日志 | 便于下次定位 |
| `_diagnose_stuck` 不再谎称已召回，改为报出真实召回了哪几支 | 单测覆盖 |
| 选框认不出（`span=0`）时，若弹窗坐标对得上且是自家领地 → 记为已占领并前进 | 真机：路线卡在 (239,290) 十几分钟，修后 22:06 正常出兵 |
| 已占领推进时顺便存盘 | 重启不再重打已占领的格 |

**真机端到端证据**：手动 `recall(thr=1.0)` → 选中编队2/编队3 → 点确定 →
8 秒后军队面板显示 `行军中`（原在木头地(238,290)）→ 约 3 分钟后 `待机中`，
**编队2 血量 0.822 → 1.0**。即：召回 → 回城 → 自动回满血，机制本身是通的。

顺带确认（同一轮）：`领取宝箱 3 个（看了 2 屏）` —— 第 1 条问题的翻页修复真的领到了。

**收尾补修（22:2x）**：`no_army` 时若队伍正在战斗/行军，召回也召不回来，
以前每个开荒周期都会去开一次行军面板白跑。现在先看军队面板状态
（`_busy_armies()`，战斗中/行军中/返回中/治疗中 = 召不回来），
有忙的队伍就直接报「等空闲编队：编队2、编队3 正在战斗/行军中」，
不再空跑；网页文案也不再谎称「正在召回休整」。

**这一轮的最终真机状态**：路线从卡死的 0/8 推进到 2/8
（(239,290)、(239,289) 均已确认占领）；宝箱连续领到 4/1/2 个（都是翻到第 2 屏才领到）；
本小时 0 个 ERROR。

### 第 5 轮（用户问「路线只能设置一条？」）

回答：以前确实只能一条（`RouteMap` 只有单个 `path/index/active`，提交新路线会**替换**旧的），
所以「多小队分别绑不同路线」这个能力是残缺的 —— 第二支小队永远用不上。已改成多条。

| 改动 | 说明 |
|---|---|
| `self.routes` = [{key, path, index, active}] + `self.current` | 每条路线独立进度；`path/index/active` 保留成属性，老代码不用改 |
| `next()` 多路线轮转 | 按顺序取，**跳过「队首还在等行军 / 刚失败在退避」的路线**，去推进别的路线；全都不可推进时返回 None（不空点） |
| `step_still_valid()` | 多路线下不能再用「next() 结果是否相同」当守卫（next 已轮到别条），改为核对版本 + 该路线是否还在跑、队首是否还有格 |
| `advance/report/avoid_and_replan` 按 `_step_key` 定位 | 哪条路线的格子就改哪条，不串台 |
| 新命令 `select_route` / `remove_route`，`start` 支持 `mode: "add"` | 网页上「再加一条路线」「看这条」「暂停/继续」「删除」 |
| 持久化 `data/route.json` 改为 `{routes:[...], current}`，并兼容旧单条格式 | 旧文件自动迁移成 1 条，进度不丢 |
| 网页：已提交路线列表（每条的进度、队首、状态）+ 该条路线的格子清单 | 版本号 → `?v=11` |

**真机验证**：旧路线自动迁移为 1 条且保住进度（3/8）；加上第二条后两条并存
（`index 3/8` + `index 0/2`）；22:51、22:52 两次「兵不血刃」出兵，
第一条队首停在 (239,287) 时脚本转去推进了第二条 (242,292) —— 轮转生效。
单测 `tests/test_multi_route.py` 8 项覆盖：追加不覆盖、各自进度、跳过等行军、
全等待不出兵、暂停跳过、删除保留其他、持久化、旧格式兼容。

### 第 6 轮（用户指出「打一格就回城补血」）

用户原话：应该到打不出兵不血刃再回城补血。核实后确实是错的，有两个错误触发点：

1. **`_need_supply` 把编队写进 `understrength`**：招兵任务看到这个名单会
   `recall(names=understrength)`，于是刚出征打地的队伍被拽回主城。
   真机证据（22:56，旧代码）：
   ```
   出兵结果: too_hard
   编队1、编队5、编队4 打不动，排队补兵并准备召回
   召回 编队2（位于荒地(239,287)，血量57%，行军464s）缺员，回城补兵
   ```
   编队2/3 正在那格打地，却被当作「缺员」召回。而且名单里一度有**全部 5 支编队**。
2. **非路线模式 `dispatched == 0 or understrength -> recall()`**：一轮没打成也召回。

修法：

| 改动 | 位置 |
|---|---|
| `_need_supply` 只设 `need_heal`，**不再写补兵名单** | `expand.py` |
| `recall()` 不再 `mark_understrength`（召回不等于缺员） | `expand.py` |
| `no_army` 分支**永不召回**，只报「等编队空出来」 | `expand.py` |
| 非路线模式删掉 `dispatched == 0 / understrength -> recall()` | `expand.py` |
| 招兵任务召回前先排除正在战斗/行军的编队 | `recruit.py` |

**现在的规则**：只有 `too_hard`（预测不是兵不血刃）才 `need_heal` → 召回。
出兵成功绝不召回；没有空闲编队只等不召。

**真机验证**：23:06:32 `出兵结果: ok 兵不血刃` → 无召回，编队2/3 在
`粮食地(240,287)` 继续打下一格；`understrength` 全程为空；
两条路线分别推进到 4/8 和 1/2。
单测：`tests/test_recall_flow.py` 扩到 12 项（含「打不动不写补兵名单」
「真召回不污染名单」「战斗中算忙」）。

### 第 7 轮（网页 UI 重构）

**技术选型判断（重要）**：项目**没有** Node 构建链（无 package.json / Vite / Tailwind），
`route_map.py` 用 `http.server` 直接返回单个 HTML，且带**按 mtime 热加载**；
用户入口是双击 `start.bat` → `pythonw console.py`，目标机器不需要 Node。
所以**没有引入** React / shadcn / Radix / Tailwind / Motion 等构建型依赖 ——
那会要求把 `dist/` 产物提交进仓库、源码与产物双份维护，还会让「改完刷新即生效」失效，
与「最少必要代码和依赖」冲突。改为**零依赖**达到同等视觉与交互质量：

| 层面 | 做法 |
|---|---|
| 设计令牌 | `:root` 定义色板 / 间距（4 的倍数）/ 圆角 / 字号 / 三级阴影 / 动效曲线与时长 |
| 图标 | 内联 Lucide SVG sprite（26 个 symbol），`<use href="#i-xxx">` 引用，无运行时依赖 |
| 组件 | 统一的 `button`（primary / danger / ghost / aria-pressed）、`panel`、`chip`、`field`、`empty`、`skeleton`、`toast` |
| 动画 | 只做状态反馈：状态灯脉冲、toast 进出、血条/资源条宽度过渡、`prefers-reduced-motion` 降级 |
| 响应式 | 1180 / 960 / 560 三个断点；侧栏在大屏 sticky，小屏单列；工具栏与资源格自动换行 |
| 无障碍 | 语义标签、`role=tablist/tab/tabpanel`、`aria-selected`、`←/→` 键盘切页签、`aria-live` 状态、`:focus-visible` 焦点环 |
| 状态完整性 | 离线卡片（图标+原因+启动入口）、地图占位、编队未读、队列空态（图标+文案）、错误 toast |
| 结构 | 页头品牌块、分段导航、地图工具栏分组（带分隔线）、侧栏每节标题带图标、路线卡片列表 |

**约束遵守**：83 个 `id` 全部保留（`grep` 校验「缺失 id: 无」），JS 逻辑零改动，
只加了一个 `toast()` 辅助与页签键盘导航。

**验证**：`check_syntax.py` 43 文件 0 失败；单测 73 项全过；两段前端脚本 `node --check` 通过；
真机浏览器实测宽屏（地图+侧栏两栏）、城建页（资源/编队卡片）、620px 窄屏（单列无溢出）、
离线态（橙色提示卡）均正常。版本号 → `?v=12`。








---

## 7. 待办 / 未验证（接手先做这些）

**必须真机验证**
1. 小队界面：新建/切换/删除，开荒日志是否出现「使用开荒小队「X」：编队N」。
   → 第 2/3 轮已真机验证：同时指派 + 新建 + 删除都点过；切换（点「用这个小队」）还没点。
2. 练兵指定第 N 个兵（`drill` job 的 `slot`）：滑到最左再取第 N 个，是否点对兵种。
   → 校场**存在**：21:22 真机读到 `兵@105 面板费用/时长：[636, 1] / 0:10:36`，
   即第一次点到医馆后继续找能找到校场。但当时资源 634 < 636，脚本正确拒绝点击，
   所以 `slot` 选择本身还没在"资源够"的情况下跑过。
3. 主城弹窗识别：`has_home_popup` 在"展开编队列表"状态是否也认得出。
   → 第 2 轮已用真机截图验证 `frame_overlays` 能认出并整帧丢弃；自然扫描触发还没抓到实例。

**第 2 轮新增的待办**
9. **宝箱入口未知**：军队按钮右上角有金色角标（真机确认），但「所有军队」5 张卡翻到底、
   连「战报」页签也翻过，都没有「获得宝箱」入口。现在会如实记一条
   `宝箱角标亮着，但军队面板/战报里都没有可领入口；3 分钟后再看` 并退避，不再空转。
   **下一步**：需要一次真实"有宝箱"的时刻打开面板观察入口在哪（可能在编队详情/背包）。
10. 拖动地图到未扫描区会出现空白（原有行为）。已在离主城 >12 格时不自动同步模拟器，
    只提示点「同步模拟器到这里」。
11. `data/route.json` 新增：路线会持久化。换账号/主城时会自动作废（按 home 比对）。


**功能缺失**
4. `assets/res_icons/icon_scroll.png`、`icon_nail.png` 还没有（当时这两项为 0，游戏不显示）。等它们有值时截一次模板即可。现在读不到就保持 `null`，不许猜。
5. README 还提旧的 PySide6 控制台，需要按现在的 console.py + 8765 流程重写。
6. `data/map_cache.json` 约 3.9MB，可以考虑压缩或裁剪历史帧。

**已知行为**
7. `web.port` 被占用时 console.py 会 +1..+3 换端口。换端口后 `web_url.txt` 会更新，但用户如果收藏了旧地址会打不开——所以尽量别占 8765。
8. 每次改 `route_map.html` 必须递增 `console.html` 里的 `?v=N`，否则浏览器吃旧缓存，用户会看到修好之前的页面。

---

## 8. 踩过的坑（别再犯）

- **别用 `os.replace` 一次就信**：OneDrive/杀毒会短暂锁文件，`state.py` 已改成重试 5 次 + 直接覆盖兜底。
- **别用颜色猜资源种类**：石头行灰白，会被判成铁。只认图标。
- **别按行号对特殊资源**：零值行不画，行号会整体上移。
- **别只看"名字读全了"就结束翻页**：最后一队可能贴在屏幕底边，士兵区被截断。
- **页面 JS 里 `state` 可能为 null**：任何 `state.xxx` 都要写成 `state?.xxx`，否则一抛错整个轮询停掉，表现为"永远连不上"。
- **`cv2.imdecode` + `np.fromfile`** 才能读中文路径；`cv2.imread` 会静默返回 None。
- **onnxruntime 会 `bad allocation`**：内存紧张时 OCR 直接崩；`run.py` 已开 `faulthandler` 留栈。
- **诊断前先停脚本**：否则 adb 被占用，截图和点击互相打架。
- **adb 的 TMP 必须可写**：进程继承的 TMP 可能指向沙箱临时目录，adb 连 `adb.log`
  都写不进去（`Permission denied` → `failed to start daemon`），表现是截图全部 20 秒超时。
  `device.py` 的 `_adb_env()` 已把 TMP/TEMP 固定到 `logs/adb_tmp`。
- **`threading.Lock` 不可重入**：`RouteMap.command()`/`identify()` 已持锁，
  里面的 `_route_save`/`_route_load`/`scan_region` 不能再 `with self.lock`，否则整页卡死。
- **别把固定遮挡区画到地图中央**：主城周围的格子会被永久丢掉。固定 HUD 只挡边缘，
  中央弹窗靠 `frame_overlays()` 逐帧检测后整帧丢弃。

---

## 9. 新会话开场提示（可直接粘贴）

```
项目在 C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu
先读 HANDOFF.md，再跑 tools\check_syntax.py 和 unittest discover -s tests 确认基线绿。
不要自己给脚本加限制（不加难度门槛、不改用户路线）。
改动完必须：语法检查 + 单测 + 真机验证，用日志和接口结果说话，不许凭猜。
```
