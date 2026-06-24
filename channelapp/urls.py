"""channelapp/urls.py — mount under /api/channels/ in the project root urls.py."""

from django.urls import path

from .views import (
    ChannelListCreateView,
    ChannelDetailView,
    ToggleDiscoverableView,
    SubscribeView,
    ChannelPostListCreateView,
    PinPostView,
    DeletePostView,
    PostReactionView,
    PostCommentView,
    BoostChannelView,
    BoostWebhookView,
)

urlpatterns = [
    path("", ChannelListCreateView.as_view(), name="channel-list-create"),
    path("<int:pk>/", ChannelDetailView.as_view(), name="channel-detail"),
    path("<int:pk>/discoverable/", ToggleDiscoverableView.as_view(), name="channel-discoverable"),
    path("<int:pk>/subscribe/", SubscribeView.as_view(), name="channel-subscribe"),
    path("<int:pk>/posts/", ChannelPostListCreateView.as_view(), name="channel-posts"),
    path("<int:pk>/posts/<int:post_id>/pin/", PinPostView.as_view(), name="channel-post-pin"),
    path("<int:pk>/posts/<int:post_id>/", DeletePostView.as_view(), name="channel-post-delete"),
    path("<int:pk>/posts/<int:post_id>/react/", PostReactionView.as_view(), name="channel-post-react"),
    path("<int:pk>/posts/<int:post_id>/comments/", PostCommentView.as_view(), name="channel-post-comments"),
    path("<int:pk>/boost/", BoostChannelView.as_view(), name="channel-boost"),
    path("boost/webhook/<int:payment_id>/confirm/", BoostWebhookView.as_view(), name="channel-boost-webhook"),
]