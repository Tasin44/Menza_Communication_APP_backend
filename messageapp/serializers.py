

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




















