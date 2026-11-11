from test_support import local_data_test  # Offline safety guard before project imports
"""Offline fixtures and read-only audits; no credentials, network or temp files."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from options_intelligence.pipeline import assemble
from options_intelligence.provenance_manager import (
    FIELDS, SYMBOLS, build_audit_report, inspect_payload, load_audited_file,
    main, sha256_bytes,
)
from options_intelligence.technical_indicators import PROJECT_ROOT

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def fixture():
    # All prices and receipts in this fixture are simulated test evidence.
    return {"symbol": "NVDA", "source": "alpha_vantage_TIME_SERIES_DAILY",
            "data_type": "REAL_MARKET_DATA", "timeframe": "1d",
            "provenance": {"kind": "ALPHA_VANTAGE_ADAPTER", "method": "DIRECT_APPROVED_FETCH"},
            "snapshot_utc": "2026-11-10T16:00:00Z",
            "fetched_at_utc": "2026-11-10T17:00:00Z", "data_date": "2026-11-09",
            "latest_trading_day": "2026-11-09", "bar_count": 2,
            "candles": [{"date": day, "open": 100, "high": 102, "low": 99,
                         "close": 101, "volume": 1000}
                        for day in ("2026-11-06", "2026-11-09")]}


def audit(payload, *, receipt=False):
    digest = sha256_bytes(json.dumps(payload).encode())
    evidence = {"file_sha256": digest, "symbol": "NVDA",
                **{key: payload.get(key) for key in FIELDS}} if receipt else None
    return inspect_payload(payload, "NVDA", NOW, file_sha256=digest,
                           trusted_receipt=evidence)


class ProvenanceManagerTests(unittest.TestCase):
    @local_data_test('data/output/alpha_vantage/NVDA_daily.json')
    def test_sha256_known_vector_and_exact_file_bytes(self):
        self.assertEqual(sha256_bytes(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        path = PROJECT_ROOT / "data/output/alpha_vantage/NVDA_daily.json"
        _, record = load_audited_file(path, "NVDA", NOW)
        self.assertEqual(record["file_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_unknown_source_rejected_even_with_receipt(self):
        payload = fixture()
        payload["source"] = "unknown_provider"
        record = audit(payload, receipt=True)
        self.assertEqual(record["verification_status"], "UNVERIFIED")
        self.assertIn("UNKNOWN_OR_UNTRUSTED_SOURCE", record["validation_errors"])

    def test_symbol_mismatch(self):
        payload = fixture()
        payload["symbol"] = "AMD"
        self.assertIn("SYMBOL_MISMATCH", audit(payload, receipt=True)["validation_errors"])

    def test_future_candles_timestamps_and_data_date(self):
        for key in ("fetched_at_utc", "snapshot_utc", "data_date", "candle"):
            with self.subTest(key=key):
                payload = fixture()
                if key == "candle":
                    payload["candles"][-1]["date"] = "2026-11-12"
                    payload["latest_trading_day"] = "2026-11-12"
                else:
                    payload[key] = "2026-11-12" if key == "data_date" else "2026-11-12T17:00:00Z"
                record = audit(payload, receipt=True)
                self.assertEqual(record["verification_status"], "UNVERIFIED")
                self.assertTrue(any("FUTURE" in e for e in record["validation_errors"]))

    def test_synthetic_and_real_declarations_need_distinct_evidence(self):
        payload = fixture()
        self.assertEqual(audit(payload)["verification_status"], "UNVERIFIED")
        self.assertEqual(audit(payload, receipt=True)["verification_status"], "VERIFIED")
        payload["data_type"] = "SYNTHETIC_TEST_DATA"
        record = audit(payload, receipt=True)
        self.assertEqual(record["classification"], "SYNTHETIC")
        self.assertEqual(record["data_type"], "SYNTHETIC_TEST_DATA")
        self.assertEqual(record["verification_status"], "UNVERIFIED")
        del payload["data_type"]
        self.assertIsNone(audit(payload)["data_type"])

    def test_missing_metadata_not_invented(self):
        for key in FIELDS:
            with self.subTest(key=key):
                payload = fixture()
                del payload[key]
                record = audit(payload, receipt=True)
                self.assertEqual(record["verification_status"], "UNVERIFIED")
                self.assertIn(key, record["missing_original_metadata"])
                if key in ("snapshot_utc", "fetched_at_utc"):
                    self.assertIsNone(record[key])
        payload = fixture()
        del payload["data_date"]
        record = audit(payload)
        self.assertEqual(record["data_date"], "2026-11-09")
        self.assertEqual(record["field_evidence"]["data_date"]["basis"], "DERIVED_FROM_LOCAL_CANDLES")

    def test_missing_or_invalid_prices_rejected(self):
        for change in ({"candles": []}, {"candles": None}, {"candles": [{}]}):
            record = audit({**fixture(), **change}, receipt=True)
            self.assertEqual(record["verification_status"], "UNVERIFIED")
            self.assertIn("INVALID_OR_MISSING_OHLCV", record["validation_errors"])

    def test_unsorted_or_duplicate_candles_rejected_without_reordering(self):
        payload = fixture()
        payload["candles"].reverse()
        before = copy.deepcopy(payload)
        record = audit(payload, receipt=True)
        self.assertIn("CANDLES_NOT_CHRONOLOGICAL", record["validation_errors"])
        self.assertEqual(payload, before)
        payload["candles"][0] = copy.deepcopy(payload["candles"][1])
        self.assertIn("INVALID_OR_MISSING_OHLCV", audit(payload)["validation_errors"])

    def test_receipt_hash_mismatch_rejected(self):
        payload = fixture()
        receipt = {"symbol": "NVDA", "file_sha256": "wrong",
                   **{key: payload[key] for key in FIELDS}}
        record = inspect_payload(payload, "NVDA", NOW, file_sha256="actual", trusted_receipt=receipt)
        self.assertIn("INGESTION_EVIDENCE_MISMATCH", record["validation_errors"])
        self.assertEqual(record["verification_status"], "UNVERIFIED")

    @local_data_test(*[f'data/output/alpha_vantage/{s}_daily.json' for s in SYMBOLS],
                     'outputs/market_pipeline_report.json', 'outputs/market_pipeline_report_v2.json')
    def test_original_files_and_previous_reports_unchanged(self):
        paths = [PROJECT_ROOT / f"data/output/alpha_vantage/{s}_daily.json" for s in SYMBOLS]
        paths += [PROJECT_ROOT / f"outputs/{name}.json" for name in
                  ("market_pipeline_report", "market_pipeline_report_v2")]
        before = {p: p.read_bytes() for p in paths}
        report = build_audit_report(NOW)
        self.assertEqual(report["counts"], {"VERIFIED": 0, "UNVERIFIED": 5})
        self.assertEqual(report["previous_report"]["conflicts"], [])
        self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_synthetic_exact_bytes_hashed_without_reserialization(self):
        raw = b'\xef\xbb\xbf' + json.dumps(fixture(), indent=3).encode() + b'\r\n'
        with patch('pathlib.Path.read_bytes', return_value=raw) as reader:
            parsed, record = load_audited_file('data/output/alpha_vantage/NVDA_daily.json', 'NVDA', NOW)
        reader.assert_called_once_with()
        self.assertEqual(parsed, fixture())
        self.assertEqual(record['file_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(record['file_sha256'], hashlib.sha256(json.dumps(parsed).encode()).hexdigest())
        self.assertEqual(sha256_bytes(b'abc'),
                         'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')

    def test_synthetic_audit_reads_only_and_preserves_previous_report(self):
        originals = {}
        observations = []
        for symbol in SYMBOLS:
            data = fixture()
            data['symbol'] = symbol
            originals[f'{symbol}_daily.json'] = json.dumps(data).encode()
            observations.append(dict(symbol=symbol, source=data['source'],
                                     fetched_at_utc=data['fetched_at_utc'], observed_bar_count=2,
                                     observed_latest_trading_day=data['latest_trading_day']))
        originals['market_pipeline_report_v2.json'] = json.dumps({'symbols': observations}).encode()
        before = copy.deepcopy(originals)
        with patch('pathlib.Path.read_bytes', autospec=True,
                   side_effect=lambda path: originals[path.name]) as reader, \
             patch('pathlib.Path.write_bytes') as write_bytes, \
             patch('pathlib.Path.write_text') as write_text, \
             patch('pathlib.Path.open', side_effect=AssertionError('unexpected file open')):
            report = build_audit_report(NOW)
        self.assertEqual(reader.call_count, 6)
        write_bytes.assert_not_called()
        write_text.assert_not_called()
        self.assertEqual(report['counts'], {'VERIFIED': 0, 'UNVERIFIED': 5})
        self.assertEqual(report['previous_report']['conflicts'], [])
        self.assertEqual(report['previous_report']['file_sha256'],
                         hashlib.sha256(originals['market_pipeline_report_v2.json']).hexdigest())
        self.assertEqual(originals, before)

    def test_pipeline_blocks_complete_metadata_without_independent_evidence(self):
        # Validated candles alone must not bypass the new file provenance gate.
        from test_pipeline import simulated_ingestion, eligibility_fixture, NOW as pipeline_now
        prices = {s: simulated_ingestion(s) for s in SYMBOLS}
        stage, evidence = eligibility_fixture()
        records = {s: inspect_payload(p, s, pipeline_now, file_sha256="test_hash")
                   for s, p in prices.items()}
        report = assemble(prices, stage, pipeline_now, evidence, provenance_records=records)
        self.assertIsNone(report["opportunity_engine"])
        self.assertEqual(report["counts"]["price_sets_validated"], 0)
        for item in report["symbols"]:
            self.assertIn("PRICE_PROVENANCE_UNVERIFIED", item["validation_errors"])
            self.assertFalse(item["execution_enabled"])

    def test_malformed_json_is_unverified_with_byte_hash(self):
        with patch("pathlib.Path.read_bytes", return_value=b"{broken"):
            payload, record = load_audited_file("data/output/alpha_vantage/NVDA_daily.json", "NVDA", NOW)
        self.assertIsNone(payload)
        self.assertEqual(record["file_sha256"], sha256_bytes(b"{broken"))
        self.assertEqual(record["verification_status"], "UNVERIFIED")

    @local_data_test('outputs/market_pipeline_report_v2.json', 'data/output/alpha_vantage/NVDA_daily.json', 'data/output/alpha_vantage/AMD_daily.json', 'data/output/alpha_vantage/TSLA_daily.json', 'data/output/alpha_vantage/SPY_daily.json', 'data/output/alpha_vantage/QQQ_daily.json')
    def test_offline_pipeline_integration_preserves_v2_indicator_values(self):
        from options_intelligence.pipeline import run
        previous = json.loads((PROJECT_ROOT / "outputs/market_pipeline_report_v2.json").read_text(encoding="utf-8"))
        old = {item["symbol"]: item for item in previous["symbols"]}
        with patch("options_intelligence.pipeline.write_new") as writer, \
             patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            report = run(NOW, output="outputs/_provenance_validation_only.json")
        writer.assert_called_once()
        self.assertIsNone(report["opportunity_engine"])
        for item in report["symbols"]:
            self.assertEqual(item["verification_status"], "UNVERIFIED")
            self.assertEqual(item["data_provenance"]["file_sha256"], item["file_sha256"])
            self.assertEqual(item["local_indicator_diagnostics"]["indicators"],
                             old[item["symbol"]]["local_indicator_diagnostics"]["indicators"])

    def test_forbidden_paths_rejected_before_read(self):
        with patch("pathlib.Path.read_bytes") as reader:
            for path in (".env", "../outside.json", "outputs/credentials.json"):
                with self.assertRaises(ValueError):
                    load_audited_file(path, "NVDA", NOW)
            reader.assert_not_called()

    def test_report_never_overwrites_existing_output(self):
        with patch("sys.argv", ["provenance"]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.open") as opened, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main()
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
