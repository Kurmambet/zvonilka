import threading
import unittest

from argon2.exceptions import InvalidHashError, VerificationError
from zvonilka.auth.passwords import Passwords


class PasswordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.passwords = Passwords(memory_cost=8192, time_cost=1)
        await self.passwords.initialize()

    async def test_real_argon2_hash_verify_and_mismatch(self):
        encoded = await self.passwords.hash("my-correct-password")
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertNotIn("my-correct-password", encoded)
        self.assertTrue(await self.passwords.verify(encoded, "my-correct-password"))
        self.assertFalse(await self.passwords.verify(encoded, "incorrect-password"))

    async def test_random_salt(self):
        first = await self.passwords.hash("same-password-123")
        second = await self.passwords.hash("same-password-123")
        self.assertNotEqual(first, second)

    async def test_unknown_user_never_authenticates(self):
        self.assertFalse(await self.passwords.verify(None, "dummy-password-not-an-account"))

    async def test_corrupt_hash_is_not_silently_wrong_password(self):
        with self.assertRaises((InvalidHashError, VerificationError)):
            await self.passwords.verify("corrupt-hash", "some-password-123")

    async def test_hash_really_runs_in_worker_thread(self):
        actual = self.passwords.hasher
        threads = []

        class RecordingHasher:
            def hash(self, value):
                threads.append(threading.get_ident())
                return actual.hash(value)

            def verify(self, encoded, value):
                return actual.verify(encoded, value)

            def check_needs_rehash(self, encoded):
                return actual.check_needs_rehash(encoded)

        self.passwords.hasher = RecordingHasher()
        await self.passwords.hash("thread-test-password")
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_production_parameters(self):
        production = Passwords()
        encoded = await production.hash("production-parameters-test")
        self.assertIn("m=65536,t=3,p=1", encoded)
        self.assertFalse(await production.needs_rehash(encoded))
        assert self.passwords.dummy_hash is not None
        self.assertTrue(await production.needs_rehash(self.passwords.dummy_hash))
