

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
from rest_framework.fields import ModelField
from django.db.models import constraints
from os import read
from operator import truediv
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
    last_message_at = models.DateTimeField(#❔I used db_index=True here, this is why I haven't added this field on meta class indexes = [ ]  am I right? how to kow which field I've to use for indexing
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



# ─────────────────────────────────────────────────────────────
# MESSAGE  (unified DM + Group)
# ─────────────────────────────────────────────────────────────
class Message(models.Model):
    """
    Single table for both DM messages and Group messages.

    EXACTLY ONE of conversation or group must be set.
    The CHECK constraint in the DB enforces this:
        CHECK ((conversation_id IS NULL) != (group_id IS NULL))

    E2E Encryption:
        content_encrypted stores the ciphertext blob only.
        The server NEVER sees plaintext. Keys live on devices.
        For search: we store a server-side search_index (encrypted
        with a server key, not user key) — optional, off by default.

    reply_to is a self-referencing FK for threaded replies.
    """

    class MessageType(models.TextChoices):#❔why I'm using nested class inside message class, what does it called?
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        VOICE = "voice", "Voice Note"
        FILE = "file", "File"
        PDF = "pdf", "PDF"
        DOCUMENT = "document", "Document"
        SYSTEM = "system", "System"    # e.g. "X joined the group"

    # ── Context: belongs to DM OR group, never both ──────────────
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
        db_index=True,
    )
    # group FK — points to groupapp.Group
    # Using string reference to avoid circular import
    group = models.ForeignKey(
        "groupapp.Group",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
        db_index=True,
    )

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_messages",
        db_index=True,
    )

    # ── Threading: reply to another message ──────────────────────
    reply_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
        help_text="The message this is a reply to",
    )

    # ── Content ──────────────────────────────────────────────────
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.TEXT,
    )
    # E2E ciphertext — server never decrypts this
    content_encrypted = models.TextField(
        blank=True,
        help_text="Client-side encrypted message content (ciphertext only)",
    )
    # Optional: plaintext ONLY for system messages (join/leave notifications)
    # because those don't contain user content
    system_text = models.CharField(
        max_length=255,
        blank=True,
        help_text="Used only for system messages (type=system) — plaintext is safe here",
    )

    # ── Soft delete ───────────────────────────────────────────────
    is_deleted = models.BooleanField(default=False)
    # deleted_for_everyone = True → hidden for ALL participants (like WhatsApp)
    deleted_for_everyone = models.BooleanField(default=False)

    # ── Pin ───────────────────────────────────────────────────────
    is_pinned = models.BooleanField(default=False, db_index=True)

    # ── Timestamps ────────────────────────────────────────────────
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "messages"
        ordering = ["sent_at"]    # chronological inside a chat
        indexes = [
            # Composite: fetch all messages in a conversation, ordered by time
            # This is the most frequent query — must be fast
            models.Index(fields=["conversation", "sent_at"]),#❔why I'm passing two field on indexes where sometimes I pass only one field, how to know when to pass single or multiple,
            models.Index(fields=["group", "sent_at"]),
            models.Index(fields=["sender"]),
            models.Index(fields=["is_pinned"]),
        ]
        constraints = [#❔what is this constraint, is it built in?explain this part 
            # Enforce: message belongs to exactly one context
            models.CheckConstraint(
                check=(
                    models.Q(conversation__isnull=True, group__isnull=False) |
                    models.Q(conversation__isnull=False, group__isnull=True)
                ),
                name="message_belongs_to_one_context",
            )
        ]

    def __str__(self):
        ctx = f"Conv#{self.conversation_id}" if self.conversation_id else f"Group#{self.group_id}"
        return f"Message#{self.pk} in {ctx} by {self.sender.username}"

    def soft_delete(self, for_everyone: bool = False):
        """
        Soft-delete a message.
        for_everyone=True → WhatsApp-style delete for all participants.
        for_everyone=False → only hides for the sender (delete for me).
        """
        self.is_deleted = True
        self.deleted_for_everyone = for_everyone
        # Clear content on server so ciphertext is gone too
        if for_everyone:
            self.content_encrypted = ""
        self.save(update_fields=["is_deleted", "deleted_for_everyone", "content_encrypted", "updated_at"])



