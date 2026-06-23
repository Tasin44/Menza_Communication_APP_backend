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

# ─────────────────────────────────────────────────────────────
# CONVERSATION DETAIL
# ─────────────────────────────────────────────────────────────
class ConversationDetailView(BaseMessagingView):
    """
    GET    /api/messages/conversations/<id>/
           Full conversation info: other person, pinned messages, media.

    DELETE /api/messages/conversations/<id>/
           Soft-delete this conversation for the requesting user only.
           (Other participant is not affected.)
    """

    def get(self, request, pk):
        conversation = self.get_conversation_or_403(pk, request.user)
        if not conversation:
            return self.not_found("Conversation not found.")

        serializer = ConversationDetailSerializer(
            conversation,
            context={"request": request},
        )
        return self.ok(serializer.data)

    def delete(self, request, pk):
        """
        Soft-delete: marks the participant row as inactive.
        The conversation still exists for the other person.
        """
        try:
            participant = ConversationParticipant.objects.get(
                conversation_id=pk,
                user=request.user,
                is_active=True,
            )
        except ConversationParticipant.DoesNotExist:
            return self.not_found("Conversation not found.")

        participant.is_active = False
        participant.left_at = timezone.now()
        participant.save(update_fields=["is_active", "left_at"])

        return Response(
            {"success": True, "message": "Conversation deleted."},
            status=status.HTTP_204_NO_CONTENT,
        )



# ─────────────────────────────────────────────────────────────
# CONVERSATION ACTIONS  (mute / block / archive)
# ─────────────────────────────────────────────────────────────
class ConversationActionView(BaseMessagingView):
    """
    POST /api/messages/conversations/<id>/mute/
    POST /api/messages/conversations/<id>/unmute/
    POST /api/messages/conversations/<id>/block/
    POST /api/messages/conversations/<id>/archive/
    POST /api/messages/conversations/<id>/unarchive/

    All are per-user actions — don't affect the other participant.
    """

    def post(self, request, pk, action):
        """
        Dispatch to appropriate action handler.
        Using a dispatch dict instead of long if/elif.
        """
        dispatch = {
            "mute": self._mute,
            "unmute": self._unmute,
            "block": self._block,
            "archive": self._archive,
            "unarchive": self._unarchive,
        }
        handler = dispatch.get(action)
        if not handler:
            return self.bad_request({}, f"Unknown action: {action}")

        return handler(request, pk)

    def _mute(self, request, pk):
        """Mute notifications for this conversation."""
        serializer = MuteConversationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            participant = ConversationParticipant.objects.get(
                conversation_id=pk,
                user=request.user,
            )
        except ConversationParticipant.DoesNotExist:
            return self.not_found()

        participant.mark_muted(until=serializer.validated_data.get("muted_until"))
        return self.ok({}, "Conversation muted.")

    def _unmute(self, request, pk):
        try:
            participant = ConversationParticipant.objects.get(
                conversation_id=pk,
                user=request.user,
            )
        except ConversationParticipant.DoesNotExist:
            return self.not_found()

        participant.unmute()
        return self.ok({}, "Conversation unmuted.")



    def _archive(self, request, pk):
        """Archive conversation — hides from main list but keeps history."""
        conversation = self.get_conversation_or_403(pk, request.user)
        if not conversation:
            return self.not_found()

        ArchivedConversation.objects.get_or_create(
            user=request.user,
            conversation=conversation,
        )
        return self.ok({}, "Conversation archived.")

    def _unarchive(self, request, pk):
        ArchivedConversation.objects.filter(
            user=request.user,
            conversation_id=pk,
        ).delete()
        return self.ok({}, "Conversation unarchived.")


