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


class GroupAdminPermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupAdminPermission
        fields = [
            "can_change_group_info", "can_delete_messages",
            "can_add_admins", "can_delete_admins", "can_delete_group",
        ]


# ─────────────────────────────────────────────────────────────
# GROUP — LIST  (dashboard row)
# ─────────────────────────────────────────────────────────────
class GroupListSerializer(serializers.ModelSerializer):
    """
    Lightweight representation for the dashboard list.
    last_message comes from messageapp.Message, attached by the view via
    a single prefetch (see views.py) — avoids an N+1 subquery per group.
    """

    last_message = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = [
            "id", "name", "logo", "group_type", "member_count",
            "my_role", "last_message", "updated_at",
        ]
        read_only_fields = fields

    def get_last_message(self, obj):
        # `prefetched_last_message` is attached in the view's queryset
        # construction step — see GroupListCreateView.get().
        msg = getattr(obj, "prefetched_last_message", None)
        if not msg:
            return None
        return {
            "id": msg.id,
            "type": msg.message_type,
            "sent_at": msg.sent_at,
            "sender_username": msg.sender.username,
        }

    def get_my_role(self, obj):
        request = self.context.get("request")
        membership = getattr(obj, "prefetched_my_membership", None)
        if membership:
            return membership.role
        if request:
            m = obj.members.filter(user=request.user, is_active=True).first()
            return m.role if m else None
        return None