

"""
messageapp/models.py

Covers:
  - Conversation          (1-to-1 DM thread)
  - ConversationParticipant (per-user settings: mute, last_read)
  - Message               (DM + Group, unified table)
  - MessageFile           (attachments — one message can have many files)
  - MessageStatus         (delivered / read receipts per recipient)
  - PinnedMessage         (pinned inside a chat box)
  - PinnedItem            (dashboard pins — max 5 per user)
  - ArchivedConversation  (soft-archive per user)

Design decisions:
  - Messages table is unified: has conversation_id OR group_id (enforced by CHECK constraint)
  - No plaintext stored — content_encrypted is the E2E ciphertext blob
  - Soft delete: is_deleted flag, deleted_for_everyone flag
  - All PKs are BIGINT (BigAutoField) for scale
"""
from authapp import serializers
from django.db import models
from django.conf import settings
from django.utils import timezone


# ─────────────────────────────────────────────────────────────
# CONVERSATION  (1-to-1 Direct Message thread)
# ─────────────────────────────────────────────────────────────
class Conversation(models.Model):
    """
    A private 1-to-1 thread between exactly two users.

    Why no 'type' column?
    Groups live in groupapp.Group, channels in channelapp.Channel.
    This table is ONLY for DMs — the type is implied by the table itself.

    updated_at is indexed and used to sort the home dashboard
    (most recently active conversation appears first).
    """

    # The user who initiated the conversation
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_conversations",
        db_index=True,
    )

    # Denormalized: last message text for dashboard preview
    # Avoids a JOIN + subquery on every dashboard load
    last_message_preview = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Cached last message snippet for dashboard list — updated on every new message",
    )
    last_message_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,    # sorted by this on dashboard
        help_text="Timestamp of the last message — used for ordering",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    # updated_at changes on every new message → drives dashboard ordering
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = "conversations"
        ordering = ["-updated_at"]    # newest activity first by default
        indexes = [
            models.Index(fields=["-updated_at"]),
            models.Index(fields=["created_by"]),
        ]

    def __str__(self):
        return f"Conversation #{self.pk} by {self.created_by.username}"

    def update_last_message(self, message_text: str, timestamp):
        """
        Called every time a new message is saved.
        Updates the denormalized preview so the dashboard
        doesn't need a subquery on every page load.
        """
        # Truncate to 200 chars for the preview
        self.last_message_preview = message_text[:200] if message_text else "Attachment"
        self.last_message_at = timestamp
        # save only the two columns that changed — efficient UPDATE
        self.save(update_fields=["last_message_preview", "last_message_at", "updated_at"])














