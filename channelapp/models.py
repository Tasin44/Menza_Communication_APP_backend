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

    # ── discoverability state machine ──────────────────────────────
    @property
    def is_discoverable(self) -> bool:
        return self.channel_type == self.ChannelType.PUBLIC and self.deleted_at is None

    def make_discoverable(self):
        """
        Spec: "Default OFF... Confirmation modal... before the toggle
        saves." Caller (the view) is responsible for having already
        collected that confirmation — this method just enforces that the
        consent timestamp exists before flipping the flag, so the rule
        can never be bypassed by calling the model directly either.
        """
        if not self.discoverable_consented_at:
            self.discoverable_consented_at = timezone.now()
        self.channel_type = self.ChannelType.PUBLIC
        self.save(update_fields=["channel_type", "discoverable_consented_at", "updated_at"])

    def make_private(self):
        """
        Spec: "Do existing subscribers keep access when discovery is
        turned off?" — yes, we only flip the flag; subscriber rows are
        untouched. Historical posts are RETAINED (legal doc K: "Deleted
        posts (12 months) — yes, court order"), so we don't touch posts
        either; we simply stop indexing the channel for discovery.
        """
        self.channel_type = self.ChannelType.PRIVATE
        self.save(update_fields=["channel_type", "updated_at"])

    # ── subscriber counter (atomic, race-safe — same F() pattern as
    #    groupapp.Group.bump_member_count) ──────────────────────────
    def bump_subscriber_count(self, delta: int):
        Channel.objects.filter(pk=self.pk).update(
            subscriber_count=models.F("subscriber_count") + delta
        )
        self.refresh_from_db(fields=["subscriber_count"])

    def apply_boost(self, days: int):
        self.is_boosted = True
        self.boost_count = models.F("boost_count") + 1
        self.boost_expires_at = timezone.now() + timezone.timedelta(days=days)
        self.save(update_fields=["is_boosted", "boost_count", "boost_expires_at"])
        self.refresh_from_db(fields=["boost_count"])

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])


# ─────────────────────────────────────────────────────────────
# CHANNEL SUBSCRIBER
# ─────────────────────────────────────────────────────────────
class ChannelSubscriber(models.Model):
    """
    Spec: subscribing gives notifications + (optionally) the ability to
    comment; viewing (without subscribing) is allowed on public channels
    but isn't tracked here as a row — see ChannelView for anonymous-ish
    view analytics instead.
    """

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="subscribers")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_subscriptions"
    )
    notifications_enabled = models.BooleanField(default=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "channel_subscribers"
        unique_together = [("channel", "user")]
        indexes = [models.Index(fields=["user"])]   # "channels I'm subscribed to"

    def __str__(self):
        return f"{self.user.username} → @{self.channel.handle}"

    @classmethod
    @transaction.atomic
    def subscribe(cls, channel: Channel, user) -> "ChannelSubscriber":
        sub, created = cls.objects.get_or_create(channel=channel, user=user)
        if created:
            channel.bump_subscriber_count(+1)
        return sub

    @classmethod
    @transaction.atomic
    def unsubscribe(cls, channel: Channel, user) -> bool:
        deleted, _ = cls.objects.filter(channel=channel, user=user).delete()
        if deleted:
            channel.bump_subscriber_count(-1)
        return bool(deleted)

# ─────────────────────────────────────────────────────────────
# CHANNEL VIEW  (lightweight analytics — unique viewers, incl. non-subs)
# ─────────────────────────────────────────────────────────────
class ChannelView(models.Model):
    """
    Spec: "Do owners see unique viewer count separate from subscriber
    count?" — yes. One row per (channel, user) so repeat views by the
    same user don't inflate the count; this is intentionally separate
    from ChannelSubscriber since viewing doesn't require subscribing.
    """

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="views")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_views")
    first_viewed_at = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "channel_views"
        unique_together = [("channel", "user")]

    @classmethod
    def record(cls, channel: Channel, user):
        cls.objects.update_or_create(channel=channel, user=user)


# ─────────────────────────────────────────────────────────────
# CHANNEL POST
# ─────────────────────────────────────────────────────────────
class ChannelPost(models.Model):
    """
    See module docstring re: plaintext content — this is intentional and
    matches the legal framework's public-channel model, not an oversight.
    """

    class MediaType(models.TextChoices):
        NONE = "none", "None"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        FILE = "file", "File"

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_posts")

    content = models.TextField(blank=True, default="")
    media_url = models.CharField(max_length=500, blank=True, null=True)
    media_type = models.CharField(max_length=10, choices=MediaType.choices, default=MediaType.NONE)

    comments_enabled = models.BooleanField(default=True)
    is_pinned = models.BooleanField(default=False, db_index=True)

    # Premium feature: schedule a post for the future. publish_post()
    # (called by a periodic task) flips published_at once scheduled_at
    # has passed.
    scheduled_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Soft-delete with 12-month retention, per legal doc K:
    # "Deleted posts (12 months) — yes, court order".
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "channel_posts"
        ordering = ["-published_at"]
        indexes = [
            # Hottest query: "feed for this channel, newest first".
            models.Index(fields=["channel", "-published_at"]),
            models.Index(fields=["is_pinned"]),
        ]

    def __str__(self):
        return f"Post#{self.pk} in @{self.channel.handle}"

    @property
    def is_visible(self) -> bool:
        return self.deleted_at is None and self.published_at is not None and self.published_at <= timezone.now()

    def publish_now(self):
        self.published_at = timezone.now()
        self.save(update_fields=["published_at"])

    def soft_delete(self):
        """Display-flag only — row is retained 12 months for legal reasons."""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def pin(self):
        # Unpin any previously pinned post in the same channel first —
        # only one pinned post at a time, mirrors messageapp's single-pin
        # convention per chat box.
        ChannelPost.objects.filter(channel=self.channel, is_pinned=True).update(is_pinned=False)
        self.is_pinned = True
        self.save(update_fields=["is_pinned"])

class ChannelPostReaction(models.Model):
    post = models.ForeignKey(ChannelPost, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_post_reactions")
    emoji = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "channel_post_reactions"
        unique_together = [("post", "user")]
