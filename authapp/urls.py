"""
authapp URL configuration

All auth, profile, contacts, and block routes live here.
Mount in project urls.py as:
    path("api/auth/", include("authapp.urls")),
"""

from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    # Signup
    SignupView,
    VerifySignupOTPView,
    # Login / Logout
    LoginView,
    LogoutView,
    # Forgot + Reset password
    ForgotPasswordView,
    VerifyForgotPasswordOTPView,
    ResetPasswordView,
    # Profile
    ProfileView,
    UpdateAvatarView,
    # User search
    UserSearchView,
    # Contacts
    ContactListCreateView,
    ContactDetailView,
    # Block
    BlockedUserListView,
    BlockUserView,
)

urlpatterns = [
    # ── Signup ────────────────────────────────────────────────────────────
    # Step 1: submit details → OTP sent
    path("signup/", SignupView.as_view(), name="signup"),
    # Step 2: verify OTP → account created → tokens returned
    path("signup/verify/", VerifySignupOTPView.as_view(), name="signup-verify"),

    # ── Login / Logout ────────────────────────────────────────────────────
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    # ── Token refresh (simplejwt built-in) ────────────────────────────────
    # POST { "refresh": "<token>" } → returns new access token
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),

    # ── Forgot password ───────────────────────────────────────────────────
    # Step 1: enter email or phone → OTP sent
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    # Step 2: verify OTP → get reset_token
    path("forgot-password/verify/", VerifyForgotPasswordOTPView.as_view(), name="forgot-password-verify"),
    # Step 3: new password + confirm → done
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),

    # ── Profile ───────────────────────────────────────────────────────────
    # GET own profile (username, image, email, phone — all read-only except image)
    path("profile/", ProfileView.as_view(), name="profile"),
    # PATCH profile image only (spec: "Just image editable here")
    path("profile/avatar/", UpdateAvatarView.as_view(), name="profile-avatar"),

    # ── User search ───────────────────────────────────────────────────────
    # GET /auth/users/search/?q=<username_or_phone>
    # Returns id, username, profile_image only (spec requirement)
    path("users/search/", UserSearchView.as_view(), name="user-search"),

    # ── Contacts ──────────────────────────────────────────────────────────
    # GET  → list all contacts
    # POST → create contact { contact_first_name, contact_last_name, contact_phone }
    path("contacts/", ContactListCreateView.as_view(), name="contacts"),
    # DELETE → remove contact by id
    path("contacts/<int:pk>/", ContactDetailView.as_view(), name="contact-detail"),

    # ── Blocked users ─────────────────────────────────────────────────────
    # GET /auth/blocked/ → list all blocked users (profile page)
    path("blocked/", BlockedUserListView.as_view(), name="blocked-list"),
    # POST /auth/block/ → block { user_id }
    path("block/", BlockUserView.as_view(), name="block-user"),
    # DELETE /auth/block/<id>/ → unblock by BlockedUser.id
    path("block/<int:pk>/", BlockUserView.as_view(), name="unblock-user"),
]
