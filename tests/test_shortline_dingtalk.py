"""Tests for the isolated shortline/DingTalk pipeline; all endpoints are mocked."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import shortline_dingtalk as mod


def stock(code, name, board):
    return {"代码": code, "名称": name, "连板数": board}


def snapshot():
    return {
        "limit_up_stocks": [stock("603197", "保隆科技", 2)],
        "yesterday_limit_up_stocks": [
            stock("603197", "保隆科技", 1),
            stock("002242", "九阳股份", 1),
        ],
        "broken_stocks": [stock("600241", "时代万恒", 0)],
    }


class CandidateTests(unittest.TestCase):
    def test_overnight_rejects_non_limit_up_but_watch_and_avoid_can_use_prior_pool(self):
        plan = {
            "overnight": "排",
            "overnight_candidates": [
                {"code": "603197", "reason": "研究候选"},
                {"code": "002242", "reason": "未收盘涨停，不得隔夜排"},
            ],
            "watchlist": [{"code": "002242", "reason": "观察"}],
            "avoid": [{"code": "600241", "reason": "炸板风险"}],
            "temperature": 38,
            "predictions": ["次日检验"],
        }
        result = mod.validate_plan(plan, snapshot())
        self.assertEqual(result["overnight"], "排")
        self.assertEqual([x["code"] for x in result["overnight_candidates"]], ["603197"])
        self.assertEqual([x["code"] for x in result["watchlist"]], ["002242"])
        self.assertEqual([x["code"] for x in result["avoid"]], ["600241"])

    def test_do_not_publish_overnight_candidates_when_model_says_no(self):
        result = mod.validate_plan({
            "overnight": "不排",
            "overnight_candidates": [{"code": "603197", "reason": "冲突"}],
            "temperature": -8,
        }, snapshot())
        self.assertEqual(result["overnight"], "不排")
        self.assertEqual(result["overnight_candidates"], [])
        self.assertEqual(result["temperature"], 0)

    def test_malformed_model_predictions_type(self):
        result = mod.validate_plan({
            "overnight": "不排",
            "predictions": "这个应当是数组而不是字符串",
        }, snapshot())
        self.assertEqual(result["predictions"], [])

    def test_invalid_codes_never_pass(self):
        result = mod.validate_plan({
            "overnight": "排",
            "overnight_candidates": [{"code": "123456", "reason": "编造"}],
            "watchlist": [{"code": "654321", "reason": "编造"}],
        }, snapshot())
        self.assertEqual(result["overnight"], "不排")
        self.assertEqual(result["watchlist"], [])

    def test_missing_api_key_is_conservative(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            result = mod.model_plan(snapshot(), {}, {})
        self.assertEqual(result["overnight"], "不排")
        self.assertFalse(result["overnight_candidates"])


class NotificationTests(unittest.TestCase):
    def test_signature_is_correct_and_encoded_once(self):
        secret = "SEC-TEST+/="
        url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
        with mock.patch.object(mod.time, "time", return_value=1700000000.123):
            result = mod.signed_url(url, secret)
        qs = parse_qs(urlsplit(result).query)
        stamp = "1700000000123"
        expected = base64.b64encode(hmac.new(
            secret.encode("utf-8"), (stamp + "\n" + secret).encode("utf-8"),
            hashlib.sha256,
        ).digest()).decode("utf-8")
        self.assertEqual(qs["timestamp"], [stamp])
        self.assertEqual(qs["sign"], [expected])
        self.assertEqual(qs["access_token"], ["abc"])

    def test_unofficial_webhook_is_rejected_without_network(self):
        with mock.patch.dict("os.environ", {
            "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com.evil.test/robot/send",
            "DINGTALK_SECRET": "",
        }), mock.patch.object(mod.requests, "post") as post:
            with self.assertRaises(RuntimeError):
                mod.send_dingtalk("测试")
            post.assert_not_called()

    def test_dingtalk_rejection_is_failure_even_when_http_is_200(self):
        response = mock.Mock()
        response.json.return_value = {"errcode": 310000, "errmsg": "keyword missing"}
        with mock.patch.dict("os.environ", {
            "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "DINGTALK_SECRET": "",
        }), mock.patch.object(mod.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "310000"):
                mod.send_dingtalk("没有关键词")
    
    def test_http_failure_does_not_expose_secret_url(self):
        secret_url = "https://oapi.dingtalk.com/robot/send?access_token=SENSITIVE-SECRET"
        with mock.patch.dict("os.environ", {
            "DINGTALK_WEBHOOK": secret_url, "DINGTALK_SECRET": "",
        }), mock.patch.object(mod.requests, "post", side_effect=
                             mod.requests.ConnectionError(secret_url)):
            with self.assertRaises(RuntimeError) as context:
                mod.send_dingtalk("股票复盘")
        self.assertNotIn("SENSITIVE-SECRET", str(context.exception))
        self.assertIn("URL已隐藏", str(context.exception))

    def test_dingtalk_success(self):
        response = mock.Mock()
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        with mock.patch.dict("os.environ", {
            "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "DINGTALK_SECRET": "",
        }), mock.patch.object(mod.requests, "post", return_value=response) as post:
            mod.send_dingtalk("【股票】测试")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["msgtype"], "markdown")


class SnapshotTests(unittest.TestCase):
    def test_trade_day_counts_and_promotion(self):
        import pandas as pd
        stub = types.SimpleNamespace(
            tool_trade_date_hist_sina=lambda: pd.DataFrame({
                "trade_date": ["2026-10-16", "2026-10-19"]}),
            stock_zt_pool_em=lambda date: pd.DataFrame(
                [stock("603197", "保隆科技", 2)] if date == "20261019" else
                [stock("603197", "保隆科技", 1),
                 stock("002242", "九阳股份", 1)]),
            stock_zt_pool_zbgc_em=lambda date: pd.DataFrame([
                {"代码": "600241", "名称": "时代万恒"}]),
            stock_zt_pool_previous_em=lambda date: pd.DataFrame([
                {"涨跌幅": -1.0}, {"涨跌幅": 3.0}]),
        )
        with mock.patch.dict(sys.modules, {"akshare": stub}):
            result = mod.fetch_market_snapshot("20261019")
        self.assertEqual(result["statistics"]["limit_up"], 1)
        self.assertEqual(result["statistics"]["broken"], 1)
        self.assertEqual(result["statistics"]["broken_rate_percent"], 50.0)
        self.assertEqual(result["statistics"]["promotions"]["1进2"]["rate"], 50.0)
        self.assertEqual(result["statistics"]["previous_limit_up_avg_change_percent"], 1.0)

    def test_non_trading_day_is_not_reported(self):
        import pandas as pd
        stub = types.SimpleNamespace(tool_trade_date_hist_sina=lambda:
            pd.DataFrame({"trade_date": ["2026-10-16", "2026-10-19"]}))
        with mock.patch.dict(sys.modules, {"akshare": stub}):
            self.assertIsNone(mod.fetch_market_snapshot("20261018"))

    def test_historical_plan_is_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file = Path(tempdir) / "state.json"
            file.write_text(json.dumps({
                "date": "20261019",
                "plan": {"overnight": "不排", "predictions": ["次日走势检验"]},
            }), encoding="utf-8")
            with mock.patch.object(mod, "STATE_FILE", file):
                result = mod.previous_plan("20261020")
        self.assertEqual(result["plan"]["predictions"], ["次日走势检验"])


if __name__ == "__main__":
    unittest.main()
