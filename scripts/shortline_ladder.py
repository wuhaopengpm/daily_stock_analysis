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


def ladder_report(snapshot, plan):
    """Markdown 段落：先解释整个梯队，再列可复核、尚未触发的条件机会。"""
    st = snapshot.get("statistics", {})
    dist = st.get("board_distribution_today", {})
    prev = st.get("board_distribution_previous", {})
    promotions = st.get("promotions", {})
    lines = [
        "### 连板全梯队（收盘数据）",
        f"首板 {st.get('first_board_count', '未知')} 家；"
        f"今日最高 {st.get('max_board_today', '未知')} 板；"
        f"昨日最高 {st.get('max_board_previous', '未知')} 板。",
    ]
    levels = sorted({int(k) for k in dist} | {int(k) for k in prev}, reverse=True)
    if not levels:
        lines.append("无法确认有效连板层级（数据源可能为空）。")
    for level in levels:
        today_count = dist.get(level, dist.get(str(level), 0))
        prev_count = prev.get(level, prev.get(str(level), 0))
        lines.append(f"- {level}板：今日{today_count}家／昨日{prev_count}家")
    lines.append("### 全部晋级梯队（昨日→今日）")
    for stage, info in promotions.items():
        count, base, rate = info["count"], info["base"], info["rate"]
        rate_text = f"{rate}%" if base >= 3 and rate is not None else (
            f"{rate}%（样本仅{base}，不宜外推）" if rate is not None else "无样本")
        lines.append(f"- {stage}：{count}/{base}，晋级率{rate_text}")
    lines.append("### 高标生态及风险")
    high_base = st.get("high_board_previous_count", 0)
    high_fail = st.get("high_board_not_advanced_count", 0)
    lines.append(
        f"昨日4板以上样本{high_base}家，今日未继续晋级{high_fail}家"
        "（可能包含停牌、数据差异，并非直接判为跌停）。"
    )
    leaders = st.get("top_board_stocks", [])
    if leaders:
        lines.append("收盘最高板代表：" + "；".join(
            f"{x.get('名称','未知')} {x.get('代码','未知')} {x.get('连板数',0)}板"
            for x in leaders[:8]))
    inds = st.get("industry_limit_up_counts", [])
    if inds:
        lines.append("行业涨停分布前列：" + "；".join(
            f"{name} {value}家" for name, value in inds[:4])
            + "；**行业不等于热点概念主线**。")
    lines.append("### 条件触发的高关注机会（不代表当前可买）")
    cap = plan.get("reference_max_position")
    if cap is not None:
        lines.append(f"总仓位参考风控上限：{cap}%；实际参与前须复核竞价、盘中承接与公告。")
    setups = plan.get("opportunities", [])
    if not setups:
        lines.append("**当前没有同时符合收盘数据筛选条件的机会，选择不出手。**")
    for item in setups[:12]:
        lines += [
            f"- **{item['stage']} / {item['code']}**：{item['status']}；"
            f"单票参考上限{item['position_cap']}%。",
            f"  - 确认条件：{item['trigger']}",
            f"  - 立即放弃：{item['invalidation']}",
        ]
    lines.append(
        "**收盘预筛≠盘中信号已触发；没有实时竞价、量价和封单验证，"
        "不能推断排板能成交，也不能声称“确定性买点”。**"
    )
    return lines
