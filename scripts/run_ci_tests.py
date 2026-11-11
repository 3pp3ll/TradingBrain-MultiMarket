"""Run only offline synthetic tests with no access to local market artifacts."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ["TRADINGBRAIN_CI"] = "1"
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
import test_support  # noqa: E402; installs the guard before test discovery

suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
# New accidental skips must not silently reduce security coverage.
expected_local_skips = {
    "test_existing_local_inputs_fail_closed",
    "test_existing_nvda_data_report",
    "test_legacy_inputs_remain_unverified",
    "test_v2_local_indicators_do_not_bypass_missing_provenance",
    "test_offline_pipeline_integration_preserves_v2_indicator_values",
    "test_original_files_and_previous_reports_unchanged",
    "test_sha256_known_vector_and_exact_file_bytes",
}
unexpected = [test.id() for test, reason in result.skipped
              if test.id().rsplit(".", 1)[-1] not in expected_local_skips
              or not reason.startswith("LOCAL_DATA_ONLY:")]
missing = expected_local_skips - {test.id().rsplit(".", 1)[-1] for test, _ in result.skipped}
if unexpected:
    print("Unexpected skipped tests:", ", ".join(unexpected))
if missing:
    print("Missing expected local-only checks:", ", ".join(sorted(missing)))
sys.exit(0 if result.wasSuccessful() and not unexpected and not missing and result.testsRun else 1)
