import unittest

from zvonilka.auth.identity import (
    new_refresh_token,
    normalize_username,
    refresh_digest,
    validate_display_name,
    validate_password,
)


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_username(" Alice_123 "), "alice_123")

    def test_invalid_names(self):
        for name in ("ab", "1alice", "alice-bob", "аlice", "Kilo", "x" * 33, "alice bob"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_username(name)

    def test_password_not_normalized(self):
        self.assertEqual(validate_password("  a long password  "), "  a long password  ")

    def test_password_bounds(self):
        for value in ("x" * 11, "x" * 129):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                validate_password(value)
        self.assertEqual(len(validate_password("x" * 128)), 128)

    def test_display_name(self):
        self.assertEqual(validate_display_name(" Алиса "), "Алиса")
        for value in ("", "x" * 65, "hello\nworld", "name\x7f", "\ud800"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_display_name(value)

    def test_refresh_entropy_format_and_digest(self):
        tokens = [new_refresh_token() for _ in range(100)]
        self.assertEqual(len(set(tokens)), 100)
        for token in tokens:
            self.assertEqual(len(token), 43)
            digest = refresh_digest(token)
            self.assertEqual(len(digest), 64)
            self.assertNotIn(token, digest)
            self.assertEqual(refresh_digest(token), digest)

    def test_bad_refresh(self):
        for token in ("", "x" * 42, "x" * 44, "я" * 43, " " * 43):
            with self.subTest(token=token), self.assertRaises(ValueError):
                refresh_digest(token)
