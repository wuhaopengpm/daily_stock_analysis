#!/usr/bin/env python3
"""独立的收盘短线复盘与钉钉通知。不会操作证券账户。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import requests

# 支持既作为包导入单测，也支持 python scripts/shortline_dingtalk.py 直接执行。
try:
    from scripts.shortline_ladder import high_board_risk, screen_full_ladder, ladder_report
except ModuleNotFoundError:
    from shortline_ladder import high_board_risk, screen_full_ladder, ladder_report

TZ = ZoneInfo("Asia/Shanghai")
STATE_FILE = Path(".shortline_state/state.json")
OUT_DIR = Path("reports/shortline")


def local_now():
    return datetime.now(TZ)


def fmt(v):
    if v is None:
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, (float, int, bool)):
        return v
    return str(v).strip()


def rows(df, keys=None, max_rows=350):
    if df is None or df.empty:
        return []
    if keys is None:
        keys = list(df.columns)
    return [
        {k: fmt(row[k]) for k in keys if k in df.columns}
        for _, row in df.head(max_rows).iterrows()
    ]


def number(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def code_of(row):
    return str(row.get("代码") or row.get("股票代码") or "").zfill(6)


def board_of(row):
    return int(number(row.get("连板数"), 0))


def fetch_market_snapshot(ymd):
    """只使用带日期参数的数据；关键接口不可用则中止，不猜行情。"""
    import akshare as ak
    import pandas as pd

    trading = ak.tool_trade_date_hist_sina()
    if trading is None or trading.empty or "trade_date" not in trading:
        raise RuntimeError("交易日历不可用，停止生成报告")
    calendar = sorted(pd.to_datetime(trading["trade_date"]).dt.strftime("%Y%m%d").tolist())
    if ymd not in calendar:
        return None
    before = [day for day in calendar if day < ymd]
    if not before:
        raise RuntimeError("找不到前一交易日")
    previous_day = before[-1]

    zt_today_df = ak.stock_zt_pool_em(date=ymd)
    zt_yesterday_df = ak.stock_zt_pool_em(date=previous_day)
    broken_df = ak.stock_zt_pool_zbgc_em(date=ymd)
    previous_performance_df = ak.stock_zt_pool_previous_em(date=ymd)
    if any(x is None or not hasattr(x, "columns") for x in
           (zt_today_df, zt_yesterday_df, broken_df, previous_performance_df)):
        raise RuntimeError("关键行情接口未返回表格，停止分析")
    for name, df in (("当日涨停池", zt_today_df), ("昨日涨停池", zt_yesterday_df)):
        if not {"代码", "名称", "连板数"}.issubset(set(df.columns)):
            raise RuntimeError(f"{name}缺少必要列，停止分析：{list(df.columns)}")
    # 仅对当日涨停股计算晋级；复牌一字板的成交可行性需单独核验。
    # 不截断涨停/昨日涨停全梯队，避免高板数量与晋级分母发生系统性遗漏。
    today = rows(zt_today_df, max_rows=len(zt_today_df))
    yesterday = rows(zt_yesterday_df, max_rows=len(zt_yesterday_df))
    broken = rows(broken_df, max_rows=len(broken_df))
    yesterday_perf = rows(previous_performance_df, max_rows=len(previous_performance_df))
    today_by_code = {code_of(r): r for r in today}
    prev_by_code = {code_of(r): r for r in yesterday}
    today_levels = {}
    for row in today:
        board = board_of(row)
        if board >= 1:
            today_levels[board] = today_levels.get(board, 0) + 1
    yesterday_levels = {}
    for row in yesterday:
        board = board_of(row)
        if board >= 1:
            yesterday_levels[board] = yesterday_levels.get(board, 0) + 1
    max_today_board = max(today_levels, default=0)
    max_prev_board = max(yesterday_levels, default=0)
    max_level = max(5, max_today_board, max_prev_board)
    promotions = {}
    # 前一日1板→今日2板，...，直至最高连板；首板不是连板晋级。
    for level in range(1, max_level + 1):
        cohort = [c for c, r in prev_by_code.items() if board_of(r) == level]
        advanced = [c for c in cohort if c in today_by_code
                    and board_of(today_by_code[c]) == level + 1]
        promotions[f"{level}进{level + 1}"] = {
            "count": len(advanced), "base": len(cohort),
            "rate": round(len(advanced) / len(cohort) * 100, 1) if cohort else None,
            "codes": advanced,
        }
    # 只表示「昨日4板以上样本今天没有继续晋级」：也可能停牌或数据异常，
    # 不能未核验即断言「跌停」「核按钮」。
    high_prior = {c: r for c, r in prev_by_code.items() if board_of(r) >= 4}
    high_not_advanced = [
        c for c, row in high_prior.items()
        if c not in today_by_code
        or board_of(today_by_code[c]) != board_of(row) + 1
    ]
    perf_col = "涨跌幅"
    perf_values = [number(r.get(perf_col), 0) for r in yesterday_perf
                   if r.get(perf_col) not in (None, "")]
    sectors = {}
    for r in today:
        sector = r.get("所属行业")
        if sector:
            sectors[sector] = sectors.get(sector, 0) + 1
    snapshot = {
        "date": ymd, "previous_trade_day": previous_day,
        "sources": {
            "trade_calendar": "AKShare tool_trade_date_hist_sina",
            "limit_up": "AKShare stock_zt_pool_em (东方财富)",
            "broken": "AKShare stock_zt_pool_zbgc_em (东方财富)",
            "previous_performance": "AKShare stock_zt_pool_previous_em (东方财富)",
            "remark": "所属行业不等于热点题材；以下仅为行情接口统计，未核验公告或竞价逐笔数据。",
        },
        "statistics": {
            "limit_up": len(today), "broken": len(broken),
            "broken_rate_percent": round(len(broken) / (len(today) + len(broken)) * 100, 1)
            if today or broken else None,
            "previous_limit_up_avg_change_percent": round(sum(perf_values)/len(perf_values), 2)
            if perf_values else None,
            "promotions": promotions,
            "first_board_count": today_levels.get(1, 0),
            "max_board_today": max_today_board,
            "max_board_previous": max_prev_board,
            "board_distribution_today": today_levels,
            "board_distribution_previous": yesterday_levels,
            "high_board_previous_count": len(high_prior),
            "high_board_not_advanced_count": len(high_not_advanced),
            "high_board_not_advanced_codes": high_not_advanced[:25],
            "top_board_stocks": [
                {"代码": code_of(r), "名称": r.get("名称"), "连板数": board_of(r)}
                for r in sorted(today, key=board_of, reverse=True)[:12]
            ],
            "industry_limit_up_counts": sorted(sectors.items(), key=lambda p: -p[1])[:15],
        },
        "limit_up_stocks": today,
        "yesterday_limit_up_stocks": yesterday,
        "broken_stocks": broken[:120],
    }
    return snapshot


def previous_plan(ymd):
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if state.get("date") and state["date"] < ymd:
                return {"origin": f"上次系统保存的复盘：{state['date']}",
                        "plan": state.get("plan", {})}
        except (OSError, ValueError):
            pass
    if ymd == "20261019":
        return {
            "origin": "2026-10-18 ChatGPT 对话（初步建议，尚未验证实际成交）",
            "plan": {
                "market_view": "低空经济较强但以首板为主，高位分歧大；这一判断尚需以官方行情核实。",
                "overnight": "仅保隆科技603197作为最多5%仓位的激进试错候选，稳健方案不挂。",
                "overnight_candidates": [{"code": "603197", "action": "小仓试错，不超过总资金5%"}],
                "watchlist": [{"code": "002085", "action": "万丰奥威看竞价"},
                              {"code": "601811", "action": "新华文轩等换手回封"},
                              {"code": "002242", "action": "九阳股份等突破"},
                              {"code": "600241", "action": "时代万恒看承接"}],
                "avoid": [{"code": "600825", "action": "新华传媒不隔夜排高位一字板"}],
                "predictions": [
                    "低空经济能否由首板发展出二连板梯队",
                    "603197是否晋级、是否存在真实可成交时段",
                    "600825高位情绪对601811及传媒的影响",
                ],
            },
        }
    return {"origin": "未找到上次完整策略记录", "plan": {}}


def stock_outcomes(snapshot, prior):
    """按代码核对日线；无法获取竞价封单和排队次序，明确留空。"""
    import akshare as ak
    codes = []
    plan = prior.get("plan", {})
    for group in ("overnight_candidates", "watchlist", "avoid"):
        for x in plan.get(group, []):
            c = x.get("code", "")
            if isinstance(c, str) and len(c) == 6 and c.isdigit() and c not in codes:
                codes.append(c)
    results = {}
    for code in codes[:8]:
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=snapshot["previous_trade_day"],
                                    end_date=snapshot["date"], adjust="")
            rr = rows(df, max_rows=4)
            today = next((r for r in rr if str(r.get("日期", "")).replace("-", "") ==
                          snapshot["date"]), None)
            if today:
                results[code] = {
                    k: today.get(k) for k in
                    ("日期", "股票代码", "开盘", "收盘", "最高", "最低", "涨跌幅", "成交量", "换手率")
                }
            else:
                results[code] = {"status": "缺少本交易日有效日线"}
        except Exception:
            results[code] = {"status": "日线接口失败，不评价该标的走势"}
    return results


SYSTEM_PROMPT = """你是A股短线研究员。只依据JSON里的已核实数字输出结构化、可证伪的决策；不能杜撰竞价数据、公告、热点题材、机构资金、成交概率、封单变化或盘中精确封板过程。行业分布不是主线题材证据。所有事实明确标明日期和数据来源，不知道写未知。不得把涨停即视为能排到；成交可能恰恰意味着开板风险。考虑A股T+1。禁止保证次日收益。昨日没有明确推荐过的票不能事后算推荐。必须核对前次方案对错，并给出下一次验证条件。只有隔夜排板候选必须来自当日收盘涨停池（最多两只，市场差时零只）；竞价观察和明确放弃的股票可来自当日收盘涨停池、前一交易日涨停池或当日炸板池；当日没有收盘涨停的票绝不可标注为连板晋级。不得凭名称臆测概念关联。
仅返回合法JSON对象，字段严格如下：
{"temperature":整数0到100,"phase":"短文字","summary":"一段短文字",
"retrospection":"昨日观点→本日可核实结果→判断偏差和改进；缺失的数据也须说清",
"overnight":"排或不排","overnight_candidates":[{"code":"6位股票代码","reason":"理由、条件、风险、单票仓位上限"}],
"watchlist":[{"code":"6位股票代码","reason":"为什么只观察竞价或等回封"}],
"avoid":[{"code":"6位股票代码","reason":"避免的风险"}],
"predictions":["下一交易日可证伪的判断，3至5条"]}
所有名单都必须来自输入的股票池，绝不能写不存在的代码；隔夜排板候选只能从今日收盘涨停池挑选。没有确认的题材联动、公告或交易可行性，不应给出确定性隔夜排板结论；缺少必需信息时输出不排。
"""



def local_rule_plan(snapshot, prior, outcomes):
    """离线可复现的日线情绪规则，不调用LLM；不产生自动隔夜挂单指令。"""
    stats = snapshot.get("statistics", {})
    elevated_risk = high_board_risk(stats)
    limit_up = max(0, int(number(stats.get("limit_up"), 0)))
    broken_rate = stats.get("broken_rate_percent")
    premium = stats.get("previous_limit_up_avg_change_percent")
    promotions = stats.get("promotions", {})
    one = promotions.get("1进2", {})
    two = promotions.get("2进3", {})

    def rate_score(cohort, target_rate):
        if not isinstance(cohort, dict) or number(cohort.get("base")) < 3:
            return 0.0  # 样本小于3时不靠百分比外推强度
        value = cohort.get("rate")
        return min(1.0, max(0.0, number(value) / target_rate)) if value is not None else 0.0

    # 评分只表示当日短线样本情绪，不代表未来收益、成交率或股票质量。
    breadth_score = 27 * min(1.0, limit_up / 75)
    failure_score = (23 * (1 - min(1.0, max(0.0, number(broken_rate) / 50)))
                     if broken_rate is not None else 0)
    upper = [
        rate_score(value, 35) for key, value in promotions.items()
        if isinstance(value, dict) and key.split("进")[0].isdigit()
        and int(key.split("进")[0]) >= 3 and number(value.get("base")) >= 3
    ]
    ladder_score = (14 * rate_score(one, 45) + 10 * rate_score(two, 40)
                    + 6 * (sum(upper) / len(upper) if upper else 0))
    premium_score = (20 * min(1.0, max(0.0, (number(premium) + 2) / 6))
                     if premium is not None else 0)
    temperature = max(0, min(100, round(
        breadth_score + failure_score + ladder_score + premium_score
        - (15 if elevated_risk else 0))))

    one_rate = one.get("rate")
    two_rate = two.get("rate")
    if temperature < 45:
        phase, style, max_position = "退潮或数据偏弱", "空仓观察", 0
    elif temperature < 60:
        phase, style, max_position = "修复试错", "优先观察1进2，不追高位缩量加速", 10
    elif temperature < 75:
        if two_rate is not None and number(two.get("base")) >= 3 and number(two_rate) >= 35:
            style = "优先观察2进3的分歧回封，1进2只看最强换手"
        else:
            style = "优先观察1进2换手，减少高位接力"
        phase, max_position = "情绪回暖", 25
    else:
        phase, style, max_position = "情绪偏强", "覆盖首板到高标，但高标须分歧转一致后再次确认", 40
    if elevated_risk:
        max_position = min(max_position, 10)
        style += "；昨日高标未晋级偏多，暂停高位接力"

    screening = screen_full_ladder(snapshot, temperature, max_position,
                                   high_risk=elevated_risk)
    watchlist = screening["watchlist"]

    avoid = []
    broken = sorted(snapshot.get("broken_stocks", []),
                    key=lambda r: number(r.get("炸板次数"), 0), reverse=True)
    for row in broken:
        code = code_of(row)
        if len(code) != 6 or not code.isdigit() or code in [r["code"] for r in avoid]:
            continue
        avoid.append({"code": code, "reason": "当天在炸板池中；承接和次日流动性尚未验证，不隔夜排。"})
        if len(avoid) >= 3:
            break

    checks = []
    for code, result in outcomes.items():
        if result.get("status"):
            checks.append(f"{code}：{result['status']}")
        elif result.get("涨跌幅") is not None:
            checks.append(f"{code}：当日收盘涨跌幅{result['涨跌幅']}%（非交易收益率）")
    retrospection = (
        f"前次基线：{prior.get('origin', '未知')}。"
        + ("；".join(checks[:6]) if checks else "未取得可验证的前次个股收盘数据")
        + "；不具备逐笔成交和排队记录，不能按实际买入或卖出盈亏验收。"
    )
    premium_label = f"{premium}%" if premium is not None else "缺失"
    summary = (
        f"【纯本地规则，无大模型】水温{temperature}/100（评分，不是预测概率），"
        f"涨停{limit_up}、炸板率{broken_rate if broken_rate is not None else '未知'}%、"
        f"昨日涨停股当日平均涨跌{premium_label}。"
        f"策略：{style}；参考总仓位上限{max_position}%（非买入指令），"
        f"单一观察标的参考仓位上限{min(5, max_position)}%，"
        "遵守单日亏损停止交易及A股T+1约束。"
        "没有实时竞价、监管公告及隔夜排队条件核验，今晚默认不排。"
    )
    predictions = [
        "次日开盘前复核昨日涨停股平均溢价及竞价高开分布，未验证不提高仓位。",
        "次日收盘核对首板、1进2、2进3、3进4、4进5以及所有高标的样本晋级情况。",
        "观察名单仅在次日盘中核对换手、承接和分歧回封，不据此自动发单或撤单。",
    ]
    plan = validate_plan({
        "temperature": temperature, "phase": phase,
        "summary": summary, "retrospection": retrospection,
        "overnight": "不排", "overnight_candidates": [],
        "watchlist": watchlist, "avoid": avoid,
        "predictions": predictions,
    }, snapshot)
    checked = {item["code"] for item in plan["watchlist"]}
    plan["opportunities"] = [
        item for item in screening["opportunities"] if item["code"] in checked
    ]
    plan["reference_max_position"] = max_position
    plan["high_board_risk"] = elevated_risk
    return plan


def model_plan(snapshot, prior, outcomes):
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return local_rule_plan(snapshot, prior, outcomes)
    from openai import OpenAI
    kwargs = {"api_key": key, "timeout": 75}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    client = OpenAI(**kwargs)
    data = {"market": snapshot, "previous_plan": prior, "today_outcomes": outcomes}
    result = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)},
        ],
        temperature=0.1,
    )
    answer = json.loads(result.choices[0].message.content or "{}")
    return validate_plan(answer, snapshot)


def validate_plan(plan, snapshot):
    """不允许模型推荐非当日涨停池证券；不完整则不推隔夜候选。"""
    if not isinstance(plan, dict):
        raise ValueError("模型报告不是JSON对象")
    allowed_overnight = {code_of(r): r for r in snapshot["limit_up_stocks"]}
    allowed_watch = {}
    for pool_name in ("yesterday_limit_up_stocks", "broken_stocks", "limit_up_stocks"):
        for r in snapshot.get(pool_name, []):
            allowed_watch[code_of(r)] = r
    clean = {}
    for key in ("overnight_candidates", "watchlist", "avoid"):
        valid = []
        candidates = plan.get(key, [])
        if not isinstance(candidates, list):
            candidates = []
        allowed = allowed_overnight if key == "overnight_candidates" else allowed_watch
        for item in candidates:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if code in allowed and code not in [r["code"] for r in valid]:
                valid.append({"code": code, "name": allowed[code].get("名称", ""),
                              "board": board_of(allowed[code]),
                              "reason": str(item.get("reason", ""))[:400]})
        clean[key] = valid[:2 if key == "overnight_candidates" else
                           24 if key == "watchlist" else 6]
    decision = "排" if plan.get("overnight") == "排" and clean["overnight_candidates"] else "不排"
    # 模型的“不排”判断必须与实际候选保持一致，避免自相矛盾的报告。
    if decision == "不排":
        clean["overnight_candidates"] = []
    temp = plan.get("temperature")
    temp = max(0, min(100, int(number(temp)))) if temp is not None else None
    predictions = plan.get("predictions", [])
    if not isinstance(predictions, list):
        predictions = []
    return {
        "temperature": temp, "phase": str(plan.get("phase", "待核实"))[:30],
        "summary": str(plan.get("summary", ""))[:1200],
        "retrospection": str(plan.get("retrospection", ""))[:1900],
        "overnight": decision,
        **clean,
        "predictions": [str(x)[:260] for x in predictions[:5]
                        if isinstance(x, str)],
    }


def render(snapshot, prior, outcomes, plan):
    s = snapshot["statistics"]
    lines = [
        f"## 【股票】短线梯队复盘｜{snapshot['date']}",
        f"**今晚隔夜排板：{plan['overnight']}**（研究建议，非自动下单）",
        f"市场水温：{plan.get('temperature') if plan.get('temperature') is not None else '未评估'}/100；{plan.get('phase','待核实')}",
        "",
        f"涨停 {s['limit_up']} 家｜炸板 {s['broken']} 家｜炸板率 {s['broken_rate_percent']}%",
        f"昨日涨停股当日平均涨跌：{s['previous_limit_up_avg_change_percent']}%（接口可用时）",
    ]
    lines.extend(ladder_report(snapshot, plan))
    lines += ["", "### 昨日策略验收", f"基线：{prior['origin']}",
              plan.get("retrospection") or "无可验证的前日策略记录。"]
    if outcomes:
        for code, result in outcomes.items():
            if result.get("status"):
                lines.append(f"- {code}：{result['status']}")
            else:
                lines.append(
                    f"- {code}：日线涨跌幅 {result.get('涨跌幅','未知')}%，"
                    f"最高 {result.get('最高','未知')}，最低 {result.get('最低','未知')}，"
                    f"收盘 {result.get('收盘','未知')}；是否排到涨停无法从日线确认。"
                )
    lines += ["", "### 今日判断", plan.get("summary", "未生成观点。"), "",
              "### 今晚隔夜候选"]
    if not plan["overnight_candidates"]:
        lines.append("无合格的隔夜排板候选。")
    for item in plan["overnight_candidates"]:
        lines.append(f"- {item['name']} {item['code']}（{item['board']}板）：{item['reason']}")
    lines.append("### 仅观察竞价／等待回封")
    for item in plan["watchlist"]:
        lines.append(f"- {item['name']} {item['code']}：{item['reason']}")
    if not plan["watchlist"]:
        lines.append("无核实候选。")
    lines.append("### 明确放弃")
    for item in plan["avoid"]:
        lines.append(f"- {item['name']} {item['code']}：{item['reason']}")
    lines.append("### 明日验证")
    for i, p in enumerate(plan["predictions"], 1):
        lines.append(f"{i}. {p}")
    lines += [
        "", "数据：AKShare／东方财富涨停池、炸板池、昨日涨停池；不保证第三方接口完整性。",
        "**不足：本流程不提供实时竞价封单、逐笔排队、监管公告的自动核验，不能作为9:20撤单信号。**",
        "A股T+1及涨跌停流动性风险较大。GitHub Actions定时任务可能延迟。",
    ]
    return "\n\n".join(lines)


def signed_url(url, secret):
    if not secret:
        return url
    ts = str(int(time.time() * 1000))
    raw = f"{ts}\n{secret}".encode("utf-8")
    sig = base64.b64encode(hmac.new(secret.encode("utf-8"), raw,
                                         hashlib.sha256).digest()).decode("utf-8")
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs.extend([("timestamp", ts), ("sign", sig)])
    # urlencode只执行一次百分号编码；避免钉钉校验出现sign not match。
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(pairs), parts.fragment))


def send_dingtalk(text):
    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    host = urlsplit(webhook).hostname or ""
    if not webhook.startswith("https://") or host not in ("oapi.dingtalk.com", "api.dingtalk.com"):
        raise RuntimeError("请配置钉钉官方HTTPS Webhook到GitHub Secret：DINGTALK_WEBHOOK")
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    # 控制单条钉钉payload，长报告按正文行尽力分段。
    limit = 13500
    chunks, buf = [], ""
    for paragraph in text.split("\n\n"):
        if len((buf + paragraph).encode("utf-8")) > limit and buf:
            chunks.append(buf)
            buf = ""
        if len(paragraph.encode("utf-8")) > limit:
            while paragraph:
                piece = paragraph[:3500]
                chunks.append(piece)
                paragraph = paragraph[3500:]
        if paragraph:
            buf = (buf + "\n\n" + paragraph).strip()
    if buf:
        chunks.append(buf)
    if not chunks:
        raise RuntimeError("消息内容为空")
    for i, body in enumerate(chunks, 1):
        url = signed_url(webhook, secret)
        msg = {"msgtype": "markdown", "markdown": {
            "title": f"股票短线复盘（{i}/{len(chunks)}）",
            "text": f"### 【股票】短线复盘（{i}/{len(chunks)}）\n\n" + body,
        }}
        try:
            res = requests.post(url, json=msg, timeout=20)
            res.raise_for_status()
        except requests.RequestException as exc:
            # SDK/requests errors may include the signed webhook URL with its secret token.
            raise RuntimeError(f"钉钉推送网络或HTTP失败：{type(exc).__name__}（请求URL已隐藏）") from None
        result = res.json()
        if result.get("errcode", -1) != 0:
            raise RuntimeError(f"钉钉拒收消息，errcode={result.get('errcode')}，errmsg={str(result.get('errmsg'))[:120]}")
        if i < len(chunks):
            time.sleep(1.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("test", "review"), default="test")
    parser.add_argument("--as-of", default="", help="手动补跑交易日 YYYYMMDD；定时任务默认当日")
    args = parser.parse_args()
    if args.mode == "test":
        send_dingtalk(f"【股票】GitHub Actions→钉钉通道测试成功；北京时间：{local_now():%Y-%m-%d %H:%M}。未调用行情和AI接口。")
        print("DingTalk test message delivered")
        return 0
    today = local_now().strftime("%Y%m%d")
    historical = bool(args.as_of)
    if historical:
        try:
            target = datetime.strptime(args.as_of, "%Y%m%d").date()
        except ValueError:
            raise SystemExit("手动补跑日期必须为真实有效的 YYYYMMDD，例如 20261016")
        delta = local_now().date() - target
        if delta.days < 0 or delta.days > 30:
            raise SystemExit("手动补跑仅允许最近30日内且不晚于今天的日期")
        today = target.strftime("%Y%m%d")
    elif os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        # GitHub 手动 review 适用于非交易日的联调；默认工作日定时任务不补跑。
        import akshare as ak
        import pandas as pd
        calendar = ak.tool_trade_date_hist_sina()
        if calendar is None or calendar.empty or "trade_date" not in calendar:
            raise RuntimeError("手动补跑无法验证交易日历，不生成历史报告")
        dates = pd.to_datetime(calendar["trade_date"]).dt.strftime("%Y%m%d")
        prior_days = [d for d in dates if d <= today]
        if not prior_days:
            raise RuntimeError("交易日历没有可用的历史交易日")
        manual_day = max(prior_days)
        if manual_day != today:
            today, historical = manual_day, True
            if (local_now().date() - datetime.strptime(today, "%Y%m%d").date()).days > 7:
                raise RuntimeError("最近交易日超过7天，不自动历史补跑")
    snapshot = fetch_market_snapshot(today)
    if snapshot is None:
        print(f"{today}不是A股交易日，不发送复盘。")
        return 0
    prior = previous_plan(today)
    outcomes = stock_outcomes(snapshot, prior)
    # 不配置API Key时直接运行确定性行情规则；模型异常时同样回退本地规则。
    try:
        plan = model_plan(snapshot, prior, outcomes)
    except Exception as exc:
        print(f"模型调用失败，改为本地规则复盘：{type(exc).__name__}", file=sys.stderr)
        plan = local_rule_plan(snapshot, prior, outcomes)
    content = render(snapshot, prior, outcomes, plan)
    if historical and today != local_now().strftime("%Y%m%d"):
        stamp = local_now().strftime("%Y-%m-%d %H:%M")
        content = (
            f"## 【股票】历史交易日复盘补跑｜行情日期：{today}｜实际发送：{stamp}（北京时间）\n\n"
            "**这是一份历史收盘研究记录，不代表今天市场的实时信号；"
            "任何候选、仓位或排板条件均须用下一个实际交易日行情重新验证。**\n\n"
            + content
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{today}.md"
    path.write_text(content, encoding="utf-8")
    send_dingtalk(content)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"date": today, "plan": plan},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已推送并保存为 {path}")
    return 0


def model_plan_without_ai():
    return {"temperature": None, "phase": "未验证", "summary": "AI不可用，今晚不排。",
            "retrospection": "可核实的标的日线见下；没有模型推理，不推断板块情绪。",
            "overnight": "不排", "overnight_candidates": [], "watchlist": [], "avoid": [],
            "predictions": []}


if __name__ == "__main__":
    raise SystemExit(main())
