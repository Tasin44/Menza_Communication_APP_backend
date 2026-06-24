from django.shortcuts import render

# Create your views here.
"""
Menza — authapp views

All auth flows from spec:
  POST /auth/signup/             → send OTP (user not created yet)
  POST /auth/signup/verify/      → verify OTP → create user → return tokens
  POST /auth/login/              → username + password → tokens
  POST /auth/token/refresh/      → simplejwt built-in (wired in urls.py)
  POST /auth/logout/             → blacklist refresh token
  POST /auth/forgot-password/    → send reset OTP
  POST /auth/forgot-password/verify/   → verify OTP → return reset_token
  POST /auth/reset-password/     → new + confirm password

Profile:
  GET  /auth/profile/            → own profile
  PATCH /auth/profile/avatar/    → update profile image (only editable field per spec)

User search (for starting conversations):
  GET  /auth/users/search/?q=    → search by username or phone

Contacts:
  GET  /auth/contacts/           → list own contacts
  POST /auth/contacts/           → create contact
  DELETE /auth/contacts/<id>/    → delete contact

Block:
  GET  /auth/blocked/            → list blocked users
  POST /auth/block/              → block a user
  DELETE /auth/block/<id>/       → unblock
"""

from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from aamyproject.mixins import StandardResponseMixin

from .models import User, Contact, BlockedUser
from .serializers import (
    SignupSerializer,
    VerifySignupOTPSerializer,
    LoginSerializer,
    ForgotPasswordSerializer,
    VerifyForgotPasswordOTPSerializer,
    ResetPasswordSerializer,
    UserProfileSerializer,
    UpdateAvatarSerializer,
    PublicUserSearchSerializer,
    ContactSerializer,
    BlockedUserSerializer,
    BlockUserSerializer,
)

# ── placeholder: replace with your actual email/SMS sender ──────────────────
def _send_otp(identifier: str, otp_code: str, purpose: str):
    """
    Replace this stub with:
    - SendGrid / AWS SES for email OTPs
    - Twilio / AWS SNS for SMS OTPs
    Detect type: '@' in identifier → email, else → SMS
    """
    print(f"[OTP STUB] Sending {purpose} OTP {otp_code} to {identifier}")


# ─────────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────────
class SignupView(StandardResponseMixin, APIView):
    """
    POST /auth/signup/
    Body: { username, email|phone, password, first_name?, last_name? }

    Flow:
    1. Validate uniqueness
    2. Save pending data to cache (keyed by identifier)
    3. Create OTP record
    4. Send OTP to email or phone
    5. Return 200 — user is NOT created yet
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)

        otp_code, identifier, pending_data = serializer.save()

        # Store pending signup data server-side so verify step can use it.
        # In production: use Redis with TTL=10min, key=f"signup:{identifier}"
        # For now we return pending_data to client and expect it back in verify step.
        # The client sends it again with the OTP — see VerifySignupOTPSerializer.
        _send_otp(identifier, otp_code, "signup")#❓why _used before _send_otp
        return self.success_response(
            data={"identifier": identifier},
            message=f"OTP sent to {identifier}. Please verify within 10 minutes."
        )


class VerifySignupOTPView(StandardResponseMixin, APIView):
    """
    POST /auth/signup/verify/
    Body: { otp_code }

    On success: creates user, returns JWT tokens and signup details.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifySignupOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)

        user, tokens, pending_data = serializer.save()
        
        # User requested: "return user id, all the details got from signup and acceess refresh token"
        pending_data.pop("password", None) # Do not return password
        response_data = {
            "user_id": user.id,
            "signup_details": pending_data,
            "tokens": tokens,
            "user": UserProfileSerializer(user).data,
        }

        return self.success_response(
            data=response_data,
            message="Account created successfully.",
            status_code=status.HTTP_201_CREATED
        )


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
class LoginView(StandardResponseMixin, APIView):
    """
    POST /auth/login/
    Body: { username, password, device_token?, platform? }
    Returns JWT tokens.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)

        user, tokens = serializer.save()

        return self.success_response(
            data={
                "tokens": tokens,
                "user": UserProfileSerializer(user).data,
            },
            message="Login successful."
        )


# ─────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────
class LogoutView(StandardResponseMixin, APIView):
    """
    POST /auth/logout/
    Body: { refresh }
    Blacklists the refresh token so it can't be used again.
    Requires simplejwt BLACKLIST app in INSTALLED_APPS.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return self.error_response("Refresh token is required.")
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            return self.error_response("Token is invalid or already expired.")
        return self.success_response(data={}, message="Logged out successfully.")


# ─────────────────────────────────────────────
# FORGOT PASSWORD
# ─────────────────────────────────────────────
class ForgotPasswordView(StandardResponseMixin, APIView):
    """
    POST /auth/forgot-password/
    Body: { identifier }  ← email or phone number

    Spec: Choose option → enter email/phone → OTP sent.
    Always returns 200 even if identifier not found (security).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)

        # We still call is_valid but catch the "account not found" validation
        # separately so the HTTP response is always 200 (no account enumeration)
        if not serializer.is_valid():#❓why not I called here serializer.is_valid(raise_exception=true) like I did in previous view classes 
            # If only error is our vague "if account exists" message, return 200
            errors = serializer.errors
            if list(errors.keys()) == ["identifier"]:
                return self.success_response(
                    data={},
                    message="If an account with this detail exists, an OTP has been sent."
                )
            return self.error_response("Validation Error", data=errors)

        otp_code, identifier = serializer.save()
        _send_otp(identifier, otp_code, "forgot_password")

        return self.success_response(
            data={"identifier": identifier},
            message="If an account with this detail exists, an OTP has been sent."
        )


class VerifyForgotPasswordOTPView(StandardResponseMixin, APIView):
    """
    POST /auth/forgot-password/verify/
    Body: { identifier, otp_code }

    Returns reset_token (otp.id) to use in the reset step.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyForgotPasswordOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)

        otp = serializer.save()

        return self.success_response(
            data={"reset_token": otp.id},
            message="OTP verified. Proceed to reset your password."
        )


