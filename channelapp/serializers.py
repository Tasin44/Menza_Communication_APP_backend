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