import random
import string
from datetime import timedelta

from django.utils import timezone
from django.contrib.auth import authenticate
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, OTPVerification, Contact, BlockedUser, UserDevice


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _generate_otp(length: int = 6) -> str:
    """Cryptographically random 6-digit OTP."""
    '''
    def _generate_otp(length=6):           # ✅ Works
    def _generate_otp(length: int = 6):    # ✅ Also works (with type hint)
    '''


    return "".join(random.choices(string.digits, k=length))
    '''
    string.digits--it's built-in - it's from Python's string module, string.digits  # Returns: "0123456789", Without it (alternative): "0123456789". Randomly picks any of the 6 digits from 0 to 9, after that 
    .join(...) → Combines them into a single string
    '''


def _otp_expiry(minutes: int = 10):
    return timezone.now() + timedelta(minutes=minutes)
    '''
    How it works:
        timezone.now() → Current date & time.
        timedelta(minutes=10) → A duration of 10 minutes.
        + → Adds 10 minutes to the current time.
    '''


def _issue_tokens(user: User) -> dict:#Creates a pair of JWT tokens (access + refresh) for a user.
    """Return access + refresh JWT pair for a user."""
    '''
    writing its one, Instead of

        refresh=RefreshToken.for_user(user)

    everywhere, we simply do
        _issue_tokens(user)

    Cleaner way.
    '''
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),# Creates an access token from the refresh token
    }
'''
Think of RefreshToken as an object (a Python class), not just the token string.
refresh (RefreshToken object)
│
├── token string (refresh JWT)
├── user information (payload)
└── access_token (a PROPERTY/METHOD that creates a new access token)

when we do - refresh.access_token
you're asking the RefreshToken object to generate a new access token using the same user information.

Easy visualization: 
  User
   │
   ▼
RefreshToken.for_user(user)
   │
   ▼
RefreshToken Object
   │
   ├── Refresh JWT
   ├── User ID
   └── Can generate Access Token
             │
             ▼
refresh.access_token
             │
             ▼
    New Access Token
'''


# ─────────────────────────────────────────────
# AUTH — SIGNUP
# ─────────────────────────────────────────────
class SignupSerializer(serializers.Serializer):
    """
    Step 1 of signup flow.
    Fields: username, email OR phone, password.
    OTP is sent after this — user is NOT created yet.

    Spec:
    - username must be unique
    - email OR phone required (not both mandatory)
    - password stored via Argon2id (configured in Django settings)
    """

    username = serializers.CharField(max_length=100)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=50, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=50, required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate_email(self, value):
        if value and User.objects.filter(email__iexact=value).exists():
            '''
            email__iexact is a Django lookup that performs a case-insensitive exact match.
            How:

                iexact = "insensitive exact"

            Checks if email exists regardless of case

                "John@email.com" matches "john@email.com" and "JOHN@EMAIL.COM"
            '''
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower() if value else value

    def validate_phone(self, value):
        if value and User.objects.filter(phone=value).exists():
            '''
            Why not phone__iexact: Phone numbers are numeric strings (like "+1234567890"). Case sensitivity doesn't matter for numbers. Using exact (default) is sufficient and slightly faster.
            '''
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate(self, attrs):
        #"""attrs contains all validated data from previous validators"""
        email = attrs.get("email", "").strip()
        phone = attrs.get("phone", "").strip()
        if not email and not phone:
            raise serializers.ValidationError(
                {"non_field_errors": "At least one of email or phone is required."}
            )
        # Clean empty strings to None so DB UNIQUE constraint works correctly
        attrs["email"] = email or None
        attrs["phone"] = phone or None
        return attrs
    '''
    Why: This is a "cross-field validation" - it validates multiple fields together (email AND phone).

    What: attrs (short for "attributes") is a dictionary containing all validated data from previous validators.
    Suppose user sends
        {
            "username":"John",
            "email":"john@gmail.com",
            "password":"12345678"
        }

    then attrs becomes 
    {
        'username':'John',
        'email':'john@gmail.com',
        'password':'12345678'
    }
    How validate works:

    -validate() runs after individual field validators

    -It checks if at least email OR phone is provided

    -It cleans empty strings to None for database consistency

    -Returns modified attrs dictionary

    '''

    @transaction.atomic
    def save(self):
        """
        Does NOT create the user yet.
        Saves pending data to session (handled in view) and creates OTP record.
        Returns (otp_code, identifier) so the view can send it.
        """
        data = self.validated_data
        # Identifier = email if provided, else phone
        identifier = data.get("email") or data.get("phone")

        # Invalidate any previous signup OTPs for this identifier
        '''
        why using filter instead of get()
            Using filter() with .update() allows bulk operation on multiple records. If multiple unverified OTPs exist for the same identifier, this marks ALL of them as verified at once.
            Alternative: If using get(), it would only update one and you'd need a loop.

            Suppose database 
            111111
            111111
            111111
            All become verified=True
            If we used  get(),it would return only one row. 

        '''
        OTPVerification.objects.filter(
            identifier=identifier,
            purpose=OTPVerification.Purpose.SIGNUP,
            is_verified=False,
        ).update(is_verified=True)   # mark old ones dead

        otp_code = _generate_otp()
        OTPVerification.objects.create(
            user=None,                          # no user yet
            identifier=identifier,
            otp_code=otp_code,
            purpose=OTPVerification.Purpose.SIGNUP,
            expires_at=_otp_expiry(minutes=10),
        )
        
        # Cache the pending signup data with the OTP code as part of the key
        from django.core.cache import cache
        cache.set(f"signup_{otp_code}", data, timeout=600)
        
        return otp_code, identifier, data       # view stores `data` temporarily


