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