import unittest
from uuid import uuid4

from pydantic import ValidationError
from zvonilka.signaling.auth_protocol import (
    Reauthenticate,
    Reauthenticated,
    ReauthRequired,
    reauthentication_client_adapter,
    reauthentication_server_adapter,
)


class ReauthenticationProtocolTests(unittest.TestCase):
    def setUp(self):
        self.generation = str(uuid4())
        self.secret = "private-new-access-token"
        self.request = {
            "version": 1,
            "type": "reauthenticate",
            "request_id": str(uuid4()),
            "generation": self.generation,
            "access_token": self.secret,
        }
        self.response = {
            "version": 1,
            "type": "reauthenticated",
            "request_id": self.request["request_id"],
            "generation": self.generation,
            "expires_at": 1800000000,
        }

    def test_client_token_requires_explicit_secret_access(self):
        message = reauthentication_client_adapter.validate_python(self.request)
        self.assertIsInstance(message, Reauthenticate)
        self.assertEqual(message.access_token.get_secret_value(), self.secret)
        for value in (repr(message), str(message), message.model_dump_json()):
            self.assertNotIn(self.secret, value)

    def test_server_messages_round_trip(self):
        for payload, expected in (
            (self.response, Reauthenticated),
            (
                {"version": 1, "type": "reauth_required", "generation": self.generation},
                ReauthRequired,
            ),
        ):
            message = reauthentication_server_adapter.validate_python(payload)
            self.assertIsInstance(message, expected)
            self.assertEqual(
                reauthentication_server_adapter.validate_json(message.model_dump_json()), message
            )

    def test_wrong_direction_is_rejected(self):
        with self.assertRaises(ValidationError):
            reauthentication_client_adapter.validate_python(self.response)
        with self.assertRaises(ValidationError):
            reauthentication_server_adapter.validate_python(self.request)

    def test_client_cannot_supply_identity_or_expiry(self):
        for name in ("user_id", "session_id", "room_id", "expires_at", "refresh_token"):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                Reauthenticate.model_validate({**self.request, name: str(uuid4())})

    def test_generation_and_correlation_are_required_uuids(self):
        for name in ("generation", "request_id"):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                Reauthenticate.model_validate({**self.request, name: "invalid"})
            with self.subTest(missing=name), self.assertRaises(ValidationError):
                Reauthenticate.model_validate({k: v for k, v in self.request.items() if k != name})

    def test_token_limits_and_types(self):
        for token in ("", "a" * 4097, 1, None):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(ValidationError):
                Reauthenticate.model_validate({**self.request, "access_token": token})
        Reauthenticate.model_validate({**self.request, "access_token": "a" * 4096})

    def test_version_remains_exact(self):
        for value in (True, 1.0, "1", 2, None):
            with self.subTest(version=value), self.assertRaises(ValidationError):
                Reauthenticate.model_validate({**self.request, "version": value})

    def test_expiry_is_a_positive_integer(self):
        for value in (True, 1.0, "1800000000", 0, -1, None):
            with self.subTest(expiry=value), self.assertRaises(ValidationError):
                Reauthenticated.model_validate({**self.response, "expires_at": value})
