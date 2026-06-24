from django.shortcuts import render

# Create your views here.
"""
channelapp/views.py

OOP structure:
  - BaseChannelView          — shared helpers + owner guard
  - ChannelListCreateView    — discover/search public channels, create one
  - ChannelDetailView        — get/update/soft-delete
  - ToggleDiscoverableView   — consent-gated public/private switch
  - SubscribeView            — subscribe/unsubscribe
  - ChannelPostListCreateView — feed + new post (owner-only)
  - PostReactionView         — react/unreact
  - PostCommentView          — comment list/create
  - PinPostView              — pin/unpin
  - BoostChannelView         — create boost payment + webhook confirm
"""
import logging

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Channel,
    ChannelSubscriber,
    ChannelView,
    ChannelPost,
    ChannelPostReaction,
    ChannelPostComment,
    ChannelBoostPayment,
)
from .serializers import (
    ChannelListSerializer,
    ChannelDetailSerializer,
    CreateChannelSerializer,
    ToggleDiscoverableSerializer,
    ChannelPostSerializer,
    CreateChannelPostSerializer,
    ChannelPostReactionSerializer,
    ChannelPostCommentSerializer,
    CreateBoostPaymentSerializer,
)

logger = logging.getLogger(__name__)


class ChannelFeedPagination(CursorPagination):
    """Cursor pagination on published_at — same O(1)-seek rationale used
    throughout the project for any feed that can grow unbounded."""
    ordering = "-published_at"
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50

    # ─────────────────────────────────────────────────────────────
