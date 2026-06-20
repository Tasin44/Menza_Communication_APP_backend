

from typing import Required
from django.http import request
from django.db import transaction
from django.contrib.auth import get_user_model
from rest_framework import serializers
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

User=get_user_model


# ─────────────────────────────────────────────────────────────
# BASE MIXIN — Blocked user guard
# ─────────────────────────────────────────────────────────────
class BlockCheckMixin:
    """
    Reusable mixin: checks if either user has blocked the other.
    Used in conversation creation and message sending.

    Binary search is NOT used here because blocked_ids is fetched
    as a QuerySet (SQL handles the lookup). If we ever load all
    blocked IDs into memory for a different reason, bisect would apply.
    """

    def _check_not_blocked(self, user_a, user_b):
        """
        Returns True if neither user has blocked the other.
        Raises ValidationError if blocked.
        """
        from authapp.models import BlockedUser
        # One DB query using OR — checks both directions
        is_blocked = BlockedUser.objects.filter(#❔what is models here? what it is pointing?
            # A blocked B  OR  B blocked A
            models.Q(blocker=user_a, blocked=user_b) |
            models.Q(blocker=user_b, blocked=user_a)
        ).exists()

        if is_blocked:
            raise serializers.ValidationError(
                "You cannot message this user."
            )

# ─────────────────────────────────────────────────────────────
# SENDER MINI PROFILE  (nested inside messages)
# ─────────────────────────────────────────────────────────────
class SenderSerializer(serializers.ModelSerializer):
    """
    Lightweight sender info shown on each message bubble.
    Spec: Each message shows the person's image and name.
    Only expose safe public fields — never email or phone here.
    """

    class Meta:
        model = User
        fields = ["id", "username", "profile_image"]
        # All read-only — this is a nested read serializer
        read_only_fields = ["id", "username", "profile_image"]


# ─────────────────────────────────────────────────────────────
# MESSAGE FILE  (nested in MessageSerializer)
# ─────────────────────────────────────────────────────────────
class MessageFileSerializer(serializers.ModelSerializer):
    """
    Serializes file attachments nested inside a message.
    file_url is the S3/R2 URL — frontend uses it to render/download.
    """

    class Meta:
        model = MessageFile
        fields = [
            "id",
            "file_name",
            "file_url",
            "file_size",
            "media_type",
            "duration_seconds",   # for voice notes and videos
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]



# ─────────────────────────────────────────────────────────────
# MESSAGE REACTION  (nested in MessageSerializer)
# ─────────────────────────────────────────────────────────────
class MessageReactionSerializer(serializers.ModelSerializer):
    """Emoji reactions on a message."""

    reactor = SenderSerializer(source="user", read_only=True)#❔isn't it will be receiver, the person who receive message he'll react?

    class Meta:
        model = MessageReaction
        fields = ["id", "emoji", "reactor", "created_at"]
        read_only_fields = ["id", "reactor", "created_at"]



# ─────────────────────────────────────────────────────────────
# REPLY PREVIEW  (lightweight — shown inside a replied message)
# ─────────────────────────────────────────────────────────────
class ReplyPreviewSerializer(serializers.ModelSerializer):
    """
    Minimal info about the message being replied to.
    We show only sender + a content snippet — not the full message tree.
    Avoids infinite nesting (reply → reply → reply...).
    """

    sender = SenderSerializer(read_only=True)
    # Show just a snippet of the original content
    preview = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "sender", "message_type", "preview", "sent_at"]
        read_only_fields = fields

    def get_preview(self, obj):
        """
        For text: first 100 chars of encrypted content identifier.
        For files: show the type label instead.
        NOTE: actual decryption happens client-side — we just show type.
        """
        if obj.message_type == Message.MessageType.TEXT:
            # We don't decrypt — just acknowledge there's a text reply
            return "[Text message]"
        return f"[{obj.get_message_type_display()}]"



# ─────────────────────────────────────────────────────────────
# MESSAGE READ SERIALIZER  (full, with nested data)
# ─────────────────────────────────────────────────────────────
class MessageSerializer(serializers.ModelSerializer):
    """
    Full message representation for the chat page.
    Uses select_related + prefetch_related in the view to avoid N+1.

    Nested fields:
      - sender: profile info
      - files: list of attachments
      - reactions: emoji reactions
      - reply_to: preview of replied message
      - is_read_by_me: personalised to the requesting user
    """

    sender = SenderSerializer(read_only=True)
    files = MessageFileSerializer(many=True, read_only=True)
    reactions = MessageReactionSerializer(many=True, read_only=True)
    reply_to = ReplyPreviewSerializer(read_only=True)

    # Personalised: did the current user read this message?
    is_read_by_me = serializers.SerializerMethodField()
    # How many people have read it (for group read receipts)
    read_count = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "conversation",
            "group",
            "sender",
            "reply_to",
            "message_type",
            "content_encrypted",   # ciphertext — client decrypts#❔what does ciphertext means 
            "system_text",         # only set for system messages
            "is_deleted",
            "deleted_for_everyone",
            "is_pinned",
            "files",
            "reactions",
            "is_read_by_me",
            "read_count",
            "sent_at",
            "updated_at",
        ]
        read_only_fields = fields   # this is a READ serializer only

    def get_is_read_by_me(self, obj):
        """
        Returns True if the current user has read this message.
        Uses prefetched statuses to avoid extra DB queries.
        """
        request = self.context.get("request")
        if not request:
            return False
        user = request.user
        # Use prefetched data if available (set in view)
        if hasattr(obj, "prefetched_statuses"):#❔explain it, how it is working 
            return any(
                s.recipient_id == user.id and s.is_read
                for s in obj.prefetched_statuses
            )
        # Fallback DB query (only hits if prefetch wasn't done)
        return MessageStatus.objects.filter(
            message=obj,
            recipient=user,
            is_read=True,
        ).exists()

    def get_read_count(self, obj):
        """
        For group messages: how many members have read this.
        Uses prefetched statuses.
        """
        if hasattr(obj, "prefetched_statuses"):#❔explain whole part 
            return sum(1 for s in obj.prefetched_statuses if s.is_read)
        return MessageStatus.objects.filter(message=obj, is_read=True).count()




 







