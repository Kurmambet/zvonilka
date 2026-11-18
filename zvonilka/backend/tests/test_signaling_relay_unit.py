"""Routing/schema tests with pure dependency doubles, not Redis/auth verification."""

import json
import unittest
from dataclasses import replace
from functools import partial
from time import monotonic, time
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError
from zvonilka.signaling.auth_protocol import Reauthenticate
from zvonilka.signaling.schemas import (
    Answer,
    Authenticate,
    Heartbeat,
    IceCandidate,
    Offer,
    RelayMessage,
    client_message_adapter,
    server_message_adapter,
)
from zvonilka.signaling.service import Attempt, Policy, Rejected, SignalingService, parse_active


class Auth:
    def __init__(self):
        self.user_id = uuid4()
        self.session_id = uuid4()
        self.expires_at = int(time()) + 3600

    @property
    def principal(self) -> "Auth":
        return self

    async def check_session(self, user_id: UUID, session_id: UUID) -> "Auth":
        return self

    async def authenticate_access(self, access_token: str) -> "Auth":
        return self


class Rooms:
    def __init__(self):
        self.present: dict[tuple[str, str], str] = {}

    async def join(self, actor: str, room_id: str, generation: str) -> dict:
        self.present[(actor, room_id)] = generation
        return {
            "room": {
                "room_id": room_id,
                "owner_id": actor,
                "state": "active",
                "revision": 1,
                "members": [{"user_id": actor, "online": True}],
            }
        }

    async def heartbeat(self, actor: str, room_id: str, generation: str) -> dict:
        return {}

    async def leave(self, actor: str, room_id: str, generation: str) -> dict:
        if self.present.get((actor, room_id)) == generation:
            self.present.pop((actor, room_id))
        return {}


class Peer:
    def __init__(self, attempt: Attempt):
        self.attempt = attempt
        self.messages: list[dict] = []
        self.fail = False

    def send(self, value: str) -> None:
        if self.fail:
            raise OSError("private transport detail")
        self.messages.append(json.loads(value))


def payload(kind: str = "offer") -> dict:
    result = {
        "version": 1,
        "type": kind,
        "request_id": str(uuid4()),
        "generation": str(uuid4()),
        "target_user_id": str(uuid4()),
        "target_generation": str(uuid4()),
    }
    if kind == "ice_candidate":
        result["candidate"] = {
            "candidate": "candidate:1 1 UDP 1 127.0.0.1 5000 typ host",
            "sdp_mid": "0",
            "sdp_mline_index": 0,
        }
    else:
        result["sdp"] = "v=0\r\nopaque-test-description"
    return result


class RelayUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.rooms = Auth(), Rooms()
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45)
        self.room = uuid4()
        self.peers: list[Peer] = []

    async def asyncTearDown(self):
        for peer in self.peers:
            attempt = peer.attempt
            self.service.participants.detach(attempt.room_id, attempt.user_id, attempt.generation)
            await self.service.leave(attempt)
        self.assertEqual(self.service.participants.room_count, 0)
        self.assertEqual(self.service.registry_size, 0)

    async def connect(self, user: UUID | None = None, room: UUID | None = None) -> Peer:
        self.auth.user_id = user or uuid4()
        message = Authenticate.model_validate(
            {
                "version": 1,
                "type": "authenticate",
                "request_id": uuid4(),
                "room_id": room or self.room,
                "access_token": "unit",
                "app_version": "1",
                "capabilities": [],
            }
        )
        attempt = await self.service.identify(message)
        peer = Peer(attempt)
        self.peers.append(peer)
        await self.service.participants.admit(
            attempt.room_id,
            attempt.user_id,
            attempt.generation,
            peer.send,
            attempt.connection.stop,
            partial(self.service.join, attempt, message.request_id),
            ready=partial(self.service.is_current, attempt),
        )
        for other in self.peers:
            other.messages.clear()
        return peer

    def message(self, source: Peer, target: Peer, kind: str = "offer", **overrides) -> RelayMessage:
        data = {
            **payload(kind),
            "generation": str(source.attempt.generation),
            "target_user_id": str(target.attempt.user_id),
            "target_generation": str(target.attempt.generation),
            **overrides,
        }
        result = parse_active(json.dumps(data))
        if isinstance(result, Heartbeat | Reauthenticate):
            raise TypeError("expected relay request")
        return result

    async def test_offer_reaches_only_target_with_server_identity(self):
        first, second, third = await self.connect(), await self.connect(), await self.connect()
        request = self.message(first, second)
        self.assertIsNone(self.service.relay(first.attempt, request))
        self.assertEqual(first.messages, [])
        self.assertEqual(third.messages, [])
        result = second.messages[0]
        server_message_adapter.validate_python(result)
        self.assertEqual(result["from_user_id"], str(first.attempt.user_id))
        self.assertEqual(result["from_generation"], str(first.attempt.generation))
        self.assertEqual(result["target_generation"], str(second.attempt.generation))
        self.assertEqual(result["request_id"], str(request.request_id))
        self.assertEqual(result["room_id"], str(self.room))
        self.assertNotIn("generation", result)

    async def test_answer_flows_in_reverse(self):
        first, second = await self.connect(), await self.connect()
        request = self.message(second, first, "answer")
        self.assertIsInstance(request, Answer)
        assert isinstance(request, Answer)
        self.assertIsNone(self.service.relay(second.attempt, request))
        self.assertEqual(first.messages[0]["sdp"], request.sdp)
        self.assertEqual(first.messages[0]["type"], "answer")

    async def test_ice_and_explicit_end_preserve_order_both_directions(self):
        first, second = await self.connect(), await self.connect()
        for source, target in ((first, second), (second, first)):
            for candidate in (payload("ice_candidate")["candidate"], None):
                request = self.message(source, target, "ice_candidate", candidate=candidate)
                self.assertIsNone(self.service.relay(source.attempt, request))
            self.assertIsNotNone(target.messages[-2]["candidate"])
            self.assertIsNone(target.messages[-1]["candidate"])

    async def test_unknown_target_error_is_nonfatal_and_correlated(self):
        first, second = await self.connect(), await self.connect()
        request = self.message(first, second, target_user_id=str(uuid4()))
        error = self.service.relay(first.attempt, request)
        assert error is not None
        self.assertEqual(
            (error.code, error.fatal, error.request_id),
            ("peer_unavailable", False, request.request_id),
        )
        self.assertIsNone(self.service.relay(first.attempt, self.message(first, second)))

    async def test_cross_room_target_is_indistinguishable_from_offline(self):
        first, second = await self.connect(), await self.connect(room=uuid4())
        error = self.service.relay(first.attempt, self.message(first, second))
        assert error is not None
        self.assertEqual(error.code, "peer_unavailable")
        self.assertEqual(second.messages, [])

    async def test_wrong_target_generation_is_not_redirected(self):
        first, second = await self.connect(), await self.connect()
        error = self.service.relay(
            first.attempt, self.message(first, second, target_generation=str(uuid4()))
        )
        assert error is not None
        self.assertEqual(error.code, "peer_replaced")
        self.assertEqual(second.messages, [])

    async def test_replacement_before_enqueue_never_uses_new_generation_implicitly(self):
        first, old = await self.connect(), await self.connect()
        request = self.message(first, old)
        new = await self.connect(user=old.attempt.user_id)
        error = self.service.relay(first.attempt, request)
        assert error is not None
        self.assertEqual(error.code, "peer_replaced")
        self.assertEqual(new.messages, [])
        self.assertIsNone(self.service.relay(first.attempt, self.message(first, new)))
        self.assertEqual(len(new.messages), 1)

    async def test_replaced_sender_cannot_forward(self):
        old, target = await self.connect(), await self.connect()
        await self.connect(user=old.attempt.user_id)
        with self.assertRaises(Rejected) as caught:
            self.service.relay(old.attempt, self.message(old, target))
        self.assertEqual(caught.exception.code, "connection_replaced")
        self.assertEqual(target.messages, [])

    async def test_wrong_own_generation_is_fatal(self):
        first, second = await self.connect(), await self.connect()
        with self.assertRaises(Rejected) as caught:
            self.service.relay(first.attempt, self.message(first, second, generation=str(uuid4())))
        self.assertEqual(caught.exception.code, "stale_generation")
        self.assertEqual(second.messages, [])

    async def test_expired_sender_cannot_extend_lease_by_signaling(self):
        first, second = await self.connect(), await self.connect()
        first.attempt.deadline = monotonic() - 1
        with self.assertRaises(Rejected) as caught:
            self.service.relay(first.attempt, self.message(first, second))
        self.assertEqual(caught.exception.code, "heartbeat_timeout")

    async def test_expired_target_is_unavailable_before_cleanup(self):
        first, second = await self.connect(), await self.connect()
        second.attempt.deadline = monotonic() - 1
        error = self.service.relay(first.attempt, self.message(first, second))
        assert error is not None
        self.assertEqual(error.code, "peer_unavailable")
        self.assertEqual(second.messages, [])

    async def test_stopped_target_is_unavailable(self):
        first, second = await self.connect(), await self.connect()
        second.attempt.connection.stop("server_shutdown")
        error = self.service.relay(first.attempt, self.message(first, second))
        assert error is not None
        self.assertEqual(error.code, "peer_unavailable")

    async def test_self_target_is_nonfatal(self):
        first = await self.connect()
        error = self.service.relay(first.attempt, self.message(first, first))
        assert error is not None
        self.assertEqual((error.code, error.fatal), ("invalid_target", False))

    async def test_failed_queue_does_not_stop_sender_or_other_peer(self):
        first, second, third = await self.connect(), await self.connect(), await self.connect()
        second.fail = True
        error = self.service.relay(first.attempt, self.message(first, second))
        assert error is not None
        self.assertEqual((error.code, error.fatal), ("peer_overloaded", False))
        self.assertEqual(second.attempt.connection.reason, "overloaded")
        self.assertIsNone(first.attempt.connection.reason)
        self.assertIsNone(self.service.relay(first.attempt, self.message(first, third)))
        self.assertEqual(len(third.messages), 1)

    async def test_expanded_envelope_limit_does_not_touch_target(self):
        first, second = await self.connect(), await self.connect()
        self.service.policy = replace(self.service.policy, max_message_bytes=120)
        error = self.service.relay(first.attempt, self.message(first, second))
        assert error is not None
        self.assertEqual((error.code, error.fatal), ("message_too_large", False))
        self.assertEqual(second.messages, [])
        self.assertIsNone(second.attempt.connection.reason)

    async def test_token_bucket_burst_and_refill(self):
        first, second = await self.connect(), await self.connect()
        self.service.policy = replace(self.service.policy, relay_rate=1, relay_burst=1)
        now = monotonic()
        with patch("zvonilka.signaling.service.monotonic", return_value=now) as clock:
            self.service.relay(first.attempt, self.message(first, second))
            with self.assertRaises(Rejected) as caught:
                self.service.relay(first.attempt, self.message(first, second))
            self.assertEqual(caught.exception.code, "rate_limited")
            clock.return_value = now + 1
            self.service.relay(first.attempt, self.message(first, second))
        self.assertEqual(len(second.messages), 2)

    async def test_success_does_not_renew_heartbeat_deadline(self):
        first, second = await self.connect(), await self.connect()
        deadline = first.attempt.deadline
        self.service.relay(first.attempt, self.message(first, second))
        self.assertEqual(first.attempt.deadline, deadline)

    async def test_unregistered_sender_cannot_route_even_if_service_gate_is_current(self):
        first, second = await self.connect(), await self.connect()
        a = first.attempt
        self.service.participants.detach(a.room_id, a.user_id, a.generation)
        second.messages.clear()
        with self.assertRaises(Rejected) as caught:
            self.service.relay(a, self.message(first, second))
        self.assertEqual(caught.exception.code, "stale_generation")
        self.assertEqual(second.messages, [])

    async def test_shutdown_rejects_new_relay(self):
        first, second = await self.connect(), await self.connect()
        self.service.closing = True
        with self.assertRaises(Rejected) as caught:
            self.service.relay(first.attempt, self.message(first, second))
        self.assertEqual(caught.exception.code, "server_shutdown")


