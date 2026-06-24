"""
groupapp/views.py

OOP structure (mirrors messageapp/views.py):
  - BaseGroupView            — shared response helpers + membership guard
  - GroupListCreateView      — dashboard list / create group
  - GroupDetailView          — get / update / soft-delete a group
  - GroupMemberListView      — list members
  - AddMemberView            — add a contact to the group
  - MemberRoleView           — promote/demote + permission grant
  - RemoveMemberView         — kick / leave
  - GroupMessageListView     — paginated group chat history (reuses Message)
  - SendGroupMessageView     — post into the group chat
  - MuteGroupView            — per-user mute
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from aamyproject.mixins import StandardResponseMixin

from messageapp.models import Message
from messageapp.serializers import MessageSerializer

from .models import Group, GroupMember, GroupPermissionResolver
from .serializers import (
    GroupListSerializer,
    GroupDetailSerializer,
    CreateGroupSerializer,
    AddMemberSerializer,
    ChangeMemberRoleSerializer,
    GroupMemberSerializer,
    SendGroupMessageSerializer,
)

logger = logging.getLogger(__name__)
channel_layer = get_channel_layer()


class GroupMessageCursorPagination(CursorPagination):
    """Same rationale as messageapp's MessageCursorPagination — O(1) seek
    instead of an ever-growing OFFSET scan as group history grows."""
    ordering = "-sent_at"
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 100


# ─────────────────────────────────────────────────────────────
# BASE VIEW
# ─────────────────────────────────────────────────────────────
class BaseGroupView(StandardResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    def ok(self, data, message="Success"):
        return self.success_response(data, message=message)

    def created(self, data, message="Created"):
        return self.success_response(data, message=message, status_code=status.HTTP_201_CREATED)

    def bad_request(self, errors, message="Validation error"):
        return self.error_response(message, status_code=status.HTTP_400_BAD_REQUEST, data=errors)

    def not_found(self, message="Not found"):
        return self.error_response(message, status_code=status.HTTP_404_NOT_FOUND)

    def forbidden(self, message="Permission denied"):
        return self.error_response(message, status_code=status.HTTP_403_FORBIDDEN)

    # ── membership guard, used by almost every view below ─────────
    def get_membership_or_403(self, group_id, user):
        """
        Returns (membership, None) if the user is an active member,
        or (None, response) otherwise. Single query with select_related
        so callers can immediately use membership.group without a
        second hit.
        """
        membership = (
            GroupMember.objects.select_related("group")
            .filter(group_id=group_id, user=user, is_active=True)
            .first()
        )
        if not membership:
            return None, self.not_found("Group not found or you are not a member.")
        return membership, None

    def broadcast_to_group(self, group_id: int, event: dict):
        try:
            async_to_sync(channel_layer.group_send)(f"group_{group_id}", event)
        except Exception as e:
            logger.error(f"Group WS broadcast failed: {e}")


# ─────────────────────────────────────────────────────────────
# LIST + CREATE
# ─────────────────────────────────────────────────────────────
class GroupListCreateView(BaseGroupView):
    """
    GET  /api/groups/        — dashboard list of my groups
    POST /api/groups/        — create a new group
    """

    def get(self, request):
        user = request.user

        # Step 1: every group I'm an active member of, with my own
        # membership row preloaded (avoids a second query per group
        # to compute "my_role" in the serializer).
        my_membership_ids = GroupMember.objects.filter(
            user=user, is_active=True
        ).values_list("group_id", flat=True)

        groups = (
            Group.objects.filter(id__in=my_membership_ids, is_active=True)
            .prefetch_related(
                Prefetch(
                    "members",
                    queryset=GroupMember.objects.filter(user=user),
                    to_attr="prefetched_my_membership_list",
                )
            )
        )

        # Step 2: attach each group's most recent message in ONE extra
        # query instead of N — fetch the latest message per group_id by
        # pulling a small ordered slice and bucketing in Python (cheap,
        # since the result set per page is small — 20-50 groups).
        group_ids = list(groups.values_list("id", flat=True))
        latest_by_group = {}
        if group_ids:
            recent_messages = (
                Message.objects.filter(group_id__in=group_ids, is_deleted=False)
                .select_related("sender")
                .order_by("group_id", "-sent_at")
            )
            for msg in recent_messages:
                # First message seen per group_id (already ordered DESC) wins.
                latest_by_group.setdefault(msg.group_id, msg)

        result = []
        for group in groups:
            group.prefetched_last_message = latest_by_group.get(group.id)
            membership_list = getattr(group, "prefetched_my_membership_list", [])
            group.prefetched_my_membership = membership_list[0] if membership_list else None
            result.append(group)

        serializer = GroupListSerializer(result, many=True, context={"request": request})
        return self.ok(serializer.data)

    def post(self, request):
        serializer = CreateGroupSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return self.bad_request(serializer.errors)
        group = serializer.save()
        return self.created(
            GroupDetailSerializer(group, context={"request": request}).data,
            "Group created.",
        )


# ─────────────────────────────────────────────────────────────
# DETAIL
# ─────────────────────────────────────────────────────────────
class GroupDetailView(BaseGroupView):
    """
    GET    /api/groups/<id>/
    PATCH  /api/groups/<id>/    — name/logo/description (requires permission)
    DELETE /api/groups/<id>/    — soft-delete (requires can_delete_group)
    """

    def get(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err
        group = Group.objects.select_related("created_by").prefetch_related(
            Prefetch("members", queryset=GroupMember.objects.filter(is_active=True).select_related("user"))
        ).get(pk=pk)
        return self.ok(GroupDetailSerializer(group, context={"request": request}).data)

    def patch(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        if not GroupPermissionResolver(membership).can_change_group_info():
            return self.forbidden("You don't have permission to edit this group.")

        group = membership.group
        for field in ("name", "logo", "description"):
            if field in request.data:
                setattr(group, field, request.data[field])
        group.save()

        self.broadcast_to_group(pk, {"type": "group.updated", "group_id": pk})
        return self.ok(GroupDetailSerializer(group, context={"request": request}).data, "Group updated.")

    def delete(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        if not GroupPermissionResolver(membership).can_delete_group():
            return self.forbidden("You don't have permission to delete this group.")

        membership.group.soft_delete()
        self.broadcast_to_group(pk, {"type": "group.deleted", "group_id": pk})
        return self.success_response(data={}, message="Group deleted.", status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────
# MEMBERS
# ─────────────────────────────────────────────────────────────
class GroupMemberListView(BaseGroupView):
    """GET /api/groups/<id>/members/"""

    def get(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err
        members = membership.group.members.filter(is_active=True).select_related("user")
        return self.ok(GroupMemberSerializer(members, many=True, context={"request": request}).data)


class AddMemberView(BaseGroupView):
    """POST /api/groups/<id>/members/add/  { user_id }"""

    def post(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        # Any active member can add a contact unless the group is
        # locked-down — kept simple: elevated access required for PUBLIC
        # groups (to curb spam-adding), open for PRIVATE groups.
        if membership.group.group_type == Group.GroupType.PUBLIC and not membership.has_elevated_access:
            return self.forbidden("Only admins/moderators can add members to a public group.")

        serializer = AddMemberSerializer(data=request.data)
        if not serializer.is_valid():
            return self.bad_request(serializer.errors)
        try:
            new_member = serializer.save(group=membership.group)
        except ValueError as e:
            return self.bad_request(str(e))

        self.broadcast_to_group(pk, {
            "type": "group.member_joined",
            "user_id": new_member.user_id,
            "username": new_member.user.username,
        })
        return self.created(GroupMemberSerializer(new_member).data, "Member added.")


class MemberRoleView(BaseGroupView):
    """PATCH /api/groups/<id>/members/<member_id>/role/ { role, permissions }"""

    def patch(self, request, pk, member_id):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        target = membership.group.members.filter(user_id=member_id, is_active=True).first()
        if not target:
            return self.not_found("User not exist")

        resolver = GroupPermissionResolver(membership)
        new_role = request.data.get("role")

        # Promoting to admin requires can_add_admins; demoting an admin
        # (or any role change targeting an admin) requires can_delete_admins
        # unless the actor is the owner.
        if new_role == GroupMember.Role.ADMIN and not resolver.can_add_admins():
            return self.forbidden("You can't promote members to admin.")
        if target.role == GroupMember.Role.ADMIN and new_role != GroupMember.Role.ADMIN:
            if not resolver.can_delete_admins():
                return self.forbidden("You can't demote an admin.")

        serializer = ChangeMemberRoleSerializer(data=request.data)
        if not serializer.is_valid():
            return self.bad_request(serializer.errors)
        updated = serializer.save(target_member=target, granted_by_user=request.user)

        self.broadcast_to_group(pk, {
            "type": "group.role_changed",
            "user_id": updated.user_id,
            "new_role": updated.role,
        })
        return self.ok(GroupMemberSerializer(updated).data, "Role updated.")


class RemoveMemberView(BaseGroupView):
    """
    DELETE /api/groups/<id>/members/<member_id>/   — kick (admin/mod only)
    DELETE /api/groups/<id>/leave/                 — leave voluntarily
    """

    def delete(self, request, pk, member_id=None):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        if member_id is None:
            # Voluntary leave
            try:
                membership.leave()
            except ValueError as e:
                return self.bad_request(str(e))
            self.broadcast_to_group(pk, {
                "type": "group.member_left", "user_id": request.user.id,
            })
            return self.success_response(data={}, message=f'user "{request.user.username}" left this group', status_code=status.HTTP_200_OK)

        target = membership.group.members.filter(user_id=member_id, is_active=True).first()
        if not target:
            return self.not_found("User not exist")

        if not GroupPermissionResolver(membership).can_remove_member(target):
            return self.forbidden("You can't remove this member.")

        target.kick(by_member=membership)
        self.broadcast_to_group(pk, {
            "type": "group.member_kicked", "user_id": target.user_id, "kicked_by": request.user.id,
        })
        return self.success_response(data={}, message="Member kicked.", status_code=status.HTTP_204_NO_CONTENT)


class MuteGroupView(BaseGroupView):
    """POST /api/groups/<id>/mute/  — per-user mute, DELETE to unmute."""

    def post(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err
        membership.mute()
        return self.ok({}, "Group muted.")

    def delete(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err
        membership.unmute()
        return self.ok({}, "Group unmuted.")


# ─────────────────────────────────────────────────────────────
# GROUP MESSAGES  (reuses messageapp.Message)
# ─────────────────────────────────────────────────────────────
class GroupMessageListView(BaseGroupView):
    """GET /api/groups/<id>/messages/ — paginated chat history."""

    def get(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        messages = (
            Message.objects.filter(group_id=pk, is_deleted=False)
            .select_related("sender", "reply_to__sender")
            .prefetch_related("files", "reactions")
            .order_by("-sent_at")
        )

        paginator = GroupMessageCursorPagination()
        page = paginator.paginate_queryset(messages, request)
        serializer = MessageSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class SendGroupMessageView(BaseGroupView):
    """POST /api/groups/<id>/messages/ — send a message into the group."""

    def post(self, request, pk):
        membership, err = self.get_membership_or_403(pk, request.user)
        if err:
            return err

        # Broadcast-only groups: only elevated members may post.
        if getattr(membership.group, "posting_restricted", False):
            if not GroupPermissionResolver(membership).can_post_in_broadcast_mode():
                return self.forbidden("Only admins can post in this group.")

        serializer = SendGroupMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return self.bad_request(serializer.errors)
        message = serializer.save(group=membership.group, sender=request.user)

        self.broadcast_to_group(pk, {
            "type": "chat.message",
            "message_id": message.id,
            "sender_id": request.user.id,
            "sender_username": request.user.username,
            "message_type": message.message_type,
            "sent_at": message.sent_at.isoformat(),
        })
        return self.created(
            MessageSerializer(message, context={"request": request}).data,
            "Message sent.",
        )