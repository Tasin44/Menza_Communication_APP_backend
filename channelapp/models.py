from django.db import models

# Create your models here.
"""
channelapp/models.py

Covers:
  - Channel               (public/private broadcast channel)
  - ChannelSubscriber      (M2M through table — subscribe/view)
  - ChannelPost            (owner-only posts, plaintext — see legal note)
  - ChannelPostReaction    (emoji reactions on posts)
  - ChannelPostComment     (optional per-post comments)
  - ChannelBoostPayment    (pay-to-boost in Discover)

Legal note (from Menza_BackendReportingandDiscoveryFunctions.pdf, section J):
  Discoverable channels are PUBLIC by design — content, once the owner
  opts in, can be produced to law enforcement on a valid request and is
  NOT end-to-end encrypted like DMs/groups. That's why ChannelPost.content
  is a plain TextField (content_plaintext in the client's own schema notes)
  rather than content_encrypted like messageapp.Message. Never "encrypt"
  this field thinking it adds privacy — it would just be obfuscation
  without protecting anyone, since the whole point of the channel is to be
  publicly readable.

  Spec requires an explicit consent step before a channel becomes
  discoverable ("Confirmation modal: user taps 'I understand this channel
  will be public'") — modelled as `discoverable_consented_at`. The toggle
  cannot flip to PUBLIC without that timestamp being set first.
"""

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone



# ─────────────────────────────────────────────────────────────
# CHANNEL
# ─────────────────────────────────────────────────────────────
class Channel(models.Model):
    """
    Spec: only the channel creator can post; subscribers can view + react.
    There is deliberately no "channel admin" role beyond the owner — the
    client's spec only ever refers to a single owner who posts.
    """

    class ChannelType(models.TextChoices):
        PUBLIC = "public", "Public"
        PRIVATE = "private", "Private"

    class Category(models.TextChoices):
        FASHION = "fashion", "Fashion"
        TRADING = "trading", "Trading"
        BUSINESS = "business", "Business"
        TRAVEL = "travel", "Travel"
        SPORTS = "sports", "Sports"
        MUSIC = "music", "Music"
        TECH = "tech", "Tech"
        FOOD = "food", "Food"
        OTHER = "other", "Other"

    # Unique public @handle — e.g. @MenzaFashion (branding spec section 7).
    handle = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=100)
    logo = models.CharField(max_length=500, blank=True, null=True)
    banner = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)

    channel_type = models.CharField(max_length=10, choices=ChannelType.choices, default=ChannelType.PRIVATE)
    # Set the moment the owner taps "I understand this channel will be
    # public" — required before channel_type can become PUBLIC. NULL means
    # consent was never given (channel has always been private, or consent
    # was revoked when the owner turned discovery back off — we keep the
    # historical timestamp instead of nulling it, see make_private()).
    discoverable_consented_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_channels",
        db_index=True,
    )

    # Denormalized — avoids COUNT(*) on channel_subscribers for every
    # discovery list render (the single hottest query in the whole app).
    subscriber_count = models.PositiveIntegerField(default=0)

    # Branding extras
    is_verified = models.BooleanField(default=False)
    external_links = models.JSONField(default=list, blank=True, help_text="[{'label':'Website','url':'...'}]")

    # Boost (paid promotion in Discover — see ChannelBoostPayment)
    boost_count = models.PositiveIntegerField(default=0)
    is_boosted = models.BooleanField(default=False)
    boost_expires_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "channels"
        ordering = ["-subscriber_count"]
        indexes = [
            models.Index(fields=["handle"]),
            models.Index(fields=["channel_type"]),
            models.Index(fields=["category"]),
            # Composite — the Discover "boosted channels first" query.
            models.Index(fields=["is_boosted", "boost_count"]),
            # Composite — trending-by-category query in discoveryapp.
            models.Index(fields=["category", "subscriber_count"]),
        ]

    def __str__(self):
        return f"@{self.handle}"






