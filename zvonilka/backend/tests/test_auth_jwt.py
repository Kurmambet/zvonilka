import importlib.util
import time
import unittest
from uuid import uuid4


@unittest.skipUnless(importlib.util.find_spec("jwt"), "PyJWT dependency is required")
class JwtTests(unittest.TestCase):
    def setUp(self):
        import jwt
        from zvonilka.auth.errors import AuthError
        from zvonilka.auth.jwt_tokens import AccessTokens

        self.jwt, self.AuthError = jwt, AuthError
        self.secret = "unit-test-secret-" + "a" * 48
        self.tokens = AccessTokens(self.secret, "issuer", "audience")
        self.user, self.session = uuid4(), uuid4()
        raw = self.tokens.issue(self.user, self.session)
        self.claims = jwt.decode(raw, self.secret, algorithms=["HS256"], audience="audience")

    def reject(self, **changes):
        claims = {**self.claims, **changes}
        token = self.jwt.encode(claims, self.secret, algorithm="HS256")
        with self.assertRaises(self.AuthError):
            self.tokens.decode(token)

    def test_valid(self):
        self.assertEqual(
            self.tokens.decode(self.tokens.issue(self.user, self.session)),
            (self.user, self.session),
        )

    def test_expired(self):
        self.reject(exp=int(time.time()) - 1)

    def test_issuer_audience_and_purpose(self):
        for key, value in (("iss", "other"), ("aud", "other"), ("typ", "refresh")):
            with self.subTest(key=key):
                self.reject(**{key: value})

    def test_required_claims(self):
        for name in self.claims:
            claims = {k: v for k, v in self.claims.items() if k != name}
            token = self.jwt.encode(claims, self.secret, algorithm="HS256")
            with self.subTest(name=name), self.assertRaises(self.AuthError):
                self.tokens.decode(token)

    def test_wrong_signature(self):
        token = self.jwt.encode(self.claims, "different-secret-" + "z" * 48, algorithm="HS256")
        with self.assertRaises(self.AuthError):
            self.tokens.decode(token)

    def test_algorithm_not_taken_from_header(self):
        token = self.jwt.encode(self.claims, self.secret, algorithm="HS384")
        with self.assertRaises(self.AuthError):
            self.tokens.decode(token)

    def test_noninteger_and_invalid_ids(self):
        for key, value in (
            ("exp", str(self.claims["exp"])),
            ("iat", True),
            ("sub", "admin"),
            ("sid", "session"),
            ("jti", "id"),
        ):
            with self.subTest(key=key):
                self.reject(**{key: value})

    def test_future_nbf_and_excessive_lifetime(self):
        self.reject(nbf=int(time.time()) + 100)
        self.reject(exp=self.claims["iat"] + 601)
