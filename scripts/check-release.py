#!/usr/bin/env python3
"""Validate release archives and optionally write reproducible SHA-256 sums."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 2000
TEXT_SUFFIXES = {"", ".css", ".html", ".js", ".json", ".md", ".mjs", ".py", ".sh", ".toml", ".txt", ".yml", ".yaml"}
PATTERNS = {
    "AWS access key": re.compile("AK" + "IA[0-9A-Z]{16}"),
    "GitHub token": re.compile("gh" + "[pousr]_[A-Za-z0-9_]{20,}"),
    "OpenAI-style key": re.compile("s" + "k-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile("-----BEGIN " + "(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "macOS home path": re.compile("/Us" + "ers/[A-Za-z0-9._-]+/"),
}


def archives() -> list[Path]:
    return sorted([*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")])


def members(path: Path) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ValueError(f"{path.name}: oversized member {info.filename}")
                result.append((info.filename, archive.read(info)))
    else:
        with tarfile.open(path, "r:gz") as archive:
            for info in archive.getmembers():
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ValueError(f"{path.name}: unsupported archive member type")
                if info.size > MAX_MEMBER_BYTES:
                    raise ValueError(f"{path.name}: oversized member {info.name}")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise ValueError(f"{path.name}: unreadable member {info.name}")
                result.append((info.name, extracted.read()))
    return result


def validate_archive(path: Path) -> None:
    contents = members(path)
    if not contents or len(contents) > MAX_MEMBERS:
        raise ValueError(f"{path.name}: invalid member count")
    if sum(len(data) for _, data in contents) > MAX_TOTAL_BYTES:
        raise ValueError(f"{path.name}: uncompressed archive is too large")
    names = {name for name, _ in contents}
    for name, data in contents:
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name:
            raise ValueError(f"{path.name}: unsafe archive path")
        if member.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                raise ValueError(f"{path.name}: possible {label} in {name}")
    if path.suffix == ".whl":
        required = {
            "collector/cli.py",
            "collector/providers/vllm.py",
            "collector/providers/nvidia_smi.py",
            "collector/providers/json_command.py",
            "collector/dashboard/index.html",
            "proxy/openai_proxy.py",
        }
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"{path.name}: missing wheel files: {', '.join(missing)}")
        forbidden = [
            name for name in names
            if name.startswith(("tests/", "proxy/test/", "packages/acp-observer/test/"))
        ]
        if forbidden:
            raise ValueError(f"{path.name}: test files leaked into wheel")
        metadata = next((data for name, data in contents if name.endswith(".dist-info/METADATA")), None)
        if metadata is None or b"Version: 0.1.0\n" not in metadata:
            raise ValueError(f"{path.name}: release metadata version is not 0.1.0")


def write_checksums(paths: list[Path]) -> Path:
    destination = DIST / "SHA256SUMS"
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in paths]
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-checksums", action="store_true")
    args = parser.parse_args()
    paths = archives()
    if not paths:
        print("release check failed: dist contains no wheel or source archive", file=sys.stderr)
        return 1
    try:
        for path in paths:
            validate_archive(path)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"release check failed: {error}", file=sys.stderr)
        return 1
    checksum = write_checksums(paths) if args.write_checksums else None
    message = f"release check passed ({len(paths)} archives)"
    if checksum is not None:
        message += f"; wrote {checksum.relative_to(ROOT)}"
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
