

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
        on_delete=models.CASCADE,#❔is it like when a user deleted, his all conversation will be also deleted, then what will see the other person whom talk with him?
        related_name="created_conversations",
        db_index=True,
    )

    # Denormalized: last message text for dashboard preview
    # Avoids a JOIN + subquery on every dashboard load
    '''
    Conversation has a last_message_preview and last_message_at column — these are denormalized intentionally. Without them, every dashboard list load would need a subquery SELECT MAX(sent_at) FROM messages WHERE conversation_id = ? for every conversation. With them, it's a single column read.
    '''
    last_message_preview = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Cached last message snippet for dashboard list — updated on every new message",
    )
    last_message_at = models.DateTimeField(#❔I used db_index=True here, this is why I haven't added this field on meta class indexes = [ ]  am I right?
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
        indexes = [#❔how the indexing works? why we use indexing
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


# ─────────────────────────────────────────────────────────────
# CONVERSATION PARTICIPANT
# ─────────────────────────────────────────────────────────────
class ConversationParticipant(models.Model):
    """
    Stores per-user settings for a conversation.

    Why a separate table instead of columns on Conversation?
    Because is_muted, last_read_message_id are PER-USER.
    If stored on Conversation, muting would affect both people.

    Two rows per conversation (one per participant).
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,    # if user deleted, keep conversation history
        null=True,
        related_name="conversation_memberships",
    )

    # Per-user mute settings
    is_muted = models.BooleanField(default=False)
    muted_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="NULL means muted forever; a timestamp means muted until that time",
    )

    # For unread count calculation:
    # unread = messages.filter(id__gt=last_read_message_id).count()
    last_read_message_id = models.BigIntegerField(null=True, blank=True)

    # Soft-leave: user deleted the conversation from their view
    # Other participant still sees it
    is_active = models.BooleanField(default=True)

    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "conversation_participants"
        # Each user can only be in a conversation once
        unique_together = [("conversation", "user")]
        indexes = [
            models.Index(fields=["user"]),          # "get all my conversations"
            models.Index(fields=["conversation"]),  # "get all members of this conv"
        ]

    def __str__(self):
        return f"{self.user} in Conversation#{self.conversation_id}"

    def mark_muted(self, until=None):
        """Mute this conversation for this user. until=None means mute forever."""
        self.is_muted = True
        self.muted_until = until
        self.save(update_fields=["is_muted", "muted_until"])

    def unmute(self):
        self.is_muted = False
        self.muted_until = None
        self.save(update_fields=["is_muted", "muted_until"])







































