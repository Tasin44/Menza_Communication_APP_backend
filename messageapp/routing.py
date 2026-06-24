from django.urls import path
from . import consumers
import groupapp.consumers

websocket_urlpatterns = [
    path("ws/chat/<int:conversation_id>/", consumers.ChatConsumer.as_asgi()),
    path("ws/groups/<int:group_id>/", groupapp.consumers.GroupConsumer.as_asgi()),
    path("ws/presence/", consumers.PresenceConsumer.as_asgi()),
]
