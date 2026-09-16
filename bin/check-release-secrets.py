#!/usr/bin/env python3
"""Fail closed on high-confidence credentials in release source.

Only paths and line numbers are reported; matching text is never printed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "sshpass-argv": re.compile(r"\bsshpass\s+-p(?:\s|$)"),
    "literal-sensitive-assignment": re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN)"
        r"[A-Z0-9_]*\s*=\s*(['\"])(?!\1)(?!\$)[^\r\n]+\1\s*(?:#.*)?$"
    ),
    "curl-literal-basic-auth": re.compile(r"\bcurl\b[^\n]*\s-u\s+['\"][^:$'\"]+:[^$'\"]+['\"]"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}

TEXT_SUFFIXES = {
    ".env",
    ".ini",
    ".json",
    ".mdc",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[tuple[str, int, str]] = []
    for path in tracked_files(root):
        if path == Path(__file__).resolve():
            continue
        if not path.is_file():
            continue
        if path.name not in {"Dockerfile", "rack-config.env"} and path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append((str(path.relative_to(root)), text.count("\n", 0, match.start()) + 1, name))

    if findings:
        for path, line, name in sorted(set(findings)):
            print(f"{path}:{line}: {name}", file=sys.stderr)
        print(f"release secret scan failed: {len(set(findings))} high-confidence finding(s)", file=sys.stderr)
        return 1
    print("release secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
