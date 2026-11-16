from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError

from .errors import AuthError


@dataclass(frozen=True, slots=True)
class VerifiedAccessClaims:
    user_id: UUID
    session_id: UUID
    expires_at: int  # Verified JWT exp, in UTC Unix seconds; not a renewed deadline.


class AccessTokens:
    def __init__(self, secret: str, issuer: str, audience: str, ttl: int = 600):
        if len(secret.encode()) < 32 or not 60 <= ttl <= 3600:
            raise ValueError("invalid access token configuration")
        self.secret, self.issuer, self.audience, self.ttl = secret, issuer, audience, ttl

    def issue(self, user_id: UUID, session_id: UUID) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": str(user_id),
                "sid": str(session_id),
                "jti": str(uuid4()),
                "iss": self.issuer,
                "aud": self.audience,
                "typ": "access",
                "iat": int(now.timestamp()),
                "nbf": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=self.ttl)).timestamp()),
            },
            self.secret,
            algorithm="HS256",
            headers={"typ": "JWT"},
        )

    def decode(self, token: str) -> tuple[UUID, UUID]:
        claims = self.decode_claims(token)
        return claims.user_id, claims.session_id

    def decode_claims(self, token: str) -> VerifiedAccessClaims:
        if not isinstance(token, str) or len(token) > 4096:
            raise AuthError("invalid_access")
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                leeway=0,
                options={
                    "require": ["sub", "sid", "jti", "iss", "aud", "typ", "iat", "nbf", "exp"],
                    "strict_aud": True,
                },
            )
            if claims["typ"] != "access":
                raise ValueError("wrong token purpose")
            for key in ("iat", "nbf", "exp"):
                if type(claims[key]) is not int:
                    raise ValueError("timestamp must be an integer")
            if not claims["iat"] <= claims["nbf"] < claims["exp"]:
                raise ValueError("invalid token timeline")
            if claims["exp"] - claims["iat"] > self.ttl:
                raise ValueError("access lifetime exceeds policy")
            UUID(claims["jti"])
            return VerifiedAccessClaims(UUID(claims["sub"]), UUID(claims["sid"]), claims["exp"])
        except (InvalidTokenError, ValueError, TypeError, KeyError, AttributeError):
            raise AuthError("invalid_access") from None