class ResetPasswordView(StandardResponseMixin, APIView):
    """
    POST /auth/reset-password/
    Body: { reset_token, new_password, confirm_password }

    Spec: New password + Confirm password fields.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)
        serializer.save()

        return self.success_response(data={}, message="Password reset successfully. Please log in.")


# ─────────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────────
class ProfileView(StandardResponseMixin, APIView):
    """
    GET  /auth/profile/   → return own full profile
    Spec fields shown: username, image, email, phone
    Only image is editable — handled in UpdateAvatarView.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return self.success_response(data=serializer.data, message="Profile fetched successfully.")


class UpdateAvatarView(StandardResponseMixin, APIView):
    """
    PATCH /auth/profile/avatar/
    Body: { profile_image: "https://..." }

    Spec: "Just image editable here"
    Frontend uploads to S3/R2 first to get the URL, then sends URL here.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = UpdateAvatarSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)
        user = serializer.save(user=request.user)
        return self.success_response(
            data={"profile_image": user.profile_image},
            message="Profile image updated."
        )


# ─────────────────────────────────────────────
# USER SEARCH
# ─────────────────────────────────────────────
class UserSearchView(StandardResponseMixin, APIView):
    """
    GET /auth/users/search/?q=<username_or_phone>

    Spec:
    - User can search by username OR phone number
    - Used when starting a new conversation
    - Returns only username and image (per spec: "It'll show username, image just")
    - Blocked users are excluded from results
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get("q", "").strip()

        if len(query) < 2:
            return self.error_response("Search query must be at least 2 characters.")

        # Get ids of users this person has blocked (hide them from search too)
        blocked_ids = BlockedUser.objects.filter(
            blocker=request.user
        ).values_list("blocked_id", flat=True)#❓what does .values_list use here means ?

        from django.db.models import Q
        users = User.objects.filter(
            Q(username__icontains=query) | Q(phone=query),
            status=User.Status.ACTIVE,
            is_verified=True,
        ).exclude(
            id=request.user.id
        ).exclude(
            id__in=blocked_ids
        )[:20]   # cap at 20 results

        serializer = PublicUserSearchSerializer(users, many=True)
        return self.success_response(data=serializer.data, message="Users fetched successfully.")


# ─────────────────────────────────────────────
# CONTACTS
# ─────────────────────────────────────────────
class ContactListCreateView(StandardResponseMixin, APIView):
    """
    GET  /auth/contacts/   → list all contacts for logged-in user
    POST /auth/contacts/   → create a new contact

    Spec:
    - Create contact: first name, last name, phone number
    - If phone belongs to Menza user → is_app_user=True, can message them
    - If not → is_app_user=False, show invite option
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        contacts = (
            Contact.objects.filter(owner=request.user)
            .select_related("contact_user")
            .order_by("contact_first_name", "contact_last_name")
        )
        serializer = ContactSerializer(
            contacts, many=True, context={"request": request}#❓what does meany true means here , when I've to pass it 
        )
        return self.success_response(data=serializer.data, message="Contacts fetched successfully.")

    def post(self, request):
        serializer = ContactSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)
        contact = serializer.save()
        return self.success_response(
            data=ContactSerializer(contact, context={"request": request}).data,
            message="Contact created successfully.",
            status_code=status.HTTP_201_CREATED
        )


class ContactDetailView(StandardResponseMixin, APIView):
    """
    DELETE /auth/contacts/<id>/   → remove a contact

    No update endpoint — spec doesn't mention editing contacts.
    """

    permission_classes = [IsAuthenticated]

    def _get_contact(self, request, pk):
        try:
            return Contact.objects.get(id=pk, owner=request.user)#❓how to know I've to pass this id and owner here ?
        except Contact.DoesNotExist:
            return None

    def delete(self, request, pk):
        contact = self._get_contact(request, pk)
        if not contact:
            return self.error_response("Contact not found.", status_code=status.HTTP_404_NOT_FOUND)
        contact.delete()
        return self.success_response(data={}, message="Contact removed.")


# ─────────────────────────────────────────────
# BLOCKED USERS
# ─────────────────────────────────────────────
class BlockedUserListView(StandardResponseMixin, APIView):
    """
    GET /auth/blocked/
    Spec: Blocked user list (shown on profile page).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        blocked = (
            BlockedUser.objects.filter(blocker=request.user)
            .select_related("blocked")
            .order_by("-created_at")
        )
        serializer = BlockedUserSerializer(blocked, many=True)
        return self.success_response(data=serializer.data, message="Blocked users fetched successfully.")


class BlockUserView(StandardResponseMixin, APIView):
    """
    POST   /auth/block/         → block a user { user_id }
    DELETE /auth/block/<id>/    → unblock by BlockedUser.id
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BlockUserSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return self.error_response("Validation Error", data=serializer.errors)
        obj, created = serializer.save(blocker=request.user)

        if not created:
            return self.success_response(data={}, message="User is already blocked.")
        return self.success_response(
            data={},
            message="User blocked successfully.",
            status_code=status.HTTP_201_CREATED
        )

    def delete(self, request, pk):
        try:
            block = BlockedUser.objects.get(id=pk, blocker=request.user)
        except BlockedUser.DoesNotExist:
            return self.error_response("Block record not found.", status_code=status.HTTP_404_NOT_FOUND)
        block.delete()
        return self.success_response(data={}, message="User unblocked.")