# ─────────────────────────────────────────────────────────────
# MESSAGE FILE  (attachments — one message → many files)
# ─────────────────────────────────────────────────────────────
class MessageFile(models.Model):
    """
    Stores file attachments for messages.
    One message can have multiple files (e.g. send 3 images at once).

    file_url points to S3/R2 — never stored locally.
    media_type tells the frontend how to render it.
    """

    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        FILE = "file", "File"
        PDF = "pdf", "PDF"
        DOCUMENT = "document", "Document"#❔why here SYSTEM not present, like in message model?

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="files",    # message.files.all()
    )
    file_name = models.CharField(max_length=255, blank=True)
    # URL on S3/R2 — generated by presigned upload on client, sent here
    file_url = models.CharField(max_length=500)
    file_size = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="File size in bytes",
    )
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    # Duration in seconds for voice notes and videos
    duration_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration for audio/video files in seconds",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "message_files"
        indexes = [
            models.Index(fields=["message"]),    # "get all files for this message"
        ]

    def __str__(self):
        return f"{self.media_type}: {self.file_name} (Message#{self.message_id})"


# ─────────────────────────────────────────────────────────────
# MESSAGE REACTION
# ─────────────────────────────────────────────────────────────
class MessageReaction(models.Model):
    """
    Emoji reactions on messages.
    One reaction per user per message (unique_together enforces this).
    """

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="message_reactions",
    )
    # Store the emoji character directly (e.g. "👍", "❤️")
    emoji = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "message_reactions"
        # One reaction per user per message — to change it, DELETE then POST
        unique_together = [("message", "user")]#❔

    def __str__(self):
        return f"{self.user.username} reacted {self.emoji} to Message#{self.message_id}"





# ─────────────────────────────────────────────────────────────
# PINNED MESSAGE  (inside a chat or group)
# ─────────────────────────────────────────────────────────────
class PinnedMessage(models.Model):
    """
    Pin a specific message inside a conversation or group chat box.
    Different from PinnedItem (which pins entire conversations to the dashboard).
    """

    # Context: pinned inside a DM or a group
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pinned_messages",
    )
    group = models.ForeignKey(
        "groupapp.Group",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pinned_messages",
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="pins",
    )
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pinned_messages",
    )
    pinned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pinned_messages"
        indexes = [
            models.Index(fields=["conversation"]),
            models.Index(fields=["group"]),
        ]

    def __str__(self):
        return f"Pinned Message#{self.message_id} by {self.pinned_by.username}"



# ─────────────────────────────────────────────────────────────
# PINNED ITEM  (dashboard — max 5 per user)
# ─────────────────────────────────────────────────────────────
class PinnedItem(models.Model):
    """
    Pins a conversation, group, or channel to the TOP of the dashboard.
    Spec: max 5 pins per user.
    position 1–5, enforced by CHECK constraint + application logic.
    """

    class ItemType(models.TextChoices):
        CONVERSATION = "conversation", "Conversation"
        GROUP = "group", "Group"
        CHANNEL = "channel", "Channel"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pinned_items",
    )
    item_type = models.CharField(max_length=20, choices=ItemType.choices)
    # Generic FK pattern: store the ID of whatever is pinned
    item_id = models.BigIntegerField()
    # Position 1–5 on the dashboard
    position = models.PositiveSmallIntegerField(
        help_text="Pin position 1–5 on the dashboard",
    )
    pinned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pinned_items"
        # Can't pin the same item twice
        unique_together = [("user", "item_type", "item_id")]#❔how it is working
        constraints = [
            # Enforce max position = 5
            models.CheckConstraint(
                check=models.Q(position__gte=1, position__lte=5),
                name="pin_position_1_to_5",
            ),
            # No two items at the same position for the same user
            models.UniqueConstraint(#❔how it is working explain with example
                fields=["user", "position"],
                name="uq_pin_position_per_user",
            ),
        ]
        indexes = [models.Index(fields=["user", "position"])]

    def __str__(self):
        return f"{self.user.username} pinned {self.item_type}#{self.item_id} at position {self.position}"
        