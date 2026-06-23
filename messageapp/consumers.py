
"""
messageapp/consumers.py

Django Channels WebSocket consumers for real-time messaging.

Two consumers:
  1. ChatConsumer   — handles DM chat rooms
  2. PresenceConsumer — handles user online/offline/last_seen

Connection URLs:
  ws://api.menza.com/ws/chat/<conversation_id>/
  ws://api.menza.com/ws/presence/

Events dispatched via channel layer (Redis pub/sub):
  chat.message       → new message in room
  chat.typing        → typing indicator
  message.delivered  → delivery receipt
  message.read       → read receipt
  message.deleted    → message deleted for everyone
  message.reaction   → emoji reaction added/removed

Security:
  - JWT token sent in query string on connect
  - Authorization checked BEFORE accept()
  - If token invalid or user not in conversation → close immediately

Scaling:
  - Each user joins a personal room "user_{id}" on connect
    so we can push to them from anywhere (across multiple server instances)
    via Redis channel layer
  - Redis handles pub/sub across horizontally scaled server instances
"""

import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# JWT AUTH HELPER
# ─────────────────────────────────────────────────────────────
@database_sync_to_async
def get_user_from_token(token: str):
    """
    Validates a JWT access token and returns the User object.
    Returns None if token is invalid or expired.
    Runs in a thread pool (database_sync_to_async) because
    ORM queries are synchronous.
    """
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        from django.contrib.auth import get_user_model
        User = get_user_model()

        access = AccessToken(token)        # validates signature + expiry
        user_id = access["user_id"]        # extract claim
        # Fetch user from DB — exclude deleted/suspended accounts
        return User.objects.get(#❔instead of filter why get used 
            id=user_id,
            is_active=True,
            status="active",
        )
    except Exception as e:
        logger.warning(f"WebSocket JWT auth failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# CHAT CONSUMER  (DM conversations)
# ─────────────────────────────────────────────────────────────
class ChatConsumer(AsyncWebsocketConsumer):
    """
    Handles real-time messaging for a single DM conversation.

    Room name: "dm_{conversation_id}"
    Each conversation has its own room.
    All participants of that conversation are in the room.

    On connect:
      1. Validate JWT from query string
      2. Verify user is a participant in this conversation
      3. Join the room group
      4. Join personal room (for cross-device push)
      5. Mark existing messages as delivered

    On receive (message from this client):
      1. Parse the action type
      2. Dispatch to appropriate handler method
    """

    async def connect(self):
        """Called when client opens WebSocket connection."""

        # ── Step 1: Extract JWT from query string ─────────────────
        # URL: ws://api.menza.com/ws/chat/42/?token=<jwt>
        query_string = self.scope.get("query_string", b"").decode()#❔
        token = None
        for part in query_string.split("&"):#❔
            if part.startswith("token="):
                token = part.split("=", 1)[1]
                break

        if not token:
            logger.warning("WebSocket connection rejected: no token provided")
            await self.close(code=4001)   # custom close code: unauthenticated
            return

        # ── Step 2: Validate token and get user ───────────────────
        self.user = await get_user_from_token(token)
        if not self.user:
            logger.warning("WebSocket connection rejected: invalid token")
            await self.close(code=4001)#❔
            return

        # ── Step 3: Get conversation_id from URL route ────────────
        # Defined in routing.py: ws/chat/<int:conversation_id>/
        self.conversation_id = self.scope["url_route"]["kwargs"].get("conversation_id")

        # ── Step 4: Verify user is a participant ──────────────────
        is_authorized = await self._check_participant()
        if not is_authorized:
            logger.warning(
                f"User {self.user.id} tried to connect to conversation "
                f"{self.conversation_id} without permission"
            )
            await self.close(code=4003)   # custom: forbidden
            return

        # ── Step 5: Join the conversation's channel group ─────────
        # All participants of this conversation are in this group
        self.room_group_name = f"dm_{self.conversation_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,    # unique name for this connection
        )

        # ── Step 6: Join personal user room ───────────────────────
        # So we can push notifications to this user from any server
        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name,
        )

        # ── Step 7: Accept the connection ─────────────────────────
        await self.accept()

        # ── Step 8: Mark user as online ───────────────────────────
        await self._set_user_online(True)

        # ── Step 9: Send undelivered messages as delivered ─────────
        await self._mark_existing_as_delivered()

        logger.info(
            f"User {self.user.username} connected to dm_{self.conversation_id}"
        )

    async def disconnect(self, close_code):
        """Called when client closes the WebSocket."""
        if hasattr(self, "room_group_name"):
            # Leave the conversation room
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )
        if hasattr(self, "user_group_name"):
            # Leave personal room
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name,
            )
        # Update last_seen timestamp on disconnect
        if hasattr(self, "user"):
            await self._set_user_online(False)

        logger.info(f"User {getattr(self, 'user', 'unknown')} disconnected")

    async def receive(self, text_data):
        """
        Called when client sends a message over the WebSocket.
        We use a dispatch pattern: action field determines which
        handler method is called.

        Expected JSON structure:
        {
            "action": "send_message" | "typing" | "read" | "react" | "delete",
            ...action-specific fields...
        }
        """
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON")
            return

        action = data.get("action")

        # ── Dispatch table (replaces long if/elif chain) ──────────
        dispatch = {
            "send_message": self._handle_send_message,
            "typing": self._handle_typing,
            "read": self._handle_read_receipt,
            "react": self._handle_reaction,
            "delete": self._handle_delete,
        }

        handler = dispatch.get(action)
        if not handler:
            await self._send_error(f"Unknown action: {action}")
            return

        await handler(data)

    # ─── Action Handlers ──────────────────────────────────────────

    async def _handle_send_message(self, data):
        """
        Handle incoming message from client.
        NOTE: We do NOT save the message here via WebSocket.
        The client sends via REST API (POST /messages/) which saves to DB,
        then the view calls group_send() to broadcast here.
        This handler is for typing and presence only.
        Rationale: REST API has auth middleware, validation, rate limiting.
        WebSocket is for real-time delivery only.
        """
        # Broadcast to all participants in the room
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat.message",    # maps to chat_message method
                "message_id": data.get("message_id"),
                "sender_id": self.user.id,
                "sender_username": self.user.username,
                "sender_image": self.user.profile_image,
                "content_encrypted": data.get("content_encrypted", ""),
                "message_type": data.get("message_type", "text"),
                "files": data.get("files", []),
                "reply_to_id": data.get("reply_to_id"),
                "sent_at": timezone.now().isoformat(),
            },
        )

    async def _handle_typing(self, data):
        """
        Broadcast typing indicator to OTHER participants only.
        The sender doesn't need to receive their own typing event.
        """
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat.typing",
                "user_id": self.user.id,
                "username": self.user.username,
                "is_typing": data.get("is_typing", True),
            },
        )
    async def _handle_read_receipt(self, data):
        """
        User read a message — update DB and notify sender.
        message_id: the last message they've read.
        """
        message_id = data.get("message_id")
        if not message_id:
            return

        # Update DB in background thread
        await self._mark_message_read(message_id)

        # Broadcast read receipt to the room (shows double blue tick)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message.read",
                "message_id": message_id,
                "read_by_user_id": self.user.id,
                "read_at": timezone.now().isoformat(),
            },
        )

    async def _handle_reaction(self, data):
        """User added/changed a reaction on a message."""
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message.reaction",
                "message_id": data.get("message_id"),
                "user_id": self.user.id,
                "emoji": data.get("emoji"),
            },
        )

    async def _handle_delete(self, data):
        """User deleted a message for everyone — broadcast to room."""
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message.deleted",
                "message_id": data.get("message_id"),
                "deleted_by": self.user.id,
                "for_everyone": data.get("for_everyone", False),
            },
        )

    # ─── Channel Layer Event Handlers (group_send receivers) ──────
    # These are called when a message is dispatched to this group.
    # Method names must match the "type" field with dots replaced by underscores.

    async def chat_message(self, event):
        """Receive a chat message from the group and send to this WebSocket client."""
        await self.send(text_data=json.dumps({
            "event": "new_message",
            "message_id": event.get("message_id"),
            "sender_id": event["sender_id"],
            "sender_username": event["sender_username"],
            "sender_image": event.get("sender_image"),
            "content_encrypted": event.get("content_encrypted", ""),
            "message_type": event.get("message_type", "text"),
            "files": event.get("files", []),
            "reply_to_id": event.get("reply_to_id"),
            "sent_at": event["sent_at"],
        }))


    async def chat_typing(self, event):
        """Forward typing indicator to this client."""
        # Don't send typing indicator back to the typer themselves
        if event["user_id"] == self.user.id:
            return
        await self.send(text_data=json.dumps({
            "event": "typing",
            "user_id": event["user_id"],
            "username": event["username"],
            "is_typing": event["is_typing"],
        }))

    async def message_read(self, event):
        """Forward read receipt to this client."""
        await self.send(text_data=json.dumps({
            "event": "message_read",
            "message_id": event["message_id"],
            "read_by_user_id": event["read_by_user_id"],
            "read_at": event["read_at"],
        }))

    async def message_delivered(self, event):
        """Forward delivery receipt to this client."""
        await self.send(text_data=json.dumps({
            "event": "message_delivered",
            "message_id": event["message_id"],
            "delivered_to_user_id": event["delivered_to_user_id"],
            "delivered_at": event["delivered_at"],
        }))

    async def message_reaction(self, event):
        """Forward reaction event to this client."""
        await self.send(text_data=json.dumps({
            "event": "reaction",
            "message_id": event["message_id"],
            "user_id": event["user_id"],
            "emoji": event["emoji"],
        }))

    async def message_deleted(self, event):
        """Forward delete event — client should remove/grey out the message."""
        await self.send(text_data=json.dumps({
            "event": "message_deleted",
            "message_id": event["message_id"],
            "deleted_by": event["deleted_by"],
            "for_everyone": event["for_everyone"],
        }))

    # ─── DB Helpers (sync wrapped for async) ──────────────────────

    @database_sync_to_async
    def _check_participant(self):
        """Check if self.user is a participant in this conversation."""
        from .models import ConversationParticipant
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user,
            is_active=True,
        ).exists()


    @database_sync_to_async
    def _set_user_online(self, is_online: bool):
        """
        Update user's online status and last_seen.
        Called on connect (online=True) and disconnect (online=False).
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()
        updates = {"is_online": is_online}
        if not is_online:
            updates["last_seen"] = timezone.now()    # record when they went offline
        User.objects.filter(id=self.user.id).update(**updates)


    @database_sync_to_async
    def _mark_existing_as_delivered(self):
        """
        When a user connects, mark all undelivered messages in this
        conversation as delivered.
        """
        from .models import MessageStatus
        from django.utils import timezone as tz
        now = tz.now()
        # Bulk update — one SQL UPDATE instead of N updates
        MessageStatus.objects.filter(
            message__conversation_id=self.conversation_id,
            recipient=self.user,
            is_delivered=False,
        ).update(is_delivered=True, delivered_at=now)

    @database_sync_to_async
    def _mark_message_read(self, message_id: int):
        """Mark a specific message as read by the current user."""
        from .models import MessageStatus
        from django.utils import timezone as tz
        now = tz.now()
        MessageStatus.objects.filter(
            message_id=message_id,
            recipient=self.user,
        ).update(is_read=True, read_at=now, is_delivered=True, delivered_at=now)

    async def _send_error(self, message: str):
        """Send an error event back to this client."""
        await self.send(text_data=json.dumps({
            "event": "error",
            "message": message,
        }))

# ─────────────────────────────────────────────────────────────
# PRESENCE CONSUMER  (online/offline status)
# ─────────────────────────────────────────────────────────────
class PresenceConsumer(AsyncWebsocketConsumer):
    """
    Handles user presence (online/offline).
    URL: ws://api.menza.com/ws/presence/

    When a user connects here, they are marked online globally.
    Their contacts receive a presence update.
    When they disconnect, they're marked offline and last_seen is set.

    This is separate from ChatConsumer so presence works even when
    the user is not in any specific chat room.
    """

    async def connect(self):
        # ── Auth ──────────────────────────────────────────────────
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

        # ── Join personal room ────────────────────────────────────
        self.user_group = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()

        # ── Mark online ───────────────────────────────────────────
        await self._set_online(True)
        # Notify contacts this user is online
        await self._notify_contacts_presence(True)

    async def disconnect(self, close_code):
        if hasattr(self, "user_group"):
            await self.channel_layer.group_discard(self.user_group, self.channel_name)
        if hasattr(self, "user"):
            await self._set_online(False)
            await self._notify_contacts_presence(False)

    async def receive(self, text_data):
        """Presence consumer doesn't handle incoming messages."""
        pass

    async def user_presence(self, event):
        """Forward presence update to this client."""
        await self.send(text_data=json.dumps({
            "event": "presence",
            "user_id": event["user_id"],
            "is_online": event["is_online"],
            "last_seen": event.get("last_seen"),
        }))