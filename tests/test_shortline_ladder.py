"""全连板梯队统计、收盘预筛、条件触发和市场退潮的离线回归。"""
import sys
from pathlib import Path
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.shortline_ladder import screen_full_ladder, high_board_risk, ladder_report
from scripts import shortline_dingtalk as mod


def item(code, name, board, *, turnover=8.0, breaks=0):
    return {"代码": code, "名称": name, "连板数": board,
            "换手率": turnover, "炸板次数": breaks, "封板资金": 100000}


def market():
    levels = [
        item("600001", "首板", 1),
        item("600002", "二板", 2),
        item("600003", "三板", 3),
        item("600004", "四板", 4),
        item("600005", "五板", 5),
        item("600006", "六板", 6),
        item("600007", "七板", 7),
    ]
    promotions = {f"{n}进{n+1}": {"base": 4, "count": 2, "rate": 50}
                  for n in range(1, 8)}
    return {
        "limit_up_stocks": levels,
        "broken_stocks": [],
        "yesterday_limit_up_stocks": [],
        "statistics": {
            "limit_up": 90, "broken": 8, "broken_rate_percent": 8.2,
            "previous_limit_up_avg_change_percent": 4.0,
            "promotions": promotions,
            "first_board_count": 1, "max_board_today": 7,
            "max_board_previous": 6,
            "board_distribution_today": {n: 1 for n in range(1, 8)},
            "board_distribution_previous": {n: 4 for n in range(1, 7)},
            "high_board_previous_count": 3,
            "high_board_not_advanced_count": 0,
            "top_board_stocks": [{"名称": "七板", "代码": "600007", "连板数": 7}],
        },
    }


class FullLadderTests(unittest.TestCase):
    def test_1_to_7_board_all_covered_and_not_automatic_order(self):
        result = mod.local_rule_plan(market(), {"origin": "前日"}, {})
        watch = {r["code"] for r in result["watchlist"]}
        self.assertEqual(watch, {f"60000{n}" for n in range(1, 8)})
        opportunities = {r["code"]: r for r in result["opportunities"]}
        self.assertIn("600007", opportunities)
        self.assertEqual(opportunities["600007"]["stage"], "7进8")
        self.assertEqual(opportunities["600007"]["position_cap"], 3)
        self.assertIn("待确认", opportunities["600007"]["status"])
        self.assertIn("利空", opportunities["600007"]["invalidation"])
        self.assertEqual(result["overnight"], "不排")
        self.assertEqual(result["overnight_candidates"], [])

    def test_weaker_market_never_promotes_setup_to_trade(self):
        snap = market()
        snap["statistics"].update({
            "limit_up": 6, "broken_rate_percent": 75,
            "previous_limit_up_avg_change_percent": -5,
            "promotions": {},
        })
        result = mod.local_rule_plan(snap, {"origin": "前日"}, {})
        self.assertEqual(result["reference_max_position"], 0)
        self.assertEqual(result["opportunities"], [])
        self.assertIn("0%", result["summary"])

    def test_high_board_nonpromotion_is_risk_signal_not_claimed_limit_down(self):
        snap = market()
        snap["statistics"]["high_board_not_advanced_count"] = 2
        self.assertTrue(high_board_risk(snap["statistics"]))
        result = mod.local_rule_plan(snap, {"origin": "前日"}, {})
        self.assertLessEqual(result["reference_max_position"], 10)
        self.assertTrue(result["high_board_risk"])
        self.assertNotIn("600007", [p["code"] for p in result["opportunities"]])
        rendered = ladder_report(snap, result)
        self.assertTrue(any("可能包含停牌" in line for line in rendered))

    def test_no_exchange_specific_comparability_and_missing_fields(self):
        snap = market()
        snap["limit_up_stocks"] += [
            item("300001", "创业板", 2),
            item("688001", "科创板", 3),
            item("600008", "缺数据", 4, turnover=None, breaks=None),
            item("600009", "*ST测试", 2),
        ]
        screened = screen_full_ladder(snap, 95, 40)
        watch = [p["code"] for p in screened["watchlist"]]
        setups = [p["code"] for p in screened["opportunities"]]
        self.assertIn("300001", watch)
        self.assertNotIn("300001", setups)
        self.assertNotIn("688001", setups)
        self.assertNotIn("600008", setups)
        self.assertNotIn("600009", watch)

    def test_empty_snapshot_has_no_invented_stock(self):
        snap = market()
        snap["limit_up_stocks"] = []
        screened = screen_full_ladder(snap, 80, 40)
        self.assertEqual(screened["watchlist"], [])
        self.assertEqual(screened["opportunities"], [])

    def test_full_ladder_is_rendered(self):
        snap = market()
        result = mod.local_rule_plan(snap, {"origin": "前日"}, {})
        text = mod.render({
            **snap,
            "date": "20261019",
        }, {"origin": "前日"}, {}, result)
        for name in ("1进2", "2进3", "3进4", "4进5", "5进6", "6进7", "7进8"):
            self.assertIn(name, text)
        self.assertIn("条件触发的高关注机会", text)
        self.assertIn("确认条件", text)
        self.assertIn("立即放弃", text)
        self.assertIn("收盘预筛≠盘中信号已触发", text)

    def test_real_snapshot_dynamic_promotion_of_high_board(self):
        import pandas as pd
        today = [item("600007", "七板", 7), item("600001", "首板", 1)]
        yesterday = [item("600007", "六板", 6), item("600008", "五板", 5)]
        fake = types.SimpleNamespace(
            tool_trade_date_hist_sina=lambda: pd.DataFrame(
                {"trade_date": ["2026-10-16", "2026-10-19"]}),
            stock_zt_pool_em=lambda date: pd.DataFrame(
                today if date == "20261019" else yesterday),
            stock_zt_pool_zbgc_em=lambda date: pd.DataFrame(
                [item("600008", "五板炸板", 5)]),
            stock_zt_pool_previous_em=lambda date: pd.DataFrame(
                [{"涨跌幅": 1.0}]),
        )
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            snap = mod.fetch_market_snapshot("20261019")
        self.assertEqual(snap["statistics"]["max_board_today"], 7)
        self.assertEqual(snap["statistics"]["first_board_count"], 1)
        self.assertEqual(snap["statistics"]["promotions"]["6进7"]["count"], 1)
        self.assertEqual(snap["statistics"]["promotions"]["5进6"]["count"], 0)
        self.assertEqual(snap["statistics"]["high_board_not_advanced_count"], 1)


if __name__ == "__main__":
    unittest.main()
