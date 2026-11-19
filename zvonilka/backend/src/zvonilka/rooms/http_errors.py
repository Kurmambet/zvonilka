"""Finite mapping: unknown repository errors never become public error strings."""

STATUSES = {
    "room_id_exists": 409,
    "room_not_found": 404,
    "room_closed": 410,
    "not_admitted": 403,
    "not_owner": 403,
    "pending_limit": 409,
    "room_limit": 409,
    "owner_room_limit": 409,
    "participant_limit": 409,
    "overloaded": 503,
    "registry_full": 503,
    "schema_mismatch": 503,
    "invite_exists": 409,
    "invite_limit": 409,
    "invite_history_limit": 409,
    "invite_not_found": 404,
    "invite_consumed": 409,
    "invite_revoked": 410,
    "invite_expired": 410,
    "stale_generation": 409,
    "owner_cannot_redeem": 403,
}


def room_http_error(code: str) -> tuple[int, str]:
    status = STATUSES.get(code)
    return (status, code) if status is not None else (500, "internal_error")
