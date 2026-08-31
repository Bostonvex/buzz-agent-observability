from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from collector.auth import TokenFileError, load_or_create_identity_salt, load_or_create_token


class TokenFileTests(unittest.TestCase):
    def test_token_is_created_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "token"
            token = load_or_create_token(path)
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_or_create_token(path), token)

    def test_permissive_token_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("x" * 40, encoding="ascii")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(TokenFileError, "0600"):
                load_or_create_token(path)

    def test_identity_salt_is_distinct_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            salt_path = Path(directory) / "salt"
            token = load_or_create_token(token_path)
            salt = load_or_create_identity_salt(salt_path)
            self.assertNotEqual(token, salt)
            self.assertEqual(stat.S_IMODE(salt_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
