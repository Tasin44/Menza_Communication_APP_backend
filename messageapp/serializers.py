

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



# ─────────────────────────────────────────────────────────────
# SEND MESSAGE  (write serializer — lean and flat)
# ─────────────────────────────────────────────────────────────
class SendMessageSerializer(BlockCheckMixin, serializers.Serializer):
    """
    Write serializer for sending a new message.
    Kept flat (no nesting) for easy POST body.

    Supports:
      - DM: provide conversation_id
      - Group: provide group_id
      - File attachments: list of file objects
      - Reply: provide reply_to_id
    """

    # Context: DM or group — exactly one required
    conversation_id = serializers.IntegerField(required=False, allow_null=True)
    group_id = serializers.IntegerField(required=False, allow_null=True)

    # Reply to an existing message
    reply_to_id = serializers.IntegerField(required=False, allow_null=True)

    message_type = serializers.ChoiceField(
        choices=Message.MessageType.choices,
        default=Message.MessageType.TEXT,
    )
    # E2E ciphertext — required for text/voice, optional if it's a file-only message
    content_encrypted = serializers.CharField(required=False, allow_blank=True)

    # File attachments: list of {file_url, file_name, file_size, media_type, duration_seconds?}
    files = serializers.ListField(
        child=serializers.DictField(),#❔explain this , why ListField used, why child inside it
        required=False,
        default=list,
        help_text="List of file objects with file_url, media_type, etc.",
    )

    def validate(self, attrs):#❔is attrs a list ? what is place I'm passing attrs on the validate method?where I'm calling this 
        """
        Top-level cross-field validation.
        Ensures exactly one of conversation_id or group_id is provided.
        Validates the user is a participant in the target.
        """
        conversation_id = attrs.get("conversation_id")
        group_id = attrs.get("group_id")

        # ── Rule 1: exactly one context ──────────────────────────
        if not conversation_id and not group_id:
            raise serializers.ValidationError(
                "Provide either conversation_id or group_id."
            )
        if conversation_id and group_id:
            raise serializers.ValidationError(
                "Provide only one of conversation_id or group_id, not both."
            )

        # ── Rule 2: must have some content ───────────────────────
        has_content = attrs.get("content_encrypted", "").strip()#❔why not i'm using bool here , like I did on has_files
        has_files = bool(attrs.get("files"))
        if not has_content and not has_files:
            raise serializers.ValidationError(
                "Message must have content or at least one file."
            )

        sender = self.context["request"].user

        # ── Rule 3: validate DM conversation ─────────────────────
        if conversation_id:
            try:
                conversation = Conversation.objects.get(id=conversation_id)
            except Conversation.DoesNotExist:
                raise serializers.ValidationError({"conversation_id": "Conversation not found."})

            # Check sender is a participant
            is_participant = ConversationParticipant.objects.filter(
                conversation=conversation,
                user=sender,
                is_active=True,
            ).exists()
            if not is_participant:
                raise serializers.ValidationError(
                    {"conversation_id": "You are not part of this conversation."}
                )

            # Check block status between participants
            # Get the OTHER participant
            other_participant = ConversationParticipant.objects.filter(
                conversation=conversation,
                is_active=True,
            ).exclude(user=sender).select_related("user").first()

            if other_participant:
                self._check_not_blocked(sender, other_participant.user)#why _ used before _check_not_blocked, is it means protected?

            attrs["_conversation"] = conversation
            attrs["_group"] = None#for conversation , group will be none 

        # ── Rule 4: validate group membership ────────────────────
        if group_id:
            try:
                from groupapp.models import Group, GroupMember
                group = Group.objects.get(id=group_id)
            except Exception:
                raise serializers.ValidationError({"group_id": "Group not found."})

            is_member = GroupMember.objects.filter(
                group=group,
                user=sender,
            ).exists()
            if not is_member:
                raise serializers.ValidationError(
                    {"group_id": "You are not a member of this group."}
                )

            attrs["_group"] = group
            attrs["_conversation"] = None

        # ── Rule 5: validate reply_to ─────────────────────────────
        reply_to_id = attrs.get("reply_to_id")
        if reply_to_id:
            try:
                reply_msg = Message.objects.get(
                    id=reply_to_id,
                    is_deleted=False,
                )
                attrs["_reply_to"] = reply_msg
            except Message.DoesNotExist:
                raise serializers.ValidationError(
                    {"reply_to_id": "Message to reply to not found."}
                )
        else:
            attrs["_reply_to"] = None

        return attrs

    def validate_files(self, files):
        """Validate each file object has required fields."""
        for f in files:
            if "file_url" not in f:
                raise serializers.ValidationError(
                    "Each file must have a file_url."
                )
            if "media_type" not in f:
                raise serializers.ValidationError(
                    "Each file must have a media_type."
                )
            # if not f["file_url"].startswith("https://"):
            #     raise serializers.ValidationError(
            #         "file_url must be a valid HTTPS URL (upload to S3/R2 first)."
            #     )
        return files

    @transaction.atomic
    def save(self):
        """
        Creates the Message + MessageFile rows + MessageStatus rows.
        Uses atomic transaction so partial saves don't corrupt data.
        """
        data = self.validated_data#❔from where this validated_data coming, I can't see any validated_data name field here 
        sender = self.context["request"].user

        # ── Create the message ────────────────────────────────────
        message = Message.objects.create(
            conversation=data["_conversation"],
            group=data["_group"],
            sender=sender,
            reply_to=data.get("_reply_to"),
            message_type=data["message_type"],
            content_encrypted=data.get("content_encrypted", ""),
        )

        # ── Create file attachments ───────────────────────────────
        file_objs = []
        for f in data.get("files", []):
            file_objs.append(MessageFile(
                message=message,
                file_name=f.get("file_name", ""),
                file_url=f["file_url"],
                file_size=f.get("file_size"),
                media_type=f["media_type"],
                duration_seconds=f.get("duration_seconds"),
            ))
        if file_objs:
            # bulk_create: one INSERT for all files, not N inserts
            MessageFile.objects.bulk_create(file_objs)

        # ── Create MessageStatus rows (delivery tracking) ─────────
        # For DM: one status row for the other participant
        # For group: one row per member excluding sender
        status_objs = []
        if data["_conversation"]:
            # Get the other participant(s)
            recipients = ConversationParticipant.objects.filter(
                conversation=data["_conversation"],
                is_active=True,
            ).exclude(user=sender).values_list("user_id", flat=True)#❔why values_list used here , flat true means?

            for recipient_id in recipients:
                status_objs.append(MessageStatus(
                    message=message,
                    recipient_id=recipient_id,
                ))

        elif data["_group"]:
            from groupapp.models import GroupMember
            recipients = GroupMember.objects.filter(
                group=data["_group"],
            ).exclude(user=sender).values_list("user_id", flat=True)

            for recipient_id in recipients:
                status_objs.append(MessageStatus(
                    message=message,
                    recipient_id=recipient_id,
                ))

        if status_objs:
            # bulk_create: one INSERT regardless of group size
            MessageStatus.objects.bulk_create(status_objs)

        # ── Update conversation's last_message cache ───────────────
        if data["_conversation"]:#❔from where this _conversation coming?I see the db table name conversations not conversation
            preview_text = data.get("content_encrypted", "")
            data["_conversation"].update_last_message(preview_text, message.sent_at)#update_last_message is the Conversations table method 

        return message
 


