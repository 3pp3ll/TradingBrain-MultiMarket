"""Shared offline safety boundary; no production behavior is changed."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CI_SAFE = os.environ.get("TRADINGBRAIN_CI") == "1"


def secret_path(path):
    parts = Path(path).parts
    return any(
        part.lower().startswith((".env", "credentials", "service-account"))
        or part.lower() in {"secrets", ".aws", ".ssh", ".supabase"}
        or "secret" in part.lower() or "token" in part.lower()
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"))
        for part in parts
    )


def audit_guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo",
                 "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto",
                 "subprocess.Popen", "os.system", "os.spawn", "os.exec"}:
        raise PermissionError("TEST_EXTERNAL_IO_BLOCKED")
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    if secret_path(path):
        raise PermissionError("TEST_SECRET_FILE_ACCESS_BLOCKED")
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        return
    if CI_SAFE and relative.parts and relative.parts[0] in {"data", "outputs", "logs", "reports"}:
        raise PermissionError("CI_LOCAL_ARTIFACT_ACCESS_BLOCKED")
    mode, flags = args[1:3]
    writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    if writing and relative.parts and relative.parts[0] in {"data", "outputs", "logs", "reports"}:
        raise PermissionError("TEST_HISTORICAL_ARTIFACT_WRITE_BLOCKED")


sys.addaudithook(audit_guard)


def local_data_test(*required):
    """Skip only explicitly listed historical checks in CI or absent fixtures."""
    available = all((ROOT / name).is_file() for name in required)
    reason = "LOCAL_DATA_ONLY: excluded from synthetic CI" if CI_SAFE else "LOCAL_DATA_ONLY: fixtures unavailable"
    return unittest.skipIf(CI_SAFE or not available, reason)