class VerifySignupOTPSerializer(serializers.Serializer):
    """
    Step 2: user submits the 6-digit OTP.
    If valid → create the User row → return JWT tokens.
    """

    otp_code = serializers.CharField(max_length=6, min_length=6)

    def validate(self, attrs):
        otp_code = attrs["otp_code"]

        try:
            # Note: without identifier, we assume OTPs are unique enough for concurrent signups
            # or we get the latest one if there are somehow duplicates.
            otp = OTPVerification.objects.filter(
                otp_code=otp_code,
                purpose=OTPVerification.Purpose.SIGNUP,
                is_verified=False,
            ).latest("created_at")
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError({"otp_code": "Invalid OTP."})

        if otp.is_expired():
            raise serializers.ValidationError({"otp_code": "OTP has expired. Please request a new one."})

        # Fetch cached pending data
        from django.core.cache import cache
        pending_data = cache.get(f"signup_{otp_code}")
        if not pending_data:
            raise serializers.ValidationError({"otp_code": "Signup session expired. Please sign up again."})

        attrs["_otp"] = otp
        attrs["pending_data"] = pending_data
        return attrs

    @transaction.atomic
    def save(self):
        data = self.validated_data
        otp: OTPVerification = data["_otp"]
        pending_data = data["pending_data"]

        user = User.objects.create_user(
            username=pending_data["username"],
            password=pending_data["password"],
            email=pending_data.get("email") or None,
            phone=pending_data.get("phone") or None,
            first_name=pending_data.get("first_name", ""),
            last_name=pending_data.get("last_name", ""),
            is_verified=True,
        )

        otp.is_verified = True
        otp.user = user
        otp.save(update_fields=["is_verified", "user"])

        # Also return pending_data so the view can return all details got from signup
        return user, _issue_tokens(user), pending_data
        '''
        This returns a tuple so the view can get both the user and their tokens.
        What: The view expects two values back: user object and token dictionary.
        like in views.py 
                user, tokens = serializer.save()  # Unpacks the tuple
        '''


# ─────────────────────────────────────────────
# AUTH — LOGIN
# ─────────────────────────────────────────────
class LoginSerializer(serializers.Serializer):
    """
    Login with username + password.
    Spec says login identifier is username (not email/phone).
    Returns JWT access + refresh tokens.
    """

    username = serializers.CharField()
    password = serializers.CharField(write_only=True)
    # Optional: register device token on login for push notifications
    device_token = serializers.CharField(required=False, allow_blank=True)
    platform = serializers.ChoiceField(
        choices=UserDevice.Platform.choices,
        required=False,
    )

    def validate(self, attrs):
        user = authenticate(username=attrs["username"], password=attrs["password"])
        '''
        What: authenticate() is a Django function that:
            Takes username and password
            Checks if user exists
            Verifies password hash
            Returns user object or None
        '''
        if not user:
            raise serializers.ValidationError(
                {"non_field_errors": "Invalid username or password."}
            )
        if not user.is_verified:
            raise serializers.ValidationError(
                {"non_field_errors": "Account not verified. Please complete OTP verification."}
            )
        if user.status == User.Status.SUSPENDED:
            raise serializers.ValidationError(
                {"non_field_errors": "Your account has been suspended."}
            )
        if user.status == User.Status.DELETED:
            raise serializers.ValidationError(
                {"non_field_errors": "Account not found."}
            )
        attrs["_user"] = user
        return attrs

    @transaction.atomic
    def save(self):
        user: User = self.validated_data["_user"]
        device_token = self.validated_data.get("device_token", "").strip()
        platform = self.validated_data.get("platform", "")

        # Register device for push notifications if token provided
        if device_token and platform:
            UserDevice.objects.update_or_create(
                user=user,
                device_token=device_token,
                defaults={"platform": platform},
            )

        return user, _issue_tokens(user)#❓why what how  this line 