# ─────────────────────────────────────────────────────────────
# CONVERSATION PARTICIPANT  (mute state per user)
# ─────────────────────────────────────────────────────────────
class ConversationParticipantSerializer(serializers.ModelSerializer):
    """Serializes participant info including their mute state."""

    user = SenderSerializer(read_only=True)

    class Meta:
        model = ConversationParticipant
        fields = [
            "id",
            "user",
            "is_muted",
            "muted_until",
            "last_read_message_id",
            "is_active",
            "joined_at",
        ]
        read_only_fields = ["id", "user", "joined_at"]




# ─────────────────────────────────────────────────────────────
# CONVERSATION LIST  (dashboard — minimal, fast)
# ─────────────────────────────────────────────────────────────
class ConversationListSerializer(serializers.ModelSerializer):
    """
    Used on the home dashboard to show the conversation list.
    Spec fields: other person's name, image, last message, last message time.

    Kept MINIMAL — no nested messages — to keep the list fast.
    Uses denormalized last_message_preview/last_message_at on the model
    so no subquery is needed.

    other_person is computed via SerializerMethodField because
    'the other person' depends on who the requesting user is.
    """

    other_person = serializers.SerializerMethodField()
    last_message = serializers.CharField(#❔is it fetching last_message_preview data from the model?
        source="last_message_preview",
        read_only=True,
    )
    last_message_time = serializers.DateTimeField(
        source="last_message_at",
        read_only=True,
    )
    # Is this conversation muted by the requesting user?
    is_muted = serializers.SerializerMethodField()
    # Is this pinned to the dashboard by the requesting user?
    is_pinned = serializers.SerializerMethodField()
    # Unread count for this user
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "other_person",
            "last_message",
            "last_message_time",
            "is_muted",
            "is_pinned",
            "unread_count",
            "updated_at",
        ]
        read_only_fields = fields
    
    #❔what does _get_my_participant and _get_other_participant doing here? I think _get_my_participant is the thing which is fetching all the person that this user talked, then what does _get_other_participant doing here?
    def _get_my_participant(self, obj):
        """
        Get the ConversationParticipant row for the requesting user.
        Uses prefetched participants to avoid a DB query per conversation.
        #❔how it's working? and how avoiding db query per conversation?
        """
        request = self.context.get("request")
        if not request:
            return None
        user = request.user
        # participants are prefetched in the view as 'prefetched_participants'
        if hasattr(obj, "prefetched_participants"):#❔from where this prefetched_participants coming?
            for p in obj.prefetched_participants:
                if p.user_id == user.id:
                    return p
        # Fallback
        return ConversationParticipant.objects.filter(
            conversation=obj, user=user
        ).first()

    def _get_other_participant(self, obj):
        """Get the OTHER participant (not the requesting user)."""
        request = self.context.get("request")
        if not request:
            return None
        user = request.user
        if hasattr(obj, "prefetched_participants"):
            for p in obj.prefetched_participants:
                if p.user_id != user.id:
                    return p
        return ConversationParticipant.objects.filter(
            conversation=obj
        ).exclude(user=user).select_related("user").first()

    def get_other_person(self, obj):
        """Return the other participant's public profile."""
        other = self._get_other_participant(obj)
        if not other or not other.user:
            return None
        return {
            "id": other.user.id,
            "username": other.user.username,
            "profile_image": other.user.profile_image,
        }

    def get_is_muted(self, obj):
        """Is this conversation muted by the current user?"""
        participant = self._get_my_participant(obj)
        if not participant:
            return False
        # Check timed mute expiry
        from django.utils import timezone
        if participant.is_muted and participant.muted_until:
            if participant.muted_until < timezone.now():
                # Mute has expired — update lazily (don't block the response)
                ConversationParticipant.objects.filter(
                    pk=participant.pk#❔why it is used? I've seen participant is not pk in ConversationParticipant talbe 
                ).update(is_muted=False, muted_until=None)
                return False
        return participant.is_muted

    def get_is_pinned(self, obj):
        """Is this conversation pinned to the dashboard by the current user?"""
        request = self.context.get("request")
        if not request:
            return False
        # Uses prefetched pinned_ids on the view's queryset if available
        if hasattr(request, "pinned_conversation_ids"):#❔from where this pinned_conversation_ids coming?
            return obj.id in request.pinned_conversation_ids
        return PinnedItem.objects.filter(
            user=request.user,
            item_type=PinnedItem.ItemType.CONVERSATION,
            item_id=obj.id,
        ).exists()

    def get_unread_count(self, obj):
        """
        Count of messages the current user hasn't read yet.
        Algorithm:
          SELECT COUNT(*) FROM messages m
          JOIN message_status ms ON ms.message_id = m.id
          WHERE m.conversation_id = ? AND ms.recipient_id = ? AND ms.is_read = FALSE
        Uses the last_read_message_id shortcut when possible (O(1) vs O(n)).
        """
        request = self.context.get("request")
        if not request:
            return 0
        participant = self._get_my_participant(obj)
        if not participant:
            return 0

        base_qs = Message.objects.filter(
            conversation=obj,
            is_deleted=False,
        ).exclude(sender=request.user)    # don't count own messages as unread

        # Optimisation: if we know the last read message ID, count only after it
        if participant.last_read_message_id:
            base_qs = base_qs.filter(id__gt=participant.last_read_message_id)#❔how id__gt working here explain 
            return base_qs.count()

        # Fallback: check message_status table
        return MessageStatus.objects.filter(
            message__conversation=obj,
            recipient=request.user,
            is_read=False,
        ).count()





