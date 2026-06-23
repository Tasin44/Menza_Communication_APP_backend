"""
groupapp/models.py

Covers:
  - Group                 (the group itself — name, logo, type)
  - GroupMember            (membership row — role: member/moderator/admin)
  - GroupAdminPermission   (granular permission grid for a promoted admin)

Design decisions (kept consistent with authapp / messageapp):
  - db_table names match the client's SQL schema (groups_table, group_members,
    group_admin_permissions) so this app can sit on top of the existing DB
    without a destructive migration.
  - Group.created_by is the permanent OWNER. The owner is NOT a row that can
    be demoted/removed — ownership is a separate concept from "admin role",
    otherwise a malicious admin could lock the real owner out.
  - member_count is denormalized on Group (same pattern as
    Conversation.last_message_preview in messageapp) — avoids
    COUNT(*) on group_members for every dashboard render.
  - messageapp.Message.group is a ForeignKey to "groupapp.Group" (string
    reference, already wired in messageapp/models.py) — so group messages
    are NOT duplicated here. We reuse Message as-is; this app only adds
    group-specific helpers (send/list) in views.py.
  - Permission resolution (who can do what) is centralised in the
    GroupPermissionResolver class at the bottom of this file — this is the
    single source of truth so views/serializers/consumers never duplicate
    the "is this allowed?" logic (classic OOP — one object owns one
    responsibility).
"""

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


# Hard ceiling on members per group. Kept as a class constant (not a DB
# constraint) so it can be bumped per-tier later (e.g. premium groups) without
# a migration — enforced in GroupMember creation logic.
DEFAULT_MAX_GROUP_MEMBERS = 512


# ─────────────────────────────────────────────────────────────
# GROUP
# ─────────────────────────────────────────────────────────────
class Group(models.Model):
    """
    The group entity.

    group_type:
      - PRIVATE (default): findable only via invite link / contact add
      - PUBLIC: searchable by name in discoveryapp

    member_count is denormalized — updated via F() expressions in
    GroupMember.objects.create()/leave() so we never read-modify-write
    (avoids race conditions when many people join/leave concurrently).
    """

    class GroupType(models.TextChoices):
        PUBLIC = "public", "Public"
        PRIVATE = "private", "Private"

    name = models.CharField(max_length=100)
    logo = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True, default="")
    group_type = models.CharField(
        max_length=10,
        choices=GroupType.choices,
        default=GroupType.PRIVATE,
    )

    # Permanent owner. Ownership never transfers via the admin/moderator
    # role system — only via an explicit transfer_ownership() call.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_groups",
        db_index=True,
    )

    # Denormalized counter — see class docstring.
    member_count = models.PositiveIntegerField(default=0)
    max_members = models.PositiveIntegerField(default=DEFAULT_MAX_GROUP_MEMBERS)

    # Spec: "Can admins restrict posting — e.g. broadcast-only group
    # where only admins post?" — when True, GroupPermissionResolver
    # gates SendGroupMessageView to elevated members only.
    posting_restricted = models.BooleanField(default=False)

    # Soft delete: spec says "What happens to the group when the creator
    # deletes their account?" — we don't hard-delete history, we archive it.
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "groups_table"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["group_type"]),
            models.Index(fields=["created_by"]),
        ]

    def __str__(self):
        return f"Group#{self.pk} {self.name}"

    # ── membership counter helpers (atomic, race-safe) ────────────
    def bump_member_count(self, delta: int):#❔
        """
        Adjust member_count using an F() expression so concurrent
        joins/leaves never overwrite each other (no read-modify-write).
        """
        Group.objects.filter(pk=self.pk).update(
            member_count=models.F("member_count") + delta#❔what is delta here 
        )
        self.refresh_from_db(fields=["member_count"])

    def is_full(self) -> bool:
        return self.member_count >= self.max_members

    def soft_delete(self):
        """Archive instead of hard delete — group history (and its
        messages, via messageapp) stays queryable for compliance."""
        self.is_active = False
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_active", "deleted_at", "updated_at"])

    @transaction.atomic
    def transfer_ownership(self, new_owner_user):
        """
        Explicit ownership transfer (e.g. before the current owner deletes
        their account). New owner must already be a member.
        """
        membership = self.members.filter(user=new_owner_user, is_active=True).first()
        if membership is None:
            raise ValueError("New owner must be an active member of the group.")
        self.created_by = new_owner_user
        self.save(update_fields=["created_by", "updated_at"])
        membership.role = GroupMember.Role.ADMIN
        membership.save(update_fields=["role"])