# ─────────────────────────────────────────────
# AUTH — FORGOT PASSWORD
# ─────────────────────────────────────────────
class ForgotPasswordSerializer(serializers.Serializer):
    """
    Spec flow:
    1. User enters email or phone
    2. OTP sent to that contact
    Separate from signup — user must already exist.
    """

    identifier = serializers.CharField(help_text="Email address or phone number")

    def validate_identifier(self, value):
        # Check if it looks like an email
        if "@" in value:
            user = User.objects.filter(email__iexact=value.lower()).first()
        else:
            user = User.objects.filter(phone=value).first()

        if not user:
            # Vague error — don't reveal whether account exists (security)
            raise serializers.ValidationError(
                "If an account with this detail exists, an OTP will be sent."
            )
        if user.status == User.Status.DELETED:
            raise serializers.ValidationError(
                "If an account with this detail exists, an OTP will be sent."
            )

        self._user = user
        return value

    @transaction.atomic
    def save(self):
        identifier = self.validated_data["identifier"]

        # Invalidate old forgot-password OTPs for this identifier
        OTPVerification.objects.filter(
            identifier=identifier,
            purpose=OTPVerification.Purpose.FORGOT_PASSWORD,
            is_verified=False,
        ).update(is_verified=True)

        otp_code = _generate_otp()
        OTPVerification.objects.create(
            user=self._user,
            identifier=identifier,
            otp_code=otp_code,
            purpose=OTPVerification.Purpose.FORGOT_PASSWORD,
            expires_at=_otp_expiry(minutes=10),
        )
        return otp_code, identifier


class VerifyForgotPasswordOTPSerializer(serializers.Serializer):
    """
    Step 2: verify the OTP from forgot-password flow.
    Returns a short-lived reset_token (we store otp.id in it).
    """

    identifier = serializers.CharField()
    otp_code = serializers.CharField(max_length=6, min_length=6)

    def validate(self, attrs):
        try:
            otp = OTPVerification.objects.get(
                identifier=attrs["identifier"],#❓am I assigning identifier,otp_code to OTPVerification model here? is this why I'm declaredt this tow lines above here ?
                # '''
                # identifier = serializers.CharField()
                # otp_code = serializers.CharField(max_length=6, min_length=6)
                # '''
                otp_code=attrs["otp_code"],
                purpose=OTPVerification.Purpose.FORGOT_PASSWORD,
                is_verified=False,
            )
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError({"otp_code": "Invalid OTP."})

        if otp.is_expired():
            raise serializers.ValidationError({"otp_code": "OTP has expired."})

        attrs["_otp"] = otp
        return attrs

    def save(self):
        otp: OTPVerification = self.validated_data["_otp"]
        otp.is_verified = True
        otp.save(update_fields=["is_verified"])
        # Return otp.id as the reset token — view will sign it with JWT
        return otp


# ─────────────────────────────────────────────
# AUTH — RESET PASSWORD
# ─────────────────────────────────────────────
class ResetPasswordSerializer(serializers.Serializer):
    """
    Spec: New password + Confirm password.
    Requires reset_token (signed otp id) from previous step.
    """

    reset_token = serializers.IntegerField(help_text="OTP id returned from verify step")
    new_password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )

        try:
            otp = OTPVerification.objects.select_related("user").get(
                id=attrs["reset_token"],
                purpose=OTPVerification.Purpose.FORGOT_PASSWORD,
                is_verified=True,    # must have been verified in previous step
            )
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError(
                {"reset_token": "Invalid or expired reset token."}
            )

        if not otp.user:
            raise serializers.ValidationError(
                {"reset_token": "User not found."}
            )

        attrs["_user"] = otp.user
        return attrs

    @transaction.atomic
    def save(self):
        user: User = self.validated_data["_user"]
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])
        return user


