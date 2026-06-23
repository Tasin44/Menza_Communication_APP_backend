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




# ─────────────────────────────────────────────────────────────
# GROUP — DETAIL
# ─────────────────────────────────────────────────────────────
class GroupDetailSerializer(serializers.ModelSerializer):
    members = serializers.SerializerMethodField()
    my_permissions = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = [
            "id", "name", "logo", "description", "group_type",
            "member_count", "max_members", "created_by", "members",
            "my_permissions", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_members(self, obj):
        # select_related("user") must be applied by the view's queryset.
        qs = obj.members.filter(is_active=True).select_related("user")
        return GroupMemberSerializer(qs, many=True, context=self.context).data

    def get_my_permissions(self, obj):
        """
        Resolves the requesting user's exact capability set using
        GroupPermissionResolver — single source of truth, see models.py.
        """
        request = self.context.get("request")
        if not request:
            return {}
        membership = obj.members.filter(user=request.user, is_active=True).first()
        if not membership:
            return {}
        resolver = GroupPermissionResolver(membership)
        return {
            "role": membership.role,
            "is_owner": membership.is_owner,
            "can_change_group_info": resolver.can_change_group_info(),
            "can_delete_messages": resolver.can_delete_messages(),
            "can_add_admins": resolver.can_add_admins(),
            "can_delete_admins": resolver.can_delete_admins(),
            "can_delete_group": resolver.can_delete_group(),
        }



# ─────────────────────────────────────────────────────────────
# CREATE GROUP
# ─────────────────────────────────────────────────────────────
class CreateGroupSerializer(serializers.ModelSerializer):
    """
    Spec: name, logo, description, group_type at creation.
    The creator automatically becomes the owner + an ADMIN membership row
    with the full permission grid (handled in .create(), not by the DB).
    """

    # Optional: invite some contacts straight away in the same request.
    member_user_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
        write_only=True,
    )

    class Meta:
        model = Group
        fields = ["id", "name", "logo", "description", "group_type", "member_user_ids"]
        read_only_fields = ["id"]

    @transaction.atomic
    def create(self, validated_data):
        member_ids = validated_data.pop("member_user_ids", [])
        owner = self.context["request"].user

        group = Group.objects.create(created_by=owner, **validated_data)

        # Owner's own membership row — role=ADMIN with every permission,
        # though GroupMember.is_owner already grants everything implicitly.
        GroupMember.add(group, owner, role=GroupMember.Role.ADMIN)
        GroupAdminPermission.objects.create(
            group=group, admin_user=owner, granted_by=owner,
            can_change_group_info=True, can_delete_messages=True,
            can_add_admins=True, can_delete_admins=True, can_delete_group=True,
        )

        # Bulk-add initial members (contacts selected during creation).
        if member_ids:
            users = User.objects.filter(id__in=member_ids).exclude(id=owner.id)
            for user in users:
                GroupMember.add(group, user, role=GroupMember.Role.MEMBER)

        return group

# ─────────────────────────────────────────────────────────────
# ADD MEMBER
# ─────────────────────────────────────────────────────────────
class AddMemberSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()

    def validate_user_id(self, value):
        if not User.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError("User not found.")
        return value

    def save(self, group: Group):
        target = User.objects.get(id=self.validated_data["user_id"])
        return GroupMember.add(group, target, role=GroupMember.Role.MEMBER)