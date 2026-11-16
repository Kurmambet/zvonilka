"""Raw WS order/membership checks; auth and Redis boundary are doubles."""

import asyncio
import unittest
from uuid import UUID, uuid4

from fastapi import FastAPI
from signaling_test_support import StubAuth, StubRooms, authentication
from starlette.types import Message, Receive, Scope, Send
from test_signaling_transport import WsSession
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.schemas import Participants
from zvonilka.signaling.service import Policy, SignalingService


class ParticipantsTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.rooms = StubAuth(), StubRooms()
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(
                auth_timeout=1,
                operation_timeout=1,
                send_timeout=0.15,
                cleanup_timeout=0.2,
                heartbeat_min_interval=0.005,
            ),
        )
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())
        self.room_id = uuid4()
        self.path = f"/api/v1/rooms/{self.room_id}/ws"
        self.block_slow = False
        self.blocker = asyncio.Event()

    async def join(self, ws: WsSession, user_id: UUID) -> tuple[dict, Participants]:
        self.auth.user_id = user_id
        await ws.accepted()
        await ws.send(authentication(self.room_id))
        joined = await ws.receive_wire()
        self.assertEqual(joined["type"], "joined")
        snapshot = await self.snapshot(ws)
        self.assertIn(str(user_id), [str(member.user_id) for member in snapshot.members])
        return joined, snapshot

    async def snapshot(self, ws: WsSession) -> Participants:
        payload = await ws.receive_wire()
        self.assertEqual(payload["type"], "participants")
        snapshot = Participants.model_validate(payload)
        self.assertEqual(snapshot.room_id, self.room_id)
        return snapshot

    async def slow_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observed(message: Message) -> None:
            if self.block_slow and message["type"] == "websocket.send":
                await self.blocker.wait()
            await send(message)

        await self.app(scope, receive, observed)

    async def test_three_peers_join_then_disconnect_in_order(self):
        users = [uuid4() for _ in range(3)]
        async with WsSession(self.app, self.path) as first:
            _, initial = await self.join(first, users[0])
            self.assertEqual(initial.sequence, 1)
            async with WsSession(self.app, self.path) as second:
                _, second_view = await self.join(second, users[1])
                self.assertEqual(await self.snapshot(first), second_view)
                async with WsSession(self.app, self.path) as third:
                    _, third_view = await self.join(third, users[2])
                    self.assertEqual(await self.snapshot(first), third_view)
                    self.assertEqual(await self.snapshot(second), third_view)
                    self.assertEqual(third_view.sequence, 3)
                    self.assertEqual(len(third_view.members), 3)
                left_third = await self.snapshot(first)
                self.assertEqual(await self.snapshot(second), left_third)
                self.assertEqual(left_third.sequence, 4)
                self.assertEqual(len(left_third.members), 2)
            left_second = await self.snapshot(first)
            self.assertEqual(left_second.sequence, 5)
            self.assertEqual([member.user_id for member in left_second.members], [users[0]])
        self.assertEqual(self.service.participants.room_count, 0)
        self.assertEqual(self.service.connections, 0)

    async def test_replacement_does_not_publish_old_disconnect_as_new_leave(self):
        observer_id, user_id = uuid4(), uuid4()
        async with WsSession(self.app, self.path) as observer:
            await self.join(observer, observer_id)
            async with WsSession(self.app, self.path) as old:
                old_joined, _ = await self.join(old, user_id)
                await self.snapshot(observer)
                async with WsSession(self.app, self.path) as new:
                    new_joined, new_view = await self.join(new, user_id)
                    self.assertNotEqual(old_joined["generation"], new_joined["generation"])
                    self.assertEqual(await self.snapshot(observer), new_view)
                    self.assertEqual((await old.receive_wire())["code"], "connection_replaced")
                    await old.event()
                    assert old.task is not None
                    await asyncio.wait_for(old.task, 2)
                    self.assertTrue(new.outgoing.empty())
                    self.assertTrue(observer.outgoing.empty())
                    members = {
                        member.user_id: str(member.generation) for member in new_view.members
                    }
                    self.assertEqual(members[user_id], new_joined["generation"])
                    await asyncio.sleep(0.01)
                    await new.send(
                        {
                            "version": 1,
                            "type": "heartbeat",
                            "request_id": str(uuid4()),
                            "generation": new_joined["generation"],
                        }
                    )
                    self.assertEqual((await new.receive_wire())["type"], "heartbeat_ack")
                remaining = await self.snapshot(observer)
                self.assertEqual([m.user_id for m in remaining.members], [observer_id])
        self.assertEqual(self.service.participants.room_count, 0)

    async def test_slow_subscriber_does_not_block_other_join_or_leave_snapshot(self):
        async with WsSession(self.slow_app, self.path) as slow:
            await self.join(slow, uuid4())
            self.block_slow = True
            fast_id = uuid4()
            async with WsSession(self.app, self.path) as fast:
                _, both = await self.join(fast, fast_id)
                self.assertEqual(len(both.members), 2)
                remaining = await self.snapshot(fast)
                self.assertEqual([member.user_id for member in remaining.members], [fast_id])
                assert slow.task is not None
                await asyncio.wait_for(slow.task, 2)
                self.assertEqual(self.service.connections, 1)
        self.assertEqual(self.service.participants.room_count, 0)
        self.assertEqual(self.rooms.present, {})


if __name__ == "__main__":
    unittest.main()