class RelaySchemaTests(unittest.TestCase):
    def test_request_and_server_directions_are_distinct(self):
        for kind in ("offer", "answer", "ice_candidate"):
            data = payload(kind)
            request = client_message_adapter.validate_python(data)
            self.assertEqual(request.type, kind)
            with self.assertRaises(ValidationError):
                server_message_adapter.validate_python(data)
            server = {
                key: value
                for key, value in data.items()
                if key not in {"generation", "target_user_id"}
            }
            server.update(
                room_id=str(uuid4()), from_user_id=str(uuid4()), from_generation=data["generation"]
            )
            parsed = server_message_adapter.validate_python(server)
            self.assertEqual(server_message_adapter.validate_json(parsed.model_dump_json()), parsed)
            with self.assertRaises(ValidationError):
                client_message_adapter.validate_python(server)

    def test_required_fields(self):
        for kind in ("offer", "answer", "ice_candidate"):
            for key in payload(kind):
                value = payload(kind)
                value.pop(key)
                with self.subTest(kind=kind, key=key), self.assertRaises(ValidationError):
                    client_message_adapter.validate_python(value)

    def test_client_cannot_supply_sender_or_room(self):
        for name in ("user_id", "from_user_id", "from_generation", "room_id", "access_token"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Offer.model_validate({**payload(), name: str(uuid4())})

    def test_sdp_byte_boundaries_and_redacted_repr(self):
        for model, kind in ((Offer, "offer"), (Answer, "answer")):
            for value in ("a", "a" * 16384, "я" * 8192):
                model.model_validate({**payload(kind), "sdp": value})
            for value in ("", "a" * 16385, "я" * 8193, "\ud800", None, 1):
                with (
                    self.subTest(kind=kind, size=len(value) if isinstance(value, str) else None),
                    self.assertRaises(ValidationError),
                ):
                    model.model_validate({**payload(kind), "sdp": value})

        secret = "private-ice-password-in-description"
        self.assertNotIn(secret, repr(Offer.model_validate({**payload(), "sdp": secret})))

    def test_candidate_sections_bounds_and_null_end(self):
        data = payload("ice_candidate")
        self.assertIsNone(IceCandidate.model_validate({**data, "candidate": None}).candidate)
        for candidate in (
            {"candidate": "x", "sdp_mid": "0"},
            {"candidate": "x", "sdp_mline_index": 0},
        ):
            IceCandidate.model_validate({**data, "candidate": candidate})
        for candidate in (
            {},
            "",
            {"candidate": "x"},
            {"candidate": "", "sdp_mid": "0"},
            {"candidate": "я" * 1025, "sdp_mid": "0"},
            {"candidate": "x", "sdp_mline_index": True},
            {"candidate": "x", "sdp_mline_index": -1},
            {"candidate": "x", "sdp_mline_index": 65536},
            {"candidate": "x", "sdp_mid": "0", "unexpected": 1},
        ):
            with self.assertRaises(ValidationError):
                IceCandidate.model_validate({**data, "candidate": candidate})

    def test_parser_failure_never_echoes_invalid_payload(self):
        secret = "private-invalid-description"
        for raw in ("{", "null", "[]", json.dumps({**payload(), "sdp": secret, "unknown": 1})):
            with self.assertRaises(Rejected) as caught:
                parse_active(raw)
            self.assertEqual(caught.exception.code, "invalid_message")
            self.assertNotIn(secret, str(caught.exception))

    def test_rate_limits_must_be_positive_integers(self):
        for name in ("relay_rate", "relay_burst"):
            for value in (0, -1, True, 1.5):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(Policy(), **{name: value})


if __name__ == "__main__":
    unittest.main()
