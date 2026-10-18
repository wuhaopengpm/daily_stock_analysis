"""全连板梯队的收盘预筛和条件触发计划（非实时委托）。"""

def board(row):
    try:
        return int(float(row.get("连板数") or 0))
    except (TypeError, ValueError):
        return 0


def code(row):
    return str(row.get("代码") or row.get("股票代码") or "").zfill(6)


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def screen_full_ladder(snapshot, temperature, max_position, high_risk=False):
    """逐级列出代表股。条件候选并不意味着已经出现真实买点。"""
    stocks = snapshot.get("limit_up_stocks", [])
    stats = snapshot.get("statistics", {})
    promotions = stats.get("promotions", {})
    grouped = {}
    for row in stocks:
        c, b = code(row), board(row)
        name = str(row.get("名称") or "").upper()
        if not (len(c) == 6 and c.isdigit() and b >= 1):
            continue
        if "ST" in name or "退" in name:
            continue
        grouped.setdefault(b, []).append(row)

    def rank(row):
        pops = row.get("炸板次数")
        turnover = row.get("换手率")
        return (
            int(pops is not None and num(pops) <= 1),
            int(turnover is not None and 3 <= num(turnover) <= 22),
            num(row.get("封板资金")), code(row))

    levels = sorted(grouped, reverse=True)
    for lv in levels:
        grouped[lv].sort(key=rank, reverse=True)
    picks = [grouped[lv][0] for lv in levels]
    remaining = [r for lv in levels for r in grouped[lv][1:]]
    picks += sorted(remaining, key=rank, reverse=True)[:max(0, 12 - len(picks))]
    watchlist, opportunities = [], []
    for row in picks[:max(12, len(levels))]:
        c, b = code(row), board(row)
        stage = f"{b}进{b+1}"
        pops, turnover = row.get("炸板次数"), row.get("换手率")
        watchlist.append({
            "code": c,
            "reason": (
                f"今日{b}板，明日观察{stage}；换手"
                f"{turnover if turnover is not None else '未知'}%，"
                f"炸板{pops if pops is not None else '未知'}次。"
                "只用于竞价与分时观察，公告、承接和次日真实回封未验证。"
            ),
        })
        is_mainboard = c.startswith(("00", "60"))
        has_turnover = turnover is not None and 3 <= num(turnover) <= 22
        has_stability = pops is not None and num(pops) <= 1
        # 越高板越要检查昨日同层的晋级样本，小样本不可当「确定性」。
        prior_stage = promotions.get(f"{b-1}进{b}", {})
        cohort_ok = (b == 1 or (
            num(prior_stage.get("base")) >= (2 if b >= 4 else 3)
            and prior_stage.get("rate") is not None
            and num(prior_stage.get("rate")) >= (30 if b >= 4 else 35)
        ))
        phase_ok = max_position > 0 and (
            b == 1 or
            (b == 2 and temperature >= 60) or
            (b == 3 and temperature >= 65) or
            (b >= 4 and temperature >= 75 and not high_risk)
        )
        if not (is_mainboard and has_turnover and has_stability and
                cohort_ok and phase_ok):
            continue
        opportunities.append({
            "code": c,
            "stage": stage,
            "status": "收盘条件预筛通过，次日盘中待确认",
            "trigger": (
                "次日9:25先核验竞价与大盘情绪，盘中确认分歧后换手承接、"
                "实际回封涨停以及板块联动、公告风险；以上未全部确认不得行动"
            ),
            "invalidation": (
                "高标集体走弱、开板后承接不足、放量不回封、突发公告利空、"
                "一字缩量且没有真实可成交条件时放弃"
            ),
            "position_cap": min(max_position, 3 if b >= 4 else 5),
        })
    return {"watchlist": watchlist, "opportunities": opportunities,
            "observed_levels": levels}


def high_board_risk(stats):
    base = max(0, int(num(stats.get("high_board_previous_count"))))
    fail = max(0, int(num(stats.get("high_board_not_advanced_count"))))
    return base >= 2 and fail / base >= 0.5
