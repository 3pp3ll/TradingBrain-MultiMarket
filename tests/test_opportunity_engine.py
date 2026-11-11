from test_support import local_data_test  # Offline safety guard before project imports
import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from options_intelligence.opportunity_engine import (
    PROJECT_ROOT, evaluate, evaluate_technicals, local_json, run,
)
from options_intelligence.technical_indicators import build_report

NOW = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)


def bundle(symbol="AAA", slope=0.1):
    bars = []
    for i in range(100):
        close = 100 + i * slope + i % 2
        bars.append(dict(date=(date(2026, 11, 9) - timedelta(days=99-i)).isoformat(),
                         open=close, high=close+1, low=close-1, close=close, volume=1000))
    daily = dict(source="alpha_vantage_TIME_SERIES_DAILY", symbol=symbol,
                 timeframe="1d", candles=bars, fetched_at_utc="2026-11-10T15:00:00Z",
                 price_adjustment="raw_unadjusted", bar_count=100, latest_trading_day="2026-11-09")
    report = build_report(daily, f"data/output/alpha_vantage/{symbol}_daily.json")
    return dict(daily=daily, report=report, report_file=f"outputs/{symbol}_technical.json")


def stage(*symbols):
    return dict(stage="FINVIZ_STAGE_1", source="USER_SUPPLIED_CSV",
                generated_at_utc="2026-11-10T16:00:00Z", snapshot_utc="2026-11-10T15:00:00Z",
                data_date="2026-11-09",
                decisions=[dict(ticker=s, status="WATCHLIST", reasons=[]) for s in symbols])


