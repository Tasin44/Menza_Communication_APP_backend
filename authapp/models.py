from django.db import models

# Create your models here.
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


# ─────────────────────────────────────────────
# CUSTOM USER MANAGER
# ─────────────────────────────────────────────
# BaseUserManager provides core helper methods like normalize_email and set_password for custom user models.
class UserManager(BaseUserManager): 
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

        # self.model points to the User model because we assigned this manager to User.objects below.
        user = self.model( 
            username=username,
            email=email,
            phone=phone,
            **extra_fields,  # extra_fields catches any additional keyword arguments passed to the function (like is_staff) 
        )
        # set_password securely hashes the password using Argon2.
        # save() writes the user row to the database. 
        user.set_password(password)  # hashes with Argon2 (set in settings)
        user.save(using=self._db)
        return user

    # create_superuser is required by Django so `manage.py createsuperuser` knows how to create an admin user.
    def create_superuser(self, username, password, email=None, phone=None, **extra_fields): 
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

    # This links the User model to our custom UserManager above, allowing User.objects.create_user()
    objects = UserManager() 

    # Tells Django to use 'username' as the primary login identifier instead of the default username.
    USERNAME_FIELD = "username" 
    # This tells `manage.py createsuperuser` to prompt for email in the terminal.
    REQUIRED_FIELDS = ["email"]

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

    # This is an inner class (Nested Class) used to organize choices for the 'purpose' field neatly inside OTPVerification.
    class Purpose(models.TextChoices): 
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
    # Purpose inherits from models.TextChoices, which automatically generates the .choices attribute for us behind the scenes.
    purpose = models.CharField(max_length=30, choices=Purpose.choices) 
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
        # unique_together ensures one owner cannot add the same contact_user multiple times (prevents duplicates).
        unique_together = [("owner", "contact_user")] 
        indexes = [models.Index(fields=["owner"])]
        # Yes, they are built-in Meta attributes. They define database-level rules: uniqueness, indexing for speed, and custom constraints.
        constraints = [ 
            # Can't add yourself
            # This prevents a user from adding themselves as a contact. F() compares two columns in the same row. ~ means NOT.
            models.CheckConstraint( 
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
        # related_name="blocking" means user.blocking.all() will return all the users THIS user has blocked.
        related_name="blocking", 
    )
    blocked = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        # related_name="blocked_by" means user.blocked_by.all() will return all the users who have blocked THIS user.
        related_name="blocked_by",
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


