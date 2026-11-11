"""Offline heuristic publication scan. Never opens excluded/secret files.

Only paths and issue categories are printed, never matching source content.
This supports the deliberately simple, exclusion-only repository .gitignore.
"""
import ast
import fnmatch
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = {"options_intelligence", "tests", "docs", "samples", "scripts", ".github"}
ALLOWED_FILES = {"README.md", "README_AR.md", "requirements.txt", ".gitignore"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".csv", ".yml", ".yaml", ".json"}
PATTERNS = {
    "PRIVATE_KEY": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "GITHUB_TOKEN": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b",
    "AWS_ACCESS_KEY": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "JWT_OR_SERVICE_TOKEN": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    "OPENAI_STYLE_KEY": r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b",
    "CREDENTIAL_IN_URL": r"[a-z]+://[^\s/:]+:[^\s/@]+@",
    "PERSONAL_LOCAL_PATH": r"(?:[A-Z]:[\\/]Users[\\/]|/home/)[A-Za-z0-9_.-]+",
    "EMAIL_ADDRESS": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
}
SECRET_NAME = re.compile(r"(?:api[_-]?key|access[_-]?token|password|passwd|client[_-]?secret|service[_-]?role[_-]?key)", re.I)
ASSIGNMENT = re.compile(r"(?:api[_-]?key|access[_-]?token|password|passwd|client[_-]?secret|service[_-]?role[_-]?key)[\"']?\s*[:=]\s*[\"']([^\"'\r\n]+)[\"']", re.I)


def sensitive_path(relative):
    return any(
        part.lower().startswith((".env", "credentials", "service-account"))
        or part.lower() in {"secrets", ".aws", ".ssh", ".supabase"}
        or "secret" in part.lower() or "token" in part.lower()
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
        for part in Path(relative).parts)


def ignored(relative, patterns):
    path = relative.as_posix()
    parts = relative.parts
    for raw in patterns:
        pattern = raw.rstrip("/")
        if pattern.startswith("**/"):
            pattern = pattern[3:]
        if "/" in pattern:
            if fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + "/"):
                return True
        elif any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
    return False


def placeholder(value):
    return not value or value.upper() in {"REDACTION_TEST_MARKER", "FAKE", "TEST", "DUMMY", "EXAMPLE", "CHANGEME"} or value.startswith("<")


def scan_text(text, suffix):
    issues = {kind for kind, pattern in PATTERNS.items() if re.search(pattern, text, re.I if kind == "CREDENTIAL_IN_URL" else 0)}
    if any(not placeholder(match.group(1)) for match in ASSIGNMENT.finditer(text)):
        issues.add("LITERAL_CREDENTIAL_ASSIGNMENT")
    if suffix == ".py":
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if any(isinstance(t, ast.Name) and SECRET_NAME.search(t.id) for t in targets) and not placeholder(value.value):
                        issues.add("LITERAL_CREDENTIAL_ASSIGNMENT")
    return sorted(issues)


def inspect():
    patterns = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    findings, excluded, scanned = [], [], []
    if any(p.startswith("!") for p in patterns):
        findings.append({"file": ".gitignore", "type": "UNREVIEWED_IGNORE_EXCEPTION"})
    tracked = set()
    if (ROOT / ".git").exists():
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        tracked = {p for p in result.stdout.decode("utf-8").split("\0") if p}
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if ".git" in relative.parts or not path.is_file():
            continue
        name = relative.as_posix()
        blocked = sensitive_path(relative) or ignored(relative, patterns)
        if blocked:
            excluded.append(name)
            if name in tracked:
                findings.append({"file": name, "type": "EXCLUDED_FILE_ALREADY_TRACKED"})
            continue
        if path.is_symlink() or path.resolve().is_relative_to(ROOT) is False:
            findings.append({"file": name, "type": "SYMLINK_REQUIRES_REVIEW"})
            continue
        if (relative.parts[0] not in ALLOWED_ROOTS and name not in ALLOWED_FILES) or (path.suffix not in TEXT_SUFFIXES and name != ".gitignore"):
            findings.append({"file": name, "type": "UNREVIEWED_PUBLICATION_FILE"})
            continue
        scanned.append(name)
        try:
            kinds = scan_text(path.read_text(encoding="utf-8-sig"), path.suffix)
        except (UnicodeError, SyntaxError):
            kinds = ["UNREADABLE_OR_INVALID_SOURCE"]
        findings.extend({"file": name, "type": kind} for kind in kinds)
    return {"scanned_files": scanned, "excluded_files": excluded, "findings": findings,
            "limitations": ["Heuristic scan cannot prove absence of secrets or sensitive data.",
                            "Excluded files and .env templates were not read.",
                            "No Git history exists locally; future staged content needs review."]}


if __name__ == "__main__":
    result = inspect()
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result["findings"]))