# ─────────────────────────────────────────────────────────────
# MESSAGE LIST  (chat page — paginated)
# ─────────────────────────────────────────────────────────────
class MessageListView(BaseMessagingView):
    """
    GET /api/messages/conversations/<id>/messages/

    Returns paginated messages for a conversation.
    Newest first (cursor pagination), client scrolls up for older.

    Uses heavy prefetching to avoid N+1 on nested serializer fields:
      - files (MessageFile)
      - reactions (MessageReaction → user)
      - statuses (MessageStatus)
      - reply_to (Message → sender)
    """

    def get(self, request, pk):
        # ── Authorization ─────────────────────────────────────────
        conversation = self.get_conversation_or_403(pk, request.user)
        if not conversation:
            return self.not_found("Conversation not found.")

        # ── Queryset with all prefetches ──────────────────────────
        messages = Message.objects.filter(#❔how it is working 
            conversation=conversation,
            # Deleted for everyone → hide completely
            # Deleted for me → still visible to others
            # We handle "deleted for me" on client side using local storage
        ).filter(
            # Exclude messages deleted for everyone
            Q(is_deleted=False) | Q(deleted_for_everyone=False)
        ).select_related(
            "sender",          # JOIN users → avoids N+1 on sender profile
            "reply_to__sender",  # JOIN messages JOIN users for reply preview
        ).prefetch_related(
            Prefetch(
                "files",
                queryset=MessageFile.objects.all(),
                to_attr="prefetched_files",   # used by MessageFileSerializer
            ),
            Prefetch(
                "reactions",
                queryset=MessageReaction.objects.select_related("user"),
                to_attr="prefetched_reactions",
            ),
            Prefetch(
                "statuses",
                queryset=MessageStatus.objects.all(),
                to_attr="prefetched_statuses",  # used by is_read_by_me
            ),
        )

        # ── Optional: search inside conversation ──────────────────
        search = request.query_params.get("q", "").strip()
        if search:
            # Full-text search on message_text index
            # For E2E encrypted messages this is limited — add search index separately
            messages = messages.filter(
                system_text__icontains=search
            )

        # ── Cursor pagination ─────────────────────────────────────
        paginator = MessageCursorPagination()
        page = paginator.paginate_queryset(messages, request)

        serializer = MessageSerializer(
            page,
            many=True,
            context={"request": request},
        )

        # ── Mark messages as delivered on fetch ───────────────────
        # When user loads messages, mark them all as delivered
        # (read happens when they actually view them — tracked by WS)
        self._bulk_mark_delivered(conversation, request.user)

        return paginator.get_paginated_response(serializer.data)

    def _bulk_mark_delivered(self, conversation, user):
        """
        Mark all undelivered messages in this conversation as delivered.
        Single UPDATE query — not N updates.
        """
        MessageStatus.objects.filter(
            message__conversation=conversation,
            recipient=user,
            is_delivered=False,
        ).update(is_delivered=True, delivered_at=timezone.now())



# ─────────────────────────────────────────────────────────────
# SEND MESSAGE
# ─────────────────────────────────────────────────────────────
class SendMessageView(BaseMessagingView):
    """
    POST /api/messages/send/

    Creates a message in DB then broadcasts via WebSocket.
    Supports: text, voice, image, video, file, pdf, document.
    Supports: replies (reply_to_id).
    Supports: multiple file attachments.
    """

    @transaction.atomic#❔whats the impact of this here why used 
    def post(self, request):
        serializer = SendMessageSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)#❔sendmessageserializer has two method validate and validate_files, then which one is calling by is_valid here ?
        message = serializer.save()

        # ── Reload with all related data for response ─────────────
        message = Message.objects.select_related(#❔how it's working why used here 
            "sender",
            "reply_to__sender",
        ).prefetch_related(
            "files", "reactions", "statuses"
        ).get(id=message.id)

        # ── Broadcast via WebSocket ───────────────────────────────
        # All participants in the conversation room receive this
        if message.conversation_id:#❔how does this below part working 
            self.broadcast_to_conversation(
                message.conversation_id,
                {
                    "type": "chat.message",   # maps to chat_message in consumer
                    "message_id": message.id,
                    "sender_id": request.user.id,
                    "sender_username": request.user.username,
                    "sender_image": request.user.profile_image,
                    "content_encrypted": message.content_encrypted,
                    "message_type": message.message_type,
                    "files": [
                        {"file_url": f.file_url, "media_type": f.media_type}
                        for f in message.files.all()
                    ],
                    "reply_to_id": message.reply_to_id,
                    "sent_at": message.sent_at.isoformat(),
                },
            )

            # ── Push notification to offline users ────────────────
            # Get recipients who are NOT currently in the WS room
            # They'll receive a push notification via FCM/APNs
            self._send_push_notifications(message)

        response_serializer = MessageSerializer(
            message,
            context={"request": request},
        )
        return self.created(response_serializer.data, "Message sent.")

    def _send_push_notifications(self, message):
        """
        Send push notifications to offline recipients.
        TODO: Integrate with FCM (Android) / APNs (iOS) via celery task.
        """
        # Placeholder — wire up your push notification service here
        # from notifications.tasks import send_push_notification
        # recipients = MessageStatus.objects.filter(message=message, is_delivered=False)
        # for r in recipients:
        #     send_push_notification.delay(r.recipient_id, "New message")
        pass


