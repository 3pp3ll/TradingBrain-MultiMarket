"""Exercise the safety boundary without reading secrets or sending traffic."""
from test_support import CI_SAFE, ROOT, audit_guard
import importlib.util
import socket
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publication_scan", ROOT / "scripts/check_publishable.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class RepositorySafetyTests(unittest.TestCase):
    def test_actual_socket_connection_blocked_before_transport(self):
        with socket.socket() as connection:
            with self.assertRaisesRegex(PermissionError, "TEST_EXTERNAL_IO_BLOCKED"):
                connection.connect(("127.0.0.1", 9))

    def test_dns_blocked_before_resolution(self):
        with self.assertRaisesRegex(PermissionError, "TEST_EXTERNAL_IO_BLOCKED"):
            socket.getaddrinfo("offline.invalid", 443)

    def test_secret_read_blocked_without_opening_file(self):
        with self.assertRaisesRegex(PermissionError, "TEST_SECRET_FILE_ACCESS_BLOCKED"):
            audit_guard("open", (str(ROOT / ".env"), "r", 0))

    def test_historical_write_blocked(self):
        with self.assertRaisesRegex(PermissionError, "TEST_HISTORICAL_ARTIFACT_WRITE_BLOCKED|CI_LOCAL_ARTIFACT_ACCESS_BLOCKED"):
            audit_guard("open", (str(ROOT / "outputs/previous.json"), "w", 0))

    def test_ci_blocks_local_market_reads(self):
        with patch("test_support.CI_SAFE", True):
            with self.assertRaisesRegex(PermissionError, "CI_LOCAL_ARTIFACT_ACCESS_BLOCKED"):
                audit_guard("open", (str(ROOT / "data/private.csv"), "r", 0))

    def test_scanner_redacts_generated_fake_credentials(self):
        fake = "gh" + "p_" + "x" * 30
        issues = scan.scan_text('api_key = "' + fake + '"', ".py")
        self.assertIn("GITHUB_TOKEN", issues)
        self.assertIn("LITERAL_CREDENTIAL_ASSIGNMENT", issues)
        self.assertNotIn(fake, str(issues))

    def test_secret_and_artifact_exclusions(self):
        patterns = [p.strip() for p in (ROOT / ".gitignore").read_text().splitlines()
                    if p.strip() and not p.startswith("#")]
        from pathlib import Path
        for name in (".env", ".env.example", "nested/.env.production", "nested/credentials.json",
                     "private.pem", "nested/secrets/config.json", "venv/lib/a.py", ".venv/a.py",
                     "nested/__pycache__/a.pyc", "data/raw.csv", "logs/run.log", "outputs/report.json",
                     "reports/results.json", "Microsoft/Windows/PowerShell/ModuleAnalysisCache"):
            with self.subTest(path=name):
                self.assertTrue(scan.ignored(Path(name), patterns), name)
