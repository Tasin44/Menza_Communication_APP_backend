from django.shortcuts import render

# Create your views here.

"""
messageapp/views.py

All REST API views for messaging.

OOP structure:
  - BaseMessagingView         — shared permission + response helpers
  - ConversationListCreateView
  - ConversationDetailView
  - ConversationActionView    — mute, block, delete
  - MessageListView           — paginated messages for a conversation
  - SendMessageView           — create a message
  - MessageDetailView         — get/delete a single message
  - MessageSearchView         — search text inside a conversation
  - MessageReadReceiptView    — mark messages as read (REST fallback)
  - MessageReactionView       — add/remove emoji reactions
  - PinMessageView            — pin/unpin a message inside chat
  - PinnedItemView            — dashboard pins (max 5)
  - MediaFilesView            — get all media files in a conversation

WebSocket handles real-time delivery.
REST API handles persistence, history, and fallback.
"""

import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q, Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authapp.models import BlockedUser
from .models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageFile,
    MessageStatus,
    MessageReaction,
    PinnedMessage,
    PinnedItem,
    ArchivedConversation,
)
from .serializers import (
    ConversationListSerializer,
    ConversationDetailSerializer,
    CreateConversationSerializer,
    SendMessageSerializer,
    MessageSerializer,
    MuteConversationSerializer,
    MessageReactionWriteSerializer,
    PinnedItemSerializer,
    MessageFileSerializer,
)

logger = logging.getLogger(__name__)
channel_layer = get_channel_layer()    # Redis channel layer — initialized once


# ─────────────────────────────────────────────────────────────
# PAGINATION  (cursor-based for messages — scale friendly)
# ─────────────────────────────────────────────────────────────
class MessageCursorPagination(CursorPagination):
    """
    Cursor pagination for messages — better than page-number for large tables.
    Reason: page 500 with offset pagination requires scanning 500*20 = 10,000 rows.
    Cursor pagination always scans from the cursor position → O(1) seek.
    Ordered by sent_at descending: newest messages first (like WhatsApp).
    Client scrolls up to load older messages.
    #❔explain the cursor pagination more 
    """
    ordering = "-sent_at"     # newest first
    page_size = 30             # 30 messages per page
    page_size_query_param = "page_size"
    max_page_size = 100


class StandardPagePagination(CursorPagination):
    """For conversation list — cursor on updated_at."""
    ordering = "-updated_at"
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50


# ─────────────────────────────────────────────────────────────
# BASE VIEW  (shared helpers — OOP mixin)
# ─────────────────────────────────────────────────────────────
class BaseMessagingView(APIView):
    """
    Base class with shared helpers.
    All messaging views inherit from this.
    """

    permission_classes = [IsAuthenticated]

    # ── Response helpers ──────────────────────────────────────
    def ok(self, data, message="Success"):
        return Response({
            "success": True,
            "message": message,
            "data": data,
        }, status=status.HTTP_200_OK)

    def created(self, data, message="Created"):
        return Response({
            "success": True,
            "message": message,
            "data": data,
        }, status=status.HTTP_201_CREATED)

    def bad_request(self, errors, message="Validation error"):
        return Response({
            "success": False,
            "message": message,
            "errors": errors,
        }, status=status.HTTP_400_BAD_REQUEST)

    def not_found(self, message="Not found"):
        return Response({
            "success": False,
            "message": message,
        }, status=status.HTTP_404_NOT_FOUND)

    def forbidden(self, message="Permission denied"):
        return Response({
            "success": False,
            "message": message,
        }, status=status.HTTP_403_FORBIDDEN)

    # ── Conversation access guard ──────────────────────────────
    def get_conversation_or_403(self, conversation_id, user):
        """
        Returns the conversation if user is an active participant.
        Raises 403 response otherwise.
        Uses select_related to avoid N+1 on participant lookups.
        """
        try:
            # Single query: get conversation + check participation
            participant = ConversationParticipant.objects.select_related(
                "conversation"
            ).get(
                conversation_id=conversation_id,
                user=user,
                is_active=True,
            )
            return participant.conversation
        except ConversationParticipant.DoesNotExist:
            return None

    # ── WebSocket broadcast helper ─────────────────────────────
    def broadcast_to_conversation(self, conversation_id: int, event: dict):
        """
        Push an event to all participants of a conversation
        via Redis channel layer (works across multiple server instances).
        """
        try:
            async_to_sync(channel_layer.group_send)(
                f"dm_{conversation_id}",
                event,
            )
        except Exception as e:
            # Log but don't fail the REST response if WS broadcast fails
            logger.error(f"WebSocket broadcast failed: {e}")

    def broadcast_to_user(self, user_id: int, event: dict):
        """Push an event to a specific user's personal room."""
        try:
            async_to_sync(channel_layer.group_send)(
                f"user_{user_id}",
                event,
            )
        except Exception as e:
            logger.error(f"User broadcast failed: {e}")


