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