from test_support import local_data_test  # Offline safety guard before project imports
import copy
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from options_intelligence.technical_indicators import (
    PROJECT_ROOT, _project_path, atr_wilder, build_report,
    calculate_indicators, ema, main, rsi_wilder, validate_candles,
)


def candles(closes):
    return [dict(date=(date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                 open=c, high=c + 1, low=c - 1, close=c, volume=100)
            for i, c in enumerate(closes)]


def payload(bars):
    return dict(symbol="TEST", source="alpha_vantage_TIME_SERIES_DAILY",
                timeframe="1d", candles=bars)


class TechnicalIndicatorsTests(unittest.TestCase):
    def test_ema_known_seed_and_recurrence(self):
        self.assertEqual(ema([2, 4, 6, 10, 8], 3), [None, None, 4, 7, 7.5])

    def test_requested_ema_periods(self):
        rows = calculate_indicators(candles(list(range(2, 203))))
        for period in (20, 50, 200):
            key = f"ema_{period}"
            self.assertIsNone(rows[period - 2][key])
            self.assertAlmostEqual(rows[period - 1][key], (period + 3) / 2)
            self.assertAlmostEqual(rows[period][key], (period + 3) / 2 + 1)

    def test_rsi_wilder_known_values(self):
        # Wilder's common 14-period worksheet: initial gains=3.34, losses=1.4.
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10,
                  45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
                  46.28, 46.00, 46.03]
        result = rsi_wilder(closes)
        self.assertEqual(result[:14], [None] * 14)
        self.assertAlmostEqual(result[14], 70.46413502109705)
        self.assertAlmostEqual(result[15], 66.24961855355505)
        self.assertAlmostEqual(result[16], 66.48094183471265)

    def test_rsi_one_direction_and_flat(self):
        self.assertEqual(rsi_wilder(list(range(2, 18)))[-1], 100)
        self.assertEqual(rsi_wilder(list(range(18, 2, -1)))[-1], 0)
        self.assertIsNone(rsi_wilder([5] * 16)[-1])
        report = build_report(payload(candles([5] * 16)), "test.json")
        self.assertIn("UNDEFINED_RSI", report["unavailable_indicators"]["rsi_14"])

    def test_atr_wilder_seed_and_gap_in_both_directions(self):
        bars = candles([10] * 14 + [20, 5])
        result = atr_wilder(bars)
        self.assertEqual(result[:13], [None] * 13)
        self.assertAlmostEqual(result[13], 2)
        self.assertAlmostEqual(result[14], 37 / 14)  # TR=11
        self.assertAlmostEqual(result[15], 705 / 196)  # TR=16

    def test_insufficient_history(self):
        for count in (0, 1, 13, 14, 15, 19, 20, 49, 50, 100, 199, 200):
            with self.subTest(count=count):
                report = build_report(payload(candles(list(range(2, count + 2)))), "test.json")
                for key, minimum in dict(ema_20=20, ema_50=50, ema_200=200,
                                         rsi_14=15, atr_14=14).items():
                    self.assertEqual(key in report["indicators"], count >= minimum)
                    if count < minimum:
                        self.assertIn(f"requires {minimum}", report["unavailable_indicators"][key])

    def test_sorting_and_no_input_mutation(self):
        bars = candles(list(range(2, 62)))
        unordered = bars[::2] + bars[1::2][::-1]
        before = copy.deepcopy(unordered)
        self.assertEqual(calculate_indicators(unordered), calculate_indicators(bars))
        self.assertEqual(unordered, before)

    def test_every_prefix_matches_full_history(self):
        bars = candles([100 + (i % 11) * 2 + i / 10 for i in range(205)])
        full = calculate_indicators(bars[::-1])
        for count in range(1, len(bars) + 1):
            self.assertEqual(calculate_indicators(bars[:count])[-1], full[count - 1])
        changed_future = bars[:200] + candles([10000] * 5)
        for i in range(200, 205):
            changed_future[i]["date"] = bars[i]["date"]
        self.assertEqual(calculate_indicators(changed_future)[:200], full[:200])

    def test_invalid_ohlcv(self):
        for field in ("open", "high", "low", "close", "volume"):
            for value in (None, True, "100", float("nan"), float("inf"), -1):
                with self.subTest(field=field, value=value):
                    bar = candles([10])[0]
                    bar[field] = value
                    with self.assertRaises(ValueError):
                        calculate_indicators([bar])
            bar = candles([10])[0]
            del bar[field]
            with self.assertRaises(ValueError):
                calculate_indicators([bar])
        for change in ({"high": 9}, {"low": 11}, {"close": 0}, {"volume": 1.5}):
            with self.assertRaises(ValueError):
                calculate_indicators([{**candles([10])[0], **change}])

    def test_invalid_dates_and_structure(self):
        for day in (None, "2025-02-30", "20250101", "2025-1-1", 123):
            with self.assertRaises(ValueError):
                validate_candles([{**candles([10])[0], "date": day}])
        for bars in (None, {}, [None], candles([10]) * 2):
            with self.assertRaises(ValueError):
                validate_candles(bars)

    def test_invalid_periods_and_close_values(self):
        for fn in (ema, rsi_wilder, atr_wilder):
            for period in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    fn([], period)
        for fn in (ema, rsi_wilder):
            with self.assertRaises(ValueError):
                fn([float("nan")], 14)

    def test_invalid_metadata(self):
        valid = payload(candles([10]))
        for change in ({"symbol": ""}, {"source": "unknown"}, {"timeframe": "1h"},
                       {"bar_count": 2}, {"latest_trading_day": "2025-02-01"}):
            with self.assertRaises(ValueError):
                build_report({**valid, **change}, "test.json")
        with self.assertRaises(ValueError):
            build_report([], "test.json")

    def test_project_path_limits(self):
        for path in ("../outside.json", ".env", "keys.pem"):
            with self.assertRaises(ValueError):
                _project_path(path)

    def test_cli_uses_exclusive_creation(self):
        # An existing report must be preserved; no filesystem mutation needed.
        with patch("sys.argv", ["indicators"]), \
             patch("pathlib.Path.read_text", return_value=json.dumps(payload(candles([10])))), \
             patch("pathlib.Path.open", side_effect=FileExistsError("exists")) as opened, \
             patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)
            opened.assert_called_once_with("x", encoding="utf-8")

    @local_data_test('data/output/alpha_vantage/NVDA_daily.json')
    def test_existing_nvda_data_report(self):
        source = PROJECT_ROOT / "data/output/alpha_vantage/NVDA_daily.json"
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        report = build_report(data, source.relative_to(PROJECT_ROOT).as_posix())
        self.assertEqual(report["symbol"], "NVDA")
        self.assertEqual(report["date"], max(bar["date"] for bar in data["candles"]))
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["trading_enabled"])
        self.assertEqual(report["source"], data["source"])
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