# ─────────────────────────────────────────────────────────────
# MESSAGE DETAIL  (single message — get / delete)
# ─────────────────────────────────────────────────────────────
class MessageDetailView(BaseMessagingView):
    """
    GET    /api/messages/<id>/           → get a single message
    DELETE /api/messages/<id>/           → delete (for me or for everyone)
    DELETE /api/messages/<id>/?for_everyone=true  → delete for everyone
    """

    def _get_message_if_authorized(self, message_id, user):
        """
        Fetch message and verify user is a participant in its conversation/group.
        Returns (message, None) on success, (None, error_response) on failure.
        """
        try:
            message = Message.objects.select_related(
                "sender",
                "conversation",
            ).get(id=message_id, is_deleted=False)
        except Message.DoesNotExist:
            return None, self.not_found("Message not found.")

        # Verify participation
        if message.conversation_id:
            is_participant = ConversationParticipant.objects.filter(
                conversation_id=message.conversation_id,
                user=user,
                is_active=True,
            ).exists()
            if not is_participant:
                return None, self.forbidden()#❔

        return message, None

    def get(self, request, pk):
        message, err = self._get_message_if_authorized(pk, request.user)
        if err:
            return err
        serializer = MessageSerializer(message, context={"request": request})
        return self.ok(serializer.data)

    def delete(self, request, pk):
        message, err = self._get_message_if_authorized(pk, request.user)
        if err:
            return err

        for_everyone = request.query_params.get("for_everyone", "").lower() == "true"

        # ── Only sender can delete for everyone ───────────────────
        if for_everyone and message.sender != request.user:
            return self.forbidden("Only the sender can delete a message for everyone.")

        # ── Time limit: can only delete for everyone within 1 hour ─
        if for_everyone:
            age = (timezone.now() - message.sent_at).total_seconds()#❔explain 
            if age > 3600:    # 1 hour in seconds
                return self.bad_request(
                    {}, "You can only delete a message for everyone within 1 hour."
                )

        message.soft_delete(for_everyone=for_everyone)

        # ── Broadcast deletion via WebSocket ──────────────────────
        if message.conversation_id:
            self.broadcast_to_conversation(
                message.conversation_id,
                {
                    "type": "message.deleted",
                    "message_id": message.id,
                    "deleted_by": request.user.id,
                    "for_everyone": for_everyone,
                },
            )

        return Response(
            {"success": True, "message": "Message deleted."},
            status=status.HTTP_204_NO_CONTENT,
        )



# ─────────────────────────────────────────────────────────────
# MESSAGE SEARCH
# ─────────────────────────────────────────────────────────────
class MessageSearchView(BaseMessagingView):
    """
    GET /api/messages/conversations/<id>/search/?q=<text>

    Spec: User can search text messages of a conversation.
    NOTE: For E2E encrypted messages, full-text search is limited server-side.
    This searches system_text (system messages) and indexes if provided.
    True search in E2E apps is done client-side after decryption.
    """

    def get(self, request, pk):
        conversation = self.get_conversation_or_403(pk, request.user)
        if not conversation:
            return self.not_found()#❔how does this not_found working is it built in

        query = request.query_params.get("q", "").strip()#❔
        if len(query) < 2:
            return self.bad_request({}, "Search query must be at least 2 characters.")

        # Search only in system_text (unencrypted) for now
        # For real E2E apps: client downloads messages and searches locally
        messages = Message.objects.filter(
            conversation=conversation,
            is_deleted=False,
            system_text__icontains=query,
        ).select_related("sender").order_by("-sent_at")[:50]

        serializer = MessageSerializer(messages, many=True, context={"request": request})
        return self.ok({
            "query": query,
            "count": len(serializer.data),
            "results": serializer.data,
        })