# ─────────────────────────────────────────────────────────────
# CONVERSATION LIST + CREATE
# ─────────────────────────────────────────────────────────────
class ConversationListCreateView(BaseMessagingView):
    """
    GET  /api/messages/conversations/
         Returns dashboard list: name, image, last message, time, unread count.
         Filtered to show only: person | channel | group (query param).
         Supports search by contact name/username.

    POST /api/messages/conversations/
         Start a new DM. Idempotent — returns existing if already started.
    """

    def get(self, request):
        """
        Spec: Show all or filter — person, channel, group.
        Here we handle 'person' (DM conversations).
        Channels/groups have their own endpoints.

        Optimisation strategy:
        1. Get all conversation IDs the user is in (1 query)
        2. Prefetch participants + their users (avoids N+1)
        3. Prefetch pinned IDs and attach to request for serializer use
        """
        user = request.user

        # ── Get all active conversations for this user ────────────
        # We go through ConversationParticipant to find conversations
        my_conv_ids = ConversationParticipant.objects.filter(
            user=user,
            is_active=True,
        ).values_list("conversation_id", flat=True)

        # ── Exclude archived conversations ────────────────────────
        archived_ids = ArchivedConversation.objects.filter(
            user=user
        ).values_list("conversation_id", flat=True)

        # ── Search by username or contact name ────────────────────
        search = request.query_params.get("q", "").strip()

        # ── Base queryset with prefetches ─────────────────────────
        # Prefetch 'participants' with their users in one query
        conversations = Conversation.objects.filter(
            id__in=my_conv_ids,#❔what does this id__in doing here how it works 
        ).exclude(
            id__in=archived_ids,
        ).prefetch_related(#❔why not select_related used here why prefetch_related?
            Prefetch(
                "participants",
                # Prefetch ALL participants with their user profiles
                queryset=ConversationParticipant.objects.select_related("user").filter(
                    is_active=True
                ),
                to_attr="prefetched_participants",  # attach as attribute
            )
        ).order_by("-updated_at")

        # ── Search filter: filter by other person's username ──────
        if search:
            conversations = conversations.filter(
                participants__user__username__icontains=search,
            ).exclude(
                participants__user=user    # don't match self
            ).distinct()

        # ── Attach pinned IDs to request for serializer use ───────
        # Serializer reads request.pinned_conversation_ids to check is_pinned
        # This is ONE query instead of N queries (one per conversation)
        pinned_ids = set(
            PinnedItem.objects.filter(
                user=user,
                item_type=PinnedItem.ItemType.CONVERSATION,
            ).values_list("item_id", flat=True)
        )
        request.pinned_conversation_ids = pinned_ids    # attach to request

        # ── Pagination ────────────────────────────────────────────
        paginator = StandardPagePagination()
        page = paginator.paginate_queryset(conversations, request)

        serializer = ConversationListSerializer(
            page,
            many=True,
            context={"request": request},
        )
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        """
        Start a new DM conversation.
        POST body: { "username": "..." } or { "phone": "..." }
        """
        serializer = CreateConversationSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        conversation, created = serializer.save()

        response_serializer = ConversationDetailSerializer(
            conversation,
            context={"request": request},
        )
        return self.created(
            response_serializer.data,
            message="Conversation started." if created else "Existing conversation returned.",
        )




























