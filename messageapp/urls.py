
"""
messageapp/urls.py

Mount in project urls.py as:
    path("api/messages/", include("messageapp.urls")),

Full URL map:
  Conversations:
    GET  POST  /conversations/
    GET  DELETE       /conversations/<id>/
    POST              /conversations/<id>/<action>/    (mute, unmute, block, archive, unarchive)

  Messages:
    GET               /conversations/<id>/messages/    (paginated chat history)
    POST              /send/                           (send a message)
    GET  DELETE       /<msg_id>/                       (get/delete a single message)
    POST              /conversations/<id>/read/        (mark as read — REST fallback)
    GET               /conversations/<id>/search/      (search messages)

  Reactions:
    POST DELETE       /<msg_id>/react/

  Pin inside chat:
    POST DELETE       /conversations/<conv_id>/pin/<msg_id>/

  Dashboard pins (max 5):
    GET  POST         /pins/
    DELETE            /pins/<id>/

  Media files:
    GET               /conversations/<id>/media/
"""

from django.urls import path
from .views import (
    ConversationListCreateView,
    ConversationDetailView,
    ConversationActionView,
    MessageListView,
    SendMessageView,
    MessageDetailView,
    MessageSearchView,
    MessageReadReceiptView,
    MessageReactionView,
    PinMessageView,
    PinnedItemView,
    MediaFilesView,
)

urlpatterns = [
    # ── Conversations ─────────────────────────────────────────────────────
    # GET  → dashboard list (name, image, last msg, unread count)
    # POST → start new DM { username or phone }
    path(
        "conversations/",
        ConversationListCreateView.as_view(),
        name="conversation-list-create",
    ),

    # GET    → full conversation detail (other person, pinned msgs, media)
    # DELETE → soft-delete for current user only
    path(
        "conversations/<int:pk>/",
        ConversationDetailView.as_view(),
        name="conversation-detail",
    ),

    # POST /conversations/<id>/mute/
    # POST /conversations/<id>/unmute/
    # POST /conversations/<id>/block/
    # POST /conversations/<id>/archive/
    # POST /conversations/<id>/unarchive/
    path(
        "conversations/<int:pk>/<str:action>/",
        ConversationActionView.as_view(),
        name="conversation-action",
    ),

    # ── Messages ──────────────────────────────────────────────────────────
    # GET → paginated message history (cursor pagination, newest first)
    path(
        "conversations/<int:pk>/messages/",
        MessageListView.as_view(),
        name="message-list",
    ),

    # POST → send a new message { conversation_id|group_id, content_encrypted, files? }
    path(
        "send/",
        SendMessageView.as_view(),
        name="message-send",
    ),

    # GET    → single message detail
    # DELETE → soft delete (?for_everyone=true for delete for everyone)
    path(
        "<int:pk>/",
        MessageDetailView.as_view(),
        name="message-detail",
    ),

    # POST → mark all messages up to last_message_id as read
    # REST fallback when WebSocket is not connected
    path(
        "conversations/<int:pk>/read/",
        MessageReadReceiptView.as_view(),
        name="message-read-receipt",
    ),

    # GET → search messages in conversation (?q=searchterm)
    path(
        "conversations/<int:pk>/search/",
        MessageSearchView.as_view(),
        name="message-search",
    ),

    # ── Reactions ─────────────────────────────────────────────────────────
    # POST   → add/update emoji reaction { emoji: "👍" }
    # DELETE → remove my reaction
    path(
        "<int:pk>/react/",
        MessageReactionView.as_view(),
        name="message-react",
    ),

    # ── Pin message inside chat box ────────────────────────────────────────
    # POST   → pin
    # DELETE → unpin
    path(
        "conversations/<int:conv_id>/pin/<int:msg_id>/",
        PinMessageView.as_view(),
        name="pin-message",
    ),

    # ── Dashboard pins (max 5 per user) ───────────────────────────────────
    # GET  → list my 5 pinned items
    # POST → pin a conversation/group/channel to dashboard
    path(
        "pins/",
        PinnedItemView.as_view(),
        name="pinned-item-list",
    ),
    # DELETE → unpin
    path(
        "pins/<int:pk>/",
        PinnedItemView.as_view(),
        name="pinned-item-detail",
    ),

    # ── Media files in conversation ────────────────────────────────────────
    # GET → all media files shared in this conversation
    # Supports ?type=image|video|audio|file filter
    path(
        "conversations/<int:pk>/media/",
        MediaFilesView.as_view(),
        name="conversation-media",
    ),
]



















