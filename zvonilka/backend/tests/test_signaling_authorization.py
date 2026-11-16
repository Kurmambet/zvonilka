"""Deterministic timing tests: no sleeps, JWT issuance or database mocks."""

import unittest
from uuid import uuid4

from zvonilka.signaling.authorization import AuthorizationRejected, AuthorizationWindow


class AuthorizationWindowTests(unittest.TestCase):
    def setUp(self):
        self.user, self.session = uuid4(), uuid4()
        self.window = AuthorizationWindow(
            self.user,
            self.session,
            1100,
            now_wall=1000,
            now=100,
        )

    def test_expiry_and_grace_boundaries(self):
        self.assertEqual(self.window.state(199.999), "authorized")
        self.assertEqual(self.window.state(200), "reauth_required")
        self.assertEqual(self.window.state(229.999), "reauth_required")
        self.assertEqual(self.window.state(230), "expired")

    def test_session_check_every_ten_seconds_does_not_renew_jwt(self):
        self.assertFalse(self.window.session_check_due(109.999))
        self.assertTrue(self.window.session_check_due(110))
        self.window.session_checked(110)
        self.assertFalse(self.window.session_check_due(119.999))
        self.assertTrue(self.window.session_check_due(120))
        self.window.session_checked(120)
        self.assertEqual(self.window.expires_at, 1100)
        self.assertEqual(self.window.state(200), "reauth_required")

    def test_session_check_during_grace_does_not_extend_grace(self):
        self.window.session_checked(225)
        self.assertEqual(self.window.state(230), "expired")
        self.assertFalse(self.window.session_check_due(230))

    def test_success_before_expiry_keeps_identity(self):
        self.window.reauthenticated(self.user, self.session, 1200, now_wall=1050, now=150)
        self.assertEqual((self.window.user_id, self.window.session_id), (self.user, self.session))
        self.assertEqual(self.window.state(200), "authorized")
        self.assertEqual(self.window.state(300), "reauth_required")

    def test_success_during_grace_resumes_authorization(self):
        self.window.reauthenticated(self.user, self.session, 1200, now_wall=1110, now=210)
        self.assertEqual(self.window.state(210), "authorized")
        self.assertFalse(self.window.session_check_due(219.999))
        self.assertTrue(self.window.session_check_due(220))

    def test_other_user_or_session_is_rejected(self):
        for user, session in ((uuid4(), self.session), (self.user, uuid4())):
            with self.subTest(user=user), self.assertRaises(AuthorizationRejected) as error:
                self.window.reauthenticated(user, session, 1200, now_wall=1050, now=150)
            self.assertEqual(error.exception.code, "invalid_access")
        self.assertEqual(self.window.expires_at, 1100)

    def test_expired_grace_cannot_be_revived(self):
        with self.assertRaises(AuthorizationRejected) as error:
            self.window.reauthenticated(self.user, self.session, 1200, now_wall=1130, now=230)
        self.assertEqual(error.exception.code, "reauthentication_timeout")
        with self.assertRaises(AuthorizationRejected):
            self.window.session_checked(230)

    def test_already_expired_grant_is_not_initially_admitted(self):
        with self.assertRaises(AuthorizationRejected):
            AuthorizationWindow(self.user, self.session, 1000, now_wall=1000, now=100)

    def test_older_or_expired_reauthentication_does_not_replace_grant(self):
        for expiry in (1050, 1099):
            with self.subTest(expiry=expiry), self.assertRaises(AuthorizationRejected):
                self.window.reauthenticated(self.user, self.session, expiry, now_wall=1050, now=150)
        self.assertEqual(self.window.expires_at, 1100)

    def test_same_expiry_cannot_gain_time_on_wall_clock_rollback(self):
        self.window.reauthenticated(self.user, self.session, 1100, now_wall=900, now=150)
        self.assertEqual(self.window.state(200), "reauth_required")

    def test_next_wakeup_chooses_check_then_expiry_then_grace_end(self):
        self.assertEqual(self.window.next_deadline(100), 110)
        self.window.session_checked(195)
        self.assertEqual(self.window.next_deadline(195), 200)
        self.assertEqual(self.window.next_deadline(200), 205)
        self.window.session_checked(225)
        self.assertEqual(self.window.next_deadline(225), 230)
        self.assertEqual(self.window.next_deadline(230), 230)

    def test_invalid_intervals_and_monotonic_rollback_are_rejected(self):
        for value in (0.0, -1.0, float("inf"), float("nan")):
            for grace, interval in ((value, 10.0), (30.0, value)):
                with (
                    self.subTest(grace=grace, interval=interval),
                    self.assertRaises(ValueError),
                ):
                    AuthorizationWindow(
                        self.user,
                        self.session,
                        1100,
                        now_wall=1000,
                        now=100,
                        grace_seconds=grace,
                        check_interval=interval,
                    )
        with self.assertRaises(ValueError):
            self.window.state(99)

    def test_same_expiry_cannot_reopen_authorization_during_grace(self):
        with self.assertRaises(AuthorizationRejected):
            self.window.reauthenticated(
                self.user,
                self.session,
                1100,
                now_wall=900,
                now=210,
            )
        self.assertEqual(self.window.state(210), "reauth_required")
