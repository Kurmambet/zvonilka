"""Access metadata and session checks against real PostgreSQL, not a DB double."""

import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from auth_test_support import PG_URL, AuthPgFixture


@unittest.skipUnless(PG_URL, "real PostgreSQL URL is required")
class AccessMetadataPostgresTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        self.pair = await self.login()
        self.principal = await self.service.authenticate(self.pair["access_token"])

    async def test_metadata_preserves_principal_and_expiry(self):
        result = await self.service.authenticate_access(self.pair["access_token"])
        self.assertEqual(result.principal, self.principal)
        self.assertEqual(
            result.expires_at, self.access.decode_claims(self.pair["access_token"]).expires_at
        )
        self.assertNotIn(self.pair["access_token"], repr(result))
        self.assertNotIn(self.pair["refresh_token"], repr(result))

    async def test_session_check_does_not_issue_tokens(self):
        with patch.object(self.access, "issue", side_effect=AssertionError("unexpected issue")):
            for _ in range(2):
                result = await self.service.check_session(
                    self.principal.user_id, self.principal.session_id
                )
                self.assertEqual(result, self.principal)
            await self.service.authenticate_access(self.pair["access_token"])

    async def test_committed_revocation_is_seen_without_a_token(self):
        await self.service.revoke(self.principal, self.principal.session_id)
        await self.assert_auth_error(
            "invalid_access",
            self.service.check_session(self.principal.user_id, self.principal.session_id),
        )
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate_access(self.pair["access_token"])
        )

    async def test_session_cannot_be_rebound_to_another_account(self):
        await self.register("bobby")
        bob = await self.login("bobby")
        await self.assert_auth_error(
            "invalid_access", self.service.check_session(self.principal.user_id, bob["session_id"])
        )
        await self.assert_auth_error(
            "invalid_access", self.service.check_session(self.principal.user_id, uuid4())
        )

    async def test_disabled_user_is_rejected(self):
        from sqlalchemy import update
        from zvonilka.auth.models import User

        async with self.sessions() as db, db.begin():
            await db.execute(
                update(User).where(User.id == self.principal.user_id).values(enabled=False)
            )
        await self.assert_auth_error(
            "invalid_access",
            self.service.check_session(self.principal.user_id, self.principal.session_id),
        )

    async def test_expired_session_is_rejected(self):
        from sqlalchemy import update
        from zvonilka.auth.models import RefreshSession
        from zvonilka.auth.service import utcnow

        async with self.sessions() as db, db.begin():
            await db.execute(
                update(RefreshSession)
                .where(RefreshSession.id == self.principal.session_id)
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
        await self.assert_auth_error(
            "invalid_access",
            self.service.check_session(self.principal.user_id, self.principal.session_id),
        )

    async def test_refresh_keeps_session_identity(self):
        rotated = await self.service.refresh(self.pair["refresh_token"])
        result = await self.service.authenticate_access(rotated["access_token"])
        self.assertEqual(result.principal.user_id, self.principal.user_id)
        self.assertEqual(result.principal.session_id, self.principal.session_id)

    async def test_expiry_crossed_during_database_operation_is_rejected(self):
        from zvonilka.auth.service import utcnow

        later = utcnow() + timedelta(seconds=self.access.ttl + 1)
        with patch("zvonilka.auth.service.utcnow", return_value=later):
            await self.assert_auth_error(
                "invalid_access", self.service.authenticate_access(self.pair["access_token"])
            )
