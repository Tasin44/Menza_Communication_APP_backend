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
class SignupView(APIView):
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
        serializer.is_valid(raise_exception=True)#❓i see SignupSerializer contains several valid method, which valid method this is_valid catching?

        otp_code, identifier, pending_data = serializer.save()

        # Store pending signup data server-side so verify step can use it.
        # In production: use Redis with TTL=10min, key=f"signup:{identifier}"
        # For now we return pending_data to client and expect it back in verify step.
        # The client sends it again with the OTP — see VerifySignupOTPSerializer.
        _send_otp(identifier, otp_code, "signup")#❓why _used before _send_otp
        return Response(
            {
                "detail": f"OTP sent to {identifier}. Please verify within 10 minutes.",
                "identifier": identifier,
            },
            status=status.HTTP_200_OK,
        )


class VerifySignupOTPView(APIView):
    """
    POST /auth/signup/verify/
    Body: { identifier, otp_code, username, email?, phone?, password, first_name?, last_name? }

    On success: creates user, returns JWT tokens.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifySignupOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)#❓why what how reaise_exception true means here 

        user, tokens = serializer.save()#❓I've not seen any tokens field on the VerifySignupOTPSerializer, then why it's using here , also why user,token=serializer.save(), why not other field 


        return Response(
            {
                "detail": "Account created successfully.",
                "tokens": tokens,
                "user": UserProfileSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
class LoginView(APIView):
    """
    POST /auth/login/
    Body: { username, password, device_token?, platform? }
    Returns JWT tokens.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, tokens = serializer.save()

        return Response(
            {
                "tokens": tokens,
                "user": UserProfileSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────
class LogoutView(APIView):
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
            return Response(
                {"detail": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            return Response(
                {"detail": "Token is invalid or already expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "Logged out successfully."}, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────
# FORGOT PASSWORD
# ─────────────────────────────────────────────
class ForgotPasswordView(APIView):
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
                return Response(
                    {"detail": "If an account with this detail exists, an OTP has been sent."},
                    status=status.HTTP_200_OK,
                )
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        otp_code, identifier = serializer.save()
        _send_otp(identifier, otp_code, "forgot_password")

        return Response(
            {
                "detail": "If an account with this detail exists, an OTP has been sent.",
                "identifier": identifier,
            },
            status=status.HTTP_200_OK,
        )


class VerifyForgotPasswordOTPView(APIView):
    """
    POST /auth/forgot-password/verify/
    Body: { identifier, otp_code }

    Returns reset_token (otp.id) to use in the reset step.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyForgotPasswordOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp = serializer.save()

        return Response(
            {
                "detail": "OTP verified. Proceed to reset your password.",
                "reset_token": otp.id,   # used in next step
            },
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    """
    POST /auth/reset-password/
    Body: { reset_token, new_password, confirm_password }

    Spec: New password + Confirm password fields.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"detail": "Password reset successfully. Please log in."},
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────────
class ProfileView(APIView):
    """
    GET  /auth/profile/   → return own full profile
    Spec fields shown: username, image, email, phone
    Only image is editable — handled in UpdateAvatarView.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)


class UpdateAvatarView(APIView):
    """
    PATCH /auth/profile/avatar/
    Body: { profile_image: "https://..." }

    Spec: "Just image editable here"
    Frontend uploads to S3/R2 first to get the URL, then sends URL here.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = UpdateAvatarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save(user=request.user)
        return Response(
            {
                "detail": "Profile image updated.",
                "profile_image": user.profile_image,
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────
# USER SEARCH
# ─────────────────────────────────────────────
class UserSearchView(APIView):
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
            return Response(
                {"detail": "Search query must be at least 2 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get ids of users this person has blocked (hide them from search too)
        blocked_ids = BlockedUser.objects.filter(
            blocker=request.user
        ).values_list("blocked_id", flat=True)#❓what does .values_list use here means ?

        # Search by username (partial, case-insensitive) or exact phone
        users = (#❓explain this whole users() part 
            User.objects.filter(
                status=User.Status.ACTIVE,
                is_verified=True,
            )
            .filter(
                # username partial match OR exact phone match
                # Using Q objects — import added below
                **{}
            )
            .exclude(id=request.user.id)   # exclude self
            .exclude(id__in=blocked_ids)
        )

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
        return Response(serializer.data, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────
# CONTACTS
# ─────────────────────────────────────────────
class ContactListCreateView(APIView):
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
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = ContactSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        contact = serializer.save()
        return Response(
            ContactSerializer(contact, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ContactDetailView(APIView):
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
            return Response(
                {"detail": "Contact not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        contact.delete()
        return Response(
            {"detail": "Contact removed."},
            status=status.HTTP_204_NO_CONTENT,
        )


# ─────────────────────────────────────────────
# BLOCKED USERS
# ─────────────────────────────────────────────
class BlockedUserListView(APIView):
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
        return Response(serializer.data, status=status.HTTP_200_OK)


class BlockUserView(APIView):
    """
    POST   /auth/block/         → block a user { user_id }
    DELETE /auth/block/<id>/    → unblock by BlockedUser.id
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BlockUserSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        obj, created = serializer.save(blocker=request.user)

        if not created:
            return Response(
                {"detail": "User is already blocked."},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"detail": "User blocked successfully."},
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, pk):
        try:
            block = BlockedUser.objects.get(id=pk, blocker=request.user)
        except BlockedUser.DoesNotExist:
            return Response(
                {"detail": "Block record not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        block.delete()
        return Response(
            {"detail": "User unblocked."},
            status=status.HTTP_204_NO_CONTENT,
        )
