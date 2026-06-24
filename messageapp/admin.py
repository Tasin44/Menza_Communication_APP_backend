from django.contrib import admin
from .models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageFile,
    MessageStatus,
    MessageReaction,
    PinnedMessage,
    PinnedItem,
    ArchivedConversation
)

admin.site.register(Conversation)
admin.site.register(ConversationParticipant)
admin.site.register(Message)
admin.site.register(MessageFile)
admin.site.register(MessageStatus)
admin.site.register(MessageReaction)
admin.site.register(PinnedMessage)
admin.site.register(PinnedItem)
admin.site.register(ArchivedConversation)