class OpportunityEngineTests(unittest.TestCase):
    def test_ranking_and_deterministic_ties(self):
        inputs = {"AAA": bundle("AAA", .1), "BBB": bundle("BBB", .3), "CCC": bundle("CCC", .1)}
        result = evaluate(stage("CCC", "BBB", "AAA"), inputs, NOW)
        self.assertEqual(result["counts"]["WATCHLIST"], 3)
        self.assertEqual([d["ticker"] for d in result["decisions"]], ["BBB", "AAA", "CCC"])
        self.assertEqual([d["rank"] for d in result["decisions"]], [1, 2, 3])
        self.assertFalse(result["score_is_win_probability"])
        for d in result["decisions"]:
            self.assertFalse(d["execution_enabled"])
            self.assertEqual(len(d["execution_blockers"]), 5)
            self.assertNotIn("direction", d)

    def test_stage_rejection_separate_from_technical_pass(self):
        source = stage("AAA")
        source["decisions"][0].update(status="REJECTED", reasons=["SMALL_MARKET_CAP"])
        item = evaluate(source, {"AAA": bundle()}, NOW)["decisions"][0]
        self.assertEqual(item["status"], "REJECTED")
        self.assertEqual(item["technical_assessment"]["status"], "WATCHLIST")
        self.assertIn("SMALL_MARKET_CAP", item["reasons"])
        self.assertIsNone(item["score"])

    def test_oversold_is_not_buy_confirmation_and_volatility_gate(self):
        indicators = dict(ema_20=110, ema_50=100, rsi_14=20, atr_14=2)
        result = evaluate_technicals(indicators, 100)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("OVERSOLD_IS_NOT_BUY_CONFIRMATION", result["reasons"])
        indicators.update(rsi_14=60, atr_14=9)
        self.assertIn("ATR_ABOVE_RESEARCH_LIMIT", evaluate_technicals(indicators, 100)["reasons"])
        indicators.update(ema_20=90, rsi_14=40, atr_14=2)
        self.assertEqual(evaluate_technicals(indicators, 100)["trend"], "BEARISH")

    def test_stale_stage_and_daily(self):
        source = stage("AAA")
        source["snapshot_utc"] = "2026-11-01T15:00:00Z"
        item = evaluate(source, {"AAA": bundle()}, NOW)["decisions"][0]
        self.assertIn("STALE_STAGE1_SNAPSHOT", item["data_issues"])
        item = evaluate(stage("AAA"), {"AAA": bundle()}, NOW + timedelta(days=7))["decisions"][0]
        self.assertIn("STALE_DAILY_DATA", item["data_issues"])
        self.assertEqual(item["status"], "INSUFFICIENT_DATA")

    def test_missing_stage_market_timestamps_not_inferred(self):
        source = stage("AAA")
        del source["snapshot_utc"], source["data_date"]
        item = evaluate(source, {"AAA": bundle()}, NOW)["decisions"][0]
        self.assertIn("MISSING_OR_INVALID_STAGE1_SNAPSHOT", item["data_issues"])
        self.assertIn("MISSING_OR_INVALID_STAGE1_DATA_DATE", item["data_issues"])
        self.assertEqual(item["status"], "INSUFFICIENT_DATA")

    def test_symbol_mismatch(self):
        for key in ("daily", "report"):
            data = bundle()
            data[key]["symbol"] = "OTHER"
            item = evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]
            self.assertIn("SYMBOL_MISMATCH", item["data_issues"])
            self.assertEqual(item["status"], "INSUFFICIENT_DATA")

    def test_date_mismatch(self):
        source = stage("AAA")
        source["data_date"] = "2026-11-08"
        item = evaluate(source, {"AAA": bundle()}, NOW)["decisions"][0]
        self.assertIn("STAGE1_TECHNICAL_DATE_MISMATCH", item["data_issues"])
        data = bundle()
        data["report"]["date"] = "2026-11-08"
        item = evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]
        self.assertIn("REPORT_DAILY_MISMATCH:date", item["data_issues"])

    def test_missing_indicators_and_optional_ema200(self):
        for key in ("ema_20", "ema_50", "rsi_14", "atr_14"):
            data = bundle()
            del data["report"]["indicators"][key]
            data["report"]["unavailable_indicators"][key] = "MISSING_FROM_REPORT"
            item = evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]
            self.assertEqual(item["status"], "INSUFFICIENT_DATA")
            self.assertIn("MISSING_OR_INVALID:" + key, item["reasons"])
        item = evaluate(stage("AAA"), {"AAA": bundle()}, NOW)["decisions"][0]
        self.assertEqual(item["status"], "WATCHLIST")
        self.assertIn("requires 200", item["warnings"][0])
        self.assertNotIn("ema_200", item["technical_assessment"])

    def test_report_recomputed_not_blindly_trusted(self):
        for value in (999, float("nan"), True, "110"):
            data = bundle()
            data["report"]["indicators"]["ema_20"] = value
            item = evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]
            self.assertIn("INDICATOR_DAILY_MISMATCH:ema_20", item["data_issues"])

    def test_future_file_snapshot_and_availability(self):
        source = stage("AAA")
        source["generated_at_utc"] = "2026-11-11T16:00:00Z"
        source["snapshot_utc"] = "2026-11-11T15:00:00Z"
        data = bundle()
        data["daily"]["fetched_at_utc"] = "2026-11-11T15:00:00Z"
        data["report"]["source_fetched_at_utc"] = data["daily"]["fetched_at_utc"]
        item = evaluate(source, {"AAA": data}, NOW)["decisions"][0]
        self.assertEqual(item["status"], "INSUFFICIENT_DATA")
        for reason in ("FUTURE_STAGE1_FILE", "FUTURE_STAGE1_SNAPSHOT", "FUTURE_DAILY_AVAILABILITY"):
            self.assertIn(reason, item["data_issues"])
        self.assertIsNone(item["technical_assessment"])
        self.assertIsNone(item["score"])
        self.assertIsNone(item["rank"])

    def test_future_candle_never_produces_historical_watchlist(self):
        data = bundle()
        new_bar = dict(data["daily"]["candles"][-1], date="2026-11-11", close=10000,
                       open=10000, high=10001, low=9999)
        data["daily"]["candles"].append(new_bar)
        data["daily"].update(bar_count=101, latest_trading_day="2026-11-11")
        data["report"] = build_report(data["daily"], data["report"]["source_file"])
        item = evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]
        self.assertIn("DAILY_DATE_NOT_COMPLETED", item["data_issues"])
        self.assertEqual(item["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(item["technical_assessment"])
        self.assertIsNone(item["score"])

    def test_missing_bundle_and_invalid_data(self):
        self.assertEqual(evaluate(stage("AAA"), {}, NOW)["counts"]["INSUFFICIENT_DATA"], 1)
        for key, value in (("volume", None), ("high", -1)):
            data = bundle()
            data["daily"]["candles"][0][key] = value
            self.assertEqual(evaluate(stage("AAA"), {"AAA": data}, NOW)["decisions"][0]["status"], "INSUFFICIENT_DATA")
        for source in (stage("AAA", "AAA"), {"stage": "OTHER"}):
            with self.assertRaises(ValueError):
                evaluate(source, {}, NOW)

    def test_no_mutation_or_input_order_dependence(self):
        source, inputs = stage("BBB", "AAA"), {"AAA": bundle(), "BBB": bundle("BBB")}
        before = copy.deepcopy((source, inputs))
        result = evaluate(source, inputs, NOW)
        self.assertEqual((source, inputs), before)
        source["decisions"].reverse()
        self.assertEqual(evaluate(source, inputs, NOW), result)

    def test_paths_confined_and_secret_paths_blocked(self):
        for raw, folder in (("../other.json", "outputs"), (".env", "outputs"),
                            ("outputs/credentials.json", "outputs"),
                            ("outputs/x.json", "data/output/alpha_vantage")):
            with self.assertRaises(ValueError):
                local_json(raw, folder)

    def test_existing_output_not_overwritten(self):
        with patch("pathlib.Path.open", side_effect=FileExistsError("exists")) as opened, \
             patch("pathlib.Path.read_text", return_value=json.dumps(stage("AAA"))):
            with self.assertRaises(FileExistsError):
                run("outputs/finviz_stage1.json", [], "outputs/new.json", NOW)
            opened.assert_called_once_with("x", encoding="utf-8")

    @local_data_test('outputs/finviz_stage1.json', 'outputs/NVDA_technical_indicators.json', 'data/output/alpha_vantage/NVDA_daily.json')
    def test_existing_local_inputs_fail_closed(self):
        source = json.loads((PROJECT_ROOT / "outputs/finviz_stage1.json").read_text(encoding="utf-8-sig"))
        report = json.loads((PROJECT_ROOT / "outputs/NVDA_technical_indicators.json").read_text(encoding="utf-8-sig"))
        daily = json.loads((PROJECT_ROOT / "data/output/alpha_vantage/NVDA_daily.json").read_text(encoding="utf-8-sig"))
        result = evaluate(source, {"NVDA": dict(report=report, daily=daily)}, NOW)
        self.assertEqual(result["counts"], dict(WATCHLIST=0, REJECTED=0, INSUFFICIENT_DATA=5))
        nvda = next(d for d in result["decisions"] if d["ticker"] == "NVDA")
        self.assertEqual(nvda["technical_assessment"]["status"], "REJECTED")
        self.assertIn("OVERSOLD_IS_NOT_BUY_CONFIRMATION", nvda["reasons"])


if __name__ == "__main__":
    unittest.main()
