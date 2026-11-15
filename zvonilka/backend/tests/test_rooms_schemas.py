import unittest
from uuid import uuid4

from pydantic import ValidationError
from zvonilka.rooms.invitations import encode_invitation
from zvonilka.rooms.schemas import CreateRoomInput, RedeemInvitationInput


class RoomSchemaTests(unittest.TestCase):
    def test_create_cannot_override_owner_id(self):
        with self.assertRaises(ValidationError):
            CreateRoomInput(owner_id=str(uuid4()))

    def test_create_accepts_only_empty_body(self):
        self.assertEqual(CreateRoomInput().model_dump(), {})
        with self.assertRaises(ValidationError):
            CreateRoomInput(user_id=str(uuid4()))

    def test_invitation_redacted_from_repr_and_dump(self):
        token = "A" * 43
        value = RedeemInvitationInput(invitation=encode_invitation(uuid4(), token))
        self.assertNotIn(token, repr(value))
        self.assertNotIn(token, value.model_dump_json())

    def test_no_identity_override_on_redeem(self):
        with self.assertRaises(ValidationError):
            RedeemInvitationInput(
                invitation=encode_invitation(uuid4(), "A" * 43), user_id=str(uuid4())
            )

    def test_invalid_version_and_token_rejected(self):
        good = encode_invitation(uuid4(), "A" * 43)
        for value in (good.replace("zv1", "zv2"), good[:-1] + "!", "bad"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RedeemInvitationInput(invitation=value)
