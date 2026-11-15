import json
import unittest
from uuid import uuid4

from pydantic import ValidationError
from zvonilka.signaling.schemas import (
    Authenticate,
    Error,
    Joined,
    client_message_adapter,
    server_message_adapter,
)


class SignalingSchemaTests(unittest.TestCase):
    def setUp(self):
        self.user_id = str(uuid4())
        self.room_id = str(uuid4())
        self.request_id = str(uuid4())
        self.token = "unit-test-access-token-DO-NOT-LOG"

    def authentication(self) -> dict:
        return {
            "version": 1,
            "type": "authenticate",
            "request_id": self.request_id,
            "room_id": self.room_id,
            "access_token": self.token,
            "app_version": "0.5.0-dev.1+local",
            "capabilities": [],
        }

    def joined(self) -> dict:
        return {
            "version": 1,
            "type": "joined",
            "request_id": self.request_id,
            "user_id": self.user_id,
            "generation": str(uuid4()),
            "room": {
                "room_id": self.room_id,
                "owner_id": self.user_id,
                "state": "active",
                "revision": 2,
                "members": [{"user_id": self.user_id, "online": True}],
                "activated_at": "2026-11-15T12:00:00Z",
                "deadline": "2026-11-16T00:00:00Z",
            },
            "capabilities": [],
        }

    def error(self) -> dict:
        return {
            "version": 1,
            "type": "error",
            "request_id": None,
            "code": "invalid_message",
            "fatal": True,
        }

    def test_authentication_from_json(self):
        item = client_message_adapter.validate_json(json.dumps(self.authentication()))
        self.assertIsInstance(item, Authenticate)
        self.assertEqual(str(item.room_id), self.room_id)
        self.assertEqual(item.access_token.get_secret_value(), self.token)

    def test_authentication_round_trip_requires_explicit_secret_access(self):
        item = Authenticate.model_validate(self.authentication())
        data = item.model_dump(mode="json")
        self.assertNotIn(self.token, json.dumps(data))
        data["access_token"] = item.access_token.get_secret_value()
        self.assertEqual(client_message_adapter.validate_json(json.dumps(data)), item)

    def test_token_redacted_in_default_representations(self):
        item = Authenticate.model_validate(self.authentication())
        for value in (repr(item), str(item), repr(item.model_dump()), item.model_dump_json()):
            with self.subTest(value_type=type(value).__name__):
                self.assertNotIn(self.token, value)

    def test_server_json_round_trip(self):
        for payload, expected in ((self.joined(), Joined), (self.error(), Error)):
            with self.subTest(message_type=payload["type"]):
                item = server_message_adapter.validate_json(json.dumps(payload))
                self.assertIsInstance(item, expected)
                self.assertEqual(server_message_adapter.validate_json(item.model_dump_json()), item)

    def test_version_is_required_and_exact_integer(self):
        for factory, model in (
            (self.authentication, Authenticate),
            (self.joined, Joined),
            (self.error, Error),
        ):
            for version in (0, 2, True, False, 1.0, "1", None):
                with self.subTest(model=model.__name__, version=version):
                    data = factory()
                    data["version"] = version
                    with self.assertRaises(ValidationError):
                        model.model_validate(data)
            data = factory()
            del data["version"]
            with self.assertRaises(ValidationError):
                model.model_validate(data)

    def test_authentication_required_fields(self):
        original = self.authentication()
        for name in original:
            with self.subTest(field=name):
                data = dict(original)
                del data[name]
                with self.assertRaises(ValidationError):
                    Authenticate.model_validate(data)

    def test_extra_identity_and_generation_rejected(self):
        for name in ("user_id", "session_id", "generation", "unexpected"):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), name: str(uuid4())})

    def test_extra_server_fields_rejected(self):
        for factory, model in ((self.joined, Joined), (self.error, Error)):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate({**factory(), "detail": self.token})

    def test_bad_uuids_rejected(self):
        for factory, model, names in (
            (self.authentication, Authenticate, ("request_id", "room_id")),
            (self.joined, Joined, ("request_id", "user_id", "generation")),
            (self.error, Error, ("request_id",)),
        ):
            for name in names:
                with (
                    self.subTest(model=model.__name__, field=name),
                    self.assertRaises(ValidationError),
                ):
                    model.model_validate({**factory(), name: "not-a-uuid"})

    def test_token_boundaries_and_types(self):
        for length in (1, 4096):
            item = Authenticate.model_validate(
                {**self.authentication(), "access_token": "a" * length}
            )
            self.assertEqual(len(item.access_token.get_secret_value()), length)
        for value in ("", "a" * 4097, None, 123, True, []):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "access_token": value})

    def test_application_version_boundaries(self):
        for value in ("a", "a" * 64):
            Authenticate.model_validate({**self.authentication(), "app_version": value})
        for value in ("", "a" * 65, " 1.0", "1.0\n", "версия", 1, None):
            with self.subTest(version=value), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "app_version": value})

    def test_capabilities_boundaries_and_unknown_names(self):
        for values in ([], ["future.audio-v2"], ["a" * 48], [f"cap{i}" for i in range(16)]):
            item = Authenticate.model_validate({**self.authentication(), "capabilities": values})
            self.assertEqual(item.capabilities, values)
        for values in (
            ["a", "a"],
            [""],
            ["a" * 49],
            ["UPPER"],
            ["a\n"],
            [f"cap{i}" for i in range(17)],
            [1],
            None,
        ):
            with self.subTest(capabilities=values), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "capabilities": values})

    def test_unknown_type_and_direction_rejected(self):
        for adapter, payload in (
            (client_message_adapter, {**self.authentication(), "type": "offer"}),
            (client_message_adapter, self.joined()),
            (server_message_adapter, {**self.error(), "type": "unknown"}),
            (server_message_adapter, self.authentication()),
        ):
            with self.subTest(message_type=payload["type"]), self.assertRaises(ValidationError):
                adapter.validate_python(payload)

    def test_non_object_and_invalid_json_rejected(self):
        for adapter in (client_message_adapter, server_message_adapter):
            for raw in ("{", "[]", "null", '"text"', "123"):
                with self.subTest(raw=raw), self.assertRaises(ValidationError):
                    adapter.validate_json(raw)

    def test_joined_requires_online_user_and_active_room(self):
        for state in ("created", "recovering", "closed"):
            data = self.joined()
            data["room"]["state"] = state
            with self.subTest(state=state), self.assertRaises(ValidationError):
                Joined.model_validate(data)
        for members in (
            [],
            [{"user_id": self.user_id, "online": False}],
            [{"user_id": str(uuid4()), "online": True}],
        ):
            data = self.joined()
            data["room"]["members"] = members
            with self.subTest(members=members), self.assertRaises(ValidationError):
                Joined.model_validate(data)

    def test_joined_members_bounds_and_duplicates(self):
        data = self.joined()
        data["room"]["members"] *= 2
        with self.assertRaises(ValidationError):
            Joined.model_validate(data)
        data = self.joined()
        data["room"]["members"] += [{"user_id": str(uuid4()), "online": True} for _ in range(2)]
        Joined.model_validate(data)
        data["room"]["members"].append({"user_id": str(uuid4()), "online": True})
        with self.assertRaises(ValidationError):
            Joined.model_validate(data)

    def test_joined_rejects_duplicate_capabilities(self):
        with self.assertRaises(ValidationError):
            Joined.model_validate({**self.joined(), "capabilities": ["a", "a"]})

    def test_error_codes_and_fatal_flag_are_bounded(self):
        for code in ("arbitrary_exception_text", "schema_mismatch", self.token):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                Error.model_validate({**self.error(), "code": code})
        for value in (0, 1, "true", None):
            with self.subTest(fatal=value), self.assertRaises(ValidationError):
                Error.model_validate({**self.error(), "fatal": value})
        for value in (True, False):
            self.assertEqual(Error.model_validate({**self.error(), "fatal": value}).fatal, value)
        item = Error.model_validate({**self.error(), "request_id": self.request_id})
        self.assertEqual(str(item.request_id), self.request_id)


if __name__ == "__main__":
    unittest.main()
