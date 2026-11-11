from test_support import local_data_test  # Offline safety guard before project imports
import unittest
from datetime import datetime,timezone
from options_intelligence.scanner import analyze,number
NOW=datetime(2026,11,10,16,0,tzinfo=timezone.utc)
BASE={'ticker':'NVDA','market_cap':'3T','average_volume':'50M','relative_volume':'2.0','rsi':'61','sma20':'120','sma50':'110','sma200':'100','optionable':'Yes','snapshot_utc':'2026-11-10T15:30:00Z','earnings_date':'2026-12-10'}
class ScannerTests(unittest.TestCase):
 def test_units(self): self.assertEqual(number('1.5M'),1500000)
 def test_candidate(self):
  o=analyze(BASE,NOW,36,'test'); self.assertEqual(o.decision,'WATCHLIST_CANDIDATE'); self.assertEqual(o.direction,'CALL_CANDIDATE'); self.assertFalse(o.execution_enabled)
 def test_stale_rejected(self):
  o=analyze({**BASE,'snapshot_utc':'2026-11-01T00:00:00Z'},NOW,36,'test'); self.assertIn('STALE_SNAPSHOT',o.reasons)
 def test_missing_earnings_rejected(self):
  o=analyze({**BASE,'earnings_date':''},NOW,36,'test'); self.assertIn('EARNINGS_UNVERIFIED',o.reasons)
 def test_no_optionable_rejected(self):
  o=analyze({**BASE,'optionable':'No'},NOW,36,'test'); self.assertIn('NOT_CONFIRMED_OPTIONABLE',o.reasons)
if __name__=='__main__': unittest.main()
