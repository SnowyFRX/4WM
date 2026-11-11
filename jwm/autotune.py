"""按账号现状自动调整运行参数。

为什么需要：config.yaml 里的参数是死的，但账号会长——主城从 1 级升到 10 级，
每队人数上限、能打的难度、资源产量全都变了。用一套写死的值，要么新手期太激进
（打不过的硬撞），要么后期太保守（能打的也放过）。

做法：
  - 只覆盖 config 里为 null 的项（明确写了数字的，尊重用户设置，谁也不改）；
  - 依据是脚本已经读到的账号信息（建筑等级、编队兵力、资源量），不额外截图；
  - 每次刷新游戏状态后重算一次，随时跟着账号变化走。
"""
import logging

log = logging.getLogger(__name__)

# 游戏机制：每队人数上限由主城等级决定（1级3人、2级5人、3级7人，4级起 9 人封顶）
def army_cap(home_level):
    if not home_level:
        return 9
    return min(9, 2 * int(home_level) + 1)


def derive(facts, resources=None):
    """根据账号现状推导一整套参数。返回 {参数路径: 值}，只含“建议值”。

    facts 来自 operations.facts：buildings / armies / armySlots 等。
    """
    out = {}
    buildings = facts.get("buildings") or {}
    armies = facts.get("armies") or {}
    home_level = (buildings.get("主城") or {}).get("level")
    barracks = (buildings.get("兵营") or {}).get("level")
    slots = facts.get("armySlots") or []

    out["_profile"] = {
        "主城等级": home_level,
        "兵营等级": barracks,
        "编队数": slots[0] if slots else len(armies),
        "编队上限": slots[1] if len(slots) > 1 else None,
    }
    if not home_level:
        return out

    lv = int(home_level)
    out["recruit.max_per_army"] = army_cap(lv)

    # 编队数量：能同时出战的队伍越多，一轮可以多试几块地（不封顶压到很低）
    n_army = slots[0] if slots else len(armies)
    out["expand.max_candidates_per_round"] = max(4, min(12, n_army * 2))

    # 主城等级低 = 兵少、打不远：只调「排序倾向」，不设任何硬门槛。
    # 注意：这里**故意不设置 max_difficulty**。能不能打由战斗预测决定
    # （只有「兵不血刃」才出兵），不需要我再按主城等级额外卡一道。
    if lv <= 3:
        out["expand.distance_penalty"] = 0.5
        out["expand.distance_free_rings"] = 2
        out["expand.stuck_limit"] = 2
        out["expand.retry_hours"] = 6
    elif lv <= 6:
        out["expand.distance_penalty"] = 0.35
        out["expand.distance_free_rings"] = 3
        out["expand.stuck_limit"] = 3
        out["expand.retry_hours"] = 4
    elif lv <= 9:
        out["expand.distance_penalty"] = 0.25
        out["expand.distance_free_rings"] = 4
        out["expand.stuck_limit"] = 3
        out["expand.retry_hours"] = 3
    else:
        out["expand.distance_penalty"] = 0.15
        out["expand.distance_free_rings"] = 5
        out["expand.stuck_limit"] = 3
        out["expand.retry_hours"] = 3

    # 血量只影响「要不要先回血再打」，不设高门槛卡人：
    # 派出与否最终由战斗预测（兵不血刃）决定，别让血量阈值把能打的编队挡在外面。
    soldiers = [int((a or {}).get("soldiers") or 0) for a in armies.values()]
    cap = army_cap(lv)
    charged = [s for s in soldiers if s > 0]
    if charged:
        ratio = float(sum(charged)) / (len(charged) * max(1, cap))
        # 整体兵力偏低时，打完这一轮就去补兵，而不是不让人出门
        out["expand.recall_hp"] = 0.9 if ratio < 0.5 else 0.7
    else:
        out["expand.recall_hp"] = 0.7
    out["expand.min_attack_hp"] = 0.2
    # 新手期产量低：资源不够时等久一点再试，别每 25 秒硬点
    out["recruit.resource_wait_seconds"] = 900 if lv <= 6 else 600
    out["build.resource_wait_seconds"] = 900 if lv <= 6 else 600
    out["drill.resource_wait_seconds"] = 900 if lv <= 6 else 600

    # 战绩学习：样本少时本来就靠贝叶斯平滑兜着，不需要额外提高门槛
    out["expand.learn_min_try"] = 3
    out["expand.learn_stale_hours"] = 6 if lv > 6 else 4
    return out


def apply(cfg, facts, resources=None):
    """把推导出的参数写进 cfg（只覆盖值为 None 的项）。返回实际改动的列表。"""
    derived = derive(facts, resources)
    changed = []
    for path, value in derived.items():
        if path.startswith("_"):
            continue
        node = cfg
        keys = path.split(".")
        ok = True
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                ok = False
                break
            node = node[k]
        if not ok or not isinstance(node, dict):
            continue
        leaf = keys[-1]
        # 用户显式写了数字就尊重用户，只在为 None / 缺失时才自动填
        if node.get(leaf) is None:
            node[leaf] = value
            changed.append((path, value))
    if changed:
        log.info("按账号自动调整参数：%s",
                 "，".join("%s=%s" % (p.split(".")[-1], v) for p, v in changed))
    return changed, derived.get("_profile") or {}
