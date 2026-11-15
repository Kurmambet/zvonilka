import unittest

from pydantic import ValidationError
from zvonilka.auth.schemas import ChangePasswordInput, RegisterInput


class SchemaTests(unittest.TestCase):
    def test_password_repr_is_redacted(self):
        item = RegisterInput(
            username="ALICE", display_name="Алиса", password="private-password-123"
        )
        self.assertEqual(item.username, "alice")
        self.assertNotIn("private-password-123", repr(item))
        self.assertNotIn("private-password-123", item.model_dump_json())

    def test_extra_user_id_rejected(self):
        with self.assertRaises(ValidationError):
            RegisterInput(
                username="alice", display_name="A", password="password-123456", user_id="admin"
            )

    def test_new_password_must_be_valid(self):
        with self.assertRaises(ValidationError):
            ChangePasswordInput(current_password="old", new_password="short")
