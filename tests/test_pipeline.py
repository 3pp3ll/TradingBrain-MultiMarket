from test_support import local_data_test  # Offline safety guard before project imports
"""All generated candles here are synthetic fixtures, including mocked ingestion."""
import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from options_intelligence.data_integrity import REAL, SYNTHETIC, metadata, validate_prices
from options_intelligence.alpha_vantage_adapter import fetch_daily
from options_intelligence.pipeline import SYMBOLS, acquire, assemble, run, validate_eligibility, write_new
from options_intelligence.technical_indicators import PROJECT_ROOT

NOW = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)


def simulated_ingestion(symbol="NVDA", count=100):
    days, day = [], date(2026, 11, 9)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    bars = []
    for i, day in enumerate(reversed(days)):
        close = 100 + i * .1 + i % 2
        bars.append(dict(date=day, open=close, high=close+1, low=close-1, close=close, volume=1000))
    return dict(source="alpha_vantage_TIME_SERIES_DAILY", symbol=symbol,
                timeframe="1d", candles=bars, bar_count=count, latest_trading_day="2026-11-09",
                data_date="2026-11-09", snapshot_utc="2026-11-10T15:00:00Z",
                fetched_at_utc="2026-11-10T15:00:00Z", price_adjustment="raw_unadjusted",
                data_type=REAL, provenance={"kind": "ALPHA_VANTAGE_ADAPTER"})


def eligibility_fixture():
    stage = dict(stage="FINVIZ_STAGE_1", source="AUTHORIZED_FINVIZ_EXPORT",
                 data_type=REAL, provenance={"kind": "AUTHORIZED_FINVIZ_EXPORT"},
                 snapshot_utc="2026-11-10T15:00:00Z", fetched_at_utc="2026-11-10T16:00:00Z",
                 generated_at_utc="2026-11-10T17:00:00Z", data_date="2026-11-09",
                 decisions=[dict(ticker=s, status="WATCHLIST", reasons=[]) for s in SYMBOLS])
    evidence = [dict(ticker=s, market_cap="1T", average_volume="2M", optionable="yes",
                     snapshot_utc=stage["snapshot_utc"], data_date=stage["data_date"], data_type=REAL)
                for s in SYMBOLS]
    return stage, evidence


