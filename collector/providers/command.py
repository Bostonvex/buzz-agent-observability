"""Bounded, shell-free execution for optional read-only providers."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Sequence


class CommandFailure(RuntimeError):
    """A safe command failure that never includes arguments or output."""


def run_bounded(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 3.0,
    maximum_bytes: int = 1024 * 1024,
) -> bytes:
    if not argv or len(argv) > 32:
        raise ValueError("command argv must contain between 1 and 32 entries")
    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ValueError("command timeout must be between 0 and 30 seconds")
    if maximum_bytes < 1 or maximum_bytes > 4 * 1024 * 1024:
        raise ValueError("command output bound is invalid")
    for value in argv:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError("command arguments must be bounded non-empty strings")
        if any(ord(character) < 32 for character in value):
            raise ValueError("command arguments cannot contain control characters")

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandFailure("command timed out")
            events = selector.select(timeout=remaining)
            if not events:
                raise CommandFailure("command timed out")
            chunk = os.read(process.stdout.fileno(), min(65536, maximum_bytes + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > maximum_bytes:
                raise CommandFailure("command output exceeded the limit")
        remaining = max(0.01, deadline - time.monotonic())
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise CommandFailure("command exited unsuccessfully")
        return bytes(output)
    except (OSError, subprocess.SubprocessError) as error:
        raise CommandFailure("command execution failed") from error
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