# ─────────────────────────────────────────────────────────────
# CONVERSATION DETAIL  (chat page — full messages)
# ─────────────────────────────────────────────────────────────
class ConversationDetailSerializer(serializers.ModelSerializer):
    """
    Full conversation view for the chat page.
    Spec: shows contact name, image, username, email, last seen, media files.
    Messages are paginated separately via MessageSerializer (not nested here
    to avoid loading thousands of messages in one call).
    """

    other_person = serializers.SerializerMethodField()
    my_settings = serializers.SerializerMethodField()
    pinned_messages = serializers.SerializerMethodField()
    media_files = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "other_person",
            "my_settings",
            "pinned_messages",
            "media_files",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_other_person(self, obj):
        """
        Full profile of the other participant.
        Spec: name, image, username, email, last seen.
        """
        request = self.context.get("request")
        if not request:
            return None
        other_part = ConversationParticipant.objects.filter(
            conversation=obj,
        ).exclude(user=request.user).select_related("user").first()#❔I'm excluding current user, then what does .select_related("user") means?

        if not other_part or not other_part.user:
            return None

        u = other_part.user
        return {
            "id": u.id,
            "username": u.username,
            "email": u.email,           # shown on contact info panel
            "profile_image": u.profile_image,
            "last_seen": u.last_seen,   # shown as "last seen at..."
        }

    def get_my_settings(self, obj):
        """Per-user settings for this conversation (mute, etc.)."""
        request = self.context.get("request")
        if not request:
            return {}
        p = ConversationParticipant.objects.filter(
            conversation=obj, user=request.user
        ).first()#❔why always use first with .filter()
        if not p:
            return {}
        return {
            "is_muted": p.is_muted,
            "muted_until": p.muted_until,
        }

    def get_pinned_messages(self, obj):
        """Messages pinned inside this chat box."""
        pins = PinnedMessage.objects.filter(
            conversation=obj,
        ).select_related("message__sender").order_by("-pinned_at")#❔why I'm using message__sender inside select_related, why not the requested user 
        return [
            {
                "pin_id": p.id,
                "message_id": p.message_id,
                "sender": p.message.sender.username,
                "type": p.message.message_type,
                "pinned_at": p.pinned_at,
            }
            for p in pins
        ]

    def get_media_files(self, obj):
        """
        All media files shared in this conversation.
        Spec: user can see media files on the contact info panel.
        Uses a JOIN to avoid fetching full message objects.
        """
        files = MessageFile.objects.filter(#❔whats happening here , how working 
            message__conversation=obj,
            message__is_deleted=False,
        ).select_related("message").order_by("-created_at")[:50]

        return MessageFileSerializer(files, many=True).data


