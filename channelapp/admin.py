from django.contrib import admin
from .models import (
    Channel,
    ChannelSubscriber,
    ChannelView,
    ChannelPost,
    ChannelPostReaction,
    ChannelPostComment,
    ChannelBoostPayment
)

admin.site.register(Channel)
admin.site.register(ChannelSubscriber)
admin.site.register(ChannelView)
admin.site.register(ChannelPost)
admin.site.register(ChannelPostReaction)
admin.site.register(ChannelPostComment)
admin.site.register(ChannelBoostPayment)
