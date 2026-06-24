"""
channelapp/serializers.py

Same READ/WRITE split convention as messageapp.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from messageapp.serializers import SenderSerializer

from .models import (
    Channel,
    ChannelSubscriber,
    ChannelPost,
    ChannelPostReaction,
    ChannelPostComment,
    ChannelBoostPayment,
)

User = get_user_model()


# ─────────────────────────────────────────────────────────────
# CHANNEL — LIST  (discover / dashboard row)
# ─────────────────────────────────────────────────────────────
class ChannelListSerializer(serializers.ModelSerializer):
    is_subscribed = serializers.SerializerMethodField()

    class Meta:
        model = Channel
        fields = [
            "id", "handle", "name", "logo", "category", "channel_type",
            "subscriber_count", "is_verified", "is_boosted",
            "is_subscribed", "created_at",
        ]
        read_only_fields = fields

    def get_is_subscribed(self, obj):
        # `prefetched_is_subscribed` is set by the view to avoid N+1 — see
        # ChannelListCreateView.get().
        if hasattr(obj, "prefetched_is_subscribed"):
            return obj.prefetched_is_subscribed
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return obj.subscribers.filter(user=request.user).exists()


# ─────────────────────────────────────────────────────────────
# CHANNEL — DETAIL
# ─────────────────────────────────────────────────────────────
class ChannelDetailSerializer(serializers.ModelSerializer):
    owner = SenderSerializer(source="created_by", read_only=True)
    is_subscribed = serializers.SerializerMethodField()
    unique_viewers = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Channel
        fields = [
            "id", "handle", "name", "logo", "banner", "description",
            "category", "channel_type", "owner", "subscriber_count",
            "unique_viewers", "is_verified", "external_links",
            "is_boosted", "boost_expires_at", "is_subscribed", "is_owner",
            "discoverable_consented_at", "created_at",
        ]
        read_only_fields = fields

    def get_is_subscribed(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return obj.subscribers.filter(user=request.user).exists()

    def get_unique_viewers(self, obj):
        return obj.views.count()

    def get_is_owner(self, obj):
        request = self.context.get("request")
        return bool(request and obj.created_by_id == request.user.id)

# ─────────────────────────────────────────────────────────────
# CREATE / UPDATE CHANNEL
# ─────────────────────────────────────────────────────────────
class CreateChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Channel
        fields = ["id", "handle", "name", "logo", "description", "category"]
        read_only_fields = ["id"]
        # channel_type is intentionally excluded here — every new channel
        # starts PRIVATE; going PUBLIC is a separate, consent-gated action
        # via ToggleDiscoverableView (see views.py).

    def validate_handle(self, value):
        value = value.lstrip("@").lower()
        if not value.isidentifier() and not value.replace("_", "").isalnum():
            raise serializers.ValidationError("Handle may contain only letters, numbers, and underscores.")
        if Channel.objects.filter(handle__iexact=value).exists():
            raise serializers.ValidationError("This handle is already taken.")
        return value

    def create(self, validated_data):
        owner = self.context["request"].user
        channel = Channel.objects.create(created_by=owner, **validated_data)
        # Owner auto-subscribes to their own channel (so it shows in
        # their dashboard list immediately).
        ChannelSubscriber.subscribe(channel, owner)
        return channel
class ToggleDiscoverableSerializer(serializers.Serializer):
    """
    Spec: "Confirmation modal: user taps 'I understand this channel will
    be public' before toggle saves." `confirmed` must be explicitly True —
    we refuse to flip the flag on an implicit/default value.
    """

    confirmed = serializers.BooleanField()

    def save(self, channel: Channel):
        if not self.validated_data["confirmed"]:
            raise serializers.ValidationError(
                "You must confirm that channel content will become public."
            )
        channel.make_discoverable()
        return channel

# ─────────────────────────────────────────────────────────────
# POST REACTION / COMMENT  (nested in PostSerializer)
# ─────────────────────────────────────────────────────────────
class ChannelPostReactionSerializer(serializers.ModelSerializer):
    reactor = SenderSerializer(source="user", read_only=True)

    class Meta:
        model = ChannelPostReaction
        fields = ["id", "emoji", "reactor", "created_at"]
        read_only_fields = fields

class ChannelPostCommentSerializer(serializers.ModelSerializer):
    author = SenderSerializer(read_only=True)

    class Meta:
        model = ChannelPostComment
        fields = ["id", "author", "content", "created_at"]
        read_only_fields = ["id", "author", "created_at"]

    def create(self, validated_data):
        post = self.context["post"]
        if not post.comments_enabled:
            raise serializers.ValidationError("Comments are disabled on this post.")
        return ChannelPostComment.objects.create(
            post=post, author=self.context["request"].user, **validated_data
        )

# ─────────────────────────────────────────────────────────────
# CHANNEL POST
# ─────────────────────────────────────────────────────────────
class ChannelPostSerializer(serializers.ModelSerializer):
    """
    Read serializer. `reaction_counts` is a small emoji→count map
    (cheaper to render than the full reactor list for a public feed with
    potentially thousands of reactions).
    """

    author = SenderSerializer(read_only=True)
    reaction_counts = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()

    class Meta:
        model = ChannelPost
        fields = [
            "id", "channel", "author", "content", "media_url", "media_type",
            "comments_enabled", "is_pinned", "reaction_counts",
            "comment_count", "published_at", "created_at",
        ]
        read_only_fields = fields

    def get_reaction_counts(self, obj):
        # Uses prefetched .reactions (set in the view's queryset) —
        # counting in Python avoids a second aggregate query per post.
        counts = {}
        for r in obj.reactions.all():
            counts[r.emoji] = counts.get(r.emoji, 0) + 1
        return counts

    def get_comment_count(self, obj):
        return obj.comments.filter(is_deleted=False).count()