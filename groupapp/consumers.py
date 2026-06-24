import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from messageapp.consumers import get_user_from_token

logger = logging.getLogger(__name__)

class GroupConsumer(AsyncWebsocketConsumer):
    """
    Handles real-time messaging for a group conversation.
    Room name: "group_{group_id}"
    """

    async def connect(self):
        query_string = self.scope.get("query_string", b"").decode()
        token = None
        for part in query_string.split("&"):
            if part.startswith("token="):
                token = part.split("=", 1)[1]
                break

        if not token:
            await self.close(code=4001)
            return

        self.user = await get_user_from_token(token)
        if not self.user:
            await self.close(code=4001)
            return

        self.group_id = self.scope["url_route"]["kwargs"].get("group_id")

        is_authorized = await self._check_membership()
        if not is_authorized:
            await self.close(code=4003)
            return

        self.room_group_name = f"group_{self.group_id}"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)

        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.user_group_name, self.channel_name)

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        dispatch = {
            "typing": self._handle_typing,
        }
        handler = dispatch.get(action)
        if handler:
            await handler(data)

    async def _handle_typing(self, data):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat.typing",
                "user_id": self.user.id,
                "username": self.user.username,
                "is_typing": data.get("is_typing", True),
            },
        )

    # Handlers for messages broadcasted from views
    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "event": "new_message",
            "message_id": event.get("message_id"),
            "sender_id": event.get("sender_id"),
            "sender_username": event.get("sender_username"),
            "content_encrypted": event.get("content_encrypted", ""),
            "message_type": event.get("message_type", "text"),
            "files": event.get("files", []),
            "reply_to_id": event.get("reply_to_id"),
            "sent_at": event.get("sent_at"),
        }))

    async def chat_typing(self, event):
        if event["user_id"] == self.user.id:
            return
        await self.send(text_data=json.dumps({
            "event": "typing",
            "user_id": event["user_id"],
            "username": event["username"],
            "is_typing": event["is_typing"],
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            "event": "message_deleted",
            "message_id": event["message_id"],
            "deleted_by": event.get("deleted_by"),
            "for_everyone": event.get("for_everyone", True),
        }))

    @database_sync_to_async
    def _check_membership(self):
        from groupapp.models import GroupMember
        return GroupMember.objects.filter(
            group_id=self.group_id,
            user=self.user,
            is_active=True,
        ).exists()