# ─────────────────────────────────────────────────────────────
# CREATE CONVERSATION  (start a new DM)
# ─────────────────────────────────────────────────────────────
class CreateConversationSerializer(BlockCheckMixin, serializers.Serializer):
    """
    Start a new 1-to-1 conversation.
    Spec: search by username or phone number to find the other user.
    If a conversation already exists between the two users, return it
    (idempotent — no duplicate conversations).
    """

    # Find the other user by username OR phone
    username = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):#❔from where this attrs data is coming?
        username = attrs.get("username", "").strip()
        phone = attrs.get("phone", "").strip()

        if not username and not phone:
            raise serializers.ValidationError(
                "Provide either username or phone number."
            )

        sender = self.context["request"].user

        # ── Find the target user ──────────────────────────────────
        if username:
            try:
                target = User.objects.get(username__iexact=username)
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"username": "No Menza user found with this username."}
                )
        else:
            try:
                target = User.objects.get(phone=phone)
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"phone": "No Menza user found with this phone number."}
                )

        # ── Can't message yourself ────────────────────────────────
        if target == sender:
            raise serializers.ValidationError("You cannot message yourself.")

        # ── Check block status ────────────────────────────────────
        self._check_not_blocked(sender, target)

        attrs["_target"] = target#❔why I'm using _ before target here, how it is helping wokring 
        return attrs

    @transaction.atomic
    def save(self):
        """
        Creates the conversation + 2 participant rows.#❔what does two participant rows means? how it is created 
        Idempotent: if conversation already exists between the two users,
        returns existing one.

        Algorithm: find existing conversation using a subquery that
        finds conversations where BOTH users are participants.
        This is a set-intersection problem:
          Conv where sender is participant AND target is participant.
        """
        sender = self.context["request"].user
        target = self.validated_data["_target"]

        # ── Check for existing conversation (idempotent create) ───
        # Find conversation IDs where sender is a participant
        sender_conv_ids = ConversationParticipant.objects.filter(
            user=sender, is_active=True
        ).values_list("conversation_id", flat=True)#❔why flat true using here , how it values_list working 

        # Among those, find one where target is ALSO a participant
        existing = ConversationParticipant.objects.filter(
            user=target,
            conversation_id__in=sender_conv_ids,
            is_active=True,
        ).select_related("conversation").first()#❔how the select_related is working here 

        if existing:
            # Return the existing conversation — no duplicate created
            return existing.conversation, False   # (conversation, created)#❔why returning false here 

        # ── Create new conversation ───────────────────────────────
        conversation = Conversation.objects.create(created_by=sender)

        # Create participant rows for BOTH users
        ConversationParticipant.objects.bulk_create([
            ConversationParticipant(conversation=conversation, user=sender),
            ConversationParticipant(conversation=conversation, user=target),
        ])

        return conversation, True   # (conversation, created)








