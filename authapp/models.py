from django.db import models

# Create your models here.
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


# ─────────────────────────────────────────────
# CUSTOM USER MANAGER
# ─────────────────────────────────────────────
class UserManager(BaseUserManager):#❓ what does BaseUserManager means 
    """
    Custom manager because we use username as the login identifier,
    but email OR phone is required (not both).
    """

    def create_user(self, username, password, email=None, phone=None, **extra_fields):
        if not username:
            raise ValueError("Username is required.")
        if not email and not phone:
            raise ValueError("At least one of email or phone is required.")

        if email:
            email = self.normalize_email(email)

        user = self.model(#❓ what does self.model() means, which model it's using 
            username=username,
            email=email,
            phone=phone,
            **extra_fields,#❓ what does extra_fields means 
        )
        #❓explan below 2 lines 
        user.set_password(password)  # hashes with Argon2 (set in settings)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password, email=None, phone=None, **extra_fields):#❓ why this method required explain it 
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_verified", True)
        extra_fields.setdefault("status", "active")
        return self.create_user(username, password, email=email, phone=phone, **extra_fields)


# ─────────────────────────────────────────────
# USER
# ─────────────────────────────────────────────
class User(AbstractBaseUser, PermissionsMixin):
    """
    Core user model.

    DB notes from schema:
    - id: BIGINT UNSIGNED AUTO_INCREMENT (Django BigAutoField)
    - username: UNIQUE NOT NULL
    - email: UNIQUE, nullable
    - phone: UNIQUE, nullable
    - At least one of email/phone required → enforced in manager + serializer
    - password_hash stored by Django's set_password() — use Argon2 in settings
    - profile_image: URL only (actual file lives on S3/R2)
    - status ENUM: active | suspended | deleted
    - is_verified: email/phone OTP verified flag
    - last_seen: updated by WebSocket disconnect handler (not here)
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        DELETED = "deleted", "Deleted"

    # ── identifiers ──────────────────────────
    username = models.CharField(max_length=100, unique=True)
    email = models.EmailField(max_length=255, unique=True, null=True, blank=True)
    phone = models.CharField(max_length=50, unique=True, null=True, blank=True)

    # ── profile ───────────────────────────────
    first_name = models.CharField(max_length=50, null=True, blank=True)
    last_name = models.CharField(max_length=50, null=True, blank=True)
    # Only the URL is stored — binary file goes to S3/R2
    profile_image = models.CharField(max_length=500, null=True, blank=True)

    # ── status ────────────────────────────────
    is_active = models.BooleanField(default=True)       # Django login gate
    is_staff = models.BooleanField(default=False)       # Django admin access
    is_verified = models.BooleanField(default=False)    # OTP verified
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    # ── presence ──────────────────────────────
    # Updated by WebSocket on disconnect — not touched in auth views
    last_seen = models.DateTimeField(null=True, blank=True)

    # ── timestamps ────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()#❓ what does it means 

    USERNAME_FIELD = "username"#❓ why it's defined here 
    REQUIRED_FIELDS = []   # email/phone handled in manager

    class Meta:
        db_table = "users"
        indexes = [
            models.Index(fields=["username"]),
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
        ]

    def __str__(self):
        return self.username

    # Spec: only profile_image is editable from the profile page
    def update_avatar(self, url: str):
        self.profile_image = url
        self.save(update_fields=["profile_image", "updated_at"])

    def soft_delete(self):
        """Mark deleted instead of actually removing the row."""
        self.status = self.Status.DELETED
        self.is_active = False
        self.save(update_fields=["status", "is_active", "updated_at"])


# ─────────────────────────────────────────────
# USER DEVICES
# ─────────────────────────────────────────────
class UserDevice(models.Model):
    """
    Stores push-notification tokens per device.
    Needed for:
    - Push notifications when user is offline
    - Device sync (AirPods/headphone spec requirement)
    - Session management (user can see & kill all sessions)
    """

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        WEB = "web", "Web"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    device_token = models.CharField(max_length=500)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    last_active = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_devices"
        # One token per user — prevents duplicates on re-register
        unique_together = [("user", "device_token")]

    def __str__(self):
        return f"{self.user.username} — {self.platform}"


# ─────────────────────────────────────────────
# OTP VERIFICATIONS
# ─────────────────────────────────────────────
class OTPVerification(models.Model):
    """
    Handles all OTP flows:
    - signup: user_id is NULL (user row doesn't exist yet)
    - forgot_password: user_id is set
    - change_email / change_phone: user_id is set

    Security notes from spec:
    - 6-digit code
    - Expires in 10 minutes (set in view)
    - is_verified flag prevents reuse
    - identifier stores the email/phone the OTP was sent to
    """

    class Purpose(models.TextChoices):#❓what is the necessity of that class? why it is inside OTPVerification class, what does this class called as it is inside another class 
        SIGNUP = "signup", "Signup"
        FORGOT_PASSWORD = "forgot_password", "Forgot Password"
        CHANGE_EMAIL = "change_email", "Change Email"
        CHANGE_PHONE = "change_phone", "Change Phone"

    # nullable because signup OTP is created before the user row
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otps",
    )
    # The email or phone the code was sent to
    identifier = models.CharField(max_length=255)
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=30, choices=Purpose.choices)#❓Purpose class has no choices method I see, then how using it here 
    is_verified = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_verifications"
        indexes = [
            models.Index(fields=["otp_code"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["identifier"]),
        ]

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"OTP({self.purpose}) → {self.identifier}"


# ─────────────────────────────────────────────
# CONTACTS
# ─────────────────────────────────────────────
class Contact(models.Model):
    """
    A contact entry in a user's address book.

    Two cases from spec:
    1. contact_user is NOT NULL → person is a Menza user, can message directly
    2. contact_user is NULL     → not on Menza yet, show 'Invite' button

    contact_first_name / contact_last_name = the name the OWNER assigned,
    not the actual user's name (same as how phone contacts work).

    Spec: Create contact fields = first name, last name, phone number
    """

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="contacts",
    )
    # NULL when the contact hasn't joined Menza yet
    contact_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_as_contact_by",
    )
    contact_phone = models.CharField(max_length=20, null=True, blank=True)
    contact_first_name = models.CharField(max_length=100, null=True, blank=True)
    contact_last_name = models.CharField(max_length=100, null=True, blank=True)
    is_app_user = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contacts"
        # A user can't add the same Menza user twice
        unique_together = [("owner", "contact_user")]#❓why it's using here 
        indexes = [models.Index(fields=["owner"])]
        constraints = [#❓constraints,unique_together,indexes are they built in meta class method? whats their purpose 
            # Can't add yourself
            models.CheckConstraint(#❓explain this part 
                check=~models.Q(owner=models.F("contact_user")),
                name="contact_not_self",
            )
        ]

    def __str__(self):
        name = f"{self.contact_first_name or ''} {self.contact_last_name or ''}".strip()
        return f"{self.owner.username} → {name or self.contact_phone}"


# ─────────────────────────────────────────────
# BLOCKED USERS
# ─────────────────────────────────────────────
class BlockedUser(models.Model):
    """
    Spec: Blocked user list visible on profile page.
    Blocking hides DMs and prevents new messages from blocked user.
    Enforced in the messaging views, not here.
    """

    blocker = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="blocking",#❓why here related_name used blocking why not blocked_by? as blocker is the user who blocked 
    )
    blocked = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="blocked_by",#❓why what how here related_name blocked_by instead of blocked_user ?
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "blocked_users"
        unique_together = [("blocker", "blocked")]
        constraints = [
            models.CheckConstraint(
                check=~models.Q(blocker=models.F("blocked")),
                name="cannot_block_self",
            )
        ]

    def __str__(self):
        return f"{self.blocker.username} blocked {self.blocked.username}"


