#!/usr/bin/env python3
"""Small repository guard for common credentials and machine-specific paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", "dist", "build"}
SKIP_FILES = {Path(__file__).resolve()}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yml",
    ".yaml",
}

# Concatenated markers keep this detector from matching its own source.
PATTERNS = {
    "AWS access key": re.compile("AK" + "IA[0-9A-Z]{16}"),
    "GitHub token": re.compile("gh" + "[pousr]_[A-Za-z0-9_]{20,}"),
    "OpenAI-style key": re.compile("s" + "k-[A-Za-z0-9_-]{20,}"),
    "Slack token": re.compile("xo" + "x[baprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile("-----BEGIN " + "(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "macOS home path": re.compile("/Us" + "ers/[A-Za-z0-9._-]+/"),
}


def files() -> list[Path]:
    result = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() in SKIP_FILES:
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        result.append(path)
    return result


def main() -> int:
    findings: list[str] = []
    for path in files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: possible {name}")
    if findings:
        print("Repository safety scan failed:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print(f"Repository safety scan passed ({len(files())} text files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
