from test_support import local_data_test  # Offline safety guard before project imports
"""All provider responses, credentials and clocks here are synthetic fixtures."""
import copy
import hashlib
import io
import json
import unittest
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from options_intelligence.alpha_vantage_adapter import fetch_daily
from options_intelligence.verified_ingestion import IngestionAttempt, persist_ingestion

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
MARKER = "REDACTION_TEST_MARKER"


def payload():
    return {"source": "alpha_vantage_TIME_SERIES_DAILY", "symbol": "NVDA",
            "timeframe": "1d", "price_adjustment": "raw_unadjusted",
            "fetched_at_utc": "2026-11-11T12:00:00Z",
            "latest_trading_day": "2026-11-10", "bar_count": 2,
            "candles": [{"date": day, "open": 100, "high": 102, "low": 99,
                         "close": 101, "volume": 1000}
                        for day in ("2026-11-09", "2026-11-10")]}


def finish(data):
    attempt = IngestionAttempt("NVDA", clock=lambda: NOW)
    attempt.response_received()
    with patch("options_intelligence.verified_ingestion.persist_ingestion") as writer:
        record = attempt.finish(data)
    return record, writer.call_args.args[1]


class VerifiedIngestionTests(unittest.TestCase):
    def test_event_times_come_from_live_events(self):
        times = iter([NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)])
        attempt = IngestionAttempt("NVDA", clock=lambda: next(times))
        attempt.response_received()
        with patch("options_intelligence.verified_ingestion.persist_ingestion"):
            record = attempt.finish(payload())
        self.assertEqual(record["request_started_at_utc"], NOW.isoformat())
        self.assertEqual(record["response_received_at_utc"], (NOW + timedelta(seconds=2)).isoformat())
        self.assertEqual(record["verification_status"], "VALIDATED_LOCAL_INGESTION")
        self.assertEqual(record["validation_errors"], [])
        self.assertEqual(record["provider"], "ALPHA_VANTAGE")
        self.assertEqual(record["data_type"], "REAL_MARKET_DATA")

    def test_snapshot_hash_and_receipt_independence(self):
        original = payload()
        before = copy.deepcopy(original)
        record, raw = finish(original)
        self.assertEqual(original, before)
        self.assertEqual(record["file_sha256"], hashlib.sha256(raw).hexdigest())
        snapshot = json.loads(raw)
        self.assertNotIn("file_sha256", snapshot)
        self.assertNotIn("ingestion_id", snapshot)
        self.assertNotIn("candles", record)
        snapshot["candles"][0]["close"] = 999
        self.assertNotEqual(record["file_sha256"], hashlib.sha256(json.dumps(snapshot).encode()).hexdigest())
        self.assertFalse(record["externally_authenticated"])

    def test_arbitrary_fields_and_provider_text_never_serialized(self):
        data = payload()
        data.update(api_key=MARKER, request_url="https://invalid.test/?apikey=" + MARKER,
                    notes=[MARKER], provenance={"secret": MARKER})
        data["candles"][0]["debug"] = MARKER
        record, raw = finish(data)
        self.assertNotIn(MARKER, json.dumps(record))
        self.assertNotIn(MARKER.encode(), raw)
        data["symbol"] = MARKER
        record, raw = finish(data)
        self.assertNotIn(MARKER, json.dumps(record))
        self.assertIsNone(raw)

    def test_unknown_source_symbol_and_synthetic_block_false_verification(self):
        for key, value, code in (
            ("source", "unknown", "UNTRUSTED_SOURCE"),
            ("symbol", "AMD", "SYMBOL_MISMATCH"),
            ("data_type", "SYNTHETIC_TEST_DATA", "SYNTHETIC_DATA_NOT_ELIGIBLE"),
            ("data_type", "invented", "UNKNOWN_DATA_TYPE"),
        ):
            with self.subTest(key=key, value=value):
                data = payload()
                data[key] = value
                record, raw = finish(data)
                self.assertEqual(record["verification_status"], "REJECTED")
                self.assertIn(code, record["validation_errors"])
                self.assertIsNone(record["file_sha256"])
                self.assertIsNone(raw)

    def test_missing_or_invalid_data_rejected(self):
        for data in (None, {}, {**payload(), "candles": []}, {**payload(), "bar_count": True}):
            record, raw = finish(data)
            self.assertEqual(record["verification_status"], "REJECTED")
            self.assertIsNone(raw)
        data = payload()
        data["candles"][0]["volume"] = -1
        self.assertIn("INVALID_OR_MISSING_OHLCV", finish(data)[0]["validation_errors"])

    def test_dates_order_duplicates_and_future(self):
        data = payload()
        data["candles"].reverse()
        self.assertIn("CANDLES_NOT_CHRONOLOGICAL", finish(data)[0]["validation_errors"])
        data["candles"][0] = copy.deepcopy(data["candles"][1])
        self.assertIn("INVALID_OR_MISSING_OHLCV", finish(data)[0]["validation_errors"])
        data = payload()
        data["candles"][-1]["date"] = "2026-11-12"
        self.assertIn("FUTURE_OR_INCOMPLETE_CANDLE_DATE", finish(data)[0]["validation_errors"])
        data = payload()
        data["fetched_at_utc"] = "2026-11-12T12:00:00Z"
        self.assertIn("FUTURE_FETCH_TIMESTAMP", finish(data)[0]["validation_errors"])

    def test_missing_response_and_reversed_clocks_never_validate(self):
        with patch("options_intelligence.verified_ingestion.persist_ingestion"):
            attempt = IngestionAttempt("NVDA", clock=lambda: NOW)
            self.assertIn("RESPONSE_EVENT_MISSING", attempt.finish(payload())["validation_errors"])
            times = iter([NOW, NOW - timedelta(seconds=1), NOW])
            attempt = IngestionAttempt("NVDA", clock=lambda: next(times))
            attempt.response_received()
            self.assertIn("INVALID_EVENT_TIME_ORDER", attempt.finish(payload())["validation_errors"])

    def test_failure_receipt_has_no_fabricated_response_or_prices(self):
        attempt = IngestionAttempt("NVDA", clock=lambda: NOW)
        with patch("options_intelligence.verified_ingestion.persist_ingestion") as writer:
            record = attempt.finish(failed=True)
        self.assertEqual(record["verification_status"], "FAILED")
        self.assertIsNone(record["response_received_at_utc"])
        self.assertIsNone(record["file_sha256"])
        self.assertIsNone(record["data_type"])
        self.assertIsNone(writer.call_args.args[1])

    def test_identifiers_unique_and_attempt_not_reusable(self):
        first, _ = finish(payload())
        second, _ = finish(payload())
        self.assertNotEqual(first["ingestion_id"], second["ingestion_id"])
        attempt = IngestionAttempt("NVDA", clock=lambda: NOW)
        with patch("options_intelligence.verified_ingestion.persist_ingestion"):
            attempt.finish(failed=True)
        with self.assertRaises(RuntimeError):
            attempt.finish(payload())

    def test_persistence_failure_is_sanitized_and_fails_closed(self):
        attempt = IngestionAttempt("NVDA", clock=lambda: NOW)
        attempt.response_received()
        with patch("options_intelligence.verified_ingestion.persist_ingestion", side_effect=OSError(MARKER)):
            with self.assertRaisesRegex(RuntimeError, "^INGESTION_RECORD_PERSISTENCE_FAILED$"):
                attempt.finish(payload())

    def test_exclusive_storage_uses_separate_snapshot_and_receipt(self):
        record, raw = finish(payload())
        modes = []
        class Buffer(io.BytesIO):
            def close(self):
                pass
        class TextBuffer(io.StringIO):
            def close(self):
                pass
        snapshot, receipt = Buffer(), TextBuffer()
        def opened(path, mode, **kwargs):
            modes.append((path.name, mode))
            return snapshot if mode == "xb" else receipt
        with patch("pathlib.Path.mkdir"), patch("pathlib.Path.open", new=opened):
            persist_ingestion(record, raw)
        self.assertEqual([mode for _, mode in modes], ["xb", "x"])
        self.assertNotEqual(modes[0][0], modes[1][0])
        self.assertEqual(snapshot.getvalue(), raw)
        self.assertEqual(json.loads(receipt.getvalue()), record)
        with patch("pathlib.Path.mkdir"), patch("pathlib.Path.open", side_effect=FileExistsError):
            with self.assertRaises(FileExistsError):
                persist_ingestion(record, raw)

    def test_storage_path_stays_in_project_outputs(self):
        record, raw = finish(payload())
        with self.assertRaises(ValueError):
            persist_ingestion(record, raw, folder=Path("C:/outside_project"))

    def test_adapter_automatically_records_success_without_changing_return(self):
        response = {"Time Series (Daily)": {"2026-11-10": {
            "1. open": "100", "2. high": "102", "3. low": "99", "4. close": "101", "5. volume": "1000"}}}
        with patch("options_intelligence.alpha_vantage_adapter.urlopen") as http, \
             patch("options_intelligence.alpha_vantage_adapter.datetime") as clock, \
             patch("options_intelligence.verified_ingestion.persist_ingestion") as writer:
            clock.now.return_value = NOW
            clock.strptime.side_effect = datetime.strptime
            http.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            result = fetch_daily("NVDA", MARKER, timeout=17)
        self.assertEqual(result["bar_count"], 1)
        self.assertNotIn("ingestion_id", result)
        record, raw = writer.call_args.args
        self.assertEqual(record["verification_status"], "VALIDATED_LOCAL_INGESTION")
        self.assertNotIn(MARKER, json.dumps(record))
        self.assertNotIn(MARKER.encode(), raw)
        self.assertEqual(http.call_args.kwargs["timeout"], 17)

    def test_adapter_network_and_provider_failures_record_only_fixed_codes(self):
        for failure in (True, False):
            with self.subTest(network_failure=failure), \
                 patch("options_intelligence.alpha_vantage_adapter.urlopen") as http, \
                 patch("options_intelligence.verified_ingestion.persist_ingestion") as writer:
                if failure:
                    http.side_effect = URLError(MARKER)
                else:
                    http.return_value.__enter__.return_value.read.return_value = json.dumps({"Information": MARKER}).encode()
                with self.assertRaises((RuntimeError, ValueError)):
                    fetch_daily("NVDA", MARKER)
                record, raw = writer.call_args.args
                self.assertEqual(record["verification_status"], "FAILED")
                self.assertNotIn(MARKER, json.dumps(record))
                self.assertIsNone(raw)
                self.assertEqual(record["response_received_at_utc"] is None, failure)

    def test_transport_traceback_does_not_expose_provider_text(self):
        with patch('options_intelligence.alpha_vantage_adapter.urlopen', side_effect=URLError(MARKER)), \
             patch('options_intelligence.verified_ingestion.persist_ingestion'):
            try:
                fetch_daily('NVDA', MARKER)
            except RuntimeError as error:
                rendered = ''.join(traceback.format_exception(error))
            else:
                self.fail('Transport failure must fail closed')
        self.assertNotIn(MARKER, rendered)


if __name__ == "__main__":
    unittest.main()
