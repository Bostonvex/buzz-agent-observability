#!/usr/bin/env python3
"""Read the package version from a Buzz Agent Observability wheel."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: artifact-version.py WHEEL", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if path.suffix != ".whl" or not path.is_file():
        print("installer artifact must be an existing wheel", file=sys.stderr)
        return 2
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel must contain one metadata file")
            metadata = archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as error:
        print(f"could not read wheel metadata: {type(error).__name__}", file=sys.stderr)
        return 1
    match = re.search(r"^Version: ([A-Za-z0-9][A-Za-z0-9._-]*)$", metadata, re.MULTILINE)
    if match is None:
        print("wheel metadata has no safe version", file=sys.stderr)
        return 1
    print(match.group(1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
