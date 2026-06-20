

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















