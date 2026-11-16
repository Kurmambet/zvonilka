"""Verified JWT metadata, with the real PyJWT implementation."""

import unittest
from dataclasses import fields
from time import time
from uuid import uuid4

import jwt
from zvonilka.auth.errors import AuthError
from zvonilka.auth.jwt_tokens import AccessTokens


class AccessMetadataTests(unittest.TestCase):
    def setUp(self):
        self.secret = "metadata-test-secret-" + "a" * 48
        self.access = AccessTokens(self.secret, "test-issuer", "test-audience")
        self.user_id, self.session_id = uuid4(), uuid4()
        self.token = self.access.issue(self.user_id, self.session_id)
        self.payload = jwt.decode(
            self.token, self.secret, algorithms=["HS256"], audience="test-audience"
        )

    def encode(self, changes: dict) -> str:
        return jwt.encode({**self.payload, **changes}, self.secret, algorithm="HS256")

    def test_verified_metadata_contains_exact_signed_expiry(self):
        result = self.access.decode_claims(self.token)
        self.assertEqual(result.user_id, self.user_id)
        self.assertEqual(result.session_id, self.session_id)
        self.assertEqual(result.expires_at, self.payload["exp"])
        self.assertIs(type(result.expires_at), int)

    def test_existing_decode_contract_is_preserved(self):
        self.assertEqual(self.access.decode(self.token), (self.user_id, self.session_id))

    def test_reading_metadata_does_not_extend_expiry(self):
        first = self.access.decode_claims(self.token)
        second = self.access.decode_claims(self.token)
        self.assertEqual(first, second)
        self.assertEqual(first.expires_at, self.payload["exp"])

    def test_metadata_stores_no_token(self):
        result = self.access.decode_claims(self.token)
        self.assertEqual(
            {field.name for field in fields(result)}, {"user_id", "session_id", "expires_at"}
        )
        self.assertNotIn(self.token, repr(result))

    def test_expired_and_wrong_signature_are_rejected(self):
        expired = self.encode({"exp": int(time()) - 1})
        wrong = jwt.encode(self.payload, "other-secret-" + "b" * 48, algorithm="HS256")
        for token in (expired, wrong):
            with (
                self.subTest(case="expired" if token == expired else "signature"),
                self.assertRaises(AuthError),
            ):
                self.access.decode_claims(token)

    def test_required_expiry_and_identity_are_validated(self):
        for name in ("exp", "sub", "sid", "jti"):
            payload = {key: value for key, value in self.payload.items() if key != name}
            token = jwt.encode(payload, self.secret, algorithm="HS256")
            with self.subTest(field=name), self.assertRaises(AuthError):
                self.access.decode_claims(token)

    def test_timestamp_types_are_not_coerced(self):
        for value in (str(self.payload["exp"]), float(self.payload["exp"]), True):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(AuthError):
                self.access.decode_claims(self.encode({"exp": value}))

    def test_issuer_audience_purpose_and_algorithm_remain_verified(self):
        for changes in ({"iss": "other"}, {"aud": "other"}, {"typ": "refresh"}):
            with self.subTest(changes=changes), self.assertRaises(AuthError):
                self.access.decode_claims(self.encode(changes))
        token = jwt.encode(self.payload, self.secret, algorithm="HS384")
        with self.assertRaises(AuthError):
            self.access.decode_claims(token)