class MarketPipelineTests(unittest.TestCase):
    def test_explicit_synthetic_and_unknown_never_promoted_by_dates(self):
        payload = simulated_ingestion()
        payload["data_type"] = SYNTHETIC
        self.assertIn("SYNTHETIC_DATA_NOT_ELIGIBLE", validate_prices(payload, "NVDA", NOW)["validation_errors"])
        del payload["data_type"]
        checked = validate_prices(payload, "NVDA", NOW)
        self.assertIsNone(checked["data_type"])
        self.assertIn("UNKNOWN_DATA_TYPE", checked["validation_errors"])
        self.assertIsNone(assemble({"NVDA": payload}, None, NOW)["symbols"][0]["technical"])

    def test_real_declaration_requires_provenance_and_source(self):
        payload = simulated_ingestion()
        self.assertEqual(validate_prices(payload, "NVDA", NOW)["validation_errors"], [])
        del payload["provenance"]
        payload["source"] = "example_prices"
        errors = validate_prices(payload, "NVDA", NOW)["validation_errors"]
        self.assertIn("UNVERIFIED_PROVENANCE", errors)
        self.assertIn("UNTRUSTED_PRICE_SOURCE", errors)

    def test_metadata_required_fields_and_timezone(self):
        for key in ("snapshot_utc", "fetched_at_utc", "data_date"):
            payload = simulated_ingestion()
            del payload[key]
            self.assertIn("MISSING_OR_INVALID:" + key, validate_prices(payload, "NVDA", NOW)["validation_errors"])
        payload = simulated_ingestion()
        payload["snapshot_utc"] = "2026-11-10T15:00:00"
        self.assertIn("MISSING_OR_INVALID:snapshot_utc", validate_prices(payload, "NVDA", NOW)["validation_errors"])

    def test_staleness_and_inconsistent_update(self):
        payload = simulated_ingestion()
        checked = validate_prices(payload, "NVDA", NOW + timedelta(days=7))
        self.assertTrue(checked["is_stale"])
        self.assertIn("STALE_DATA", checked["validation_errors"])
        payload["fetched_at_utc"] = "2026-11-10T14:00:00Z"
        self.assertIn("SNAPSHOT_AFTER_FETCH", validate_prices(payload, "NVDA", NOW)["validation_errors"])

    def test_weekend_and_mismatched_market_date(self):
        payload = simulated_ingestion()
        payload["candles"][0]["date"] = "2026-06-06"
        self.assertIn("WEEKEND_CANDLE", validate_prices(payload, "NVDA", NOW)["validation_errors"])
        payload["data_date"] = "2026-11-08"
        checked = validate_prices(payload, "NVDA", NOW)
        self.assertIn("WEEKEND_MARKET_DATE", checked["validation_errors"])
        self.assertIn("PRICE_DATE_MISMATCH", checked["validation_errors"])

    def test_future_information_is_blocked(self):
        for key, value in (("snapshot_utc", "2026-11-11T15:00:00Z"),
                           ("fetched_at_utc", "2026-11-11T15:00:00Z"), ("data_date", "2026-11-10")):
            payload = simulated_ingestion()
            payload[key] = value
            result = assemble({"NVDA": payload}, None, NOW)
            self.assertIsNone(result["symbols"][0]["technical"])
            self.assertEqual(result["symbols"][0]["status"], "INSUFFICIENT_DATA")
        payload = simulated_ingestion()
        payload["candles"][-1]["date"] = "2026-11-11"
        payload["latest_trading_day"] = "2026-11-11"
        self.assertTrue(validate_prices(payload, "NVDA", NOW)["validation_errors"])

    def test_symbol_and_bad_ohlcv(self):
        payload = simulated_ingestion("AMD")
        self.assertIn("SYMBOL_MISMATCH", validate_prices(payload, "NVDA", NOW)["validation_errors"])
        payload["candles"][0]["close"] = None
        self.assertIn("INVALID_OHLCV_OR_METADATA", validate_prices(payload, "AMD", NOW)["validation_errors"])

    def test_history_and_no_fabricated_ema200(self):
        payload = simulated_ingestion(count=49)
        self.assertIn("INSUFFICIENT_PRICE_HISTORY:50_REQUIRED", validate_prices(payload, "NVDA", NOW)["validation_errors"])
        item = assemble({"NVDA": simulated_ingestion()}, None, NOW)["symbols"][0]
        self.assertNotIn("ema_200", item["technical"]["indicators"])
        self.assertIn("requires 200", item["technical"]["unavailable_indicators"]["ema_200"])
        result = assemble({s: simulated_ingestion(s, 200) for s in SYMBOLS}, None, NOW)
        self.assertEqual(result["counts"]["all_five_indicators"], 5)

    def test_raw_adjustment_not_misrepresented(self):
        payload = simulated_ingestion()
        checked = validate_prices(payload, "NVDA", NOW)
        self.assertFalse(checked["corporate_actions_verified"])
        payload["price_adjustment"] = "adjusted_close_only"
        self.assertIn("UNSUPPORTED_ADJUSTMENT_BASIS", validate_prices(payload, "NVDA", NOW)["validation_errors"])

    def test_prices_are_not_eligibility_or_option_contracts(self):
        stage, evidence = eligibility_fixture()
        prices = {s: simulated_ingestion(s) for s in SYMBOLS}
        result = assemble(prices, stage, NOW, evidence)
        self.assertEqual(result["counts"]["price_sets_validated"], 5)
        self.assertEqual(result["opportunity_engine"]["counts"]["WATCHLIST"], 5)
        self.assertEqual(result["counts"]["complete_options_opportunities"], 0)
        self.assertEqual(result["counts"]["INSUFFICIENT_DATA"], 5)
        for item in result["symbols"]:
            self.assertIn("OPTION_CONTRACTS", item["missing_data"])
            self.assertFalse(item["execution_enabled"])
        result = assemble(prices, None, NOW)
        self.assertIsNone(result["opportunity_engine"])

    def test_eligibility_requires_authorized_evidence_not_price_source(self):
        stage, evidence = eligibility_fixture()
        self.assertEqual(validate_eligibility(stage, "NVDA", NOW, evidence)["validation_errors"], [])
        self.assertIn("AUTHORIZED_ELIGIBILITY_EVIDENCE_MISSING", validate_eligibility(stage, "NVDA", NOW)["validation_errors"])
        evidence[0]["optionable"] = "no"
        self.assertIn("ELIGIBILITY_EVIDENCE_CONTRADICTION", validate_eligibility(stage, "NVDA", NOW, evidence)["validation_errors"])
        stage["source"] = "alpha_vantage_TIME_SERIES_DAILY"
        self.assertIn("UNTRUSTED_ELIGIBILITY_SOURCE", validate_eligibility(stage, "NVDA", NOW, evidence)["validation_errors"])

    def test_synthetic_eligibility_stays_blocked_with_valid_dates(self):
        stage, evidence = eligibility_fixture()
        stage["data_type"] = SYNTHETIC
        result = assemble({"NVDA": simulated_ingestion()}, stage, NOW, evidence)
        self.assertIsNone(result["opportunity_engine"])
        self.assertIn("SYNTHETIC_DATA_NOT_ELIGIBLE", result["symbols"][0]["validation_errors"])

    def test_fresh_cache_prevents_network_even_if_ema200_missing(self):
        fetcher = Mock()
        payload = simulated_ingestion()
        value, status = acquire("NVDA", payload, NOW, approved=True, fetcher=fetcher)
        self.assertEqual(status, "LOCAL_CACHE_USED")
        self.assertIs(value, payload)
        fetcher.assert_not_called()

    def test_network_authorization_and_journal_required(self):
        fetcher = Mock()
        self.assertEqual(acquire("AMD", None, NOW, fetcher=fetcher)[1], "NETWORK_NOT_AUTHORIZED_OR_CLIENT_UNAVAILABLE")
        self.assertEqual(acquire("AMD", None, NOW, approved=True, fetcher=fetcher)[1], "PERSISTENT_BUDGET_REQUIRED")
        fetcher.assert_not_called()

    def test_api_limit_stops_all_following_requests_without_secret_logging(self):
        events, persisted = [], []
        fetcher = Mock(side_effect=ValueError("Information: quota reached apikey=DO_NOT_LOG"))
        value, status = acquire("AMD", None, NOW, approved=True, fetcher=fetcher,
                                events=events, persist_event=persisted.append)
        self.assertIsNone(value)
        self.assertEqual(status, "PROVIDER_ERROR_OR_RATE_LIMIT_STOPPED")
        self.assertEqual([e["kind"] for e in persisted], ["ATTEMPT", "STOP"])
        self.assertNotIn("DO_NOT_LOG", json.dumps(persisted))
        status = acquire("TSLA", None, NOW + timedelta(minutes=2), approved=True, fetcher=fetcher,
                         events=events, persist_event=persisted.append)[1]
        self.assertEqual(status, "API_STOPPED_FOR_UTC_DAY")
        fetcher.assert_called_once_with("AMD")

    def test_daily_budget_cooldown_and_no_repeat(self):
        fetcher = Mock()
        params = dict(approved=True, fetcher=fetcher, persist_event=Mock())
        event = dict(kind="ATTEMPT", at=NOW.isoformat(), symbol="AMD")
        self.assertEqual(acquire("TSLA", None, NOW, events=[event], **params)[1], "REQUEST_COOLDOWN_ACTIVE")
        self.assertEqual(acquire("AMD", None, NOW + timedelta(minutes=2), events=[event], **params)[1], "SYMBOL_ALREADY_ATTEMPTED_TODAY")
        self.assertEqual(acquire("SPY", None, NOW, events=[event]*5, **params)[1], "CONSERVATIVE_DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(acquire("SPY", None, NOW, events=[{}], **params)[1], "INVALID_REQUEST_JOURNAL")
        fetcher.assert_not_called()

    def test_mocked_adapter_ingestion_and_journal_precedes_request(self):
        events = []
        def fetch(symbol):
            self.assertEqual(events[-1]["kind"], "ATTEMPT")
            value = simulated_ingestion(symbol)
            for key in ("data_type", "provenance", "data_date", "snapshot_utc"):
                del value[key]
            return value
        payload, status = acquire("AMD", None, NOW, approved=True, fetcher=fetch,
                                  events=events, persist_event=lambda e: None)
        self.assertEqual(status, "FETCHED")
        self.assertEqual(payload["data_type"], REAL)
        self.assertEqual(validate_prices(payload, "AMD", NOW)["validation_errors"], [])

    def test_provider_symbol_mismatch(self):
        fetcher = Mock(return_value=simulated_ingestion("TSLA"))
        self.assertEqual(acquire("AMD", None, NOW, approved=True, fetcher=fetcher,
                                events=[], persist_event=Mock())[1], "INVALID_PROVIDER_RESPONSE")

    def test_existing_alpha_vantage_adapter_with_mock_http_only(self):
        # Placeholder is test data, not a real API key. HTTP never leaves process.
        bars = simulated_ingestion("AMD")["candles"]
        response = {"Time Series (Daily)": {
            b["date"]: {"1. open": str(b["open"]), "2. high": str(b["high"]),
                        "3. low": str(b["low"]), "4. close": str(b["close"]),
                        "5. volume": str(b["volume"])} for b in bars}}
        with patch("options_intelligence.alpha_vantage_adapter.urlopen") as http, \
             patch("options_intelligence.alpha_vantage_adapter.datetime") as clock, \
             patch("options_intelligence.verified_ingestion.persist_ingestion"):
            clock.now.return_value = NOW
            from datetime import datetime as real_datetime
            clock.strptime.side_effect = real_datetime.strptime
            http.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            value, status = acquire("AMD", None, NOW, approved=True,
                                    fetcher=lambda symbol: fetch_daily(symbol, "SYNTHETIC_PLACEHOLDER"),
                                    events=[], persist_event=Mock())
            self.assertEqual(status, "FETCHED")
            self.assertEqual(value["bar_count"], 100)
            http.assert_called_once()

    def test_historical_network_run_forbidden(self):
        fetcher = Mock()
        with self.assertRaisesRegex(ValueError, "historical"):
            run(NOW - timedelta(days=1), approved=True, fetcher=fetcher, clock=lambda: NOW)
        fetcher.assert_not_called()

    def test_journal_persist_failure_prevents_request_and_midnight_cooldown(self):
        fetcher = Mock()
        with self.assertRaises(OSError):
            acquire("AMD", None, NOW, approved=True, fetcher=fetcher,
                    events=[], persist_event=Mock(side_effect=OSError("journal unavailable")))
        midnight = datetime(2026, 11, 11, tzinfo=timezone.utc)
        event = dict(kind="ATTEMPT", at=(midnight-timedelta(seconds=30)).isoformat(), symbol="NVDA")
        status = acquire("AMD", None, midnight, approved=True, fetcher=fetcher,
                         events=[event], persist_event=Mock())[1]
        self.assertEqual(status, "REQUEST_COOLDOWN_ACTIVE")
        fetcher.assert_not_called()

    def test_no_mutation_and_required_output_fields(self):
        payload = simulated_ingestion()
        before = copy.deepcopy(payload)
        result = assemble({"NVDA": payload}, None, NOW)
        self.assertEqual(payload, before)
        required = {"source", "snapshot_utc", "data_date", "fetched_at_utc", "data_type", "is_stale", "validation_errors"}
        for value in (result, *result["symbols"], result["symbols"][0]["technical"]):
            self.assertTrue(required.issubset(value))
        json.dumps(result, allow_nan=False)

    def test_existing_reports_preserved(self):
        with patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(FileExistsError):
                run(NOW)
        path = MagicMock()
        write_new(path, {"x": 1})
        path.open.assert_called_once_with("x", encoding="utf-8")

    @local_data_test('data/output/alpha_vantage/NVDA_daily.json')
    def test_legacy_inputs_remain_unverified(self):
        payload = json.loads((PROJECT_ROOT / "data/output/alpha_vantage/NVDA_daily.json").read_text(encoding="utf-8-sig"))
        result = assemble({"NVDA": payload}, None, NOW)
        self.assertEqual(result["counts"]["price_sets_validated"], 0)
        self.assertIsNone(result["symbols"][0]["data_type"])
        self.assertIn("UNKNOWN_DATA_TYPE", result["symbols"][0]["validation_errors"])

    @local_data_test('data/output/alpha_vantage/NVDA_daily.json', 'data/output/alpha_vantage/AMD_daily.json', 'data/output/alpha_vantage/TSLA_daily.json', 'data/output/alpha_vantage/SPY_daily.json', 'data/output/alpha_vantage/QQQ_daily.json')
    def test_v2_local_indicators_do_not_bypass_missing_provenance(self):
        prices = {}
        for symbol in SYMBOLS:
            path = PROJECT_ROOT / f"data/output/alpha_vantage/{symbol}_daily.json"
            prices[symbol] = json.loads(path.read_text(encoding="utf-8-sig"))
        before = copy.deepcopy(prices)
        stage, evidence = eligibility_fixture()
        result = assemble(prices, stage, NOW, evidence)
        self.assertEqual(result["counts"]["local_indicator_sets_calculated"], 5)
        self.assertEqual(result["counts"]["price_sets_validated"], 0)
        self.assertIsNone(result["opportunity_engine"])
        self.assertEqual(prices, before)
        for item in result["symbols"]:
            diagnostic = item["local_indicator_diagnostics"]
            self.assertEqual(set(diagnostic["indicators"]), {"ema_20", "ema_50", "rsi_14", "atr_14"})
            self.assertIn("requires 200", diagnostic["unavailable_indicators"]["ema_200"])
            self.assertFalse(diagnostic["market_data_verified"])
            self.assertFalse(diagnostic["trading_enabled"])
            self.assertEqual(diagnostic["status"], "UNVERIFIED_LOCAL_CALCULATION")
            self.assertIsNone(item["technical"])
            self.assertEqual(item["status"], "INSUFFICIENT_DATA")

    def test_v2_invalid_bars_or_symbol_have_no_diagnostic_values(self):
        for field, value in (("close", None), ("volume", -1)):
            payload = simulated_ingestion()
            payload["candles"][0][field] = value
            item = assemble({"NVDA": payload}, None, NOW)["symbols"][0]
            self.assertIsNone(item["local_indicator_diagnostics"])
            self.assertTrue(item["local_indicator_errors"])
        item = assemble({"NVDA": simulated_ingestion("AMD")}, None, NOW)["symbols"][0]
        self.assertIsNone(item["local_indicator_diagnostics"])


if __name__ == "__main__":
    unittest.main()