# ─────────────────────────────────────────────────────────────
# GROUP MEMBER
# ─────────────────────────────────────────────────────────────
class GroupMember(models.Model):
    """
    One row per (group, user).#❔ Role drives default permissions;
    GroupAdminPermission can fine-tune an ADMIN's exact capabilities.

    Role hierarchy (spec):
      ADMIN     — full control, can promote/demote, can be granted a
                  custom permission grid (see GroupAdminPermission)
      MODERATOR — "can do everything like admin, but cannot remove an admin"
      MEMBER    — can message/call/post (if not a broadcast-only group)
    """

    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        MODERATOR = "moderator", "Moderator"
        ADMIN = "admin", "Admin"

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)

    is_muted = models.BooleanField(default=False)
    # Soft-leave / soft-kick — keeps history (who said what) intact
    # even after someone leaves, same pattern as ConversationParticipant.
    is_active = models.BooleanField(default=True)

    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "group_members"
        unique_together = [("group", "user")]
        indexes = [
            models.Index(fields=["user"]),           # "all my groups"
            models.Index(fields=["group", "role"]),   # "all admins of this group"
        ]

    def __str__(self):
        return f"{self.user.username} ({self.role}) in {self.group.name}"

    # ── role-check shortcuts (used everywhere instead of string compares) ─
    @property
    def is_owner(self) -> bool:
        return self.group.created_by_id == self.user_id

    @property
    def is_admin(self) -> bool:
        return self.is_owner or self.role == self.Role.ADMIN

    @property
    def is_moderator(self) -> bool:
        return self.role == self.Role.MODERATOR

    @property
    def has_elevated_access(self) -> bool:
        """Admin or moderator — anything beyond a plain member."""
        return self.is_admin or self.is_moderator

    # ── lifecycle ──────────────────────────────────────────────────
    @classmethod
    @transaction.atomic
    def add(cls, group: Group, user, role: str = Role.MEMBER) -> "GroupMember":
        """
        Add a user to a group. Reactivates a soft-left membership instead
        of violating the unique_together constraint if they rejoin.
        """
        if group.is_full():
            raise ValueError("This group has reached its maximum member limit.")

        member, created = cls.objects.get_or_create(
            group=group,
            user=user,
            defaults={"role": role},
        )
        if not created and not member.is_active:
            member.is_active = True
            member.role = role
            member.left_at = None
            member.save(update_fields=["is_active", "role", "left_at"])
            created = True   # treat a rejoin as a fresh add for the counter

        if created:
            group.bump_member_count(+1)
        return member

    def leave(self):
        """Member leaves voluntarily. Owner must transfer ownership first."""
        if self.is_owner:
            raise ValueError(
                "Group owner cannot leave — transfer ownership first."
            )
        self.is_active = False
        self.left_at = timezone.now()
        self.save(update_fields=["is_active", "left_at"])
        self.group.bump_member_count(-1)

    def kick(self, by_member: "GroupMember"):
        """Removed by an admin/moderator. Validated by GroupPermissionResolver
        in views.py before this is called — this method just performs it."""
        self.is_active = False
        self.left_at = timezone.now()
        self.save(update_fields=["is_active", "left_at"])
        self.group.bump_member_count(-1)

    def promote(self, new_role: str):
        self.role = new_role
        self.save(update_fields=["role"])
        # Promoting away from admin clears any custom permission grid.
        if new_role != self.Role.ADMIN:
            GroupAdminPermission.objects.filter(group=self.group, admin_user=self.user).delete()

    def mute(self):
        self.is_muted = True
        self.save(update_fields=["is_muted"])

    def unmute(self):
        self.is_muted = False
        self.save(update_fields=["is_muted"])