# ─────────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────────
class UserProfileSerializer(serializers.ModelSerializer):
    """
    Spec: Profile shows username, image, email, phone.
    ONLY profile_image is editable from the profile page.
    All other fields are read-only.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "phone",
            "first_name",
            "last_name",
            "profile_image",
            "last_seen",
            "is_verified",
            "status",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "username",
            "email",
            "phone",
            "first_name",
            "last_name",
            "last_seen",
            "is_verified",
            "status",
            "created_at",
        ]
        # Only profile_image is writable — spec explicitly says this


class UpdateAvatarSerializer(serializers.Serializer):
    """
    Dedicated serializer for profile image update.
    The frontend uploads to S3/R2 first, then sends the URL here.
    """

    profile_image = serializers.CharField(max_length=500)

    def validate_profile_image(self, value):#I'll change this later 
        if not value.startswith("https://"):
            raise serializers.ValidationError(
                "profile_image must be a valid HTTPS URL from the media storage."
            )
        return value

    def save(self, user: User):
        user.profile_image = self.validated_data["profile_image"]
        user.save(update_fields=["profile_image", "updated_at"])
        return user


class PublicUserSearchSerializer(serializers.ModelSerializer):
    """
    Used when searching for a user to start a conversation.
    Spec: shows only username and image (nothing else).
    """

    class Meta:
        model = User
        fields = ["id", "username", "profile_image"]


# ─────────────────────────────────────────────
# CONTACTS
# ─────────────────────────────────────────────
class ContactSerializer(serializers.ModelSerializer):
    """
    Spec: Create contact with first name, last name, phone.
    After creation — if that phone belongs to a Menza user,
    is_app_user = True and contact_user is linked.
    If not, show invite button (handled in view logic).
    """

    # Nested read-only: when the contact IS a Menza user, expose their profile
    contact_user_profile = PublicUserSearchSerializer(

        source="contact_user",
        read_only=True,
    )
    '''
        Contact model has
        contact_user  ,  which is a ForeignKey. Serializer says
                    Use contact_user, serialize it with  PublicUserSearchSerializer.

        if contact_user present, User(id=5), output becomes 
            {
            "id":5,
            "username":"Alex",
            "profile_image":"..."
            }

        source="contact_user" means get data from the contact_user field of the Contact model
        If contact_user is null, contact_user_profile will be null
    '''

    class Meta:
        model = Contact
        fields = [
            "id",
            "contact_first_name",
            "contact_last_name",
            "contact_phone",
            "is_app_user",
            "contact_user",         # write: can set FK directly
            "contact_user_profile", # read: nested profile if app user
            "created_at",
        ]
        read_only_fields = ["id", "is_app_user", "contact_user_profile", "created_at"]
        extra_kwargs = {
            "contact_user": {"required": False, "allow_null": True, "write_only": True},
        }


    def validate(self, attrs):
        request = self.context["request"]
        owner = request.user
        phone = attrs.get("contact_phone", "").strip()

        if not phone:
            raise serializers.ValidationError(
                {"contact_phone": "Phone number is required to create a contact."}
            )

        # Check if this phone belongs to any Menza user
        menza_user = User.objects.filter(phone=phone).first()
        if menza_user:
            if menza_user == owner:
                raise serializers.ValidationError(
                    {"contact_phone": "You cannot add yourself as a contact."}
                )
            attrs["contact_user"] = menza_user
            attrs["is_app_user"] = True
        else:
            attrs["contact_user"] = None
            attrs["is_app_user"] = False

        return attrs

    def create(self, validated_data):#❓why what how from where this validated_data coming, is it comming from validate method above of ContactSerializer class? but i see there is not validated_data filed present 
        owner = self.context["request"].user
        validated_data.pop("contact_user_profile", None)#❓why doing this pop 

        contact, created = Contact.objects.get_or_create(
            owner=owner,
            contact_phone=validated_data["contact_phone"],
            defaults={
                "contact_first_name": validated_data.get("contact_first_name", ""),
                "contact_last_name": validated_data.get("contact_last_name", ""),
                "contact_user": validated_data.get("contact_user"),
                "is_app_user": validated_data.get("is_app_user", False),
            },
        )
        if not created:
            raise serializers.ValidationError(
                {"contact_phone": "This contact already exists in your list."}
            )
        return contact


# ─────────────────────────────────────────────
# BLOCKED USERS
# ─────────────────────────────────────────────
class BlockedUserSerializer(serializers.ModelSerializer):
    """
    For listing blocked users on the profile page.
    Spec: Blocked user list.
    """

    blocked_user = PublicUserSearchSerializer(source="blocked", read_only=True)#❓why PublicUserSearchSerializer why not ContactSerializer or UserProfileSerializer?

    class Meta:
        model = BlockedUser
        fields = ["id", "blocked_user", "created_at"]
        read_only_fields = ["id", "blocked_user", "created_at"]


class BlockUserSerializer(serializers.Serializer):
    """Used for POST /block/ — accepts user_id to block."""

    user_id = serializers.IntegerField()

    ## 1. validate_user_id receives user_id = 42
    def validate_user_id(self, value):
        request = self.context["request"]
        if value == request.user.id:
            raise serializers.ValidationError("You cannot block yourself.")
        if not User.objects.filter(id=value, status=User.Status.ACTIVE).exists():
            raise serializers.ValidationError("User not found.")
        return value
# 2. After validation, self.validated_data = {"user_id": 42}

    def save(self, blocker: User):
        blocked = User.objects.get(id=self.validated_data["user_id"])
        '''
        After validate_user_id() runs and returns the value, that value is stored in self.validated_data dictionary.
        '''

        obj, created = BlockedUser.objects.get_or_create(blocker=blocker, blocked=blocked)# I want to add something like if anyuser already block someone, he can't block him twice 
        return obj, created

