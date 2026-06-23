"""
groupapp/serializers.py

Mirrors the style of messageapp/serializers.py:
  - Lightweight nested "mini profile" serializers for users
  - Separate READ serializers (full, nested, select_related friendly)
    from WRITE serializers (flat Serializer subclasses with explicit
    validate()/save())
  - Group messages reuse messageapp.MessageSerializer — we don't
    re-implement message rendering here, we only add the "send into a
    group" write path.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from messageapp.models import Message, MessageFile
from messageapp.serializers import MessageSerializer, SenderSerializer

from .models import Group, GroupMember, GroupAdminPermission, GroupPermissionResolver

User = get_user_model()


# ─────────────────────────────────────────────────────────────
# MEMBER  (nested + standalone)
# ─────────────────────────────────────────────────────────────
class GroupMemberSerializer(serializers.ModelSerializer):
    """A single membership row, with the user's mini-profile nested in."""

    user = SenderSerializer(read_only=True)
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = GroupMember
        fields = [
            "id", "user", "role", "is_owner", "is_muted", "joined_at",
        ]
        read_only_fields = fields

    def get_is_owner(self, obj):
        return obj.is_owner