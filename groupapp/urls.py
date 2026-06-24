"""groupapp/urls.py — REST routes. Mount under /api/groups/ in the project root urls.py."""

from django.urls import path

from .views import (
    GroupListCreateView,
    GroupDetailView,
    GroupMemberListView,
    AddMemberView,
    MemberRoleView,
    RemoveMemberView,
    MuteGroupView,
    GroupMessageListView,
    SendGroupMessageView,
)

urlpatterns = [
    path("", GroupListCreateView.as_view(), name="group-list-create"),
    path("<int:pk>/", GroupDetailView.as_view(), name="group-detail"),
    path("<int:pk>/members/", GroupMemberListView.as_view(), name="group-members"),
    path("<int:pk>/members/add/", AddMemberView.as_view(), name="group-member-add"),
    path("<int:pk>/members/<int:member_id>/role/", MemberRoleView.as_view(), name="group-member-role"),
    path("<int:pk>/members/<int:member_id>/", RemoveMemberView.as_view(), name="group-member-remove"),
    path("<int:pk>/leave/", RemoveMemberView.as_view(), name="group-leave"),
    path("<int:pk>/mute/", MuteGroupView.as_view(), name="group-mute"),
    path("<int:pk>/messages/", GroupMessageListView.as_view(), name="group-messages-list"),
    path("<int:pk>/messages/send/", SendGroupMessageView.as_view(), name="group-messages-send"),
]