# ─────────────────────────────────────────────────────────────
# GROUP ADMIN PERMISSION  (granular grid for a promoted admin)
# ─────────────────────────────────────────────────────────────
class GroupAdminPermission(models.Model):
    """
    Mirrors the client's `group_admin_permissions` SQL table.

    Spec: "During creating a new admin, the existing admin can provide the
    new one this permission" — i.e. when ANY admin promotes someone to
    admin, they choose which of these five capabilities the new admin gets.
    The group OWNER always has every permission implicitly (see
    GroupMember.is_owner) regardless of what's stored here.
    """

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="admin_permissions",
    )
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="granted_group_permissions",
    )

    can_change_group_info = models.BooleanField(default=False)
    can_delete_messages = models.BooleanField(default=False)
    # Spec default leans ON for this one — easiest way to grow a team of
    # admins without going back to the owner every time.
    can_add_admins = models.BooleanField(default=True)
    can_delete_admins = models.BooleanField(default=False)
    can_delete_group = models.BooleanField(default=False)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="permissions_granted",
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "group_admin_permissions"
        unique_together = [("group", "admin_user")]

    def __str__(self):
        return f"Permissions for {self.admin_user.username} in {self.group.name}"



# ─────────────────────────────────────────────────────────────
# PERMISSION RESOLVER  (single source of truth — pure OOP, no Django state)
# ─────────────────────────────────────────────────────────────
class GroupPermissionResolver:
    """
    Decides "can this member do X?" for every group action.

    Why a plain class and not just methods on GroupMember?
    Because resolving a permission sometimes needs to look at a SECOND
    member (e.g. "can A kick B?" depends on both A's and B's roles), and
    putting two-member logic on a single-member model gets confusing fast.
    This class takes the actor (and optionally a target) and answers
    one question per method — easy to unit test, easy to reuse in
    views.py, serializers.py and consumers.py without re-deriving the rule.
    """

    def __init__(self, actor: GroupMember):
        self.actor = actor

    # ── cached lookup of the actor's custom permission grid ───────
    @property
    def _grid(self) -> GroupAdminPermission | None:
        if not hasattr(self, "_grid_cache"):
            self._grid_cache = GroupAdminPermission.objects.filter(
                group=self.actor.group, admin_user=self.actor.user
            ).first()
        return self._grid_cache

    def _check(self, flag_name: str) -> bool:
        """
        Resolution order:
          1. Owner            → always True
          2. Moderator        → True for everything except admin removal
          3. Admin             → check their personal permission grid
          4. Plain member      → False
        """
        if self.actor.is_owner:
            return True
        if self.actor.role == GroupMember.Role.MODERATOR:
            return flag_name != "can_delete_admins"
        if self.actor.role == GroupMember.Role.ADMIN:
            return bool(self._grid and getattr(self._grid, flag_name, False))
        return False

    def can_change_group_info(self) -> bool:
        return self._check("can_change_group_info")

    def can_delete_messages(self) -> bool:
        return self._check("can_delete_messages")

    def can_add_admins(self) -> bool:
        return self._check("can_add_admins")

    def can_delete_admins(self) -> bool:
        return self._check("can_delete_admins")

    def can_delete_group(self) -> bool:
        return self._check("can_delete_group")

    def can_remove_member(self, target: GroupMember) -> bool:
        """
        General kick rule:
          - Owner can remove anyone (except themself — they must transfer).
          - Moderator can remove members and other moderators, never admins.
          - Admin can remove members/moderators, and other admins only if
            can_delete_admins is True.
          - A member can never remove anyone.
        """
        if target.is_owner:
            return False   # nobody can kick the owner
        if self.actor.is_owner:
            return True
        if target.role == GroupMember.Role.ADMIN:
            return self.can_delete_admins()
        if self.actor.has_elevated_access:
            return True
        return False

    def can_post_in_broadcast_mode(self) -> bool:
        """Used when a group is set to 'broadcast-only' (admins post,
        members read) — same resolution as has_elevated_access."""
        return self.actor.has_elevated_access















