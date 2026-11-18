from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response

from zvonilka.auth.service import Principal

from .schemas import CreateRoomInput, IssuedInvitation, RedeemInvitationInput, RoomView


def room_router(principal: Callable, rate: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["rooms"])

    async def limited(request: Request, user: Principal, category: str) -> None:
        policy = {
            "room_create": (request.app.state.settings.room_create_rate_limit, 3600),
            "room_read": (request.app.state.settings.room_read_rate_limit, 60),
            "room_write": (request.app.state.settings.room_write_rate_limit, 300),
            "invite_redeem": (request.app.state.settings.invite_redeem_rate_limit, 300),
        }
        limit, window = policy[category]
        await rate(request, category, limit, window, str(user.user_id))

    @router.post("/rooms", response_model=RoomView, status_code=201)
    async def create_room(
        _body: CreateRoomInput, request: Request, user: Principal = Depends(principal)
    ):
        await limited(request, user, "room_create")
        return await request.app.state.rooms.create(user.user_id)

    @router.get("/rooms/{room_id}", response_model=RoomView)
    async def room_snapshot(room_id: UUID, request: Request, user: Principal = Depends(principal)):
        await limited(request, user, "room_read")
        return await request.app.state.rooms.snapshot(user.user_id, room_id)

    @router.post("/rooms/{room_id}/invites", response_model=IssuedInvitation, status_code=201)
    async def issue_invite(room_id: UUID, request: Request, user: Principal = Depends(principal)):
        await limited(request, user, "room_write")
        return await request.app.state.rooms.issue_invite(user.user_id, room_id)

    @router.delete("/rooms/{room_id}/invites/{invite_id}", status_code=204)
    async def revoke_invite(
        room_id: UUID,
        invite_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
        request: Request,
        user: Principal = Depends(principal),
    ):
        await limited(request, user, "room_write")
        await request.app.state.rooms.revoke_invite(user.user_id, room_id, invite_id)
        return Response(status_code=204)

    @router.post("/invites/redeem", response_model=RoomView)
    async def redeem_invite(
        body: RedeemInvitationInput, request: Request, user: Principal = Depends(principal)
    ):
        await limited(request, user, "invite_redeem")
        return await request.app.state.rooms.redeem(
            user.user_id, body.invitation.get_secret_value()
        )

    @router.delete("/rooms/{room_id}", status_code=204)
    async def close_room(room_id: UUID, request: Request, user: Principal = Depends(principal)):
        await limited(request, user, "room_write")
        rooms = request.app.state.rooms
        await request.app.state.signaling.participants.close_room(
            room_id, lambda: rooms.close(user.user_id, room_id)
        )
        return Response(status_code=204)

    return router