# ─────────────────────────────────────────────────────────────
# READ RECEIPT  (REST fallback — WebSocket is primary)
# ─────────────────────────────────────────────────────────────
class MessageReadReceiptView(BaseMessagingView):
    """
    POST /api/messages/conversations/<id>/read/
    Body: { "last_message_id": <id> }

    Marks all messages up to last_message_id as read.
    REST fallback for when WebSocket is not connected.
    """

    @transaction.atomic
    def post(self, request, pk):
        conversation = self.get_conversation_or_403(pk, request.user)
        if not conversation:
            return self.not_found()

        last_message_id = request.data.get("last_message_id")
        if not last_message_id:
            return self.bad_request({}, "last_message_id is required.")

        now = timezone.now()

        # ── Bulk update: mark all statuses up to this message ─────
        updated = MessageStatus.objects.filter(
            message__conversation=conversation,
            message__id__lte=last_message_id,    # all messages up to here
            recipient=request.user,
            is_read=False,
        ).update(
            is_read=True,
            read_at=now,
            is_delivered=True,
            delivered_at=now,
        )

        # ── Update last_read_message_id on participant ─────────────
        ConversationParticipant.objects.filter(
            conversation=conversation,
            user=request.user,
        ).update(last_read_message_id=last_message_id)

        # ── Notify sender via WebSocket ───────────────────────────
        self.broadcast_to_conversation(
            pk,
            {
                "type": "message.read",
                "message_id": last_message_id,
                "read_by_user_id": request.user.id,
                "read_at": now.isoformat(),
            },
        )

        return self.ok({"messages_marked_read": updated})




# ─────────────────────────────────────────────────────────────
# EMOJI REACTION
# ─────────────────────────────────────────────────────────────
class MessageReactionView(BaseMessagingView):
    """
    POST   /api/messages/<id>/react/       → add/update reaction { emoji }
    DELETE /api/messages/<id>/react/       → remove reaction
    """

    def _get_message(self, pk, user):
        """Get message and verify user is in its conversation."""
        try:
            msg = Message.objects.select_related("conversation").get(
                id=pk, is_deleted=False
            )
        except Message.DoesNotExist:
            return None, self.not_found()

        if msg.conversation_id:
            allowed = ConversationParticipant.objects.filter(
                conversation_id=msg.conversation_id,
                user=user,
                is_active=True,
            ).exists()
            if not allowed:
                return None, self.forbidden()

        return msg, None

    def post(self, request, pk):
        message, err = self._get_message(pk, request.user)
        if err:
            return err

        serializer = MessageReactionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reaction, created = serializer.save(message=message, user=request.user)

        # ── Broadcast reaction via WebSocket ──────────────────────
        if message.conversation_id:
            self.broadcast_to_conversation(
                message.conversation_id,
                {
                    "type": "message.reaction",
                    "message_id": message.id,
                    "user_id": request.user.id,
                    "emoji": reaction.emoji,
                },
            )

        return self.created(
            {"emoji": reaction.emoji},
            "Reaction added." if created else "Reaction updated.",
        )

    def delete(self, request, pk):
        message, err = self._get_message(pk, request.user)
        if err:
            return err

        deleted, _ = MessageReaction.objects.filter(#❔why deleted, _ sometimes message,err used?how to know which one I've to use 
            message=message,
            user=request.user,
        ).delete()

        if not deleted:
            return self.not_found("You haven't reacted to this message.")

        return Response(
            {"success": True, "message": "Reaction removed."},
            status=status.HTTP_204_NO_CONTENT,
        )






