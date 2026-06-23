
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





















