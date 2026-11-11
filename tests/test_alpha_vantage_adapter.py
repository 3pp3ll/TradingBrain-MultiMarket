from test_support import local_data_test  # Offline safety guard before project imports
import unittest
import traceback
from options_intelligence.alpha_vantage_adapter import normalize_response

class AlphaVantageAdapterTests(unittest.TestCase):
    def test_one_day(self):
        result=normalize_response({"Time Series (Daily)":{"2026-11-09":{"1. open":"100","2. high":"110","3. low":"95","4. close":"105","5. volume":"12500"}}}, "NVDA")
        self.assertEqual(result["bar_count"], 1)
        self.assertFalse(result["trading_enabled"])
        self.assertEqual(result["latest_trading_day"], "2026-11-09")
    def test_rate_limit(self):
        with self.assertRaisesRegex(ValueError, "Information"):
            normalize_response({"Information":"API limit reached"}, "NVDA")
    def test_empty(self):
        with self.assertRaises(ValueError):
            normalize_response({}, "NVDA")

    def test_provider_error_text_is_redacted(self):
        for field in ('Error Message', 'Note', 'Information'):
            with self.subTest(field=field), self.assertRaises(ValueError) as error:
                normalize_response({field: 'REDACTION_TEST_MARKER'}, 'NVDA')
            self.assertNotIn('REDACTION_TEST_MARKER', str(error.exception))
            self.assertIn(field, str(error.exception))

    def test_malformed_provider_fields_are_redacted(self):
        marker = 'REDACTION_TEST_MARKER'
        bar = {'1. open': '100', '2. high': '102', '3. low': '99',
               '4. close': marker, '5. volume': '1000'}
        for day in ('2026-11-09', marker):
            with self.subTest(day_is_valid=day != marker):
                try:
                    normalize_response({'Time Series (Daily)': {day: bar}}, 'NVDA')
                except ValueError as error:
                    self.assertNotIn(marker, ''.join(traceback.format_exception(error)))
                else:
                    self.fail('Invalid provider data must be rejected')

if __name__ == "__main__": unittest.main()
