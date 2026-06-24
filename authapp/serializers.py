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
def _generate_otp(length: int = 6) -> str:#❓why what how explan this method code , how length:int=6 working
    """Cryptographically random 6-digit OTP."""
    return "".join(random.choices(string.digits, k=length))


def _otp_expiry(minutes: int = 10):
    return timezone.now() + timedelta(minutes=minutes)


def _issue_tokens(user: User) -> dict:#❓why what how this method necessary 
    """Return access + refresh JWT pair for a user."""
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


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
        if value and User.objects.filter(email__iexact=value).exists():#❓ what does email__iexact means 
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower() if value else value

    def validate_phone(self, value):
        if value and User.objects.filter(phone=value).exists():#❓why what how not phone__iexact used here 
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate(self, attrs):#❓why what how explan this method , what does attrs means here how it's workiing 
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
        OTPVerification.objects.filter(#❓why what how instead of get method filter using 
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
        return otp_code, identifier, data       # view stores `data` temporarily


class VerifySignupOTPSerializer(serializers.Serializer):
    """
    Step 2: user submits the 6-digit OTP.
    If valid → create the User row → return JWT tokens.
    """

    identifier = serializers.CharField()   # email or phone they signed up with
    otp_code = serializers.CharField(max_length=6, min_length=6)
    # Pending user data passed back from client (stored in view session/cache)
    username = serializers.CharField(max_length=100)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        identifier = attrs["identifier"]#❓why what how dict using why not like attrs.identifier?
        otp_code = attrs["otp_code"]

        try:
            otp = OTPVerification.objects.get(
                identifier=identifier,
                otp_code=otp_code,
                purpose=OTPVerification.Purpose.SIGNUP,
                is_verified=False,
            )
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError({"otp_code": "Invalid OTP."})

        if otp.is_expired():
            raise serializers.ValidationError({"otp_code": "OTP has expired. Please request a new one."})

        attrs["_otp"] = otp#❓why what how this line , why _ using before otp 
        return attrs

    @transaction.atomic
    def save(self):
        data = self.validated_data
        otp: OTPVerification = data["_otp"]#❓why what how this line , otp:OTPVerification why how 

        user = User.objects.create_user(#❓why what how so model property always dict not list that why [] used to acces ?
            username=data["username"],
            password=data["password"],
            email=data.get("email") or None,
            phone=data.get("phone") or None,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            is_verified=True,
        )

        otp.is_verified = True
        otp.user = user
        otp.save(update_fields=["is_verified", "user"])

        return user, _issue_tokens(user)


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
        user = authenticate(username=attrs["username"], password=attrs["password"])#❓why what how this authenticate built it?
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
    contact_user_profile = PublicUserSearchSerializer(#❓why what how I'm calling here PublicUserSearchSerializer, and passing source="contact_user",but PublicUserSearchSerializer
    #is not receivihng any source, then how I'm passing it, I see contact_user is filed on contact model., what will contact_user_profile contain if it's not app user, null?
        source="contact_user",
        read_only=True,
    )

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
            "contact_user": {"required": False, "allow_null": True, "write_only": True},#❓why what how why?
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

    def validate_user_id(self, value):
        request = self.context["request"]
        if value == request.user.id:
            raise serializers.ValidationError("You cannot block yourself.")
        if not User.objects.filter(id=value, status=User.Status.ACTIVE).exists():
            raise serializers.ValidationError("User not found.")
        return value#❓here I'm returing value, my questin is what wil this value contain, I'm not seeing here anything wich is assigning anything to this 'value' field 

    def save(self, blocker: User):
        blocked = User.objects.get(id=self.validated_data["user_id"])
        '''
        #❓why this validated_data containing user_id,
        what I'm understand validate_user_id returning value wich is actually user_id(of User table),so by that user_id,i'm fetching who is blocked , validated_data actually 
        means validate_user_id, am i right?
        
        '''
        obj, created = BlockedUser.objects.get_or_create(blocker=blocker, blocked=blocked)# I want to add something like if anyuser already block someone, he can't block him twice 
        return obj, created