# BASE
# ─────────────────────────────────────────────────────────────
class BaseChannelView(APIView):
    permission_classes = [IsAuthenticated]

    def ok(self, data, message="Success"):
        return Response({"success": True, "message": message, "data": data})

    def created(self, data, message="Created"):
        return Response({"success": True, "message": message, "data": data}, status=status.HTTP_201_CREATED)

    def bad_request(self, errors, message="Validation error"):
        return Response({"success": False, "message": message, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    def not_found(self, message="Not found"):
        return Response({"success": False, "message": message}, status=status.HTTP_404_NOT_FOUND)

    def forbidden(self, message="Permission denied"):
        return Response({"success": False, "message": message}, status=status.HTTP_403_FORBIDDEN)

    def get_channel_or_404(self, pk):
        return Channel.objects.filter(pk=pk, deleted_at__isnull=True).select_related("created_by").first()

    def get_owned_channel_or_403(self, pk, user):
        """Spec: only the creator can post/edit/manage boost on a channel."""
        channel = self.get_channel_or_404(pk)
        if not channel:
            return None, self.not_found()
        if channel.created_by_id != user.id:
            return None, self.forbidden("Only the channel owner can do this.")
        return channel, None

# ─────────────────────────────────────────────────────────────
# LIST + CREATE
# ─────────────────────────────────────────────────────────────
class ChannelListCreateView(BaseChannelView):
    """
    GET  /api/channels/?category=tech&search=foo&mine=true
    POST /api/channels/
    """

    def get(self, request):
        qs = Channel.objects.filter(deleted_at__isnull=True)

        # `mine=true` → only channels I'm subscribed to (dashboard list);
        # default → public discoverable channels (Discover tab — heavy
        # lifting/ranking lives in discoveryapp, this is the plain list).
        if request.query_params.get("mine") == "true":
            my_channel_ids = ChannelSubscriber.objects.filter(
                user=request.user
            ).values_list("channel_id", flat=True)
            qs = qs.filter(id__in=my_channel_ids)
        else:
            qs = qs.filter(channel_type=Channel.ChannelType.PUBLIC)

        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(name__icontains=search) | qs.filter(handle__icontains=search)

        qs = qs.select_related("created_by").order_by("-is_boosted", "-subscriber_count")

        # Flag which of these the requester already subscribes to, in
        # ONE query instead of one-per-row.
        channel_ids = [c.id for c in qs]
        subscribed_ids = set(
            ChannelSubscriber.objects.filter(
                user=request.user, channel_id__in=channel_ids
            ).values_list("channel_id", flat=True)
        )
        results = list(qs)
        for c in results:
            c.prefetched_is_subscribed = c.id in subscribed_ids

        serializer = ChannelListSerializer(results, many=True, context={"request": request})
        return self.ok(serializer.data)

    def post(self, request):
        serializer = CreateChannelSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        channel = serializer.save()
        return self.created(
            ChannelDetailSerializer(channel, context={"request": request}).data,
            "Channel created.",
        )

# ─────────────────────────────────────────────────────────────
# DETAIL
# ─────────────────────────────────────────────────────────────
class ChannelDetailView(BaseChannelView):
    def get(self, request, pk):
        channel = self.get_channel_or_404(pk)
        if not channel:
            return self.not_found()

        # View on a public channel — or a private one you're subscribed
        # to — counts as a unique viewer for analytics, even if you
        # don't subscribe (spec: viewing != subscribing).
        is_subscribed = channel.subscribers.filter(user=request.user).exists()
        if channel.is_discoverable or is_subscribed:
            ChannelView.record(channel, request.user)

        return self.ok(ChannelDetailSerializer(channel, context={"request": request}).data)

    def patch(self, request, pk):
        channel, err = self.get_owned_channel_or_403(pk, request.user)
        if err:
            return err
        for field in ("name", "logo", "banner", "description", "category", "external_links"):
            if field in request.data:
                setattr(channel, field, request.data[field])
        channel.save()
        return self.ok(ChannelDetailSerializer(channel, context={"request": request}).data, "Channel updated.")

    def delete(self, request, pk):
        channel, err = self.get_owned_channel_or_403(pk, request.user)
        if err:
            return err
        channel.soft_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class ToggleDiscoverableView(BaseChannelView):
    """
    POST   /api/channels/<id>/discoverable/   { confirmed: true }  → go public
    DELETE /api/channels/<id>/discoverable/                          → go private
    """

    def post(self, request, pk):
        channel, err = self.get_owned_channel_or_403(pk, request.user)
        if err:
            return err
        serializer = ToggleDiscoverableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(channel=channel)
        return self.ok(ChannelDetailSerializer(channel, context={"request": request}).data, "Channel is now discoverable.")

    def delete(self, request, pk):
        channel, err = self.get_owned_channel_or_403(pk, request.user)
        if err:
            return err
        channel.make_private()
        return self.ok(ChannelDetailSerializer(channel, context={"request": request}).data, "Channel is now private.")


# ─────────────────────────────────────────────────────────────
# SUBSCRIBE
# ─────────────────────────────────────────────────────────────
class SubscribeView(BaseChannelView):
    def post(self, request, pk):
        channel = self.get_channel_or_404(pk)
        if not channel:
            return self.not_found()
        if channel.channel_type == Channel.ChannelType.PRIVATE and channel.created_by_id != request.user.id:
            return self.forbidden("This channel is private.")
        sub = ChannelSubscriber.subscribe(channel, request.user)
        return self.created({"subscribed": True}, "Subscribed.")

    def delete(self, request, pk):
        channel = self.get_channel_or_404(pk)
        if not channel:
            return self.not_found()
        ChannelSubscriber.unsubscribe(channel, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

# ─────────────────────────────────────────────────────────────
# POSTS  (feed + create)
# ─────────────────────────────────────────────────────────────
class ChannelPostListCreateView(BaseChannelView):
    """
    GET  /api/channels/<id>/posts/    — public feed (subscribers + viewers)
    POST /api/channels/<id>/posts/    — owner only
    """

    def get(self, request, pk):
        channel = self.get_channel_or_404(pk)
        if not channel:
            return self.not_found()

        if channel.channel_type == Channel.ChannelType.PRIVATE:
            is_subscribed = channel.subscribers.filter(user=request.user).exists()
            if not is_subscribed and channel.created_by_id != request.user.id:
                return self.forbidden("Subscribe to view this channel's posts.")

        posts = (
            ChannelPost.objects.filter(channel=channel, deleted_at__isnull=True, published_at__isnull=False)
            .select_related("author")
            .prefetch_related("reactions")
            .order_by("-is_pinned", "-published_at")
        )

        paginator = ChannelFeedPagination()
        page = paginator.paginate_queryset(posts, request)
        serializer = ChannelPostSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)
        
    def post(self, request, pk):
        channel, err = self.get_owned_channel_or_403(pk, request.user)
        if err:
            return err
        serializer = CreateChannelPostSerializer(
            data=request.data, context={"request": request, "channel": channel}
        )
        serializer.is_valid(raise_exception=True)
        post = serializer.save()
        return self.created(ChannelPostSerializer(post, context={"request": request}).data, "Post created.")