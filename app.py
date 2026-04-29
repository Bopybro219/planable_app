import hashlib
import json
import logging
import os
import re
import secrets
import smtplib
import threading
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from html import escape as html_escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_migrate import Migrate, upgrade
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import Integer, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from planira_presenters import (
    APP_NAME,
    PLAN_DETAILS,
    PLACE_STATUS_LABELS,
    TAGLINE,
    VERIFICATION_SOURCE_LABELS,
    build_access_signal as present_access_signal,
    build_place_card as present_place_card,
    build_signal_examples,
    humanize_label,
    humanize_plan_name as present_plan_name,
    verification_status as present_verification_status,
)
from slugify_fallback import slugify

try:
    import stripe
except ImportError:  # pragma: no cover - handled at runtime if dependency is missing
    stripe = None

load_dotenv()


def normalize_database_url(raw_url):
    database_url = (raw_url or "").strip()
    if not database_url:
        return "sqlite:///planable.db"
    if database_url.startswith("postgres://"):
        # Hosted providers still sometimes emit the legacy scheme.
        return database_url.replace("postgres://", "postgresql://", 1)
    return database_url


def build_engine_options(database_url):
    if database_url.startswith("postgresql"):
        return {
            "pool_pre_ping": True,
        }
    return {}


def is_sqlite_database_uri(database_url):
    return database_url.startswith("sqlite:")


def is_postgresql_database_uri(database_url):
    return database_url.startswith("postgresql")


TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
SECRET_KEY_MIN_LENGTH = 32
SECRET_KEY_PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change-me",
    "dev",
    "dev-secret",
    "dev-change-me",
    "example",
    "placeholder",
    "replace-me",
    "replace-with-a-long-random-secret",
    "secret",
    "test",
    "unsafe-dev-secret",
}
FIXED_API_SCOPES = {"places:read", "places:write", "api:usage", "admin:read"}
LEGACY_SCOPE_ALIASES = {"search:read": "places:read"}
DEFAULT_API_KEY_SCOPES = ("places:read", "api:usage")
DISABLED_API_PACK_PLAN_KEYS = {"api_20", "api_50", "api_100"}
API_PACK_DISABLED_MESSAGE = (
    "API pack checkout is temporarily disabled while lookup-credit accounting is being completed."
)
API_PACK_ACCESS_DISABLED_MESSAGE = (
    "API pack access is temporarily unavailable while lookup-credit accounting is being finished."
)
RATE_LIMIT_ERROR_MESSAGE = "Too many requests. Please wait a moment and try again."
_RATE_LIMIT_STATE = defaultdict(deque)
_RATE_LIMIT_LOCK = threading.Lock()
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
DEFAULT_OG_IMAGE_FILENAME = "logo.png"
PRIVATE_PATH_PREFIXES = (
    "/account",
    "/admin",
    "/api/",
    "/auth/",
    "/billing/",
    "/dashboard",
    "/health",
    "/obs/",
    "/staff/",
)
NOINDEX_FOLLOW_PATHS = {"/search"}
NOINDEX_NOFOLLOW_PATHS = {"/health"}
CONSENT_COOKIE_NAME = "planira_consent"
CONSENT_COOKIE_MAX_AGE = 60 * 60 * 24 * 180
CONSENT_COOKIE_VERSION = 1
CONSENT_CATEGORIES = ("necessary", "analytics", "marketing")
SENSITIVE_ANALYTICS_PARAM_KEYS = {
    "address",
    "address1",
    "email",
    "message",
    "name",
    "postcode",
    "subject",
}
ANALYTICS_DISABLED_PATH_PREFIXES = ("/admin", "/staff", "/dashboard", "/obs")
ADS_DISABLED_PATH_PREFIXES = PRIVATE_PATH_PREFIXES + ("/login",)


def env_flag(name, default=False):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in TRUTHY_ENV_VALUES


def support_email_address():
    return (app.config.get("SUPPORT_EMAIL") or app.config.get("MAIL_DEFAULT_SENDER") or "").strip()


def secret_key_issues(secret_key):
    cleaned = (secret_key or "").strip()
    lowered = cleaned.lower()
    issues = []
    if not cleaned:
        issues.append("missing")
        return issues
    if len(cleaned) < SECRET_KEY_MIN_LENGTH:
        issues.append("too_short")
    if lowered in SECRET_KEY_PLACEHOLDER_VALUES:
        issues.append("placeholder")
    if len(set(cleaned)) < 10:
        issues.append("low_entropy")
    complexity_score = sum(
        bool(pattern.search(cleaned))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"\d"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if complexity_score < 3:
        issues.append("not_complex_enough")
    return issues


app = Flask(__name__)
database_url = normalize_database_url(os.getenv("DATABASE_URL"))
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = build_engine_options(database_url)
app.config["STRIPE_SECRET_KEY"] = os.getenv("STRIPE_SECRET_KEY", "").strip()
app.config["STRIPE_PUBLISHABLE_KEY"] = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
app.config["STRIPE_WEBHOOK_SECRET"] = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
app.config["ENVIRONMENT"] = os.getenv("FLASK_ENV", os.getenv("APP_ENV", "development")).strip().lower() or "development"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
app.config["PERMANENT_SESSION_LIFETIME"] = int(os.getenv("SESSION_LIFETIME_SECONDS", "1209600"))
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", str(2 * 1024 * 1024)))
app.config["PREFERRED_URL_SCHEME"] = "https" if app.config["SESSION_COOKIE_SECURE"] else "http"
app.config["RATE_LIMIT_ENABLED"] = env_flag("RATE_LIMIT_ENABLED", default=app.config["ENVIRONMENT"] == "production")
app.config["PROFILE_IMAGE_MAX_BYTES"] = int(os.getenv("PROFILE_IMAGE_MAX_BYTES", str(2 * 1024 * 1024)))
app.config["PLACE_IMAGE_MAX_BYTES"] = int(os.getenv("PLACE_IMAGE_MAX_MB", "2")) * 1024 * 1024
app.config["PROFILE_IMAGE_UPLOAD_DIR"] = os.getenv(
    "PROFILE_IMAGE_UPLOAD_DIR",
    os.path.join(app.static_folder, "uploads", "profile_pics"),
)
app.config["PLACE_IMAGE_UPLOAD_DIR"] = os.getenv(
    "PLACE_IMAGE_UPLOAD_DIR",
    os.path.join(app.static_folder, "uploads", "place_images"),
)
app.config["CLOUDFLARE_TURNSTILE_SITE_KEY"] = os.getenv("CLOUDFLARE_TURNSTILE_SITE_KEY", "").strip()
app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = os.getenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", "").strip()
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "").strip()
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", "").strip()
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", "").strip()
app.config["MAIL_USE_TLS"] = env_flag("MAIL_USE_TLS", default=True)
app.config["MAIL_USE_SSL"] = env_flag("MAIL_USE_SSL", default=False)
app.config["SUPPORT_EMAIL"] = os.getenv("SUPPORT_EMAIL", "").strip()
app.config["EMAIL_ENABLED"] = env_flag("EMAIL_ENABLED", default=False)
app.config["EMAIL_DEV_MODE"] = env_flag(
    "EMAIL_DEV_MODE",
    default=app.config["ENVIRONMENT"] != "production",
)
app.config["NEWSLETTER_ENABLED"] = env_flag("NEWSLETTER_ENABLED", default=False)
app.config["GA_MEASUREMENT_ID"] = os.getenv("GA_MEASUREMENT_ID", "").strip()
app.config["ENABLE_ANALYTICS_IN_DEV"] = env_flag("ENABLE_ANALYTICS_IN_DEV", default=False)
app.config["ADSENSE_CLIENT_ID"] = os.getenv("ADSENSE_CLIENT_ID", "").strip()
app.config["ADSENSE_ENABLED"] = env_flag("ADSENSE_ENABLED", default=False)
app.config["ENABLE_ADS_IN_DEV"] = env_flag("ENABLE_ADS_IN_DEV", default=False)
app.config["ADSENSE_SLOT_SEARCH_RESULTS"] = os.getenv("ADSENSE_SLOT_SEARCH_RESULTS", "").strip()
app.config["ADSENSE_SLOT_PLACE_DETAIL"] = os.getenv("ADSENSE_SLOT_PLACE_DETAIL", "").strip()
app.config["ADSENSE_SLOT_FOOTER"] = os.getenv("ADSENSE_SLOT_FOOTER", "").strip()

trusted_hosts = [host.strip() for host in os.getenv("TRUSTED_HOSTS", "").split(",") if host.strip()]
if trusted_hosts:
    app.config["TRUSTED_HOSTS"] = trusted_hosts

server_name = os.getenv("SERVER_NAME", "").strip()
if server_name:
    app.config["SERVER_NAME"] = server_name

ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
CSRF_EXEMPT_ENDPOINTS = {"stripe_webhook"}
URL_FIELD_SCHEMES = ("http://", "https://")
API_KEY_TEST_PREFIX = "plnr_test_"
API_KEY_LIVE_PREFIX = "plnr_live_"
API_KEY_PATTERN = re.compile(r"^plnr_(?:live|test)_[A-Za-z0-9_-]{24,}$")
CONTACT_PHONE = os.getenv("PUBLIC_CONTACT_PHONE", "01604 289096").strip() or "01604 289096"
API_ACCESS_REQUIRED_MESSAGE = "API access requires an active Planira API or Early Access plan."
DEFAULT_MEMBER_ROLE = "member"
PLACE_WRITE_STATUS_VALUES = {"needs_call", "calling", "callback", "verified"}
ACCESSIBILITY_CHOICE_VALUES = {"yes", "no", "unknown", "partial"}
PROFILE_IMAGE_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
PROFILE_IMAGE_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
PROFILE_IMAGE_STATIC_PREFIX = "uploads/profile_pics"
PLACE_IMAGE_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
PLACE_IMAGE_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
PLACE_IMAGE_STATIC_PREFIX = "uploads/place_images"
MANUAL_ENTITLEMENT_ALLOWED_PLANS = {"paid_consumer", "api_20", "api_50", "api_100", "business"}
CONTACT_MESSAGE_STATUS_VALUES = {"new", "open", "replied", "closed"}
NEWSLETTER_STATUS_VALUES = {"subscribed", "unsubscribed"}
NEWSLETTER_CONSENT_TEXT = "I want to receive occasional Planira email updates and can unsubscribe at any time."

proxy_fix_count = int(os.getenv("PROXY_FIX_COUNT", "0"))
if proxy_fix_count:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=proxy_fix_count,
        x_proto=proxy_fix_count,
        x_host=proxy_fix_count,
        x_port=proxy_fix_count,
    )

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app.logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

if app.config["ENVIRONMENT"] == "production":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["DEBUG"] = False
else:
    dev_secret_issues = secret_key_issues(app.config["SECRET_KEY"])
    if dev_secret_issues:
        warning_message = (
            "Planira is using an unsafe development SECRET_KEY. "
            "Use a long random value before sharing the environment or testing auth flows."
        )
        app.logger.warning("%s Issues: %s", warning_message, ", ".join(dev_secret_issues))
        warnings.warn(warning_message, RuntimeWarning, stacklevel=2)

production_secret_issues = secret_key_issues(app.config["SECRET_KEY"])
if app.config["ENVIRONMENT"] == "production" and production_secret_issues:
    raise RuntimeError(
        "Refusing to start in production with an unsafe SECRET_KEY. "
        f"Issues: {', '.join(production_secret_issues)}."
    )

db = SQLAlchemy(app)
migrate = Migrate(app, db)
oauth = OAuth(app)
app.config.setdefault("_DB_SCHEMA_READY", False)

if stripe and app.config["STRIPE_SECRET_KEY"]:
    stripe.api_key = app.config["STRIPE_SECRET_KEY"]

google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url=os.getenv("GOOGLE_DISCOVERY_URL", "https://accounts.google.com/.well-known/openid-configuration"),
    client_kwargs={"scope": "openid email profile"},
)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    google_sub = db.Column(db.String(255), unique=True, index=True)
    name = db.Column(db.String(255))
    picture = db.Column(db.Text)
    profile_image_filename = db.Column(db.String(255))
    role = db.Column(db.String(50), default=DEFAULT_MEMBER_ROLE)
    plan = db.Column(db.String(50), default="free", nullable=False)
    manual_entitlement_enabled = db.Column(db.Boolean, default=False, nullable=False)
    manual_entitlement_plan = db.Column(db.String(50))
    access_override_until = db.Column(db.DateTime)
    manual_entitlement_note = db.Column(db.String(255))
    stripe_customer_id = db.Column(db.String(255), unique=True, index=True)
    stripe_subscription_id = db.Column(db.String(255), unique=True, index=True)
    subscription_status = db.Column(db.String(80), index=True)
    subscription_current_period_end = db.Column(db.DateTime)
    subscription_cancel_at_period_end = db.Column(db.Boolean)
    monthly_search_limit = db.Column(db.Integer)
    search_credits = db.Column(db.Integer, default=0, nullable=False)
    community_points = db.Column(db.Integer, default=0, nullable=False)
    rank_title = db.Column(db.String(120))
    age_verification_status = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime)

    @property
    def avatar_url(self):
        return get_avatar_url(self)

    @property
    def avatar_initials(self):
        return get_avatar_initials(self)


class Place(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    slug = db.Column(db.String(300), unique=True, index=True)
    venue_type = db.Column(db.String(80), default="pub")
    phone = db.Column(db.String(80))
    website = db.Column(db.String(255))
    address1 = db.Column(db.String(255))
    town = db.Column(db.String(120), index=True)
    county = db.Column(db.String(120), index=True)
    postcode = db.Column(db.String(30), index=True)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    priority = db.Column(db.Integer, default=3)
    status = db.Column(db.String(60), default="needs_call", index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PlaceImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    original_filename = db.Column(db.String(255))
    caption = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    is_approved = db.Column(db.Boolean, default=True, nullable=False, index=True)
    place = db.relationship("Place", backref=db.backref("images", lazy="dynamic", order_by="desc(PlaceImage.created_at)"))
    uploader = db.relationship("User", backref=db.backref("uploaded_place_images", lazy="dynamic"))


class AccessibilityProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), unique=True, nullable=False)
    place = db.relationship("Place", backref=db.backref("accessibility", uselist=False))
    toilets_available = db.Column(db.String(30), default="unknown")
    toilet_location = db.Column(db.String(120))
    toilet_distance_from_bar = db.Column(db.String(120))
    toilet_distance_from_bar_m = db.Column(db.Float)
    accessible_toilet = db.Column(db.String(30), default="unknown")
    baby_changing = db.Column(db.String(30), default="unknown")
    baby_changing_location = db.Column(db.String(120))
    step_free_entrance = db.Column(db.String(30), default="unknown")
    stairs_inside = db.Column(db.String(30), default="unknown")
    lift_available = db.Column(db.String(30), default="unknown")
    disabled_parking = db.Column(db.String(30), default="unknown")
    sensory_notes = db.Column(db.Text)
    public_comments = db.Column(db.Text)
    internal_notes = db.Column(db.Text)
    source = db.Column(db.String(80), default="not_verified")
    confidence_score = db.Column(db.Integer, default=0)
    last_verified_at = db.Column(db.DateTime)
    last_verified_by = db.Column(db.String(255))
    verified_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    verified_by_user = db.relationship("User", foreign_keys=[verified_by_user_id])
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CallLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), nullable=False)
    place = db.relationship("Place", backref="call_logs")
    user_email = db.Column(db.String(255))
    result = db.Column(db.String(80), default="answered")
    contact_name = db.Column(db.String(120))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), nullable=False)
    place = db.relationship("Place", backref="comments")
    user_email = db.Column(db.String(255))
    body = db.Column(db.Text, nullable=False)
    is_public = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    reviewed_by_user = db.relationship("User", foreign_keys=[reviewed_by_user_id])
    reviewed_at = db.Column(db.DateTime)
    moderation_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SearchEvent(db.Model):
    __table_args__ = (
        db.Index("ix_search_event_user_created_at", "user_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    user = db.relationship("User", backref="search_events")
    query_text = db.Column(db.String(255))
    town = db.Column(db.String(120))
    accessible = db.Column(db.String(30))
    filters_json = db.Column(db.JSON)
    result_count = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class APIKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user = db.relationship("User", foreign_keys=[user_id])
    key_hash = db.Column(db.String(255), unique=True, nullable=False, index=True)
    label = db.Column(db.String(120))
    scopes_json = db.Column(db.JSON)
    monthly_lookup_limit = db.Column(db.Integer)
    lookup_credits = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    last_used_at = db.Column(db.DateTime)


class APILookupEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_key.id"), nullable=False, index=True)
    api_key = db.relationship("APIKey", foreign_keys=[api_key_id])
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    user = db.relationship("User", foreign_keys=[user_id])
    endpoint = db.Column(db.String(255), nullable=False)
    query = db.Column(db.Text)
    status_code = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    actor_user = db.relationship("User", foreign_keys=[actor_user_id])
    action = db.Column(db.String(120), nullable=False, index=True)
    entity_type = db.Column(db.String(120), nullable=False, index=True)
    entity_id = db.Column(db.String(120), nullable=False, index=True)
    before_json = db.Column(db.JSON)
    after_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    reason = db.Column(db.Text)


class ContactMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    subject = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="new", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    handled_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    handled_by_user = db.relationship("User", foreign_keys=[handled_by_user_id])
    handled_at = db.Column(db.DateTime)
    reply_sent_at = db.Column(db.DateTime)


class NewsletterSubscriber(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    subscribed_at = db.Column(db.DateTime)
    unsubscribed_at = db.Column(db.DateTime)
    status = db.Column(db.String(30), default="subscribed", nullable=False, index=True)
    source = db.Column(db.String(120))
    consent_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class EmailEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(255), unique=True, nullable=False, index=True)
    category = db.Column(db.String(80), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    user = db.relationship("User", foreign_keys=[user_id])
    related_type = db.Column(db.String(80))
    related_id = db.Column(db.String(120))
    recipient_count = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class NewsletterDraft(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(255), nullable=False)
    body_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="draft", nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def is_production():
    return app.config["ENVIRONMENT"] == "production"


def should_auto_create_schema():
    return (
        app.config["ENVIRONMENT"] in {"development", "testing"}
        and is_sqlite_database_uri(app.config["SQLALCHEMY_DATABASE_URI"])
    )


def missing_config_keys():
    missing = []
    secret_issues = secret_key_issues(app.config["SECRET_KEY"])
    if secret_issues:
        missing.append(f"SECRET_KEY ({', '.join(secret_issues)})")

    if is_production():
        if not os.getenv("GOOGLE_CLIENT_ID", "").strip():
            missing.append("GOOGLE_CLIENT_ID")
        if not os.getenv("GOOGLE_CLIENT_SECRET", "").strip():
            missing.append("GOOGLE_CLIENT_SECRET")
        if not ADMIN_EMAILS:
            missing.append("ADMIN_EMAILS")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            missing.append("SESSION_COOKIE_SECURE=true")
        if app.config.get("PREFERRED_URL_SCHEME") != "https":
            missing.append("PREFERRED_URL_SCHEME=https")
        if not app.config.get("TRUSTED_HOSTS"):
            missing.append("TRUSTED_HOSTS")
        if not app.config.get("SERVER_NAME"):
            missing.append("SERVER_NAME")
        if proxy_fix_count <= 0:
            missing.append("PROXY_FIX_COUNT")
        if env_flag("FLASK_DEBUG", default=False):
            missing.append("FLASK_DEBUG must be disabled")
        if not app.config.get("CLOUDFLARE_TURNSTILE_SITE_KEY"):
            missing.append("CLOUDFLARE_TURNSTILE_SITE_KEY")
        if not app.config.get("CLOUDFLARE_TURNSTILE_SECRET_KEY"):
            missing.append("CLOUDFLARE_TURNSTILE_SECRET_KEY")
        if app.config.get("EMAIL_ENABLED"):
            if not app.config.get("MAIL_SERVER"):
                missing.append("MAIL_SERVER")
            if not app.config.get("MAIL_DEFAULT_SENDER"):
                missing.append("MAIL_DEFAULT_SENDER")
            if not support_email_address():
                missing.append("SUPPORT_EMAIL")
    return missing


def build_absolute_url(endpoint, **values):
    return url_for(endpoint, _external=True, **values)


def absolute_static_url(filename):
    return build_absolute_url("static", filename=filename)


def canonical_url_for_request(*, allowed_query_keys=None):
    allowed_query_keys = set(allowed_query_keys or ())
    query_pairs = []
    for key in sorted(allowed_query_keys):
        values = [value.strip() for value in request.args.getlist(key) if value and value.strip()]
        for value in values:
            query_pairs.append((key, value))
    query_string = urlencode(query_pairs, doseq=True)
    return f"{request.base_url}?{query_string}" if query_string else request.base_url


def robots_directive_for_request():
    path = request.path.rstrip("/") or "/"
    if path in NOINDEX_NOFOLLOW_PATHS:
        return "noindex, nofollow"
    if path in NOINDEX_FOLLOW_PATHS:
        return "noindex, follow"
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PRIVATE_PATH_PREFIXES):
        return "noindex, nofollow"
    return "index, follow"


def page_title_for_endpoint(endpoint):
    titles = {
        "account": "Account",
        "account_settings": "Settings",
        "developers": "Developer API",
        "index": APP_NAME,
        "plans": "Plans",
        "privacy": "Privacy",
        "cookies": "Cookies",
        "terms": "Terms",
        "data_rights": "Data rights",
        "search": "Search",
        "login": "Continue with Google",
    }
    if endpoint in titles:
        return f"{titles[endpoint]} | {APP_NAME}" if endpoint != "index" else f"{APP_NAME} | {TAGLINE}"
    fallback = (endpoint or APP_NAME).replace("_", " ").replace(".", " ").strip().title()
    return f"{fallback} | {APP_NAME}"


def default_description_for_request():
    descriptions = {
        "index": "Planira helps people check practical accessibility and venue details before they travel.",
        "plans": "Compare Planira plans for venue search access, richer account tools, and developer API workflows.",
        "privacy": "Read how Planira handles account details, search records, moderation data, and service security.",
        "cookies": "Planira uses essential cookies for sign-in, session security, and anti-abuse protection only.",
        "terms": "Review the terms that apply when you use Planira.",
        "data_rights": "Understand the choices you have over your Planira account data.",
        "developers": "Explore the Planira API preview for trusted place signals and structured venue lookups.",
        "login": "Continue to Planira with Google sign-in and anti-abuse protection.",
        "search": "Search results in Planira help members review venue details before they travel.",
    }
    return descriptions.get(request.endpoint, f"{APP_NAME} helps people know before they go.")


def build_organization_schema():
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": APP_NAME,
        "url": build_absolute_url("index"),
        "logo": absolute_static_url(DEFAULT_OG_IMAGE_FILENAME),
        "description": "Planira helps people know before they go with practical venue information.",
    }


def build_website_schema():
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": APP_NAME,
        "url": build_absolute_url("index"),
        "description": "Know before you go with calm, practical venue information from Planira.",
    }


def seo_place_detail_items(profile):
    if not profile:
        return []

    facts = []
    fact_mappings = (
        ("step_free_entrance", "Step-free entrance"),
        ("accessible_toilet", "Accessible toilet"),
        ("toilets_available", "Toilets available"),
        ("disabled_parking", "Disabled parking"),
        ("lift_available", "Lift available"),
        ("baby_changing", "Baby changing"),
    )
    for field_name, label in fact_mappings:
        value = getattr(profile, field_name, None)
        if value and value != "unknown":
            facts.append(f"{label}: {humanize_label(value)}")
    return facts[:3]


def build_place_seo_description(place, profile, signal):
    location_parts = [part for part in [place.town, place.county] if part]
    location_copy = ", ".join(location_parts) if location_parts else "the local area"
    details = seo_place_detail_items(profile)
    description_parts = [f"Accessibility and planning details for {place.name} in {location_copy}."]
    if signal and signal.get("summary"):
        description_parts.append(signal["summary"])
    if details:
        description_parts.append("Key details include " + "; ".join(details) + ".")
    return " ".join(description_parts)[:300]


def build_place_structured_data(place):
    schema_type = "LocalBusiness" if any([place.address1, place.town, place.postcode, place.phone, place.website]) else "Place"
    payload = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "name": place.name,
        "url": build_absolute_url("place_detail", slug=place.slug),
    }
    if place.phone:
        payload["telephone"] = place.phone
    if place.website:
        payload["sameAs"] = [place.website]
    address_fields = {
        "streetAddress": place.address1,
        "addressLocality": place.town,
        "addressRegion": place.county,
        "postalCode": place.postcode,
    }
    if any(address_fields.values()):
        payload["address"] = {"@type": "PostalAddress", **{key: value for key, value in address_fields.items() if value}}
    if place.latitude is not None and place.longitude is not None:
        payload["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": place.latitude,
            "longitude": place.longitude,
        }
    return payload


def build_seo_payload(
    *,
    title=None,
    description=None,
    canonical_url=None,
    robots=None,
    og_title=None,
    og_description=None,
    og_image=None,
    structured_data=None,
):
    resolved_title = title or page_title_for_endpoint(request.endpoint)
    resolved_description = description or default_description_for_request()
    resolved_canonical = canonical_url or canonical_url_for_request()
    resolved_robots = robots or robots_directive_for_request()
    resolved_og_image = og_image or absolute_static_url(DEFAULT_OG_IMAGE_FILENAME)
    return {
        "title": resolved_title,
        "description": resolved_description,
        "canonical_url": resolved_canonical,
        "robots": resolved_robots,
        "og_title": og_title or resolved_title,
        "og_description": og_description or resolved_description,
        "og_image": resolved_og_image,
        "twitter_card": "summary_large_image",
        "structured_data": structured_data or [],
    }


def normalized_email_address(value):
    return (value or "").strip().lower()


def email_outbox():
    return app.extensions.setdefault("email_outbox", [])


def capture_email_for_dev(subject, recipients, text_body, html_body=None, reply_to=None, category="transactional"):
    email_outbox().append(
        {
            "subject": subject,
            "recipients": list(recipients),
            "text_body": text_body,
            "html_body": html_body,
            "reply_to": reply_to,
            "category": category,
        }
    )


def email_real_delivery_enabled():
    return bool(
        app.config.get("EMAIL_ENABLED")
        and not app.config.get("EMAIL_DEV_MODE")
        and app.config.get("MAIL_SERVER")
        and app.config.get("MAIL_DEFAULT_SENDER")
    )


def send_email(subject, recipients, text_body, html_body=None, reply_to=None, category="transactional"):
    normalized_recipients = [normalized_email_address(item) for item in (recipients or []) if normalized_email_address(item)]
    if not normalized_recipients:
        app.logger.warning("Email skipped category=%s recipient_count=0 success=false", category)
        return False

    if app.config.get("TESTING") or app.config.get("EMAIL_DEV_MODE"):
        capture_email_for_dev(subject, normalized_recipients, text_body, html_body=html_body, reply_to=reply_to, category=category)
        app.logger.info("Email captured category=%s recipient_count=%s success=true", category, len(normalized_recipients))
        return True

    if not email_real_delivery_enabled():
        app.logger.warning("Email disabled category=%s recipient_count=%s success=false", category, len(normalized_recipients))
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = app.config["MAIL_DEFAULT_SENDER"]
    message["To"] = ", ".join(normalized_recipients)
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        if app.config.get("MAIL_USE_SSL"):
            with smtplib.SMTP_SSL(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
                if app.config.get("MAIL_USERNAME"):
                    server.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD", ""))
                server.send_message(message)
        else:
            with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
                if app.config.get("MAIL_USE_TLS"):
                    server.starttls()
                if app.config.get("MAIL_USERNAME"):
                    server.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD", ""))
                server.send_message(message)
    except Exception:
        app.logger.exception(
            "Email send failed category=%s recipient_count=%s success=false",
            category,
            len(normalized_recipients),
        )
        return False

    app.logger.info("Email sent category=%s recipient_count=%s success=true", category, len(normalized_recipients))
    return True


def render_email_bodies(template_name, **context):
    context.setdefault("support_email", support_email_address())
    context.setdefault("contact_phone", CONTACT_PHONE)
    context.setdefault("brand_name", APP_NAME)
    return (
        render_template(f"emails/{template_name}.txt", **context),
        render_template(f"emails/{template_name}.html", **context),
    )


def send_templated_email(subject, recipients, template_name, *, reply_to=None, category="transactional", **context):
    text_body, html_body = render_email_bodies(template_name, **context)
    return send_email(
        subject,
        recipients,
        text_body,
        html_body=html_body,
        reply_to=reply_to,
        category=category,
    )


def basic_html_from_text(value):
    lines = [html_escape(line.strip()) for line in (value or "").splitlines()]
    paragraphs = []
    current = []
    for line in lines:
        if line:
            current.append(line)
            continue
        if current:
            paragraphs.append("<p>" + "<br>".join(current) + "</p>")
            current = []
    if current:
        paragraphs.append("<p>" + "<br>".join(current) + "</p>")
    return "".join(paragraphs) or "<p></p>"


def newsletter_serializer():
    return URLSafeSerializer(app.config["SECRET_KEY"], salt="planira-newsletter-unsubscribe")


def build_unsubscribe_token(email):
    return newsletter_serializer().dumps({"email": normalized_email_address(email)})


def decode_unsubscribe_token(token):
    try:
        payload = newsletter_serializer().loads(token)
    except BadSignature:
        return None
    return normalized_email_address(payload.get("email"))


def has_sent_email_event(event_key):
    return EmailEvent.query.filter_by(event_key=event_key).first() is not None


def send_email_once(event_key, subject, recipients, text_body, html_body=None, reply_to=None, category="transactional", user=None, related_type=None, related_id=None):
    if has_sent_email_event(event_key):
        app.logger.info("Email skipped duplicate category=%s recipient_count=%s success=true", category, len(recipients or []))
        return False

    if not send_email(subject, recipients, text_body, html_body=html_body, reply_to=reply_to, category=category):
        return False

    db.session.add(
        EmailEvent(
            event_key=event_key,
            category=category,
            user_id=user.id if user else None,
            related_type=related_type,
            related_id=str(related_id) if related_id is not None else None,
            recipient_count=len(recipients or []),
        )
    )
    db.session.commit()
    return True


def send_templated_email_once(event_key, subject, recipients, template_name, *, reply_to=None, category="transactional", user=None, related_type=None, related_id=None, **context):
    text_body, html_body = render_email_bodies(template_name, **context)
    return send_email_once(
        event_key,
        subject,
        recipients,
        text_body,
        html_body=html_body,
        reply_to=reply_to,
        category=category,
        user=user,
        related_type=related_type,
        related_id=related_id,
    )


def current_newsletter_subscriber(email):
    normalized = normalized_email_address(email)
    if not normalized:
        return None
    return NewsletterSubscriber.query.filter_by(email=normalized).first()


def newsletter_status_for_email(email):
    subscriber = current_newsletter_subscriber(email)
    if not subscriber:
        return {
            "status": "not_subscribed",
            "label": "Not subscribed",
            "subscriber": None,
        }
    if subscriber.status == "subscribed":
        return {"status": "subscribed", "label": "Subscribed", "subscriber": subscriber}
    return {"status": "unsubscribed", "label": "Unsubscribed", "subscriber": subscriber}


def subscribe_newsletter(email, *, source, consent_text):
    normalized = normalized_email_address(email)
    if not normalized:
        raise ValueError("Enter a valid email address.")

    now = datetime.now(timezone.utc)
    subscriber = NewsletterSubscriber.query.filter_by(email=normalized).first()
    if subscriber and subscriber.status == "subscribed":
        return subscriber, "already_subscribed"

    if not subscriber:
        subscriber = NewsletterSubscriber(email=normalized, created_at=now)
        db.session.add(subscriber)

    subscriber.status = "subscribed"
    subscriber.subscribed_at = now
    subscriber.unsubscribed_at = None
    subscriber.source = source
    subscriber.consent_text = consent_text
    db.session.commit()
    return subscriber, "subscribed"


def unsubscribe_newsletter(email):
    subscriber = current_newsletter_subscriber(email)
    if not subscriber:
        return None, "missing"
    if subscriber.status == "unsubscribed":
        return subscriber, "already_unsubscribed"
    subscriber.status = "unsubscribed"
    subscriber.unsubscribed_at = datetime.now(timezone.utc)
    db.session.commit()
    return subscriber, "unsubscribed"


def send_newsletter_confirmation_email(subscriber):
    if not subscriber or not app.config.get("NEWSLETTER_ENABLED"):
        return False
    unsubscribe_url = url_for("newsletter_unsubscribe", token=build_unsubscribe_token(subscriber.email), _external=True)
    return send_templated_email(
        "You’re on the Planira newsletter list",
        [subscriber.email],
        "newsletter",
        category="newsletter",
        heading="You’re on the list",
        intro="Thanks for opting in to occasional Planira updates.",
        body_html=basic_html_from_text(
            "We’ll only send occasional product and launch updates.\n\nYou can unsubscribe at any time using the link below."
        ),
        body_text="We’ll only send occasional product and launch updates.\n\nYou can unsubscribe at any time using the link below.",
        unsubscribe_url=unsubscribe_url,
    )


def send_welcome_email_for_user(user):
    if not user:
        return False
    return send_templated_email_once(
        f"welcome:{user.id}",
        "Welcome to Planira",
        [user.email],
        "welcome",
        category="transactional",
        user=user,
        related_type="user",
        related_id=user.id,
        user_name=user.name or "there",
    )


def send_payment_confirmation_email(user, *, plan_name, event_key):
    if not user or not event_key:
        return False
    return send_templated_email_once(
        f"payment_confirmation:{event_key}",
        f"{plan_name} is active on your Planira account",
        [user.email],
        "payment_confirmation",
        category="transactional",
        user=user,
        related_type="billing",
        related_id=event_key,
        user_name=user.name or "there",
        plan_name=plan_name,
    )


def send_payment_failure_email(user, *, event_key):
    if not user or not event_key:
        return False
    return send_templated_email_once(
        f"payment_failure:{event_key}",
        "Planira payment update",
        [user.email],
        "payment_failure",
        category="transactional",
        user=user,
        related_type="billing",
        related_id=event_key,
        user_name=user.name or "there",
    )


def validate_basic_email_address(value, field_name="Email"):
    normalized = normalized_email_address(value)
    if not normalized or "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError(f"{field_name} must be a valid email address.")
    return normalized


def turnstile_is_configured():
    return bool(app.config.get("CLOUDFLARE_TURNSTILE_SITE_KEY") and app.config.get("CLOUDFLARE_TURNSTILE_SECRET_KEY"))


def turnstile_bypass_allowed():
    return app.config["ENVIRONMENT"] != "production" and not turnstile_is_configured()


def build_turnstile_context(action):
    if turnstile_is_configured():
        mode = "enabled"
    elif turnstile_bypass_allowed():
        mode = "bypass"
    else:
        mode = "unavailable"
    return {
        "action": action,
        "mode": mode,
        "site_key": app.config.get("CLOUDFLARE_TURNSTILE_SITE_KEY", ""),
    }


def verify_turnstile_submission(action):
    if turnstile_bypass_allowed():
        warning_message = "Turnstile verification is bypassed in this non-production environment because the keys are not configured."
        app.logger.warning(warning_message)
        return True, warning_message

    if not turnstile_is_configured():
        return False, "This form is temporarily unavailable because anti-abuse protection is not configured."

    response_token = (request.form.get("cf-turnstile-response") or "").strip()
    if not response_token:
        return False, "Please complete the anti-abuse check and try again."

    payload = urlencode(
        {
            "secret": app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"],
            "response": response_token,
            "remoteip": request_client_identifier(),
        }
    ).encode("utf-8")
    try:
        verification_request = Request(
            TURNSTILE_VERIFY_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(verification_request, timeout=5) as response:
            verification = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        app.logger.warning("Turnstile verification request failed for action=%s: %s", action, exc)
        return False, "The anti-abuse check could not be verified right now. Please try again."

    result = json.loads(verification.decode("utf-8"))

    if not result.get("success"):
        app.logger.info("Turnstile verification rejected action=%s errors=%s", action, result.get("error-codes"))
        return False, "Please complete the anti-abuse check and try again."

    returned_action = (result.get("action") or "").strip()
    if action and returned_action and returned_action != action:
        app.logger.warning("Turnstile action mismatch expected=%s actual=%s", action, returned_action)
        return False, "The anti-abuse check could not be matched to this form. Please try again."

    return True, None


def protect_with_turnstile(action, failure_category="error"):
    verified, message = verify_turnstile_submission(action)
    if verified:
        if message:
            flash(message, "info")
        return True
    flash(message, failure_category)
    return False


def refresh_session_user(user):
    session["user"] = {"email": user.email, "name": user.name, "picture": user.picture}


def current_user():
    email = session.get("user", {}).get("email")
    return User.query.filter_by(email=email).first() if email else None


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def build_analytics_template_context():
    consent = consent_cookie_preferences()
    enabled = analytics_enabled_for_request()
    measurement_id = app.config.get("GA_MEASUREMENT_ID", "")
    has_analytics_consent = bool(enabled and consent.get("analytics"))
    return {
        "cookie_name": CONSENT_COOKIE_NAME,
        "consent": consent,
        "measurement_id": measurement_id,
        "enabled": enabled,
        "has_analytics_consent": has_analytics_consent,
        "autoload": has_analytics_consent,
        "script_src": (
            f"https://www.googletagmanager.com/gtag/js?id={measurement_id}"
            if has_analytics_consent and measurement_id
            else ""
        ),
        "events": queued_analytics_events() + page_analytics_events(),
        "cookie_max_age": CONSENT_COOKIE_MAX_AGE,
    }


def ads_enabled_for_environment():
    if not app.config.get("ADSENSE_ENABLED"):
        return False
    if not app.config.get("ADSENSE_CLIENT_ID"):
        return False
    if app.config.get("ENVIRONMENT") == "production":
        return True
    return bool(app.config.get("ENABLE_ADS_IN_DEV"))


def ads_allowed_on_request_path(path=None):
    normalized_path = (path or request.path or "/").rstrip("/") or "/"
    return not any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in ADS_DISABLED_PATH_PREFIXES
    )


def ads_enabled_for_request():
    return ads_enabled_for_environment() and ads_allowed_on_request_path()


def build_ads_template_context():
    consent = consent_cookie_preferences()
    enabled = ads_enabled_for_request()
    client_id = app.config.get("ADSENSE_CLIENT_ID", "")
    has_marketing_consent = bool(enabled and consent.get("marketing"))
    return {
        "consent": consent,
        "enabled": enabled,
        "has_marketing_consent": has_marketing_consent,
        "autoload": has_marketing_consent,
        "client_id": client_id,
        "script_src": (
            f"https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client_id}"
            if has_marketing_consent and client_id
            else ""
        ),
        "slots": {
            "search_results": app.config.get("ADSENSE_SLOT_SEARCH_RESULTS", ""),
            "place_detail": app.config.get("ADSENSE_SLOT_PLACE_DETAIL", ""),
            "footer": app.config.get("ADSENSE_SLOT_FOOTER", ""),
        },
    }


@app.context_processor
def inject_user():
    user = current_user()
    staff_navigation = build_staff_navigation(user)
    account_state = build_account_state(user)
    return {
        "brand_name": APP_NAME,
        "brand_tagline": TAGLINE,
        "contact_phone": CONTACT_PHONE,
        "current_user": user,
        "is_admin": account_state["is_admin"],
        "csrf_token": csrf_token,
        "humanize_label": humanize_label,
        "account_state": account_state,
        "shell_navigation": build_shell_navigation(user),
        "staff_navigation": staff_navigation,
        "staff_nav_active": bool(request.endpoint and any(item["endpoint"] == request.endpoint for item in staff_navigation)),
        "seo": build_seo_payload(),
        "turnstile_site_key": app.config.get("CLOUDFLARE_TURNSTILE_SITE_KEY", ""),
        "turnstile_enabled": turnstile_is_configured(),
        "analytics": build_analytics_template_context(),
        "ads": build_ads_template_context(),
    }


def is_admin_email(email):
    return bool(email and email.lower() in ADMIN_EMAILS)


def build_public_author_label(email):
    cleaned = (email or "").strip().lower()
    if not cleaned or "@" not in cleaned:
        return "Planira member"

    local_part = cleaned.split("@", 1)[0]
    return f"{local_part[:3]}..."


def request_client_identifier():
    if request.access_route:
        return request.access_route[0]
    return request.remote_addr or "unknown"


def rate_limit_enabled():
    return bool(app.config.get("RATE_LIMIT_ENABLED")) and not app.config.get("TESTING", False)


def rate_limit_state_key(scope, identifier):
    return f"{scope}:{identifier}"


def is_rate_limited(scope, identifier, *, limit, window_seconds):
    if not rate_limit_enabled():
        return False

    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    state_key = rate_limit_state_key(scope, identifier)
    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_STATE[state_key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return len(bucket) >= limit


def register_rate_limit_hit(scope, identifier):
    if not rate_limit_enabled():
        return

    state_key = rate_limit_state_key(scope, identifier)
    now_ts = datetime.now(timezone.utc).timestamp()
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_STATE[state_key].append(now_ts)


def enforce_rate_limit(scope, *, limit, window_seconds, identifier=None, description=RATE_LIMIT_ERROR_MESSAGE):
    identifier = identifier or request_client_identifier()
    if is_rate_limited(scope, identifier, limit=limit, window_seconds=window_seconds):
        abort(429, description=description)


def oauth_email_is_verified(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def default_consent_preferences():
    return {
        "version": 0,
        "necessary": True,
        "analytics": False,
        "marketing": False,
    }


def normalize_consent_preferences(raw_value):
    preferences = default_consent_preferences()
    if not isinstance(raw_value, dict):
        return preferences

    for key in CONSENT_CATEGORIES:
        if key == "necessary":
            preferences[key] = True
        else:
            preferences[key] = bool(raw_value.get(key))
    if raw_value.get("version") == CONSENT_COOKIE_VERSION:
        preferences["version"] = CONSENT_COOKIE_VERSION
    return preferences


def consent_cookie_preferences():
    raw_cookie = request.cookies.get(CONSENT_COOKIE_NAME, "")
    if not raw_cookie:
        return default_consent_preferences()

    try:
        payload = json.loads(raw_cookie)
    except (TypeError, ValueError):
        return default_consent_preferences()
    return normalize_consent_preferences(payload)


def analytics_enabled_for_environment():
    if not app.config.get("GA_MEASUREMENT_ID"):
        return False
    if app.config.get("ENVIRONMENT") == "production":
        return True
    return bool(app.config.get("ENABLE_ANALYTICS_IN_DEV"))


def analytics_allowed_on_request_path(path=None):
    normalized_path = (path or request.path or "/").rstrip("/") or "/"
    return not any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in ANALYTICS_DISABLED_PATH_PREFIXES
    )


def analytics_enabled_for_request():
    return analytics_enabled_for_environment() and analytics_allowed_on_request_path()


def filter_analytics_params(params):
    if not isinstance(params, dict):
        return {}

    filtered = {}
    for key, value in params.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if normalized_key.lower() in SENSITIVE_ANALYTICS_PARAM_KEYS:
            continue
        if isinstance(value, bool):
            filtered[normalized_key] = value
        elif isinstance(value, int):
            filtered[normalized_key] = value
        elif isinstance(value, float):
            filtered[normalized_key] = round(value, 2)
        elif isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                filtered[normalized_key] = trimmed[:120]
    return filtered


def build_analytics_event(name, params=None):
    event_name = re.sub(r"[^a-z0-9_]+", "_", str(name or "").strip().lower()).strip("_")
    if not event_name:
        return None
    return {
        "name": event_name[:40],
        "params": filter_analytics_params(params),
    }


def page_analytics_events():
    events = getattr(g, "_page_analytics_events", None)
    if events is None:
        events = []
        g._page_analytics_events = events
    return events


def queued_analytics_events():
    events = getattr(g, "_queued_analytics_events", None)
    if events is None:
        events = session.pop("_analytics_events", [])
        if not isinstance(events, list):
            events = []
        g._queued_analytics_events = events
    return events


def add_page_analytics_event(name, params=None):
    event = build_analytics_event(name, params=params)
    if event:
        page_analytics_events().append(event)
    return event


def queue_analytics_event(name, params=None):
    event = build_analytics_event(name, params=params)
    if not event:
        return None
    queued_events = session.get("_analytics_events", [])
    if not isinstance(queued_events, list):
        queued_events = []
    queued_events.append(event)
    session["_analytics_events"] = queued_events
    return event


def user_has_disabled_api_pack_entitlement(user):
    if not user:
        return False
    manual_plan = (user.manual_entitlement_plan or "").strip().lower()
    return manual_plan in DISABLED_API_PACK_PLAN_KEYS or user.role == "api_buyer"


def is_safe_redirect_target(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in {"http", "https"} and ref_url.netloc == test_url.netloc


def safe_next_target_for_endpoint(endpoint, view_args=None, default_endpoint="search"):
    view_args = view_args or {}
    if endpoint == "add_comment":
        return url_for("place_detail", slug=view_args.get("slug"))
    if endpoint == "upload_place_image":
        place = db.session.get(Place, view_args.get("place_id"))
        if place and place.slug:
            return url_for("place_detail", slug=place.slug)
    if endpoint == "delete_place_image":
        place_image = db.session.get(PlaceImage, view_args.get("image_id"))
        if place_image and place_image.place and place_image.place.slug:
            return url_for("place_detail", slug=place_image.place.slug)
    if endpoint == "update_account_profile_image":
        return url_for("account_settings")
    if endpoint in {"create_developer_api_key", "update_developer_api_key"}:
        return url_for("developers")
    if endpoint == "create_checkout":
        return url_for("plans")
    return url_for(default_endpoint)


def normalize_next_target(target, default_endpoint="search"):
    if not is_safe_redirect_target(target):
        return url_for(default_endpoint)

    parsed_target = urlparse(urljoin(request.host_url, target))
    adapter = app.url_map.bind(urlparse(request.host_url).netloc)
    safe_target = parsed_target.path
    if parsed_target.query:
        safe_target = f"{safe_target}?{parsed_target.query}"

    try:
        adapter.match(parsed_target.path, method="GET")
        return safe_target
    except NotFound:
        return url_for(default_endpoint)
    except MethodNotAllowed:
        try:
            endpoint, view_args = adapter.match(parsed_target.path, method="POST")
        except Exception:
            return url_for(default_endpoint)
        return safe_next_target_for_endpoint(endpoint, view_args=view_args, default_endpoint=default_endpoint)


def safe_next_target_for_request(default_endpoint="search"):
    if request.method != "POST":
        return normalize_next_target(request.full_path, default_endpoint=default_endpoint)

    return safe_next_target_for_endpoint(
        request.endpoint or "",
        view_args=request.view_args or {},
        default_endpoint=default_endpoint,
    )


def redirect_to_next(default_endpoint="search"):
    target = session.pop("next_url", None)
    if target:
        return redirect(normalize_next_target(target, default_endpoint=default_endpoint))
    return redirect(url_for(default_endpoint))


def normalize_optional_url(value):
    value = (value or "").strip()
    if not value:
        return None
    if not value.startswith(URL_FIELD_SCHEMES):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Please enter a valid website URL.")
    return value


def get_profile_image_upload_dir():
    return app.config["PROFILE_IMAGE_UPLOAD_DIR"]


def get_place_image_upload_dir():
    return app.config["PLACE_IMAGE_UPLOAD_DIR"]


def get_avatar_initials(user):
    if not user:
        return "PA"

    raw_value = (user.name or "").strip()
    if raw_value:
        parts = [part for part in re.split(r"\s+", raw_value) if part]
    else:
        local_part = (user.email or "").split("@", 1)[0]
        parts = [part for part in re.split(r"[\s._-]+", local_part) if part]

    initials = "".join(part[:1] for part in parts[:2]).upper()
    if initials:
        return initials

    fallback = (user.email or user.name or "Planira")[:2].upper()
    return fallback or "PA"


def profile_image_filesystem_path(filename):
    safe_name = secure_filename(filename or "")
    if not safe_name or safe_name != (filename or ""):
        return None
    return os.path.join(get_profile_image_upload_dir(), safe_name)


def get_avatar_url(user):
    if not user or not user.profile_image_filename:
        return None

    path = profile_image_filesystem_path(user.profile_image_filename)
    if not path or not os.path.exists(path):
        return None

    return url_for("static", filename=f"{PROFILE_IMAGE_STATIC_PREFIX}/{user.profile_image_filename}")


def place_image_filesystem_path(filename):
    safe_name = secure_filename(filename or "")
    if not safe_name or safe_name != (filename or ""):
        return None
    return os.path.join(get_place_image_upload_dir(), safe_name)


def get_place_image_url(image):
    if not image or not image.filename:
        return None

    path = place_image_filesystem_path(image.filename)
    if not path or not os.path.exists(path):
        return None

    return url_for("static", filename=f"{PLACE_IMAGE_STATIC_PREFIX}/{image.filename}")


def sniff_supported_image_extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate_profile_image_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError("Choose an image to upload.")

    safe_original_name = secure_filename(upload.filename)
    _, extension = os.path.splitext(safe_original_name)
    normalized_extension = extension.lower().lstrip(".")
    if normalized_extension not in PROFILE_IMAGE_ALLOWED_EXTENSIONS:
        raise ValueError("Use a PNG, JPG, WEBP or GIF image.")

    mime_type = (upload.mimetype or "").lower()
    if mime_type and mime_type not in PROFILE_IMAGE_ALLOWED_MIME_TYPES:
        raise ValueError("That file type is not allowed.")

    max_bytes = int(app.config["PROFILE_IMAGE_MAX_BYTES"])
    payload = upload.stream.read(max_bytes + 1)
    upload.stream.seek(0)
    if not payload:
        raise ValueError("Choose an image to upload.")
    if len(payload) > max_bytes:
        raise ValueError("Profile pictures must be 2MB or smaller.")

    detected_extension = sniff_supported_image_extension(payload)
    if not detected_extension:
        raise ValueError("That file does not look like a supported image.")

    if detected_extension == "jpg":
        if normalized_extension not in {"jpg", "jpeg"}:
            raise ValueError("File content does not match the selected image type.")
    elif normalized_extension != detected_extension:
        raise ValueError("File content does not match the selected image type.")

    return payload, detected_extension


def save_profile_image_upload(upload):
    payload, detected_extension = validate_profile_image_upload(upload)
    upload_dir = get_profile_image_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)

    safe_filename = secure_filename(f"{secrets.token_hex(16)}.{detected_extension}")
    path = os.path.join(upload_dir, safe_filename)
    with open(path, "xb") as file_obj:
        file_obj.write(payload)
    return safe_filename


def validate_place_image_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError("Choose an image to upload.")

    safe_original_name = secure_filename(upload.filename)
    _, extension = os.path.splitext(safe_original_name)
    normalized_extension = extension.lower().lstrip(".")
    if normalized_extension not in PLACE_IMAGE_ALLOWED_EXTENSIONS:
        raise ValueError("Use a PNG, JPG, WEBP or GIF image.")

    mime_type = (upload.mimetype or "").lower()
    if mime_type and mime_type not in PLACE_IMAGE_ALLOWED_MIME_TYPES:
        raise ValueError("That file type is not allowed.")

    max_bytes = int(app.config["PLACE_IMAGE_MAX_BYTES"])
    payload = upload.stream.read(max_bytes + 1)
    upload.stream.seek(0)
    if not payload:
        raise ValueError("Choose an image to upload.")
    if len(payload) > max_bytes:
        raise ValueError("Place images must be 2MB or smaller.")

    detected_extension = sniff_supported_image_extension(payload)
    if not detected_extension:
        raise ValueError("That file does not look like a supported image.")

    if detected_extension == "jpg":
        if normalized_extension not in {"jpg", "jpeg"}:
            raise ValueError("File content does not match the selected image type.")
    elif normalized_extension != detected_extension:
        raise ValueError("File content does not match the selected image type.")

    original_filename = safe_original_name[:255] or None
    return payload, detected_extension, original_filename


def save_place_image_upload(upload):
    payload, detected_extension, original_filename = validate_place_image_upload(upload)
    upload_dir = get_place_image_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)

    safe_filename = secure_filename(f"{secrets.token_hex(16)}.{detected_extension}")
    path = os.path.join(upload_dir, safe_filename)
    with open(path, "xb") as file_obj:
        file_obj.write(payload)
    return safe_filename, original_filename


def delete_profile_image_file(filename):
    path = profile_image_filesystem_path(filename)
    if path and os.path.exists(path):
        os.remove(path)


def delete_place_image_file(filename):
    path = place_image_filesystem_path(filename)
    if path and os.path.exists(path):
        os.remove(path)


def parse_int_field(raw_value, field_name, minimum=None, maximum=None, default=None):
    if isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be a whole number.")

    if raw_value is None:
        if default is not None:
            return default
        raise ValueError(f"{field_name} is required.")

    if isinstance(raw_value, int):
        parsed = raw_value
    else:
        value = str(raw_value).strip()
        if not value:
            if default is not None:
                return default
            raise ValueError(f"{field_name} is required.")

        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number.") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be no more than {maximum}.")
    return parsed


def parse_float_field(raw_value, field_name, minimum=None, maximum=None, default=None):
    value = (raw_value or "").strip()
    if not value:
        if default is not None:
            return default
        return None

    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be no more than {maximum}.")
    return parsed


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("Please sign in before continuing.", "info")
            return redirect(url_for("login", next=safe_next_target_for_request()))
        if not current_user():
            session.clear()
            flash("Your session expired. Please sign in again before continuing.", "info")
            return redirect(url_for("login", next=safe_next_target_for_request()))
        return fn(*args, **kwargs)

    return wrapper


def request_prefers_json():
    if request.args.get("format", "").strip().lower() == "json":
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]


def auth_required_response(*, staff_only=False):
    if not session.get("user"):
        if request_prefers_json():
            return (
                jsonify(
                    {
                        "error": "authentication_required",
                        "message": "Log in with a staff account to use this streaming endpoint.",
                        "login_url": url_for("login", next=request.path),
                    }
                ),
                401,
            )
        flash("Please log in with Google to view results.", "info")
        return redirect(url_for("login", next=request.path))

    user = current_user()
    if not user:
        session.clear()
        if request_prefers_json():
            return (
                jsonify(
                    {
                        "error": "session_expired",
                        "message": "Your session expired. Log in again to use this streaming endpoint.",
                        "login_url": url_for("login", next=request.path),
                    }
                ),
                401,
            )
        flash("Your session expired, so please sign in again.", "info")
        return redirect(url_for("login", next=request.path))

    if staff_only and not is_staff_user(user):
        if request_prefers_json():
            return (
                jsonify(
                    {
                        "error": "staff_access_required",
                        "message": "Staff access is required for this streaming endpoint.",
                    }
                ),
                403,
            )
        flash("Staff access required.", "error")
        return redirect(url_for("index"))

    return None


def staff_session_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_response = auth_required_response(staff_only=True)
        if auth_response is not None:
            return auth_response
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or get_access_label(user) != "Admin":
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not is_staff_user(user):
            flash("Staff access required.", "error")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def get_or_create_profile(place):
    if not place.accessibility:
        try:
            sync_primary_key_sequence(AccessibilityProfile)
            db.session.add(AccessibilityProfile(place=place))
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            if not is_duplicate_primary_key_error(exc, "accessibility_profile_pkey"):
                raise
            sync_primary_key_sequence(AccessibilityProfile)
            db.session.add(AccessibilityProfile(place=place))
            db.session.commit()
    return place.accessibility


def ensure_accessibility_profiles(commit=True):
    missing_places = (
        db.session.query(Place)
        .outerjoin(AccessibilityProfile, AccessibilityProfile.place_id == Place.id)
        .filter(AccessibilityProfile.id.is_(None))
        .all()
    )

    if missing_places:
        sync_primary_key_sequence(AccessibilityProfile)

    for place in missing_places:
        db.session.add(AccessibilityProfile(place_id=place.id))

    try:
        if commit and missing_places:
            db.session.commit()
        elif not commit:
            db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        if not missing_places or not is_duplicate_primary_key_error(exc, "accessibility_profile_pkey"):
            raise

        sync_primary_key_sequence(AccessibilityProfile)
        for place in missing_places:
            still_missing = (
                db.session.query(AccessibilityProfile.id)
                .filter(AccessibilityProfile.place_id == place.id)
                .first()
                is None
            )
            if still_missing:
                db.session.add(AccessibilityProfile(place_id=place.id))

        if commit:
            db.session.commit()
        else:
            db.session.flush()

    return len(missing_places)


def get_postgresql_sequence_name(table_name, column_name):
    return db.session.execute(
        text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
        {"table_name": table_name, "column_name": column_name},
    ).scalar()


def sync_table_primary_key_sequence(table, pk_column):
    if not is_postgresql_database_uri(app.config["SQLALCHEMY_DATABASE_URI"]):
        return

    sequence_name = get_postgresql_sequence_name(table.name, pk_column.name)
    if not sequence_name:
        return

    max_id = db.session.query(db.func.max(pk_column)).select_from(table).scalar()
    db.session.execute(
        text("SELECT setval(:sequence_name, :sequence_value, :is_called)"),
        {
            "sequence_name": sequence_name,
            "sequence_value": max_id or 1,
            "is_called": max_id is not None,
        },
    )


def sync_primary_key_sequence(model):
    sync_table_primary_key_sequence(model.__table__, model.__table__.c.id)


def is_duplicate_primary_key_error(exc, constraint_name):
    original = getattr(exc, "orig", None)
    if original is None:
        return False
    return getattr(original, "pgcode", None) == "23505" and constraint_name in str(original)


def build_access_signal(profile):
    return present_access_signal(profile)


def build_place_card(place):
    return present_place_card(place)


def verification_status(profile):
    return present_verification_status(profile)


def build_plan_catalog():
    return [
        {
            "key": "logged_in_free",
            "name": "Logged-in free",
            "tag": "Try the product",
            "price": "PS0",
            "summary": "A limited monthly allowance for people checking a few venues.",
            "description": "A lightweight allowance for occasional checking and onboarding contributors.",
            "features": [
                "5 to 10 searches per month",
                "Basic place information",
                "Community profile and points",
            ],
            "checkout_mode": None,
            "price_id": None,
            "role": "member",
            "cta": None,
        },
        {
            "key": "paid_consumer",
            "name": "Paid consumer",
            "tag": "Most useful",
            "price": "PS9/mo idea",
            "summary": "For people who want confidence, memory, and stronger filters.",
            "description": "The premium experience for users who need confidence before leaving home.",
            "features": [
                "More searches",
                "Richer place detail",
                "Verified-only filter",
                "Advanced accessibility filters",
                "Comments, last verified, confidence score",
            ],
            "checkout_mode": "subscription",
            "price_id": os.getenv("STRIPE_PRICE_PAID_CONSUMER", "").strip(),
            "role": "paid_consumer",
            "cta": "Start subscription",
        },
        {
            "key": "api_20",
            "name": "API pack 20",
            "tag": "Developer",
            "price": "20 lookups",
            "summary": "Quick test pack for a small workflow or demo.",
            "description": "A starter one-off pack for developers buying access before recurring API billing exists.",
            "features": [
                "20 API lookups",
                "Hosted Checkout payment",
                "Good for demos and validation",
            ],
            "checkout_mode": "payment",
            "price_id": os.getenv("STRIPE_PRICE_API_20", "").strip(),
            "role": "api_buyer",
            "cta": "Buy 20 lookups",
        },
        {
            "key": "api_50",
            "name": "API pack 50",
            "tag": "Developer",
            "price": "50 lookups",
            "summary": "A starter option for teams validating demand.",
            "description": "A mid-size lookup pack for teams trying the dataset in a real workflow.",
            "features": [
                "50 API lookups",
                "One-time checkout",
                "Useful for pilot integrations",
            ],
            "checkout_mode": "payment",
            "price_id": os.getenv("STRIPE_PRICE_API_50", "").strip(),
            "role": "api_buyer",
            "cta": "Buy 50 lookups",
        },
        {
            "key": "api_100",
            "name": "API pack 100",
            "tag": "Developer",
            "price": "100 lookups",
            "summary": "A larger bundle before a recurring API plan exists.",
            "description": "The biggest one-off pack for teams who need a larger batch before moving to subscription billing.",
            "features": [
                "100 API lookups",
                "One-time checkout",
                "Best fit for larger trial usage",
            ],
            "checkout_mode": "payment",
            "price_id": os.getenv("STRIPE_PRICE_API_100", "").strip(),
            "role": "api_buyer",
            "cta": "Buy 100 lookups",
        },
    ]


def get_plan(plan_key):
    for plan in build_plan_catalog():
        if plan["key"] == plan_key:
            return plan
    return None


def stripe_checkout_ready(plan):
    return bool(stripe and app.config["STRIPE_SECRET_KEY"] and plan and plan["price_id"] and plan["checkout_mode"])


def target_plan_for_role(target_role):
    if target_role == "paid_consumer":
        return "paid"
    if target_role == "api_buyer":
        return "business"
    return None


def manual_entitlement_plan_to_billing_plan(plan_key):
    if plan_key == "paid_consumer":
        return "paid"
    if plan_key in {"api_20", "api_50", "api_100", "business"}:
        return "business"
    return None


def manual_entitlement_role(plan_key):
    if plan_key == "paid_consumer":
        return "paid_consumer"
    if plan_key in {"api_20", "api_50", "api_100", "business"}:
        return "api_buyer"
    return None


def manual_entitlement_expires_at(user):
    if not user:
        return None
    value = user.access_override_until
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def manual_entitlement_is_active(user, *, now=None):
    if not user or not user.manual_entitlement_enabled:
        return False
    plan_key = (user.manual_entitlement_plan or "").strip().lower()
    if plan_key not in MANUAL_ENTITLEMENT_ALLOWED_PLANS:
        return False
    expires_at = manual_entitlement_expires_at(user)
    comparison_time = now or datetime.now(timezone.utc)
    if expires_at and expires_at <= comparison_time:
        return False
    return True


def active_manual_entitlement_plan(user):
    if not manual_entitlement_is_active(user):
        return None
    return (user.manual_entitlement_plan or "").strip().lower() or None


def role_for_plan_name(plan_name):
    if plan_name == "paid":
        return "paid_consumer"
    if plan_name == "business":
        return "api_buyer"
    return DEFAULT_MEMBER_ROLE


def stored_billing_plan_name(user):
    if not user:
        return None
    plan_name = (user.plan or "").strip().lower()
    if plan_name in {"free", "paid", "business"}:
        return plan_name
    return None


def infer_entitlement_role(user):
    if not user:
        return None
    manual_plan = active_manual_entitlement_plan(user)
    if manual_plan:
        return manual_entitlement_role(manual_plan)
    stored_plan = stored_billing_plan_name(user)
    if stored_plan in {"paid", "business"}:
        return role_for_plan_name(stored_plan)
    if stored_plan == "free":
        return None
    if user.role in {"paid_consumer", "api_buyer"}:
        return user.role
    return None


def apply_plan_role_to_user(user_id, target_role):
    if not user_id or not target_role:
        return False

    user = db.session.get(User, int(user_id))
    if not user:
        return False

    desired_plan = target_plan_for_role(target_role)
    changed = False

    if user.role != target_role:
        user.role = target_role
        changed = True

    if desired_plan and user.plan != desired_plan:
        user.plan = desired_plan
        changed = True

    if changed:
        db.session.commit()
    return True


def build_checkout_metadata(user, plan):
    return {
        "plan_key": plan["key"],
        "user_id": str(user.id),
        "user_email": user.email,
        "target_role": plan["role"],
    }


def plan_uses_subscription(plan):
    return bool(plan and plan.get("checkout_mode") == "subscription")


def should_manage_subscription_lifecycle(target_role):
    plan_name = target_plan_for_role(target_role)
    return plan_name == "paid"


def stripe_object_value(data, key, default=None):
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def stripe_metadata_from_object(data):
    metadata = stripe_object_value(data, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = dict(metadata)

    if metadata:
        return metadata

    subscription_details = stripe_object_value(data, "subscription_details", {}) or {}
    if isinstance(subscription_details, dict):
        details_metadata = subscription_details.get("metadata", {}) or {}
        if isinstance(details_metadata, dict) and details_metadata:
            return details_metadata

    lines = stripe_object_value(data, "lines", {}) or {}
    line_items = []
    if isinstance(lines, dict):
        line_items = lines.get("data", []) or []
    for item in line_items:
        item_metadata = stripe_metadata_from_object(item)
        if item_metadata:
            return item_metadata

    return {}


def stripe_datetime_value(value):
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def stripe_subscription_snapshot(data):
    object_type = stripe_object_value(data, "object")
    subscription_id = stripe_object_value(data, "subscription")
    if isinstance(subscription_id, dict):
        subscription_id = stripe_object_value(subscription_id, "id")

    return {
        "customer_id": stripe_object_value(data, "customer"),
        "subscription_id": stripe_object_value(data, "id") if object_type == "subscription" else subscription_id,
        "subscription_status": stripe_object_value(data, "status") if object_type == "subscription" else None,
        "subscription_current_period_end": stripe_datetime_value(stripe_object_value(data, "current_period_end")) if object_type == "subscription" else None,
        "subscription_cancel_at_period_end": stripe_object_value(data, "cancel_at_period_end") if object_type == "subscription" else None,
    }


def find_user_for_stripe_object(data, metadata=None):
    metadata = metadata or stripe_metadata_from_object(data)

    user_id = metadata.get("user_id")
    if user_id:
        try:
            user = db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            user = None
        if user:
            return user

    client_reference_id = stripe_object_value(data, "client_reference_id")
    if client_reference_id:
        try:
            user = db.session.get(User, int(client_reference_id))
        except (TypeError, ValueError):
            user = None
        if user:
            return user

    for email_value in (
        metadata.get("user_email"),
        stripe_object_value(data, "customer_email"),
        stripe_object_value(data, "receipt_email"),
    ):
        normalized_email = (email_value or "").strip().lower()
        if normalized_email:
            user = User.query.filter_by(email=normalized_email).first()
            if user:
                return user

    subscription_snapshot = stripe_subscription_snapshot(data)
    subscription_id = subscription_snapshot["subscription_id"]
    if subscription_id:
        user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
        if user:
            return user

    customer_id = subscription_snapshot["customer_id"]
    if customer_id:
        user = User.query.filter_by(stripe_customer_id=customer_id).first()
        if user:
            return user

    return None


def user_matches_entitlement(user, target_role):
    expected_plan = target_plan_for_role(target_role)
    if not user or not expected_plan:
        return False
    if user.plan == expected_plan:
        return True
    return user.role == target_role


def update_user_stripe_billing_fields(user, data, *, commit=False):
    if not user:
        return False

    snapshot = stripe_subscription_snapshot(data)
    changed = False

    if snapshot["customer_id"] and user.stripe_customer_id != snapshot["customer_id"]:
        user.stripe_customer_id = snapshot["customer_id"]
        changed = True
    if snapshot["subscription_id"] and user.stripe_subscription_id != snapshot["subscription_id"]:
        user.stripe_subscription_id = snapshot["subscription_id"]
        changed = True
    if snapshot["subscription_status"] is not None and user.subscription_status != snapshot["subscription_status"]:
        user.subscription_status = snapshot["subscription_status"]
        changed = True
    if snapshot["subscription_current_period_end"] is not None and user.subscription_current_period_end != snapshot["subscription_current_period_end"]:
        user.subscription_current_period_end = snapshot["subscription_current_period_end"]
        changed = True
    if snapshot["subscription_cancel_at_period_end"] is not None and user.subscription_cancel_at_period_end != snapshot["subscription_cancel_at_period_end"]:
        user.subscription_cancel_at_period_end = snapshot["subscription_cancel_at_period_end"]
        changed = True

    if commit and changed:
        db.session.commit()
    return changed


def sync_user_entitlement(user, *, target_role, reason, actor_user_id=None, allow_staff=False):
    if not user or not target_role:
        return False

    if not allow_staff and is_staff_user(user):
        return False

    target_plan = target_plan_for_role(target_role)
    changed = False
    before_state = {"role": user.role, "plan": user.plan}

    if user.role != target_role:
        user.role = target_role
        changed = True
    if target_plan and user.plan != target_plan:
        user.plan = target_plan
        changed = True

    if not changed:
        return False

    log_audit(
        actor_user_id=actor_user_id,
        action="billing.entitlement.synced",
        entity_type="user",
        entity_id=user.id,
        before=before_state,
        after={"role": user.role, "plan": user.plan, "target_role": target_role},
        reason=reason,
    )
    db.session.commit()
    return True


def revoke_user_entitlement(user, *, target_role, reason, actor_user_id=None):
    if not user or not target_role:
        return False
    if is_staff_user(user):
        return False
    if not should_manage_subscription_lifecycle(target_role):
        return False
    if not user_matches_entitlement(user, target_role):
        return False

    changed = False
    before_state = {"role": user.role, "plan": user.plan}
    if user.role != DEFAULT_MEMBER_ROLE:
        user.role = DEFAULT_MEMBER_ROLE
        changed = True
    if user.plan != "free":
        user.plan = "free"
        changed = True

    if not changed:
        return False

    log_audit(
        actor_user_id=actor_user_id,
        action="billing.entitlement.revoked",
        entity_type="user",
        entity_id=user.id,
        before=before_state,
        after={"role": user.role, "plan": user.plan, "target_role": target_role},
        reason=reason,
    )
    db.session.commit()
    return True


def is_staff_user(user):
    if not user:
        return False
    return user.role in {"admin", "staff"} or user.plan == "admin" or is_admin_email(user.email)


def current_role_key(user):
    if not user:
        return "free_visitor"
    manual_plan = active_manual_entitlement_plan(user)
    if manual_plan == "paid_consumer":
        return "paid_consumer"
    if manual_plan in {"api_20", "api_50", "api_100", "business"}:
        return "api_buyer"
    stored_plan = stored_billing_plan_name(user)
    if stored_plan == "paid":
        return "paid_consumer"
    if stored_plan == "business":
        return "api_buyer"
    if is_staff_user(user):
        return "paid_consumer"
    inferred_role = infer_entitlement_role(user)
    if inferred_role == "paid_consumer":
        return "paid_consumer"
    if inferred_role == "api_buyer":
        return "api_buyer"
    return "logged_in_free"


def normalize_plan_name(user):
    if not user:
        return "visitor"
    manual_plan = active_manual_entitlement_plan(user)
    if manual_plan:
        return manual_entitlement_plan_to_billing_plan(manual_plan) or "free"
    stored_plan = stored_billing_plan_name(user)
    if stored_plan:
        return stored_plan
    if user.plan == "admin":
        return "admin"
    inferred_role = infer_entitlement_role(user)
    if inferred_role == "paid_consumer":
        return "paid"
    if inferred_role == "api_buyer":
        return "business"
    if is_staff_user(user):
        return "admin"
    return "free"


def humanize_plan_name(plan_name):
    return present_plan_name(plan_name)


def normalize_billing_plan_name(user):
    if not user:
        return "free"
    manual_plan = active_manual_entitlement_plan(user)
    if manual_plan:
        return manual_entitlement_plan_to_billing_plan(manual_plan) or "free"
    stored_plan = stored_billing_plan_name(user)
    if stored_plan:
        return stored_plan
    inferred_role = infer_entitlement_role(user)
    if inferred_role == "paid_consumer":
        return "paid"
    if inferred_role == "api_buyer":
        return "business"
    return "free"


def get_access_label(user):
    if not user:
        return "Visitor"
    if is_admin_email(user.email) or user.role == "admin" or user.plan == "admin":
        return "Admin"
    if user.role == "staff":
        return "Staff"
    return "Member"


def api_access_status(user):
    if not user:
        return False, API_ACCESS_REQUIRED_MESSAGE
    if user_has_disabled_api_pack_entitlement(user):
        return False, API_PACK_ACCESS_DISABLED_MESSAGE
    if is_staff_user(user):
        return True, None
    if normalize_billing_plan_name(user) in {"paid", "business"}:
        return True, None
    return False, API_ACCESS_REQUIRED_MESSAGE


def build_account_state(user):
    billing_plan_name = normalize_billing_plan_name(user)
    return {
        "plan_name": billing_plan_name,
        "plan_label": humanize_plan_name(billing_plan_name),
        "access_label": get_access_label(user),
        "is_staff": bool(user and is_staff_user(user)),
        "is_admin": bool(user and get_access_label(user) == "Admin"),
    }


def current_plan_catalog_key(user):
    manual_plan = active_manual_entitlement_plan(user)
    if manual_plan:
        return manual_plan
    plan_name = normalize_billing_plan_name(user)
    if plan_name == "paid":
        return "paid_consumer"
    if plan_name == "business":
        return "api_buyer"
    return "logged_in_free"


def user_has_api_access(user):
    allowed, _ = api_access_status(user)
    return allowed


def can_upload_place_images(user):
    if not user:
        return False
    if is_staff_user(user):
        return True
    return normalize_billing_plan_name(user) in {"paid", "business"}


def can_delete_place_image(user, place_image):
    if not user or not place_image:
        return False
    if is_staff_user(user):
        return True
    return place_image.user_id == user.id


def get_monthly_search_limit(user):
    if not user:
        return 0
    if user.monthly_search_limit is not None:
        return user.monthly_search_limit

    plan_name = normalize_plan_name(user)
    if plan_name == "paid":
        return 100
    if plan_name == "business":
        return 250
    if plan_name == "admin":
        return None
    return 10


def can_bypass_search_limits(user):
    return is_staff_user(user)


def parse_optional_datetime_local(raw_value, field_name):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date and time.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def manual_entitlement_status(user):
    if not user or not user.manual_entitlement_enabled:
        return {
            "label": "No manual override",
            "tone": "muted",
            "detail": "No manual access override is stored on this account.",
            "active": False,
        }

    expires_at = manual_entitlement_expires_at(user)
    expiry_copy = format_admin_timestamp(expires_at, with_time=True) if expires_at else "No expiry"
    plan_key = (user.manual_entitlement_plan or "").strip().lower()
    plan = get_plan(plan_key)
    plan_label = plan["name"] if plan else humanize_label(plan_key or "unknown")
    if manual_entitlement_is_active(user):
        return {
            "label": "Active manual access",
            "tone": "success",
            "detail": f"{plan_label} override active. Expires: {expiry_copy}.",
            "active": True,
        }
    return {
        "label": "Expired manual access",
        "tone": "warning",
        "detail": f"{plan_label} override no longer counts for access. Expired: {expiry_copy}.",
        "active": False,
    }


def current_api_key_prefix():
    return API_KEY_LIVE_PREFIX if is_production() else API_KEY_TEST_PREFIX


def hash_api_key_value(raw_key):
    return hashlib.sha256((raw_key or "").encode("utf-8")).hexdigest()


def is_malformed_api_key(raw_key):
    return not bool(raw_key and API_KEY_PATTERN.match(raw_key.strip()))


def build_api_key_hint(raw_key=None):
    if not raw_key:
        return "Hidden after creation"
    if raw_key.startswith(API_KEY_LIVE_PREFIX):
        prefix = API_KEY_LIVE_PREFIX
    else:
        prefix = API_KEY_TEST_PREFIX
    return f"{prefix}...{raw_key[-4:]}"


def serialize_api_key(api_key, raw_key=None):
    return {
        "id": api_key.id,
        "label": api_key.label or "API key",
        "key_hint": build_api_key_hint(raw_key),
        "is_active": bool(api_key.is_active),
        "scopes": api_key.scopes_json or [],
        "monthly_lookup_limit": api_key.monthly_lookup_limit,
        "lookup_credits": api_key.lookup_credits or 0,
        "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
        "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
    }


def owner_allowed_api_scopes(user):
    allowed = {"places:read", "api:usage"}
    if is_staff_user(user):
        allowed.update({"places:write", "admin:read"})
    return allowed


def default_api_scopes_for_user(user):
    scopes = list(DEFAULT_API_KEY_SCOPES)
    if is_staff_user(user):
        scopes.append("places:write")
    return scopes


def normalize_scope_list(scopes, *, allowed_scopes=None):
    if scopes is None:
        return None
    if isinstance(scopes, str):
        values = [item.strip() for item in scopes.split(",")]
    else:
        values = [str(item).strip() for item in scopes]
    cleaned = sorted({LEGACY_SCOPE_ALIASES.get(value, value) for value in values if value})
    if allowed_scopes is not None:
        invalid = sorted(set(cleaned) - set(allowed_scopes))
        if invalid:
            raise ValueError(f"Unsupported API scope(s): {', '.join(invalid)}.")
    return cleaned or None


def generate_api_key_value(prefix=None):
    return f"{prefix or current_api_key_prefix()}{secrets.token_urlsafe(32)}"


def create_api_key_for_user(user, label=None, scopes=None, monthly_lookup_limit=None, lookup_credits=None, prefix=None):
    normalized_label = (label or "Primary key").strip() or "Primary key"
    requested_scopes = scopes if scopes is not None else default_api_scopes_for_user(user)
    normalized_scopes = normalize_scope_list(requested_scopes, allowed_scopes=owner_allowed_api_scopes(user))
    normalized_credits = max(int(lookup_credits or 0), 0)

    for _ in range(3):
        raw_key = generate_api_key_value(prefix=prefix)
        key_hash = hash_api_key_value(raw_key)
        if not APIKey.query.filter_by(key_hash=key_hash).first():
            api_key = APIKey(
                user=user,
                key_hash=key_hash,
                label=normalized_label[:120],
                scopes_json=normalized_scopes,
                monthly_lookup_limit=monthly_lookup_limit,
                lookup_credits=normalized_credits,
                is_active=True,
            )
            db.session.add(api_key)
            db.session.flush()
            return api_key, raw_key
    raise RuntimeError("Could not generate a unique API key.")


def extract_bearer_api_key(authorization_header):
    header = (authorization_header or "").strip()
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def resolve_api_key_candidate(raw_key=None, authorization_header=None, request_obj=None):
    if raw_key:
        return raw_key.strip()
    if authorization_header:
        return extract_bearer_api_key(authorization_header)
    if request_obj is not None:
        return extract_bearer_api_key(request_obj.headers.get("Authorization", ""))
    return None


def default_api_lookup_limit_for_user(user):
    if not user:
        return 0
    if is_staff_user(user):
        return None
    plan_name = normalize_plan_name(user)
    if plan_name == "business":
        return None
    if plan_name == "paid":
        return 100
    return 0


def count_api_lookups_for_key(api_key):
    month_start, next_month_start = current_month_range()
    return (
        db.session.query(APILookupEvent).filter(
            APILookupEvent.api_key_id == api_key.id,
            APILookupEvent.created_at >= month_start,
            APILookupEvent.created_at < next_month_start,
        ).count()
    )


def build_api_key_limit_context(api_key, *, lookups_used=None):
    monthly_limit = api_key.monthly_lookup_limit
    if monthly_limit is None:
        monthly_limit = default_api_lookup_limit_for_user(api_key.user)
    if lookups_used is None:
        lookups_used = count_api_lookups_for_key(api_key)
    lookup_credits = max(api_key.lookup_credits or 0, 0)
    bypass = monthly_limit is None
    limit_reached = not bypass and lookups_used >= monthly_limit and lookup_credits <= 0
    return {
        "lookups_used": lookups_used,
        "monthly_limit": monthly_limit,
        "lookup_credits": lookup_credits,
        "bypass": bypass,
        "limit_reached": limit_reached,
    }


def api_key_limit_context(api_key):
    return build_api_key_limit_context(api_key)


def api_key_has_required_scopes(api_key, required_scopes=None):
    normalized_required = normalize_scope_list(required_scopes) or []
    if not normalized_required:
        return True
    existing_scopes = set(normalize_scope_list(api_key.scopes_json) or [])
    return set(normalized_required).issubset(existing_scopes)


def record_api_lookup(api_key_id, user_id, endpoint, query=None, status_code=None):
    event = APILookupEvent(
        api_key_id=api_key_id,
        user_id=user_id,
        endpoint=endpoint,
        query=query,
        status_code=status_code,
    )
    db.session.add(event)
    return event


def authenticate_api_key(raw_key=None, authorization_header=None, request_obj=None, required_scopes=None, endpoint=None, query=None, status_code=200, commit=True, record_event=True, apply_usage=True):
    request_context = request_obj or request
    client_identifier = request_context.access_route[0] if request_context and request_context.access_route else (
        request_context.remote_addr if request_context else "unknown"
    )
    if is_rate_limited("api_auth_fail", client_identifier, limit=20, window_seconds=300):
        return {"ok": False, "error": "rate_limited", "status_code": 429}

    candidate = resolve_api_key_candidate(raw_key=raw_key, authorization_header=authorization_header, request_obj=request_obj)
    if not candidate:
        return {"ok": False, "error": "missing_api_key", "status_code": 401}
    if is_malformed_api_key(candidate):
        register_rate_limit_hit("api_auth_fail", client_identifier)
        return {"ok": False, "error": "malformed_api_key", "status_code": 401}

    candidate_hash = hash_api_key_value(candidate)
    matched_key = APIKey.query.filter_by(key_hash=candidate_hash).first()
    if matched_key and not matched_key.is_active:
        register_rate_limit_hit("api_auth_fail", client_identifier)
        return {"ok": False, "error": "inactive_api_key", "status_code": 403}
    if not matched_key:
        register_rate_limit_hit("api_auth_fail", client_identifier)
        return {"ok": False, "error": "invalid_api_key", "status_code": 401}
    access_allowed, access_message = api_access_status(matched_key.user)
    if not access_allowed:
        return {"ok": False, "error": "api_access_required", "status_code": 403, "message": access_message}
    if not api_key_has_required_scopes(matched_key, required_scopes=required_scopes):
        register_rate_limit_hit("api_auth_fail", client_identifier)
        return {"ok": False, "error": "insufficient_scope", "status_code": 403}

    limit_context = api_key_limit_context(matched_key)
    if limit_context["limit_reached"]:
        return {
            "ok": False,
            "error": "monthly_lookup_limit_reached",
            "status_code": 429,
            "limit_context": limit_context,
        }

    if apply_usage:
        if not limit_context["bypass"] and limit_context["monthly_limit"] is not None and limit_context["lookups_used"] >= limit_context["monthly_limit"]:
            matched_key.lookup_credits = max((matched_key.lookup_credits or 0) - 1, 0)

        matched_key.last_used_at = datetime.now(timezone.utc)
        if record_event and endpoint:
            record_api_lookup(
                api_key_id=matched_key.id,
                user_id=matched_key.user_id,
                endpoint=endpoint,
                query=query,
                status_code=status_code,
            )
        if commit:
            db.session.commit()
    return {
        "ok": True,
        "api_key": matched_key,
        "user": matched_key.user,
        "limit_context": api_key_limit_context(matched_key),
        "message": access_message,
    }


def finalize_api_lookup_success(api_key, endpoint, query=None, status_code=200, commit=True):
    limit_context = api_key_limit_context(api_key)
    if not limit_context["bypass"] and limit_context["monthly_limit"] is not None and limit_context["lookups_used"] >= limit_context["monthly_limit"]:
        api_key.lookup_credits = max((api_key.lookup_credits or 0) - 1, 0)

    api_key.last_used_at = datetime.now(timezone.utc)
    record_api_lookup(
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        endpoint=endpoint,
        query=query,
        status_code=status_code,
    )
    if commit:
        db.session.commit()
    return api_key_limit_context(api_key)


def format_search_limit_copy(limit, bypass=False):
    if bypass or limit is None:
        return "Unlimited staff access"
    return f"{limit} searches per month"


def format_search_usage_copy(searches_used, limit, bypass=False):
    if bypass or limit is None:
        return f"{searches_used} searches used this month with staff access"
    return f"{searches_used} of {limit} searches used this month"


def format_search_credits_copy(credits_remaining, bypass=False):
    if bypass:
        return f"{credits_remaining} extra search credits available if staff ever needs them"
    return f"{credits_remaining} extra search credits remaining"


def api_error_response(error, message, status_code, **extra):
    payload = {
        "error": error,
        "message": message,
    }
    payload.update(extra)
    return jsonify(payload), status_code


def api_auth_error_response(auth_result, *, write=False):
    error_map = {
        "missing_api_key": ("missing_api_key", "Send an API key using the Authorization Bearer header.", 401),
        "malformed_api_key": ("invalid_api_key", "The API key format is not valid for this environment.", 401),
        "invalid_api_key": ("invalid_api_key", "The API key could not be verified.", 401),
        "inactive_api_key": ("revoked_api_key", "This API key is no longer active.", 403),
        "api_access_required": ("api_access_required", auth_result.get("message") or API_ACCESS_REQUIRED_MESSAGE, 403),
        "insufficient_scope": ("invalid_api_key", "This API key does not have access to this endpoint.", 403),
        "monthly_lookup_limit_reached": ("limit_reached", "This API key has used its available lookup allowance.", 429),
        "rate_limited": ("rate_limited", RATE_LIMIT_ERROR_MESSAGE, 429),
    }
    error_key, message, status_code = error_map.get(
        auth_result["error"],
        ("invalid_api_key", "The API request could not be authorized.", auth_result.get("status_code", 401)),
    )
    if write and auth_result["error"] == "insufficient_scope":
        error_key, message, status_code = ("invalid_api_key", "This API key does not have access to this write endpoint.", 403)
    return api_error_response(error_key, message, status_code)


def parse_json_api_payload():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object body.")
    return payload


def extract_api_write_sections(payload):
    allowed_root_keys = {"place", "accessibility", "verification", "mark_verified"}
    unknown_root_keys = sorted(set(payload) - allowed_root_keys)
    if unknown_root_keys:
        raise ValueError(f"Unknown top-level field(s): {', '.join(unknown_root_keys)}.")

    place_payload = payload.get("place")
    accessibility_payload = payload.get("accessibility")
    verification_payload = payload.get("verification")

    if place_payload is not None and not isinstance(place_payload, dict):
        raise ValueError("place must be a JSON object when provided.")
    if accessibility_payload is not None and not isinstance(accessibility_payload, dict):
        raise ValueError("accessibility must be a JSON object when provided.")
    if verification_payload is not None and not isinstance(verification_payload, dict):
        raise ValueError("verification must be a JSON object when provided.")

    effective_accessibility_payload = dict(accessibility_payload or {})
    effective_verification_payload = {}

    if verification_payload is not None:
        allowed_verification_keys = {"mark_verified", "source", "confidence_score"}
        unknown_verification_keys = sorted(set(verification_payload) - allowed_verification_keys)
        if unknown_verification_keys:
            raise ValueError(f"Unknown verification field(s): {', '.join(unknown_verification_keys)}.")

        if "mark_verified" in verification_payload:
            effective_verification_payload["mark_verified"] = verification_payload["mark_verified"]
        if "source" in verification_payload:
            if "source" in effective_accessibility_payload and effective_accessibility_payload["source"] != verification_payload["source"]:
                raise ValueError("source cannot differ between accessibility and verification payloads.")
            effective_accessibility_payload["source"] = verification_payload["source"]
        if "confidence_score" in verification_payload:
            if (
                "confidence_score" in effective_accessibility_payload
                and effective_accessibility_payload["confidence_score"] != verification_payload["confidence_score"]
            ):
                raise ValueError("confidence_score cannot differ between accessibility and verification payloads.")
            effective_accessibility_payload["confidence_score"] = verification_payload["confidence_score"]

    if "mark_verified" in payload:
        if "mark_verified" in effective_verification_payload and effective_verification_payload["mark_verified"] != payload["mark_verified"]:
            raise ValueError("mark_verified cannot differ between top-level and verification payloads.")
        effective_verification_payload["mark_verified"] = payload["mark_verified"]

    return place_payload, (effective_accessibility_payload or None), effective_verification_payload


def normalize_api_text_value(value, field_name, *, max_length=None, nullable=True):
    if value is None:
        if nullable:
            return None
        raise ValueError(f"{field_name} cannot be null.")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank. Omit it to keep the existing value or send null to clear it.")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer.")
    return normalized


def normalize_api_choice_value(value, field_name, allowed_values):
    normalized = normalize_api_text_value(value, field_name, max_length=30, nullable=False).lower()
    if normalized not in allowed_values:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed_values))}.")
    return normalized


def generate_unique_place_slug(name, town, *, current_place_id=None):
    base_slug = slugify(f"{name} {town or ''}")
    slug = base_slug
    counter = 2
    while True:
        existing = Place.query.filter_by(slug=slug).first()
        if not existing or existing.id == current_place_id:
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def apply_place_write_payload(place, payload, *, creating=False):
    allowed_keys = {
        "name",
        "venue_type",
        "phone",
        "website",
        "address1",
        "town",
        "county",
        "postcode",
        "priority",
        "status",
        "latitude",
        "longitude",
    }
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"Unknown place field(s): {', '.join(unknown_keys)}.")

    if creating and "name" not in payload:
        raise ValueError("name is required.")

    if "name" in payload:
        place.name = normalize_api_text_value(payload.get("name"), "name", max_length=255, nullable=False)
    if "venue_type" in payload:
        place.venue_type = normalize_api_text_value(payload.get("venue_type"), "venue_type", max_length=80)
    if "phone" in payload:
        place.phone = normalize_api_text_value(payload.get("phone"), "phone", max_length=80)
    if "website" in payload:
        website_value = payload.get("website")
        if website_value is None:
            place.website = None
        else:
            place.website = normalize_optional_url(normalize_api_text_value(website_value, "website", max_length=255))
    if "address1" in payload:
        place.address1 = normalize_api_text_value(payload.get("address1"), "address1", max_length=255)
    if "town" in payload:
        place.town = normalize_api_text_value(payload.get("town"), "town", max_length=120)
    if "county" in payload:
        place.county = normalize_api_text_value(payload.get("county"), "county", max_length=120)
    if "postcode" in payload:
        place.postcode = normalize_api_text_value(payload.get("postcode"), "postcode", max_length=30)
    if "priority" in payload:
        place.priority = parse_int_field(payload.get("priority"), "priority", minimum=1, maximum=5)
    if "status" in payload:
        place.status = normalize_api_choice_value(payload.get("status"), "status", PLACE_WRITE_STATUS_VALUES)
    if "latitude" in payload:
        latitude_value = payload.get("latitude")
        place.latitude = None if latitude_value is None else parse_float_field(str(latitude_value), "latitude", minimum=-90, maximum=90)
    if "longitude" in payload:
        longitude_value = payload.get("longitude")
        place.longitude = None if longitude_value is None else parse_float_field(str(longitude_value), "longitude", minimum=-180, maximum=180)

    if creating or "name" in payload or "town" in payload:
        place.slug = generate_unique_place_slug(place.name, place.town, current_place_id=place.id)


def apply_accessibility_write_payload(profile, payload):
    allowed_keys = {
        "toilets_available",
        "toilet_location",
        "toilet_distance_from_bar",
        "toilet_distance_from_bar_m",
        "accessible_toilet",
        "baby_changing",
        "baby_changing_location",
        "step_free_entrance",
        "stairs_inside",
        "lift_available",
        "disabled_parking",
        "sensory_notes",
        "public_comments",
        "internal_notes",
        "source",
        "confidence_score",
    }
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"Unknown accessibility field(s): {', '.join(unknown_keys)}.")

    choice_fields = {
        "toilets_available",
        "accessible_toilet",
        "baby_changing",
        "step_free_entrance",
        "stairs_inside",
        "lift_available",
        "disabled_parking",
    }
    text_fields = {
        "toilet_location": 120,
        "toilet_distance_from_bar": 120,
        "baby_changing_location": 120,
        "sensory_notes": None,
        "public_comments": None,
        "internal_notes": None,
        "source": 80,
    }

    for field_name in choice_fields:
        if field_name in payload:
            setattr(profile, field_name, normalize_api_choice_value(payload.get(field_name), field_name, ACCESSIBILITY_CHOICE_VALUES))

    for field_name, max_length in text_fields.items():
        if field_name in payload:
            setattr(profile, field_name, normalize_api_text_value(payload.get(field_name), field_name, max_length=max_length))

    if "toilet_distance_from_bar_m" in payload:
        distance_value = payload.get("toilet_distance_from_bar_m")
        profile.toilet_distance_from_bar_m = None if distance_value is None else parse_float_field(
            str(distance_value),
            "toilet_distance_from_bar_m",
            minimum=0,
            maximum=5000,
        )

    if "confidence_score" in payload:
        profile.confidence_score = parse_int_field(payload.get("confidence_score"), "confidence_score", minimum=0, maximum=100)


def apply_api_verification_payload(place, profile, actor_user, payload):
    if "mark_verified" not in payload:
        return

    mark_verified = payload.get("mark_verified")
    if not isinstance(mark_verified, bool):
        raise ValueError("mark_verified must be true or false.")
    if not mark_verified:
        return

    profile.last_verified_at = datetime.now(timezone.utc)
    profile.last_verified_by = actor_user.email if actor_user else None
    profile.verified_by_user_id = actor_user.id if actor_user else None
    place.status = "verified"


def authenticate_api_write_request(endpoint, *, query=None):
    auth_result = authenticate_api_key(
        request_obj=request,
        required_scopes={"places:write"},
        endpoint=endpoint,
        query=query,
        apply_usage=False,
        record_event=False,
        commit=False,
    )
    if not auth_result["ok"]:
        return None, api_auth_error_response(auth_result, write=True)
    if not is_staff_user(auth_result["user"]):
        return None, api_error_response("write_access_forbidden", "This API key can read data but does not have permission to edit it.", 403)
    return auth_result, None


def build_quota_copy(user, limit_context=None):
    limit_context = limit_context or search_limit_context(user)
    plan_name = normalize_plan_name(user)
    search_limit = limit_context["monthly_limit"]
    bypass = limit_context["bypass"]
    search_credits = limit_context["search_credits"]
    plan_label = humanize_plan_name(plan_name)

    return {
        "plan_label": plan_label,
        "search_limit_copy": format_search_limit_copy(search_limit, bypass=bypass),
        "search_usage_copy": format_search_usage_copy(limit_context["searches_used"], search_limit, bypass=bypass),
        "search_credits_copy": format_search_credits_copy(search_credits, bypass=bypass),
        "allowance_copy": (
            "Searches are tracked monthly, and extra credits only kick in after the monthly allowance is used."
            if not bypass
            else "Staff access is not blocked by member quotas, but searches are still tracked for visibility."
        ),
        "blocked_message": (
            f"You've used all {plan_label.lower()} plan searches for this month. "
            "Use an extra search credit or upgrade your plan to keep going."
        ),
        "plans_cta_copy": (
            f"{plan_label} plan includes {format_search_limit_copy(search_limit, bypass=bypass).lower()}."
            if user
            else "Sign in to start tracked search usage and a monthly search allowance."
        ),
    }


def build_plan_tier_copy(plan, *, user=None, active_quota_copy=None):
    if plan["key"].startswith("api_"):
        return {
            "limit_label": "Access pattern",
            "limit_copy": "Includes API lookups and does not change your member search allowance.",
            "credits_copy": "API lookup credits are separate from member search credits.",
        }

    default_limit = PLAN_DETAILS["paid"]["search_limit"] if plan["key"] == "paid_consumer" else PLAN_DETAILS["free"]["search_limit"]
    return {
        "limit_label": "Search limit",
        "limit_copy": (
            active_quota_copy["search_limit_copy"]
            if user and current_plan_catalog_key(user) == plan["key"] and active_quota_copy
            else format_search_limit_copy(default_limit)
        ),
        "credits_copy": (
            "Extra search credits can top up the monthly allowance whenever you need more checks."
            if plan["key"] == "paid_consumer"
            else "A lighter monthly allowance for occasional venue planning, with extra credits if needed."
        ),
    }


def build_api_key_rows_with_usage(user):
    if not user:
        return []

    rows = []
    keys = APIKey.query.filter_by(user_id=user.id).order_by(APIKey.created_at.desc(), APIKey.id.desc()).all()
    key_ids = [api_key.id for api_key in keys]
    month_start, next_month_start = current_month_range()
    monthly_counts = {}
    total_counts = {}

    if key_ids:
        monthly_counts = {
            api_key_id: count
            for api_key_id, count in db.session.query(APILookupEvent.api_key_id, db.func.count(APILookupEvent.id))
            .filter(
                APILookupEvent.api_key_id.in_(key_ids),
                APILookupEvent.created_at >= month_start,
                APILookupEvent.created_at < next_month_start,
            )
            .group_by(APILookupEvent.api_key_id)
            .all()
        }
        total_counts = {
            api_key_id: count
            for api_key_id, count in db.session.query(APILookupEvent.api_key_id, db.func.count(APILookupEvent.id))
            .filter(APILookupEvent.api_key_id.in_(key_ids))
            .group_by(APILookupEvent.api_key_id)
            .all()
        }

    for api_key in keys:
        lookups_used = monthly_counts.get(api_key.id, 0)
        limit_context = build_api_key_limit_context(api_key, lookups_used=lookups_used)
        rows.append(
            {
                **serialize_api_key(api_key),
                "lookups_used": lookups_used,
                "total_lookups": total_counts.get(api_key.id, 0),
                "lookup_limit": "Unlimited" if limit_context["monthly_limit"] is None else limit_context["monthly_limit"],
                "lookup_credits": limit_context["lookup_credits"],
                "limit_reached": limit_context["limit_reached"],
                "status_label": "Active" if api_key.is_active else "Revoked",
                "created_at_label": format_admin_timestamp(api_key.created_at, with_time=True) or "Recently",
                "last_used_at_label": format_admin_timestamp(api_key.last_used_at, with_time=True) if api_key.last_used_at else None,
            }
        )
    return rows


def get_obs_active_place():
    return (
        Place.query.filter(Place.status.in_(["calling", "needs_call", "callback"]))
        .order_by(Place.status.desc(), Place.priority.desc(), Place.updated_at.asc())
        .first()
    )


def serialize_obs_current_call(place):
    if not place:
        return {
            "active": False,
            "message": "No active call right now.",
            "place": None,
        }

    return {
        "active": True,
        "message": "Current call loaded.",
        "place": {
            "id": place.id,
            "name": place.name,
            "town": place.town,
            "venue_type": place.venue_type,
            "status": place.status,
            "status_label": humanize_label(place.status),
            "priority": place.priority,
            "worksheet_url": url_for("call_place", place_id=place.id),
            "updated_at": place.updated_at.isoformat() if place.updated_at else None,
        },
    }


def build_obs_progress_payload():
    today = datetime.now(timezone.utc).date()
    verified_today = AccessibilityProfile.query.filter(db.func.date(AccessibilityProfile.last_verified_at) == str(today)).count()
    total_verified = Place.query.filter_by(status="verified").count()
    queue = {
        "calling": Place.query.filter_by(status="calling").count(),
        "needs_call": Place.query.filter_by(status="needs_call").count(),
        "callback": Place.query.filter_by(status="callback").count(),
    }
    return {
        "verified_today": verified_today,
        "total_verified": total_verified,
        "queue": queue,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_obs_health_payload():
    return {
        "status": "ok",
        "authenticated": True,
        "staff_access": True,
        "session_cookie_expected": True,
        "browser_sources": [
            {
                "label": "Current call widget",
                "html_url": url_for("obs_current_call", _external=True),
                "json_url": url_for("obs_current_call", format="json", _external=True),
            },
            {
                "label": "Progress widget",
                "html_url": url_for("obs_progress", _external=True),
                "json_url": url_for("obs_progress", format="json", _external=True),
            },
        ],
        # TODO: If OBS browser sources cannot reliably keep the staff session cookie,
        # replace this with a short-lived signed token flow later. Do not use API keys.
        "todo": "If OBS browser sources do not reliably keep the signed-in staff session, add a short-lived signed streaming token later.",
    }


def build_developer_summary(user):
    if not user:
        return None

    key_rows = build_api_key_rows_with_usage(user)
    active_key_count = sum(1 for row in key_rows if row["is_active"])
    total_lookup_credits = sum(row["lookup_credits"] for row in key_rows if row["is_active"])
    total_lookups_used = sum(row["lookups_used"] for row in key_rows if row["is_active"])
    has_unlimited_key = any(row["is_active"] and row["lookup_limit"] == "Unlimited" for row in key_rows)

    return {
        "api_key_rows": key_rows,
        "active_key_count": active_key_count,
        "total_lookup_credits": total_lookup_credits,
        "total_lookups_used": total_lookups_used,
        "has_unlimited_key": has_unlimited_key,
    }


def build_developer_example_response():
    return {
        "count": 1,
        "results": [
            {
                "id": 42,
                "name": "The Example Arms",
                "town": "Northampton",
                "postcode": "NN1 1AA",
                "accessibility_summary": {
                    "label": "Worth checking",
                    "summary": "There is useful guidance here, but a few details still need checking before you rely on it.",
                    "tone": "moderate",
                },
                "toilets_available": "yes",
                "accessible_toilet": "yes",
                "step_free_entrance": "yes",
                "stairs_inside": "no",
                "confidence_score": 82,
                "verified": True,
                "verification_status": "Verified",
                "last_verified_at": "2026-04-28T10:30:00+00:00",
            }
        ],
    }


def build_developers_page_context(user, *, raw_api_key=None, raw_api_key_label=None):
    has_api_access, api_access_message = api_access_status(user) if user else (False, API_ACCESS_REQUIRED_MESSAGE)
    return {
        "developer_summary": build_developer_summary(user) if user else None,
        "raw_api_key": raw_api_key,
        "raw_api_key_label": raw_api_key_label,
        "has_api_access": has_api_access if user else False,
        "api_access_message": api_access_message,
        "example_api_key": f"{current_api_key_prefix()}replace_me",
        "api_search_url": url_for("api_places_search", _external=True),
        "example_response": build_developer_example_response(),
    }


def serialize_place_for_api(place):
    profile = getattr(place, "accessibility", None)
    signal = build_access_signal(profile)
    verification = verification_status(profile)
    return {
        "id": place.id,
        "name": place.name,
        "town": place.town,
        "postcode": place.postcode,
        "accessibility_summary": {
            "label": signal["label"],
            "summary": signal["summary"],
            "tone": signal["tone"],
        },
        "toilets_available": getattr(profile, "toilets_available", "unknown") if profile else "unknown",
        "accessible_toilet": getattr(profile, "accessible_toilet", "unknown") if profile else "unknown",
        "step_free_entrance": getattr(profile, "step_free_entrance", "unknown") if profile else "unknown",
        "stairs_inside": getattr(profile, "stairs_inside", "unknown") if profile else "unknown",
        "confidence_score": getattr(profile, "confidence_score", None) if profile else None,
        "verified": verification["verified"],
        "verification_status": verification["status"],
        "last_verified_at": profile.last_verified_at.isoformat() if profile and profile.last_verified_at else None,
    }


def audit_payload(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): audit_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [audit_payload(item) for item in value]
    return str(value)


def log_audit(actor_user_id, action, entity_type, entity_id, before=None, after=None, reason=None):
    db.session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            before_json=audit_payload(before),
            after_json=audit_payload(after),
            reason=reason,
        )
    )


def build_shell_navigation(user):
    nav = [
        {"label": "Home", "endpoint": "index", "icon": "home"},
        {"label": "Plans", "endpoint": "plans", "icon": "sparkles"},
    ]
    if user:
        nav.extend(
            [
                {"label": "Search", "endpoint": "search", "icon": "search"},
                {"label": "Account", "endpoint": "account", "icon": "user"},
                {"label": "Settings", "endpoint": "account_settings", "icon": "settings"},
            ]
        )
    return nav


def build_staff_navigation(user):
    if not user or not is_staff_user(user):
        return []
    navigation = [
        {"label": "Dashboard", "endpoint": "dashboard"},
        {"label": "Streaming", "endpoint": "staff_streaming_control_room"},
        {"label": "Venues", "endpoint": "admin_venues"},
        {"label": "Moderation", "endpoint": "admin_moderation"},
        {"label": "Support", "endpoint": "admin_support"},
        {"label": "Users", "endpoint": "admin_users"},
        {"label": "Legacy data view", "endpoint": "admin_data"},
        {"label": "Add venue", "endpoint": "new_place"},
    ]
    if get_access_label(user) == "Admin":
        navigation.append({"label": "Newsletter", "endpoint": "admin_newsletter"})
    return navigation


def current_month_range():
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)
    return month_start, next_month_start


def count_searches_for_user(user):
    if not user:
        return 0
    month_start, next_month_start = current_month_range()
    return (
        SearchEvent.query.filter(
            SearchEvent.user_id == user.id,
            SearchEvent.created_at >= month_start,
            SearchEvent.created_at < next_month_start,
        ).count()
    )


def track_search_event(user, query_text, town, accessible, filters_json=None, result_count=None):
    event = SearchEvent(
        user_id=user.id if user else None,
        query_text=query_text or None,
        town=town or None,
        accessible=accessible or None,
        filters_json=filters_json or None,
        result_count=result_count,
    )
    db.session.add(event)
    return event


def search_limit_context(user):
    searches_used = count_searches_for_user(user)
    monthly_limit = get_monthly_search_limit(user)
    credits_remaining = max(user.search_credits or 0, 0) if user else 0
    bypass = can_bypass_search_limits(user)
    limit_reached = (
        not bypass
        and monthly_limit is not None
        and searches_used >= monthly_limit
        and credits_remaining <= 0
    )
    return {
        "searches_used": searches_used,
        "monthly_limit": monthly_limit,
        "search_credits": credits_remaining,
        "bypass": bypass,
        "limit_reached": limit_reached,
    }


def consume_search_credit_if_needed(user, limit_context):
    if not user or limit_context["bypass"]:
        return False
    monthly_limit = limit_context["monthly_limit"]
    if monthly_limit is None or limit_context["searches_used"] < monthly_limit:
        return False
    if (user.search_credits or 0) <= 0:
        return False
    user.search_credits -= 1
    return True


def build_user_summary(user):
    account_state = build_account_state(user)
    limit_context = search_limit_context(user)
    quota_copy = build_quota_copy(user, limit_context)
    comment_count = Comment.query.filter_by(user_email=user.email).count()
    call_count = CallLog.query.filter_by(user_email=user.email).count()
    verified_count = AccessibilityProfile.query.filter_by(last_verified_by=user.email).count()
    saved_venues = 0
    current_plan = PLAN_DETAILS.get(account_state["plan_name"], PLAN_DETAILS["free"])
    search_limit = limit_context["monthly_limit"]
    if search_limit is None:
        search_limit = current_plan["search_limit"]
    return {
        "account_state": account_state,
        "plan": current_plan,
        "plan_key": account_state["plan_name"],
        "searches_used": limit_context["searches_used"],
        "search_limit": search_limit,
        "search_credits": limit_context["search_credits"],
        "quota_copy": quota_copy,
        "community_points": user.community_points or 0,
        "rank_title": user.rank_title,
        "contributions": comment_count + call_count,
        "community_notes": comment_count,
        "verifications": verified_count,
        "saved_venues": saved_venues,
        "member_since": user.created_at.strftime("%d %b %Y") if user.created_at else "Recently",
    }


def build_settings_sections(user):
    summary = build_user_summary(user)
    search_limit = summary["search_limit"] or 0
    progress = int((summary["searches_used"] / max(search_limit, 1)) * 100) if search_limit else 0
    developer_summary = build_developer_summary(user)
    return {
        "profile": {
            "name": user.name or "Planira member",
            "email": user.email,
            "avatar_initials": get_avatar_initials(user),
            "avatar_url": get_avatar_url(user),
            "plan": summary["account_state"]["plan_label"],
            "access": summary["account_state"]["access_label"],
            "rank_title": summary["rank_title"],
        },
        "preferences": {
            "distance_tolerance": "Within 5 miles",
            "avoid_stairs": True,
            "default_filter": "Accessible toilet only",
        },
        "usage": {
            "searches_used": summary["searches_used"],
            "search_limit": summary["search_limit"],
            "search_credits": summary["search_credits"],
            "search_progress": progress,
            "quota_copy": summary["quota_copy"],
            "community_points": summary["community_points"],
            "rank_title": summary["rank_title"],
            "contributions": summary["contributions"],
            "verifications": summary["verifications"],
            "saved_venues": summary["saved_venues"],
        },
        "api": {
            "api_key_rows": developer_summary["api_key_rows"],
            "active_key_count": developer_summary["active_key_count"],
            "total_lookup_credits": developer_summary["total_lookup_credits"],
            "total_lookups_used": developer_summary["total_lookups_used"],
            "has_unlimited_key": developer_summary["has_unlimited_key"],
        },
        "newsletter": {
            "status": newsletter_status_for_email(user.email)["status"],
            "label": newsletter_status_for_email(user.email)["label"],
            "enabled": bool(app.config.get("NEWSLETTER_ENABLED")),
            "consent_text": NEWSLETTER_CONSENT_TEXT,
        },
    }


def build_recent_activity(limit=6):
    recent_calls = CallLog.query.order_by(CallLog.created_at.desc()).limit(limit).all()
    recent_comments = Comment.query.filter_by(status="approved").order_by(Comment.created_at.desc()).limit(limit).all()
    activity = []
    for call in recent_calls:
        activity.append(
            {
                "title": f"{call.place.name if call.place else 'Venue'} call logged",
                "meta": call.user_email or "Staff member",
                "detail": call.result.replace("_", " ").title() if call.result else "Call updated",
                "created_at": call.created_at,
            }
        )
    for comment in recent_comments:
        activity.append(
            {
                "title": f"Community note added for {comment.place.name if comment.place else 'venue'}",
                "meta": comment.user_email or "Community member",
                "detail": (comment.body[:90] + "...") if len(comment.body) > 90 else comment.body,
                "created_at": comment.created_at,
            }
        )
    activity.sort(key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return activity[:limit]


def build_recent_search_activity(limit=6):
    events = SearchEvent.query.outerjoin(User).order_by(SearchEvent.created_at.desc()).limit(limit).all()
    rows = []
    for event in events:
        rows.append(
            {
                "title": event.query_text or "Search",
                "meta": event.user.email if event.user and event.user.email else "Anonymous session",
                "detail": f"{event.result_count if event.result_count is not None else 0} results",
                "created_at": event.created_at,
            }
        )
    return rows


def build_recent_audit_entries(limit=6):
    entries = AuditLog.query.outerjoin(User, AuditLog.actor_user_id == User.id).order_by(AuditLog.created_at.desc()).limit(limit).all()
    rows = []
    for entry in entries:
        rows.append(
            {
                "title": entry.action.replace(".", " ").replace("_", " ").title(),
                "meta": entry.actor_user.email if entry.actor_user and entry.actor_user.email else "System",
                "detail": f"{entry.entity_type} #{entry.entity_id}",
                "created_at": entry.created_at,
            }
        )
    return rows


def build_recent_api_lookup_activity(limit=6, user_id=None):
    query = (
        db.session.query(APILookupEvent)
        .outerjoin(APIKey, APILookupEvent.api_key_id == APIKey.id)
        .outerjoin(User, APILookupEvent.user_id == User.id)
    )
    if user_id is not None:
        query = query.filter(APILookupEvent.user_id == user_id)

    events = query.order_by(APILookupEvent.created_at.desc()).limit(limit).all()
    rows = []
    for event in events:
        key_label = event.api_key.label if event.api_key and event.api_key.label else "API key"
        rows.append(
            {
                "id": event.id,
                "user_email": event.user.email if event.user and event.user.email else "Unknown user",
                "key_label": key_label,
                "key_hint": serialize_api_key(event.api_key)["key_hint"] if event.api_key else "Hidden after creation",
                "endpoint": event.endpoint,
                "query": event.query,
                "status_code": event.status_code,
                "created_at_label": format_admin_timestamp(event.created_at, with_time=True) or "Recently",
            }
        )
    return rows


def build_api_operations_summary(limit=6):
    month_start, next_month_start = current_month_range()
    return {
        "active_key_count": APIKey.query.filter_by(is_active=True).count(),
        "revoked_key_count": APIKey.query.filter_by(is_active=False).count(),
        "lookup_events_this_month": (
            db.session.query(APILookupEvent).filter(
                APILookupEvent.created_at >= month_start,
                APILookupEvent.created_at < next_month_start,
            ).count()
        ),
        "recent_events": build_recent_api_lookup_activity(limit=limit),
    }


def build_moderation_items(limit=8):
    comments = Comment.query.filter_by(status="pending").order_by(Comment.created_at.desc()).limit(limit).all()
    items = []
    for comment in comments:
        items.append(
            {
                "id": comment.id,
                "venue_name": comment.place.name if comment.place else "Unknown venue",
                "submitted_changes": comment.body,
                "submitted_by": comment.user_email or "Community member",
                "submitted_at": comment.created_at.strftime("%d %b %Y %H:%M") if comment.created_at else "Recently",
                "status": (comment.status or "pending").replace("_", " ").title(),
                "edit_url": url_for("place_detail", slug=comment.place.slug) if comment.place else url_for("dashboard"),
            }
        )
    return items


def build_support_rows(messages):
    rows = []
    for message in messages:
        rows.append(
            {
                "message": message,
                "created_at_label": format_admin_timestamp(message.created_at, with_time=True) or "Recently",
                "handled_at_label": format_admin_timestamp(message.handled_at, with_time=True),
                "reply_sent_at_label": format_admin_timestamp(message.reply_sent_at, with_time=True),
                "handled_by_label": message.handled_by_user.email if message.handled_by_user else None,
            }
        )
    return rows


def build_support_stats():
    return {
        "new": ContactMessage.query.filter_by(status="new").count(),
        "open": ContactMessage.query.filter_by(status="open").count(),
        "replied": ContactMessage.query.filter_by(status="replied").count(),
        "closed": ContactMessage.query.filter_by(status="closed").count(),
    }


def build_newsletter_draft_rows():
    drafts = NewsletterDraft.query.order_by(NewsletterDraft.updated_at.desc(), NewsletterDraft.id.desc()).all()
    rows = []
    for draft in drafts:
        rows.append(
            {
                "draft": draft,
                "created_by_label": draft.created_by_user.email if draft.created_by_user else "Admin",
                "updated_at_label": format_admin_timestamp(draft.updated_at, with_time=True) or "Recently",
            }
        )
    return rows


def format_admin_timestamp(value, with_time=False):
    if not value:
        return None
    if with_time:
        return value.strftime("%d %b %Y %H:%M")
    return value.strftime("%d %b %Y")


def describe_user_activity(last_activity_at):
    if not last_activity_at:
        return {
            "label": "No recent activity",
            "detail": "No searches, comments, or call logs recorded yet.",
        }

    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)
    else:
        last_activity_at = last_activity_at.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    delta = now - last_activity_at

    if delta.days <= 0:
        label = "Active today"
    elif delta.days == 1:
        label = "Active yesterday"
    elif delta.days < 7:
        label = f"Active {delta.days} days ago"
    else:
        label = f"Last active {last_activity_at.strftime('%d %b %Y')}"

    return {
        "label": label,
        "detail": f"Latest recorded event on {last_activity_at.strftime('%d %b %Y')}.",
    }


def build_user_rows(users):
    if not users:
        return []

    user_ids = [user.id for user in users]
    user_emails = [user.email for user in users if user.email]
    month_start, next_month_start = current_month_range()

    comment_counts = {
        email: count
        for email, count in db.session.query(Comment.user_email, db.func.count(Comment.id))
        .filter(Comment.user_email.in_(user_emails))
        .group_by(Comment.user_email)
        .all()
    }
    call_counts = {
        email: count
        for email, count in db.session.query(CallLog.user_email, db.func.count(CallLog.id))
        .filter(CallLog.user_email.in_(user_emails))
        .group_by(CallLog.user_email)
        .all()
    }
    latest_comment_activity = {
        email: created_at
        for email, created_at in db.session.query(Comment.user_email, db.func.max(Comment.created_at))
        .filter(Comment.user_email.in_(user_emails))
        .group_by(Comment.user_email)
        .all()
    }
    latest_call_activity = {
        email: created_at
        for email, created_at in db.session.query(CallLog.user_email, db.func.max(CallLog.created_at))
        .filter(CallLog.user_email.in_(user_emails))
        .group_by(CallLog.user_email)
        .all()
    }
    latest_search_activity = {
        user_id: created_at
        for user_id, created_at in db.session.query(SearchEvent.user_id, db.func.max(SearchEvent.created_at))
        .filter(SearchEvent.user_id.in_(user_ids))
        .group_by(SearchEvent.user_id)
        .all()
    }
    monthly_search_counts = {
        user_id: count
        for user_id, count in db.session.query(SearchEvent.user_id, db.func.count(SearchEvent.id))
        .filter(
            SearchEvent.user_id.in_(user_ids),
            SearchEvent.created_at >= month_start,
            SearchEvent.created_at < next_month_start,
        )
        .group_by(SearchEvent.user_id)
        .all()
    }

    rows = []
    for user in users:
        comment_count = comment_counts.get(user.email, 0)
        call_count = call_counts.get(user.email, 0)
        searches_used = monthly_search_counts.get(user.id, 0)
        monthly_limit = get_monthly_search_limit(user)
        access_label = get_access_label(user)
        plan_name = normalize_billing_plan_name(user)
        plan_label = humanize_plan_name(plan_name)
        last_activity_at = max(
            (
                timestamp
                for timestamp in (
                    latest_comment_activity.get(user.email),
                    latest_call_activity.get(user.email),
                    latest_search_activity.get(user.id),
                )
                if timestamp is not None
            ),
            default=None,
        )
        activity = describe_user_activity(last_activity_at)
        can_toggle_staff = not (
            is_admin_email(user.email)
            or user.role == "admin"
            or user.plan == "admin"
        )
        is_staff_role = user.role == "staff"
        manual_override = manual_entitlement_status(user)
        rows.append(
            {
                "user": user,
                "contributions": comment_count + call_count,
                "flags": None,
                "activity": activity["label"],
                "activity_detail": activity["detail"],
                "joined_date": format_admin_timestamp(user.created_at) or "Recently",
                "joined_at_label": format_admin_timestamp(user.created_at, with_time=True) or "Recently",
                "last_login_label": format_admin_timestamp(user.last_login_at, with_time=True),
                "last_activity_label": format_admin_timestamp(last_activity_at, with_time=True),
                "role_label": access_label,
                "access_label": access_label,
                "plan_label": plan_label,
                "plan_name": plan_name,
                "searches_used": searches_used,
                "search_limit": monthly_limit,
                "search_limit_label": "Unlimited" if monthly_limit is None else str(monthly_limit),
                "search_usage_label": format_search_usage_copy(
                    searches_used,
                    monthly_limit,
                    bypass=can_bypass_search_limits(user),
                ),
                "search_credits": max(user.search_credits or 0, 0),
                "status_label": "Status not tracked yet",
                "status_note": "Suspension controls are not wired because user suspension fields do not exist yet.",
                "manual_override": manual_override,
                "manual_override_enabled": bool(user.manual_entitlement_enabled),
                "manual_override_plan": (user.manual_entitlement_plan or "").strip().lower(),
                "manual_override_until_value": (
                    manual_entitlement_expires_at(user).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
                    if manual_entitlement_expires_at(user)
                    else ""
                ),
                "manual_override_note": user.manual_entitlement_note or "",
                "primary_date_label": format_admin_timestamp(last_activity_at, with_time=True) or format_admin_timestamp(user.created_at) or "Recently",
                "primary_date_title": "Last active" if last_activity_at else "Joined",
                "staff_toggle": {
                    "enabled": can_toggle_staff,
                    "label": "Remove staff" if is_staff_role else "Promote to staff",
                    "action": "demote" if is_staff_role else "promote",
                    "title": (
                        "Admin access is controlled by ADMIN_EMAILS and is not editable here."
                        if not can_toggle_staff
                        else None
                    ),
                },
                "suspension_action": {
                    "enabled": False,
                    "label": "Suspend user",
                    "title": "Not wired yet",
                },
            }
        )
    return rows


def build_api_key_rows_for_user(user):
    if not user:
        return []
    keys = APIKey.query.filter_by(user_id=user.id).order_by(APIKey.created_at.desc(), APIKey.id.desc()).all()
    return [serialize_api_key(api_key) for api_key in keys]


def build_admin_user_query(*, q="", role="all", plan="all", manual_override="all", access="all"):
    query = User.query

    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like), User.name.ilike(like)))

    normalized_role = (role or "all").strip().lower() or "all"
    normalized_plan = (plan or "all").strip().lower() or "all"
    normalized_manual_override = (manual_override or "all").strip().lower() or "all"
    legacy_access = (access or "all").strip().lower() or "all"
    if legacy_access != "all":
        if normalized_role == "all" and legacy_access in {"member", "staff", "admin"}:
            normalized_role = legacy_access
        if normalized_plan == "all" and legacy_access in {"paid", "business"}:
            normalized_plan = legacy_access

    admin_email_list = sorted(ADMIN_EMAILS)
    admin_email_filter = db.func.lower(User.email).in_(admin_email_list) if admin_email_list else db.false()
    manual_plan_filter = User.manual_entitlement_plan.in_(sorted(MANUAL_ENTITLEMENT_ALLOWED_PLANS))
    now = datetime.now(timezone.utc)
    active_manual_filter = db.and_(
        User.manual_entitlement_enabled.is_(True),
        manual_plan_filter,
        db.or_(User.access_override_until.is_(None), User.access_override_until > now),
    )
    expired_manual_filter = db.and_(
        User.manual_entitlement_enabled.is_(True),
        manual_plan_filter,
        User.access_override_until.isnot(None),
        User.access_override_until <= now,
    )

    if normalized_role == "member":
        query = query.filter(
            ~db.or_(
                User.role.in_(["admin", "staff"]),
                User.plan == "admin",
                admin_email_filter,
            )
        )
    elif normalized_role == "staff":
        query = query.filter(User.role == "staff")
    elif normalized_role == "admin":
        query = query.filter(db.or_(User.role == "admin", User.plan == "admin", admin_email_filter))

    if normalized_plan == "free":
        query = query.filter(User.plan == "free")
    elif normalized_plan == "paid":
        query = query.filter(User.plan == "paid")
    elif normalized_plan == "business":
        query = query.filter(User.plan == "business")
    elif normalized_plan == "admin":
        query = query.filter(User.plan == "admin")

    if normalized_manual_override == "none":
        query = query.filter(User.manual_entitlement_enabled.isnot(True))
    elif normalized_manual_override == "active":
        query = query.filter(active_manual_filter)
    elif normalized_manual_override == "expired":
        query = query.filter(expired_manual_filter)

    return query


def build_admin_user_stats():
    admin_email_list = sorted(ADMIN_EMAILS)
    admin_email_filter = db.func.lower(User.email).in_(admin_email_list) if admin_email_list else db.false()
    return [
        {
            "label": "Total users",
            "value": str(User.query.count()),
            "detail": "All account records in Planira.",
        },
        {
            "label": "Staff and admin",
            "value": str(User.query.filter(db.or_(User.role.in_(["admin", "staff"]), User.plan == "admin", admin_email_filter)).count()),
            "detail": "Users with elevated access across app roles and admin email overrides.",
        },
        {
            "label": "Active paid users",
            "value": str(User.query.filter(User.plan == "paid").count()),
            "detail": "Tracked from the current billing plan field.",
        },
        {
            "label": "Suspended users",
            "value": "Not tracked yet",
            "detail": "Suspension fields and counts are not available in the current model.",
            "pending": True,
        },
    ]


def admin_user_return_url(*, user_id=None, fallback_endpoint="admin_users"):
    query_text = request.form.get("return_q") or request.args.get("q") or None
    role_filter = request.form.get("return_role") or request.args.get("role") or None
    plan_filter = request.form.get("return_plan") or request.args.get("plan") or None
    manual_override_filter = request.form.get("return_manual_override") or request.args.get("manual_override") or None
    page = request.form.get("return_page", type=int) or request.args.get("page", type=int) or 1
    return_view = (request.form.get("return_view") or request.args.get("return_view") or "").strip().lower()

    if return_view == "edit" and user_id:
        return url_for(
            "admin_user_edit",
            user_id=user_id,
            q=query_text,
            role=role_filter if role_filter and role_filter != "all" else None,
            plan=plan_filter if plan_filter and plan_filter != "all" else None,
            manual_override=manual_override_filter if manual_override_filter and manual_override_filter != "all" else None,
            page=page,
        )

    return url_for(
        fallback_endpoint,
        q=query_text,
        role=role_filter if role_filter and role_filter != "all" else None,
        plan=plan_filter if plan_filter and plan_filter != "all" else None,
        manual_override=manual_override_filter if manual_override_filter and manual_override_filter != "all" else None,
        page=page,
    )


SEARCH_BINARY_FILTER_VALUES = {"", "yes", "no"}
SEARCH_CONFIDENCE_FILTERS = {"", "70", "85", "95"}
SEARCH_VERIFICATION_FILTERS = {"", "recent", "needs_verification"}
SEARCH_TOILET_DISTANCE_FILTERS = {"", "recorded", "short", "unknown"}
SEARCH_TEXT_PRESENCE_FILTERS = {"", "has", "missing"}
SEARCH_PUBLIC_SOURCE_FILTERS = ("phone_verified", "owner_verified", "user_submitted", "not_verified")
STALE_VERIFICATION_DAYS = 90
LOW_CONFIDENCE_THRESHOLD = 40
REVIEW_CONFIDENCE_THRESHOLD = 60
KEY_ACCESSIBILITY_FIELDS = (
    ("toilets_available", "Toilets available"),
    ("accessible_toilet", "Accessible toilet"),
    ("step_free_entrance", "Step-free entrance"),
    ("stairs_inside", "Stairs inside"),
)
ADMIN_QUALITY_QUEUE_OPTIONS = [
    {"value": "all", "label": "All venues"},
    {"value": "needs_checking", "label": "Needs checking"},
    {"value": "stale_verification", "label": "Stale verification"},
    {"value": "missing_accessibility", "label": "Missing key accessibility fields"},
]


def normalize_search_choice(raw_value, allowed_values, default=""):
    value = (raw_value or "").strip().lower()
    return value if value in allowed_values else default


def build_public_search_filter_options():
    venue_type_rows = (
        db.session.query(db.func.lower(db.func.trim(Place.venue_type)))
        .filter(Place.venue_type.isnot(None), db.func.trim(Place.venue_type) != "")
        .distinct()
        .order_by(db.func.lower(db.func.trim(Place.venue_type)).asc())
        .all()
    )
    venue_type_values = [row[0] for row in venue_type_rows if row[0]]
    return {
        "venue_type_values": venue_type_values,
        "venue_type_options": [{"value": value, "label": humanize_label(value)} for value in venue_type_values],
        "source_options": [
            {"value": value, "label": humanize_label(value, VERIFICATION_SOURCE_LABELS)}
            for value in SEARCH_PUBLIC_SOURCE_FILTERS
        ],
    }


def build_search_filter_state(args):
    public_options = build_public_search_filter_options()
    return {
        "q": (args.get("q") or "").strip(),
        "town": (args.get("town") or "").strip(),
        "accessible": normalize_search_choice(args.get("accessible"), SEARCH_BINARY_FILTER_VALUES),
        "step_free": normalize_search_choice(args.get("step_free"), SEARCH_BINARY_FILTER_VALUES),
        "stairs_inside": normalize_search_choice(args.get("stairs_inside"), SEARCH_BINARY_FILTER_VALUES),
        "baby_changing": normalize_search_choice(args.get("baby_changing"), SEARCH_BINARY_FILTER_VALUES),
        "confidence": normalize_search_choice(args.get("confidence"), SEARCH_CONFIDENCE_FILTERS),
        "verification": normalize_search_choice(args.get("verification"), SEARCH_VERIFICATION_FILTERS),
        "toilet_distance": normalize_search_choice(args.get("toilet_distance"), SEARCH_TOILET_DISTANCE_FILTERS),
        "venue_type": normalize_search_choice(args.get("venue_type"), {"", *public_options["venue_type_values"]}),
        "toilets_available": normalize_search_choice(args.get("toilets_available"), {"", "yes", "no", "unknown"}),
        "lift_available": normalize_search_choice(args.get("lift_available"), SEARCH_BINARY_FILTER_VALUES),
        "disabled_parking": normalize_search_choice(args.get("disabled_parking"), SEARCH_BINARY_FILTER_VALUES),
        "sensory_notes": normalize_search_choice(args.get("sensory_notes"), SEARCH_TEXT_PRESENCE_FILTERS),
        "public_comments": normalize_search_choice(args.get("public_comments"), SEARCH_TEXT_PRESENCE_FILTERS),
        "source": normalize_search_choice(args.get("source"), {"", *SEARCH_PUBLIC_SOURCE_FILTERS}),
    }


def build_search_filter_payload(filters):
    return {
        "query": filters["q"] or None,
        "town": filters["town"] or None,
        "accessible": filters["accessible"] or None,
        "step_free": filters["step_free"] or None,
        "stairs_inside": filters["stairs_inside"] or None,
        "baby_changing": filters["baby_changing"] or None,
        "confidence": filters["confidence"] or None,
        "verification": filters["verification"] or None,
        "toilet_distance": filters["toilet_distance"] or None,
        "venue_type": filters["venue_type"] or None,
        "toilets_available": filters["toilets_available"] or None,
        "lift_available": filters["lift_available"] or None,
        "disabled_parking": filters["disabled_parking"] or None,
        "sensory_notes": filters["sensory_notes"] or None,
        "public_comments": filters["public_comments"] or None,
        "source": filters["source"] or None,
    }


def build_search_active_filters(filters):
    active_filters = []

    if filters["town"]:
        active_filters.append({"label": "Town", "value": filters["town"]})
    if filters["venue_type"]:
        active_filters.append({"label": "Venue type", "value": humanize_label(filters["venue_type"])})
    if filters["accessible"]:
        active_filters.append({"label": "Accessible toilet", "value": filters["accessible"]})
    if filters["toilets_available"]:
        active_filters.append({"label": "Toilets available", "value": filters["toilets_available"]})
    if filters["step_free"]:
        active_filters.append({"label": "Step-free entrance", "value": filters["step_free"]})
    if filters["stairs_inside"]:
        active_filters.append({"label": "Stairs inside", "value": filters["stairs_inside"]})
    if filters["baby_changing"]:
        active_filters.append({"label": "Baby changing", "value": filters["baby_changing"]})
    if filters["lift_available"]:
        active_filters.append({"label": "Lift available", "value": filters["lift_available"]})
    if filters["disabled_parking"]:
        active_filters.append({"label": "Disabled parking", "value": filters["disabled_parking"]})
    if filters["confidence"]:
        active_filters.append({"label": "Confidence", "value": f"{filters['confidence']}+"})
    if filters["verification"] == "recent":
        active_filters.append({"label": "Verification", "value": "Recently verified"})
    elif filters["verification"] == "needs_verification":
        active_filters.append({"label": "Verification", "value": "Needs verification"})
    if filters["source"]:
        active_filters.append({"label": "Source", "value": humanize_label(filters["source"], VERIFICATION_SOURCE_LABELS)})
    if filters["toilet_distance"] == "recorded":
        active_filters.append({"label": "Toilet distance", "value": "Recorded"})
    elif filters["toilet_distance"] == "short":
        active_filters.append({"label": "Toilet distance", "value": "Short distance"})
    elif filters["toilet_distance"] == "unknown":
        active_filters.append({"label": "Toilet distance", "value": "Unknown"})
    if filters["sensory_notes"] == "has":
        active_filters.append({"label": "Sensory notes", "value": "Has notes"})
    elif filters["sensory_notes"] == "missing":
        active_filters.append({"label": "Sensory notes", "value": "Missing"})
    if filters["public_comments"] == "has":
        active_filters.append({"label": "Public comments", "value": "Has comments"})
    elif filters["public_comments"] == "missing":
        active_filters.append({"label": "Public comments", "value": "Missing"})

    return active_filters


def build_search_pagination_args(filters, submitted):
    return {
        "q": filters["q"] or None,
        "town": filters["town"] or None,
        "accessible": filters["accessible"] or None,
        "step_free": filters["step_free"] or None,
        "stairs_inside": filters["stairs_inside"] or None,
        "baby_changing": filters["baby_changing"] or None,
        "confidence": filters["confidence"] or None,
        "verification": filters["verification"] or None,
        "toilet_distance": filters["toilet_distance"] or None,
        "venue_type": filters["venue_type"] or None,
        "toilets_available": filters["toilets_available"] or None,
        "lift_available": filters["lift_available"] or None,
        "disabled_parking": filters["disabled_parking"] or None,
        "sensory_notes": filters["sensory_notes"] or None,
        "public_comments": filters["public_comments"] or None,
        "source": filters["source"] or None,
    }


def is_new_search_submission(*, submitted, page):
    return submitted and page <= 1


def parse_selected_place_id(raw_value):
    try:
        return parse_int_field(raw_value, "Selected place", minimum=1, default=None)
    except ValueError:
        return None


def meaningful_text_distance_filter():
    return db.func.length(db.func.trim(db.func.coalesce(AccessibilityProfile.toilet_distance_from_bar, ""))) > 0


def meaningful_profile_text_filter(column):
    return db.func.length(db.func.trim(db.func.coalesce(column, ""))) > 0


def stale_verification_cutoff():
    return datetime.now(timezone.utc) - timedelta(days=STALE_VERIFICATION_DAYS)


def autocomplete_signal_badge_label(profile):
    if not profile:
        return None

    label = build_access_signal(profile)["label"]
    return {
        "Easy": "Looks straightforward",
        "Tricky": "Might be tricky",
    }.get(label, label)


def build_autocomplete_badges(place):
    profile = getattr(place, "accessibility", None)
    badges = []
    verification = verification_status(profile)

    if verification["verified"]:
        badges.append("Verified")
    if profile and getattr(profile, "step_free_entrance", "") == "yes":
        badges.append("Step-free")
    if profile and getattr(profile, "accessible_toilet", "") == "yes":
        badges.append("Accessible toilet")

    signal_badge = autocomplete_signal_badge_label(profile)
    if signal_badge:
        badges.append(signal_badge)

    return badges


def serialize_autocomplete_place(place, *, suggestion_type="place"):
    return {
        "type": suggestion_type,
        "id": place.id,
        "place_id": place.id,
        "title": place.name,
        "name": place.name,
        "town": place.town or "",
        "subtitle": place.town or "",
        "query": place.name,
        "selected_place_id": place.id,
        "badges": build_autocomplete_badges(place),
    }


def current_user_for_optional_request():
    if not session.get("user"):
        return None
    return current_user()


def autocomplete_recent_groups(user, query_text, *, limit=2):
    if not user:
        return []

    like = f"%{query_text}%"
    recent_events = (
        SearchEvent.query.filter(
            SearchEvent.user_id == user.id,
            db.or_(
                SearchEvent.query_text.ilike(like),
                SearchEvent.town.ilike(like),
            ),
        )
        .order_by(SearchEvent.created_at.desc())
        .limit(12)
        .all()
    )

    seen = set()
    items = []
    for event in recent_events:
        key = ((event.query_text or "").strip().lower(), (event.town or "").strip().lower())
        if key in seen or (not event.query_text and not event.town):
            continue
        seen.add(key)
        items.append(
            {
                "type": "recent",
                "title": event.query_text or event.town or "Recent search",
                "subtitle": event.town or "",
                "query": event.query_text or "",
                "town": event.town or "",
                "selected_place_id": None,
                "badges": [],
            }
        )
        if len(items) >= limit:
            break

    return items


def autocomplete_popular_places(query_text, *, town_context="", exclude_ids=None, limit=2):
    exclude_ids = exclude_ids or set()
    like = f"%{query_text}%"
    town_like = f"%{town_context}%"
    verified_cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)

    if town_context:
        query = query.filter(Place.town.ilike(town_like))
    else:
        query = query.filter(
            db.or_(
                Place.name.ilike(like),
                Place.town.ilike(like),
            )
        )

    if exclude_ids:
        query = query.filter(~Place.id.in_(exclude_ids))

    results = (
        query.order_by(
            db.case(
                (
                    db.and_(
                        AccessibilityProfile.last_verified_at.isnot(None),
                        AccessibilityProfile.last_verified_at >= verified_cutoff,
                    ),
                    0,
                ),
                else_=1,
            ).asc(),
            AccessibilityProfile.confidence_score.desc().nullslast(),
            AccessibilityProfile.last_verified_at.desc().nullslast(),
            Place.name.asc(),
        )
        .limit(limit)
        .all()
    )
    return [serialize_autocomplete_place(place, suggestion_type="popular") for place in results]


def autocomplete_pg_trgm_supported():
    if not is_postgresql_database_uri(app.config["SQLALCHEMY_DATABASE_URI"]):
        return False

    try:
        return bool(
            db.session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
            ).scalar()
        )
    except Exception:
        return False


def autocomplete_place_matches(query_text, *, town_context="", limit=4):
    like = f"%{query_text}%"
    starts_like = f"{query_text}%"
    query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)

    similarity_supported = autocomplete_pg_trgm_supported()
    if similarity_supported:
        lowered_query = query_text.lower()
        query = query.filter(
            db.or_(
                Place.name.ilike(like),
                Place.town.ilike(like),
                db.func.similarity(db.func.lower(Place.name), lowered_query) >= 0.35,
            )
        )
        relevance_order = [
            db.case(
                (Place.name.ilike(starts_like), 0),
                (Place.name.ilike(like), 1),
                (db.func.similarity(db.func.lower(Place.name), lowered_query) >= 0.5, 2),
                (Place.town.ilike(starts_like), 3),
                (Place.town.ilike(like), 4),
                else_=5,
            ),
            db.func.similarity(db.func.lower(Place.name), lowered_query).desc(),
            Place.name.asc(),
        ]
    else:
        query = query.filter(
            db.or_(
                Place.name.ilike(like),
                Place.town.ilike(like),
            )
        )
        relevance_order = [
            db.case(
                (Place.name.ilike(starts_like), 0),
                (Place.name.ilike(like), 1),
                (Place.town.ilike(starts_like), 2),
                (Place.town.ilike(like), 3),
                else_=4,
            ),
            Place.name.asc(),
        ]

    if town_context:
        query = query.filter(Place.town.ilike(f"%{town_context}%"))
        relevance_order.insert(0, db.case((Place.town.ilike(f"{town_context}%"), 0), (Place.town.ilike(f"%{town_context}%"), 1), else_=2).asc())

    results = query.order_by(*relevance_order, Place.town.asc().nullslast()).limit(limit).all()
    return [serialize_autocomplete_place(place, suggestion_type="place") for place in results]


def build_autocomplete_groups(query_text, *, town_context="", user=None):
    place_items = autocomplete_place_matches(query_text, town_context=town_context, limit=4)
    groups = []

    recent_items = autocomplete_recent_groups(user, query_text, limit=2)
    if recent_items:
        groups.append({"key": "recent", "label": "Recent", "items": recent_items})

    if place_items:
        groups.append({"key": "places", "label": "Places", "items": place_items})

    popular_items = autocomplete_popular_places(
        query_text,
        town_context=town_context,
        exclude_ids={item["place_id"] for item in place_items if item.get("place_id")},
        limit=2,
    )
    if popular_items:
        groups.append({"key": "popular", "label": "Popular", "items": popular_items})

    return groups


def missing_key_accessibility_fields(profile):
    if not profile:
        return [label for _, label in KEY_ACCESSIBILITY_FIELDS]

    missing = []
    for field_name, label in KEY_ACCESSIBILITY_FIELDS:
        if getattr(profile, field_name, "unknown") in {"", None, "unknown", "partial"}:
            missing.append(label)
    return missing


def quality_queue_for_profile(profile):
    verification = verification_status(profile)
    missing_fields = missing_key_accessibility_fields(profile)
    confidence_score = getattr(profile, "confidence_score", None) if profile else None
    low_confidence = confidence_score is None or confidence_score < REVIEW_CONFIDENCE_THRESHOLD
    urgent_confidence = confidence_score is None or confidence_score < LOW_CONFIDENCE_THRESHOLD

    if verification["status"] == "Needs checking" or verification["status"] == "Not verified yet" or urgent_confidence:
        queue_label = "Needs checking"
    elif missing_fields:
        queue_label = "Missing key accessibility fields"
    else:
        queue_label = "Checked recently"

    return {
        "queue_label": queue_label,
        "missing_fields": missing_fields,
        "missing_fields_count": len(missing_fields),
        "low_confidence": low_confidence,
        "urgent_confidence": urgent_confidence,
        "stale_verification": verification["status"] == "Needs checking",
        "never_verified": verification["status"] == "Not verified yet",
    }


def missing_key_accessibility_filter():
    unknown_values = {"unknown", "partial"}
    conditions = []
    for field_name, _ in KEY_ACCESSIBILITY_FIELDS:
        column = getattr(AccessibilityProfile, field_name)
        conditions.append(column.is_(None))
        conditions.append(column.in_(unknown_values))
    return db.or_(AccessibilityProfile.id.is_(None), *conditions)


def build_quality_queue_query(kind):
    query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)
    cutoff = stale_verification_cutoff()

    if kind == "needs_checking":
        return query.filter(
            db.or_(
                AccessibilityProfile.id.is_(None),
                AccessibilityProfile.last_verified_at.is_(None),
                AccessibilityProfile.last_verified_at < cutoff,
                AccessibilityProfile.confidence_score.is_(None),
                AccessibilityProfile.confidence_score < REVIEW_CONFIDENCE_THRESHOLD,
            )
        )
    if kind == "stale_verification":
        return query.filter(
            AccessibilityProfile.last_verified_at.isnot(None),
            AccessibilityProfile.last_verified_at < cutoff,
        )
    if kind == "missing_accessibility":
        return query.filter(missing_key_accessibility_filter())
    return query


def build_dashboard_quality_queues(limit=5):
    queue_map = {
        "needs_checking": {
            "label": "Needs checking",
            "description": "Records with no recent verification or low confidence.",
        },
        "stale_verification": {
            "label": "Stale verification",
            "description": "Previously checked records that now need a fresh confirmation.",
        },
        "missing_accessibility": {
            "label": "Missing key accessibility fields",
            "description": "Core access answers still missing or too vague to trust.",
        },
    }
    queues = []
    for key, config in queue_map.items():
        query = build_quality_queue_query(key).order_by(Place.priority.desc(), Place.updated_at.desc(), Place.name.asc())
        sample_places = query.limit(limit).all()
        queues.append(
            {
                "key": key,
                "label": config["label"],
                "description": config["description"],
                "count": query.count(),
                "href": url_for("admin_venues", quality_queue=key),
                "sample_rows": build_admin_venue_rows(sample_places),
            }
        )
    return queues


def build_admin_venue_query(
    *,
    q="",
    town="",
    postcode="",
    status="all",
    profile="all",
    toilet_distance="all",
    confidence="all",
    verified="all",
    quality_queue="all",
    sort="priority",
):
    query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Place.name.ilike(like),
                Place.address1.ilike(like),
                Place.postcode.ilike(like),
            )
        )

    if town:
        query = query.filter(Place.town.ilike(f"%{town}%"))

    if postcode:
        query = query.filter(Place.postcode.ilike(f"%{postcode}%"))

    if status and status != "all":
        query = query.filter(Place.status == status)

    if profile == "has_profile":
        query = query.filter(AccessibilityProfile.id.isnot(None))
    elif profile == "missing_profile":
        query = query.filter(AccessibilityProfile.id.is_(None))

    if toilet_distance == "known":
        query = query.filter(AccessibilityProfile.toilet_distance_from_bar_m.isnot(None))
    elif toilet_distance == "missing":
        query = query.filter(
            db.or_(
                AccessibilityProfile.id.is_(None),
                AccessibilityProfile.toilet_distance_from_bar_m.is_(None),
            )
        )

    if confidence == "low":
        query = query.filter(AccessibilityProfile.confidence_score.isnot(None), AccessibilityProfile.confidence_score < 40)
    elif confidence == "medium":
        query = query.filter(
            AccessibilityProfile.confidence_score.isnot(None),
            AccessibilityProfile.confidence_score >= 40,
            AccessibilityProfile.confidence_score < 70,
        )
    elif confidence == "high":
        query = query.filter(AccessibilityProfile.confidence_score.isnot(None), AccessibilityProfile.confidence_score >= 70)
    elif confidence == "missing":
        query = query.filter(
            db.or_(
                AccessibilityProfile.id.is_(None),
                AccessibilityProfile.confidence_score.is_(None),
            )
        )

    stale_cutoff = stale_verification_cutoff()
    if verified == "verified":
        query = query.filter(
            AccessibilityProfile.last_verified_at.isnot(None),
            AccessibilityProfile.last_verified_at >= stale_cutoff,
        )
    elif verified == "never_verified":
        query = query.filter(
            db.or_(
                AccessibilityProfile.id.is_(None),
                AccessibilityProfile.last_verified_at.is_(None),
            )
        )
    elif verified == "stale":
        query = query.filter(AccessibilityProfile.last_verified_at.isnot(None), AccessibilityProfile.last_verified_at < stale_cutoff)

    if quality_queue == "needs_checking":
        query = query.filter(
            db.or_(
                AccessibilityProfile.id.is_(None),
                AccessibilityProfile.last_verified_at.is_(None),
                AccessibilityProfile.last_verified_at < stale_cutoff,
                AccessibilityProfile.confidence_score.is_(None),
                AccessibilityProfile.confidence_score < REVIEW_CONFIDENCE_THRESHOLD,
            )
        )
    elif quality_queue == "stale_verification":
        query = query.filter(
            AccessibilityProfile.last_verified_at.isnot(None),
            AccessibilityProfile.last_verified_at < stale_cutoff,
        )
    elif quality_queue == "missing_accessibility":
        query = query.filter(missing_key_accessibility_filter())

    priority_ordering = [
        db.case((AccessibilityProfile.id.is_(None), 0), else_=1).asc(),
        db.case((AccessibilityProfile.last_verified_at.is_(None), 0), (AccessibilityProfile.last_verified_at < stale_cutoff, 1), else_=2).asc(),
        db.case(
            (AccessibilityProfile.confidence_score.is_(None), 0),
            (AccessibilityProfile.confidence_score < LOW_CONFIDENCE_THRESHOLD, 1),
            (AccessibilityProfile.confidence_score < REVIEW_CONFIDENCE_THRESHOLD, 2),
            else_=3,
        ).asc(),
        db.case((missing_key_accessibility_filter(), 0), else_=1).asc(),
        Place.priority.desc(),
        Place.updated_at.desc(),
        Place.name.asc(),
    ]
    sort_map = {
        "priority": priority_ordering,
        "updated": [Place.updated_at.desc(), Place.priority.desc(), Place.name.asc()],
        "name": [Place.name.asc()],
        "confidence": [AccessibilityProfile.confidence_score.desc().nullslast(), Place.name.asc()],
        "last_verified": [AccessibilityProfile.last_verified_at.desc().nullslast(), Place.name.asc()],
    }
    order_by = sort_map.get(sort, sort_map["priority"])
    return query.order_by(*order_by)


def build_admin_venue_rows(places):
    rows = []
    for place in places:
        profile = place.accessibility
        signal = build_access_signal(profile)
        verification = verification_status(profile)
        quality = quality_queue_for_profile(profile)
        rows.append(
            {
                "place": place,
                "profile": profile,
                "status_label": humanize_label(place.status, PLACE_STATUS_LABELS),
                "confidence_score": profile.confidence_score if profile and profile.confidence_score is not None else None,
                "toilet_distance_from_bar_m": profile.toilet_distance_from_bar_m if profile else None,
                "last_verified": verification["date"] or "Not checked yet",
                "last_checked_copy": verification["last_checked_copy"],
                "verification_label": verification["label"],
                "verification_badge_class": verification["badge_class"],
                "verification_relative_time": verification["relative_time"],
                "verified_by_label": profile.last_verified_by if profile and profile.last_verified_by else None,
                "signal_label": signal["label"],
                "profile_missing": profile is None,
                "quality_queue_label": quality["queue_label"],
                "quality_badge_class": "badge-warning" if quality["queue_label"] != "Checked recently" else "badge-verified",
                "missing_fields": quality["missing_fields"],
                "missing_fields_count": quality["missing_fields_count"],
                "low_confidence": quality["low_confidence"],
                "stale_verification": quality["stale_verification"],
            }
        )
    return rows


def build_data_rows(limit=20):
    places = (
        Place.query.options(selectinload(Place.accessibility))
        .outerjoin(AccessibilityProfile)
        .order_by(Place.updated_at.desc(), Place.name.asc())
        .limit(limit)
        .all()
    )
    rows = []
    for row in build_admin_venue_rows(places):
        rows.append(
            {
                "place": row["place"],
                "profile": row["profile"],
                "confidence": row["confidence_score"],
                "verification": row["verification_label"],
                "verification_badge_class": row["verification_badge_class"],
                "last_verified": row["last_verified"],
                "last_checked_copy": row["last_checked_copy"],
                "verified_by_label": row["verified_by_label"],
                "status_label": row["status_label"],
            }
        )
    return rows


@app.before_request
def ensure_database_ready():
    if should_auto_create_schema():
        inspector = inspect(db.engine)
        schema_missing = not inspector.has_table("user")
        if schema_missing:
            # Manual review: local SQLite fallback stays convenient, but production
            # and PostgreSQL environments should use Flask-Migrate instead.
            db.create_all()
            app.config["_DB_SCHEMA_READY"] = True
            return None

    if app.config.get("_DB_SCHEMA_READY"):
        return None

    if should_auto_create_schema():
        # Manual review: local SQLite fallback stays convenient, but production
        # and PostgreSQL environments should use Flask-Migrate instead.
        db.create_all()
    app.config["_DB_SCHEMA_READY"] = True
    return None


@app.before_request
def protect_forms():
    if request.method != "POST":
        return None
    if request.endpoint in {"add_comment", "upload_place_image", "delete_place_image"} and (
        not session.get("user") or not current_user()
    ):
        session.clear()
        flash("Your session expired. Please sign in again before continuing.", "info")
        return redirect(url_for("login", next=safe_next_target_for_request()))
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return None
    if request.path.startswith("/api/"):
        return None

    sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not sent_token or sent_token != session.get("_csrf_token"):
        abort(400, description="Invalid or missing CSRF token.")
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if response.status_code >= 400:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    else:
        response.headers.setdefault("X-Robots-Tag", robots_directive_for_request())
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(400)
def bad_request(error):
    return render_template(
        "error.html",
        title="Bad request",
        message=getattr(error, "description", "The request could not be processed."),
    ), 400


@app.errorhandler(403)
def forbidden(error):
    return render_template(
        "error.html",
        title="Forbidden",
        message=getattr(error, "description", "You do not have permission to access this page."),
    ), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", title="Page not found", message="The page you were looking for does not exist."), 404


@app.errorhandler(413)
def payload_too_large(error):
    return render_template("error.html", title="Upload too large", message="That submission was too large to process."), 413


@app.errorhandler(429)
def too_many_requests(error):
    return render_template(
        "error.html",
        title="Too many requests",
        message=getattr(error, "description", RATE_LIMIT_ERROR_MESSAGE),
    ), 429


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template("error.html", title="Server error", message="Something went wrong on the server."), 500


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "environment": app.config["ENVIRONMENT"],
            "database": app.config["SQLALCHEMY_DATABASE_URI"].split(":", 1)[0],
            "oauth_configured": bool(os.getenv("GOOGLE_CLIENT_ID", "").strip() and os.getenv("GOOGLE_CLIENT_SECRET", "").strip()),
            "stripe_configured": bool(app.config["STRIPE_SECRET_KEY"]),
            "turnstile_configured": turnstile_is_configured(),
        }
    )


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /account",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /auth/",
        "Disallow: /billing/",
        "Disallow: /dashboard",
        "Disallow: /health",
        "Disallow: /obs/",
        "Disallow: /search",
        "Disallow: /staff/",
        f"Sitemap: {build_absolute_url('sitemap_xml')}",
    ]
    return app.response_class("\n".join(lines) + "\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    entries = [
        build_absolute_url("index"),
        build_absolute_url("plans"),
        build_absolute_url("developers"),
        build_absolute_url("privacy"),
        build_absolute_url("terms"),
        build_absolute_url("data_rights"),
        build_absolute_url("cookies"),
    ]
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in entries:
        xml_parts.append(f"<url><loc>{xml_escape(url)}</loc></url>")

    place_rows = (
        db.session.query(Place.slug, Place.updated_at)
        .filter(Place.slug.isnot(None))
        .order_by(Place.id.asc())
        .yield_per(500)
    )
    for slug, updated_at in place_rows:
        if not slug:
            continue
        lastmod = ""
        if updated_at:
            lastmod = f"<lastmod>{updated_at.date().isoformat()}</lastmod>"
        xml_parts.append(
            f"<url><loc>{xml_escape(build_absolute_url('place_detail', slug=slug))}</loc>{lastmod}</url>"
        )

    xml_parts.append("</urlset>")
    return app.response_class("\n".join(xml_parts), mimetype="application/xml")


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    accessible = request.args.get("accessible", "").strip()
    preview_places = (
        Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)
        .order_by(
            db.case(
                (AccessibilityProfile.last_verified_at.isnot(None), 0),
                else_=1,
            ),
            Place.name.asc(),
        )
        .limit(3)
        .all()
    )
    catalog = build_plan_catalog()
    plans = [plan for plan in catalog if plan["key"] in {"logged_in_free", "paid_consumer"}]
    api_packs = [plan for plan in catalog if plan["key"].startswith("api_")]
    preview_cards = [build_place_card(place) for place in preview_places]
    return render_template(
        "index.html",
        q=q,
        town=town,
        accessible=accessible,
        preview_cards=preview_cards,
        plans=plans,
        api_packs=api_packs,
        signal_examples=build_signal_examples(),
        seo=build_seo_payload(
            title=f"{APP_NAME} | {TAGLINE}",
            description="Planira helps you know before you go with practical venue accessibility and planning details.",
            canonical_url=build_absolute_url("index"),
            structured_data=[build_organization_schema(), build_website_schema()],
        ),
    )


@app.route("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        seo=build_seo_payload(
            title=f"Privacy | {APP_NAME}",
            description="Learn what personal data Planira stores, why it is needed, and how abuse protection is handled.",
            canonical_url=build_absolute_url("privacy"),
        ),
    )


@app.route("/terms")
def terms():
    return render_template(
        "terms.html",
        seo=build_seo_payload(
            title=f"Terms | {APP_NAME}",
            description="Read the terms for using Planira and contributing to the service.",
            canonical_url=build_absolute_url("terms"),
        ),
    )


@app.route("/data-rights")
def data_rights():
    return render_template(
        "data_rights.html",
        seo=build_seo_payload(
            title=f"Data Rights | {APP_NAME}",
            description="Understand your account data choices and how to contact Planira about them.",
            canonical_url=build_absolute_url("data_rights"),
        ),
    )


@app.route("/cookies")
def cookies():
    return render_template(
        "cookies.html",
        seo=build_seo_payload(
            title=f"Cookies | {APP_NAME}",
            description="Planira uses essential cookies for sign-in, session security, and Turnstile anti-abuse checks.",
            canonical_url=build_absolute_url("cookies"),
        ),
    )


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form_values = {
        "name": (request.form.get("name") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "subject": (request.form.get("subject") or "").strip(),
        "message": (request.form.get("message") or "").strip(),
    }
    if request.method == "POST":
        enforce_rate_limit("contact_form", limit=5, window_seconds=3600)
        if not protect_with_turnstile("contact_submission"):
            return redirect(url_for("contact"))

        try:
            name = form_values["name"][:255]
            email = validate_basic_email_address(form_values["email"])
            subject = form_values["subject"][:255]
            message = form_values["message"]
            if not name:
                raise ValueError("Please add your name.")
            if not subject:
                raise ValueError("Please add a subject.")
            if not message:
                raise ValueError("Please add a message.")
            if len(message) > 5000:
                raise ValueError("Messages must be 5000 characters or fewer.")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "contact.html",
                form_values=form_values,
                turnstile_contact=build_turnstile_context("contact_submission"),
                enable_turnstile=True,
                seo=build_seo_payload(
                    title=f"Contact | {APP_NAME}",
                    description="Send a support message to Planira without exposing a public inbox address.",
                    canonical_url=build_absolute_url("contact"),
                ),
            )

        contact_message = ContactMessage(name=name, email=email, subject=subject, message=message, status="new")
        db.session.add(contact_message)
        db.session.commit()
        queue_analytics_event("contact_submitted", {"source": "contact_form"})
        email_sent = False
        if support_email_address():
            email_sent = send_templated_email(
                f"Planira support: {subject}",
                [support_email_address()],
                "contact_notification",
                reply_to=email,
                category="support",
                contact_message=contact_message,
            )

        if email_sent:
            flash("Message received. We’ll review it and reply if needed.", "success")
        else:
            flash("Message received. Your message was saved safely even though email delivery could not be confirmed.", "info")
        return redirect(url_for("contact"))

    return render_template(
        "contact.html",
        form_values=form_values,
        turnstile_contact=build_turnstile_context("contact_submission"),
        enable_turnstile=True,
        seo=build_seo_payload(
            title=f"Contact | {APP_NAME}",
            description="Send a support message to Planira without exposing a public inbox address.",
            canonical_url=build_absolute_url("contact"),
        ),
    )


@app.route("/newsletter", methods=["GET", "POST"])
def newsletter():
    form_values = {"email": (request.form.get("email") or "").strip()}
    if request.method == "POST":
        enforce_rate_limit("newsletter_signup", limit=5, window_seconds=3600)
        if not protect_with_turnstile("newsletter_signup"):
            return redirect(url_for("newsletter"))
        if request.form.get("newsletter_opt_in") != "yes":
            flash("Please confirm that you want to receive occasional Planira emails.", "error")
            return redirect(url_for("newsletter"))

        try:
            email = validate_basic_email_address(form_values["email"])
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("newsletter"))

        subscriber, result = subscribe_newsletter(
            email,
            source="public_newsletter_form",
            consent_text=NEWSLETTER_CONSENT_TEXT,
        )
        if result == "already_subscribed":
            flash("That email address is already subscribed.", "info")
            return redirect(url_for("newsletter"))

        confirmation_sent = send_newsletter_confirmation_email(subscriber)
        if confirmation_sent:
            flash("You’re subscribed. A confirmation email is on the way.", "success")
        else:
            flash("You’re subscribed. We may not be able to send confirmation email right away, but your preference has been saved.", "success")
        return redirect(url_for("newsletter"))

    return render_template(
        "newsletter.html",
        consent_text=NEWSLETTER_CONSENT_TEXT,
        form_values=form_values,
        turnstile_newsletter=build_turnstile_context("newsletter_signup"),
        enable_turnstile=True,
        seo=build_seo_payload(
            title=f"Newsletter | {APP_NAME}",
            description="Opt in to occasional Planira email updates with a one-click unsubscribe link.",
            canonical_url=build_absolute_url("newsletter"),
        ),
    )


@app.route("/newsletter/unsubscribe/<token>")
def newsletter_unsubscribe(token):
    email = decode_unsubscribe_token(token)
    state = "invalid"
    if email:
        _, result = unsubscribe_newsletter(email)
        if result in {"unsubscribed", "already_unsubscribed"}:
            state = result
    return render_template(
        "newsletter_unsubscribed.html",
        state=state,
        seo=build_seo_payload(
            title=f"Newsletter unsubscribe | {APP_NAME}",
            description="Manage your Planira newsletter subscription.",
            canonical_url=build_absolute_url("newsletter_unsubscribe", token=token),
            robots="noindex, nofollow",
        ),
    )


@app.route("/plans")
def plans():
    user = current_user()
    account_state = build_account_state(user)
    add_page_analytics_event("pricing_viewed", {"signed_in": bool(user)})
    active_key = current_plan_catalog_key(user) if user else "free_visitor"
    account_summary = build_user_summary(user) if user else None
    active_quota_copy = account_summary["quota_copy"] if account_summary else None
    tiers = [
        {
            "name": "Free visitor",
            "price": "PS0",
            "description": "Explore what Planira offers before signing in. Create an account when you're ready to save usage and unlock search access.",
            "features": [
                "Search starts from the home experience",
                "Google login unlocks venue results",
                "Clear dataset and coverage messaging",
            ],
            "cta": None,
            "checkout_enabled": False,
            "key": "free_visitor",
            "role": "visitor",
            "limit_label": "Access",
            "search_limit_copy": "Preview only until you sign in",
            "credits_copy": "Sign in to start tracked search usage and a monthly search allowance.",
        },
        *[
            {
                **plan,
                "checkout_enabled": stripe_checkout_ready(plan),
                "limit_label": build_plan_tier_copy(plan, user=user, active_quota_copy=active_quota_copy)["limit_label"],
                "search_limit_copy": build_plan_tier_copy(plan, user=user, active_quota_copy=active_quota_copy)["limit_copy"],
                "credits_copy": build_plan_tier_copy(plan, user=user, active_quota_copy=active_quota_copy)["credits_copy"],
            }
            for plan in build_plan_catalog()
        ],
    ]
    community_rewards = [
        "Points for useful contributions",
        "Ranks: Tourist, Explorer, Mapper, Inspector",
        "Verified by community status",
        "Leaderboard widget for OBS",
    ]
    stream_revenue = [
        "Developer lookup packs for trusted data",
        "Team-ready access for structured workflows",
        "Verified records powering stronger filters",
        "Clearer place detail for people planning ahead",
    ]
    return render_template(
        "plans.html",
        tiers=tiers,
        community_rewards=community_rewards,
        stream_revenue=stream_revenue,
        stripe_configured=bool(stripe and app.config["STRIPE_SECRET_KEY"]),
        active_key=active_key,
        account_summary=account_summary,
        account_state=account_state,
        seo=build_seo_payload(
            title=f"Plans | {APP_NAME}",
            description="Compare Planira plans for calm place search, account tools, and developer API access.",
            canonical_url=build_absolute_url("plans"),
        ),
    )


@app.route("/account")
@login_required
def account():
    user = current_user()
    account_state = build_account_state(user)
    developer_summary = build_developer_summary(user)
    access_map = {
        "Free": "This account uses the free plan with a tracked monthly search allowance.",
        "Paid": "This account uses the paid plan with a larger monthly search allowance.",
        "Business": "This account uses the business plan for developer and API workflows.",
    }
    current_access = {
        "name": account_state["plan_label"],
        "summary": access_map.get(account_state["plan_label"], access_map["Free"]),
    }
    summary = build_user_summary(user)
    quick_actions = [
        {"label": "Manage account", "href": url_for("account_settings"), "style": "primary"},
        {"label": "Search venues", "href": url_for("search"), "style": "secondary"},
        {"label": "Developer API", "href": url_for("developers"), "style": "secondary"},
        {"label": "Upgrade plan", "href": url_for("plans"), "style": "secondary"},
    ]
    return render_template(
        "account.html",
        current_access=current_access,
        account_state=account_state,
        account_summary=summary,
        developer_summary=developer_summary,
        quick_actions=quick_actions,
        seo=build_seo_payload(
            title=f"Account | {APP_NAME}",
            description="Manage your Planira account, plan, search usage, and developer access.",
            canonical_url=build_absolute_url("account"),
            robots="noindex, nofollow",
        ),
    )


@app.route("/account/settings")
@login_required
def account_settings():
    user = current_user()
    settings_sections = build_settings_sections(user)
    return render_template(
        "settings.html",
        settings_sections=settings_sections,
        developer_summary=build_developer_summary(user),
        seo=build_seo_payload(
            title=f"Settings | {APP_NAME}",
            description="Update your Planira account settings and profile preferences.",
            canonical_url=build_absolute_url("account_settings"),
            robots="noindex, nofollow",
        ),
    )


@app.route("/account/settings/profile-image", methods=["POST"])
@login_required
def update_account_profile_image():
    user = current_user()
    if not user:
        abort(403)

    action = (request.form.get("action") or "upload").strip().lower()
    if action == "remove":
        if user.profile_image_filename:
            old_filename = user.profile_image_filename
            user.profile_image_filename = None
            db.session.commit()
            delete_profile_image_file(old_filename)
            flash("Profile picture removed.", "success")
        else:
            flash("There is no profile picture to remove.", "info")
        return redirect(url_for("account_settings"))

    upload = request.files.get("profile_image")
    try:
        new_filename = save_profile_image_upload(upload)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("account_settings"))

    old_filename = user.profile_image_filename
    user.profile_image_filename = new_filename
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_profile_image_file(new_filename)
        raise

    if old_filename and old_filename != new_filename:
        delete_profile_image_file(old_filename)

    flash("Profile picture updated.", "success")
    return redirect(url_for("account_settings"))


@app.route("/account/settings/newsletter", methods=["POST"])
@login_required
def update_account_newsletter():
    user = current_user()
    if not user:
        abort(403)

    action = (request.form.get("action") or "").strip().lower()
    if action == "subscribe":
        subscriber, result = subscribe_newsletter(
            user.email,
            source="account_settings",
            consent_text=NEWSLETTER_CONSENT_TEXT,
        )
        if result == "already_subscribed":
            flash("This account is already subscribed to newsletter updates.", "info")
        else:
            confirmation_sent = send_newsletter_confirmation_email(subscriber)
            if confirmation_sent:
                flash("Newsletter subscription saved and confirmation email sent.", "success")
            else:
                flash("Newsletter subscription saved.", "success")
    elif action == "unsubscribe":
        _, result = unsubscribe_newsletter(user.email)
        if result == "missing":
            flash("This account was not subscribed to newsletter updates.", "info")
        else:
            flash("Newsletter subscription updated.", "success")
    else:
        flash("That newsletter action is not available.", "error")
    return redirect(url_for("account_settings"))


@app.route("/account/api-keys")
@login_required
def account_api_keys():
    user = current_user()
    has_api_access, message = api_access_status(user)
    if not has_api_access:
        return jsonify({"error": "api_access_required", "message": message}), 403
    return jsonify({"api_keys": build_api_key_rows_for_user(user)})


@app.route("/account/api-keys", methods=["POST"])
@login_required
def create_account_api_key():
    user = current_user()
    has_api_access, message = api_access_status(user)
    if not has_api_access:
        return jsonify({"error": "api_access_required", "message": message}), 403
    label = request.form.get("label", "").strip() or "Primary key"
    scopes = request.form.get("scopes", "").strip() or None
    try:
        api_key, raw_key = create_api_key_for_user(user, label=label, scopes=scopes)
    except ValueError as exc:
        return jsonify({"error": "invalid_scope", "message": str(exc)}), 400
    db.session.commit()
    queue_analytics_event("api_key_created", {"surface": "account"})
    return (
        jsonify(
            {
                "api_key": serialize_api_key(api_key, raw_key=raw_key),
                "raw_key": raw_key,
                "copy_warning": "Copy this API key now. The raw key is only shown once.",
            }
        ),
        201,
    )


@app.route("/account/api-keys/<int:key_id>", methods=["POST"])
@login_required
def update_account_api_key(key_id):
    user = current_user()
    api_key = APIKey.query.filter_by(id=key_id, user_id=user.id).first_or_404()
    action = request.form.get("action", "").strip().lower()
    if action == "deactivate":
        api_key.is_active = False
    elif action == "rename":
        api_key.label = (request.form.get("label", "").strip() or api_key.label or "API key")[:120]
    else:
        return jsonify({"error": "invalid_action"}), 400
    db.session.commit()
    return jsonify({"api_key": serialize_api_key(api_key)})


@app.route("/developers")
def developers():
    user = current_user()
    page_context = build_developers_page_context(user)
    page_context["seo"] = build_seo_payload(
        title=f"Developer API | {APP_NAME}",
        description="Explore the Planira developer API preview for structured place search and trusted venue signals.",
        canonical_url=build_absolute_url("developers"),
    )
    page_context["turnstile_create_api_key"] = build_turnstile_context("create_api_key")
    page_context["enable_turnstile"] = bool(user and page_context.get("has_api_access"))
    return render_template("developers.html", **page_context)


@app.route("/developers/api-keys", methods=["POST"])
@login_required
def create_developer_api_key():
    user = current_user()
    has_api_access, message = api_access_status(user)
    if not has_api_access:
        flash(message, "info")
        return redirect(url_for("developers"))
    if not protect_with_turnstile("create_api_key"):
        return redirect(url_for("developers"))
    label = request.form.get("label", "").strip() or "Primary key"
    try:
        api_key, raw_key = create_api_key_for_user(user, label=label)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("developers"))
    db.session.commit()
    add_page_analytics_event("api_key_created", {"surface": "developers"})
    flash("API key created. Copy it now because it will not be shown again.", "success")
    return render_template("developers.html", **build_developers_page_context(user, raw_api_key=raw_key, raw_api_key_label=label))


@app.route("/developers/api-keys/<int:key_id>", methods=["POST"])
@login_required
def update_developer_api_key(key_id):
    user = current_user()
    api_key = APIKey.query.filter_by(id=key_id, user_id=user.id).first_or_404()
    action = request.form.get("action", "").strip().lower()
    if action != "deactivate":
        flash("That API key action is not available.", "error")
        return redirect(url_for("developers"))

    api_key.is_active = False
    db.session.commit()
    flash("API key revoked.", "success")
    return redirect(url_for("developers"))


@app.route("/billing/checkout/<plan_key>", methods=["POST"])
@login_required
def create_checkout(plan_key):
    plan = get_plan(plan_key)
    if not plan or not plan["checkout_mode"]:
        flash("That plan is not available for checkout.", "error")
        return redirect(url_for("plans"))
    if plan_key in DISABLED_API_PACK_PLAN_KEYS:
        flash(API_PACK_DISABLED_MESSAGE, "info")
        return redirect(url_for("plans"))

    if not stripe:
        flash("Stripe is not installed yet. Add the Stripe dependency first.", "error")
        return redirect(url_for("plans"))

    if not app.config["STRIPE_SECRET_KEY"]:
        flash("Stripe is not configured yet. Add your Stripe keys and price IDs to .env.", "error")
        return redirect(url_for("plans"))

    if not plan["price_id"]:
        flash(f"Stripe price ID missing for {plan['name']}.", "error")
        return redirect(url_for("plans"))

    user = current_user()
    if not user:
        flash("Please log in before starting checkout.", "info")
        return redirect(url_for("login", next=url_for("plans")))

    if current_role_key(user) == plan["key"]:
        flash(f"You already have {plan['name']} on this account.", "info")
        return redirect(url_for("plans"))

    metadata = build_checkout_metadata(user, plan)
    checkout_kwargs = {
        "mode": plan["checkout_mode"],
        "line_items": [{"price": plan["price_id"], "quantity": 1}],
        "success_url": url_for("billing_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": url_for("billing_cancel", _external=True),
        "customer_email": user.email,
        "client_reference_id": str(user.id),
        "metadata": metadata,
    }
    if plan_uses_subscription(plan):
        # Mirror user and entitlement metadata onto the subscription so later
        # lifecycle webhooks can resolve the right account without schema changes.
        checkout_kwargs["subscription_data"] = {"metadata": metadata}

    try:
        session_checkout = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception:  # pragma: no cover - third-party API path
        app.logger.exception("Stripe checkout creation failed")
        flash("Checkout could not be started. Please try again in a moment.", "error")
        return redirect(url_for("plans"))
    return redirect(session_checkout.url, code=303)


@app.route("/billing/success")
@login_required
def billing_success():
    checkout_session_id = request.args.get("session_id", "").strip()
    if not checkout_session_id:
        flash("Payment complete, but the checkout session ID was missing.", "warn")
        return redirect(url_for("plans"))

    if not stripe or not app.config["STRIPE_SECRET_KEY"]:
        flash("Payment returned, but Stripe is not configured on the server.", "error")
        return redirect(url_for("plans"))

    try:
        checkout_session = stripe.checkout.Session.retrieve(checkout_session_id)
    except Exception:  # pragma: no cover - third-party API path
        app.logger.exception("Stripe checkout confirmation failed")
        flash("Checkout confirmation could not be completed right now. Please try again in a moment.", "error")
        return redirect(url_for("plans"))

    plan_key = (checkout_session.metadata or {}).get("plan_key")
    target_role = (checkout_session.metadata or {}).get("target_role")
    plan = get_plan(plan_key)

    if not plan:
        flash("Payment completed, but the selected plan could not be matched.", "warn")
        return redirect(url_for("plans"))
    if plan_key in DISABLED_API_PACK_PLAN_KEYS:
        flash(API_PACK_DISABLED_MESSAGE, "warn")
        return redirect(url_for("plans"))

    payment_ok = checkout_session.payment_status == "paid"
    subscription_ok = plan["checkout_mode"] == "subscription" and checkout_session.status == "complete"
    if not (payment_ok or subscription_ok):
        flash("Checkout has not completed yet.", "warn")
        return redirect(url_for("plans"))

    user = current_user()
    if not user or str(user.id) != (checkout_session.client_reference_id or ""):
        flash("Checkout completed, but it could not be matched to your signed-in account.", "error")
        return redirect(url_for("plans"))

    stripe_fields_changed = update_user_stripe_billing_fields(user, checkout_session)
    entitlement_changed = sync_user_entitlement(
        user,
        target_role=target_role,
        reason=f"Stripe checkout completed for {plan['key']}.",
    )
    if entitlement_changed:
        db.session.refresh(user)
        refresh_session_user(user)
    elif stripe_fields_changed:
        db.session.commit()
        db.session.refresh(user)
        refresh_session_user(user)

    send_payment_confirmation_email(
        user,
        plan_name=plan["name"],
        event_key=checkout_session_id,
    )
    flash(f"{plan['name']} is now active on your account.", "success")
    return redirect(url_for("plans"))


@app.route("/billing/cancel")
@login_required
def billing_cancel():
    flash("Checkout cancelled. Your plan has not changed.", "info")
    return redirect(url_for("plans"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not stripe or not app.config["STRIPE_SECRET_KEY"] or not app.config["STRIPE_WEBHOOK_SECRET"]:
        return "Stripe webhook not configured", 400

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            app.config["STRIPE_WEBHOOK_SECRET"],
        )
    except Exception:
        return "Invalid webhook signature", 400

    event_type = event["type"]
    event_object = event["data"]["object"]
    metadata = stripe_metadata_from_object(event_object)
    user = find_user_for_stripe_object(event_object, metadata=metadata)
    target_role = metadata.get("target_role") or infer_entitlement_role(user)
    stripe_fields_changed = False
    entitlement_changed = False

    if user:
        stripe_fields_changed = update_user_stripe_billing_fields(user, event_object)

    if event_type == "checkout.session.completed":
        if metadata.get("plan_key") in DISABLED_API_PACK_PLAN_KEYS or target_role == "api_buyer":
            app.logger.warning("Ignoring disabled API pack webhook fulfilment for user=%s", getattr(user, "id", None))
        else:
            entitlement_changed = sync_user_entitlement(
                user,
                target_role=target_role,
                reason=f"Stripe webhook {event_type}.",
            )
    elif event_type == "customer.subscription.deleted":
        entitlement_changed = revoke_user_entitlement(
            user,
            target_role=target_role,
            reason=f"Stripe webhook {event_type}.",
        )
    elif event_type == "customer.subscription.updated":
        subscription_status = stripe_object_value(event_object, "status", "") or ""
        if subscription_status in {"canceled", "unpaid", "incomplete_expired"}:
            entitlement_changed = revoke_user_entitlement(
                user,
                target_role=target_role,
                reason=f"Stripe webhook {event_type} with status {subscription_status}.",
            )
    elif event_type == "invoice.payment_failed":
        entitlement_changed = revoke_user_entitlement(
            user,
            target_role=target_role,
            reason=f"Stripe webhook {event_type}.",
        )

    if stripe_fields_changed and not entitlement_changed:
        db.session.commit()

    event_object_id = stripe_object_value(event_object, "id", "") or metadata.get("checkout_session_id") or metadata.get("session_id")
    if event_type == "checkout.session.completed" and user and metadata.get("plan_key") and metadata.get("plan_key") not in DISABLED_API_PACK_PLAN_KEYS and target_role != "api_buyer":
        plan = get_plan(metadata.get("plan_key"))
        if plan:
            send_payment_confirmation_email(
                user,
                plan_name=plan["name"],
                event_key=event_object_id,
            )
    elif event_type == "invoice.payment_failed" and user:
        send_payment_failure_email(user, event_key=event_object_id or event_type)

    return jsonify({"received": True})


@app.route("/api/v1/places/search")
def api_places_search():
    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    postcode = request.args.get("postcode", "").strip()

    if not any([q, town, postcode]):
        return api_error_response("missing_query", "Add a place query, town, or postcode before calling this endpoint.", 400)

    auth_result = authenticate_api_key(
        request_obj=request,
        required_scopes={"places:read"},
        endpoint="/api/v1/places/search",
        query=f"q={q}&town={town}&postcode={postcode}",
        apply_usage=False,
        record_event=False,
        commit=False,
    )
    if not auth_result["ok"]:
        return api_auth_error_response(auth_result)

    query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Place.name.ilike(like),
                Place.address1.ilike(like),
                Place.postcode.ilike(like),
            )
        )
    if town:
        query = query.filter(Place.town.ilike(f"%{town}%"))
    if postcode:
        query = query.filter(Place.postcode.ilike(f"%{postcode}%"))

    places = query.order_by(Place.name.asc()).limit(25).all()
    if not places:
        return api_error_response("no_results", "No places matched that lookup.", 404)

    # TODO: Add rate limiting before wider external release.
    limit_context = finalize_api_lookup_success(
        auth_result["api_key"],
        endpoint="/api/v1/places/search",
        query=f"q={q}&town={town}&postcode={postcode}",
        status_code=200,
        commit=True,
    )
    return jsonify(
        {
            "count": len(places),
            "results": [serialize_place_for_api(place) for place in places],
            "usage": {
                "lookups_used": limit_context["lookups_used"],
                "lookup_credits_remaining": limit_context["lookup_credits"],
                "lookup_limit": None if limit_context["monthly_limit"] is None else limit_context["monthly_limit"],
            },
        }
    )


@app.route("/api/v1/places", methods=["POST"])
def api_create_place():
    auth_result, error_response = authenticate_api_write_request("/api/v1/places", query="create")
    if error_response is not None:
        return error_response

    try:
        payload = parse_json_api_payload()
        place_payload, accessibility_payload, verification_payload = extract_api_write_sections(payload)
        if place_payload is None:
            raise ValueError("place must be a JSON object.")

        place = Place()
        apply_place_write_payload(place, place_payload, creating=True)
        db.session.add(place)
        db.session.flush()

        profile = AccessibilityProfile(place=place)
        db.session.add(profile)
        if accessibility_payload:
            apply_accessibility_write_payload(profile, accessibility_payload)
        apply_api_verification_payload(place, profile, auth_result["user"], verification_payload)

        auth_result["api_key"].last_used_at = datetime.now(timezone.utc)
        record_api_lookup(
            api_key_id=auth_result["api_key"].id,
            user_id=auth_result["user"].id,
            endpoint="/api/v1/places",
            query=place.name,
            status_code=201,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return api_error_response("invalid_payload", str(exc), 400)
    except IntegrityError as exc:
        db.session.rollback()
        return api_error_response("invalid_payload", f"Could not save place data: {exc.orig}", 400)

    return jsonify({"place": serialize_place_for_api(place)}), 201


@app.route("/api/v1/places/<int:place_id>", methods=["PATCH"])
def api_update_place(place_id):
    auth_result, error_response = authenticate_api_write_request("/api/v1/places/<id>", query=str(place_id))
    if error_response is not None:
        return error_response

    place = db.session.get(Place, place_id)
    if not place:
        return api_error_response("not_found", "Place not found.", 404)

    profile = place.accessibility
    if not profile:
        profile = AccessibilityProfile(place=place)
        db.session.add(profile)

    try:
        payload = parse_json_api_payload()
        place_payload, accessibility_payload, verification_payload = extract_api_write_sections(payload)
        if place_payload is None and accessibility_payload is None and "mark_verified" not in verification_payload:
            raise ValueError("Include place, accessibility, or mark_verified in the payload.")

        if place_payload:
            apply_place_write_payload(place, place_payload, creating=False)
        if accessibility_payload:
            apply_accessibility_write_payload(profile, accessibility_payload)
        apply_api_verification_payload(place, profile, auth_result["user"], verification_payload)

        auth_result["api_key"].last_used_at = datetime.now(timezone.utc)
        record_api_lookup(
            api_key_id=auth_result["api_key"].id,
            user_id=auth_result["user"].id,
            endpoint="/api/v1/places/<id>",
            query=str(place.id),
            status_code=200,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return api_error_response("invalid_payload", str(exc), 400)
    except IntegrityError as exc:
        db.session.rollback()
        return api_error_response("invalid_payload", f"Could not save place data: {exc.orig}", 400)

    return jsonify({"place": serialize_place_for_api(place)})


@app.route("/search")
def search():
    if not session.get("user"):
        flash("Search results are available after Google login.", "info")
        return redirect(url_for("login", next=safe_next_target_for_request()))

    user = current_user()
    if not user:
        session.clear()
        flash("Your session expired, so please sign in again.", "info")
        return redirect(url_for("login", next=safe_next_target_for_request()))

    filters = build_search_filter_state(request.args)
    public_filter_options = build_public_search_filter_options()
    q = filters["q"]
    town = filters["town"]
    accessible = filters["accessible"]
    venue_type = filters["venue_type"]
    toilets_available = filters["toilets_available"]
    step_free = filters["step_free"]
    stairs_inside = filters["stairs_inside"]
    baby_changing = filters["baby_changing"]
    lift_available = filters["lift_available"]
    disabled_parking = filters["disabled_parking"]
    confidence = filters["confidence"]
    verification = filters["verification"]
    toilet_distance = filters["toilet_distance"]
    sensory_notes = filters["sensory_notes"]
    public_comments = filters["public_comments"]
    source = filters["source"]
    selected_place_id = parse_selected_place_id(request.args.get("selected_place_id"))
    submitted = request.args.get("submitted") == "1"
    try:
        page = parse_int_field(request.args.get("page"), "Page", minimum=1, default=1)
    except ValueError:
        page = 1
    per_page = 12
    is_counted_submission = is_new_search_submission(submitted=submitted, page=page)

    has_filters = any(
        [
            q,
            town,
            venue_type,
            toilets_available,
            accessible,
            step_free,
            stairs_inside,
            baby_changing,
            lift_available,
            disabled_parking,
            confidence,
            verification,
            toilet_distance,
            sensory_notes,
            public_comments,
            source,
            selected_place_id,
        ]
    )
    should_search = has_filters
    if is_counted_submission and has_filters:
        enforce_rate_limit("search_submit", limit=20, window_seconds=300)
    limit_context = search_limit_context(user)
    filters_payload = build_search_filter_payload(filters)
    active_filter_chips = build_search_active_filters(filters)
    pagination_args = build_search_pagination_args(filters, submitted)
    limit_message = None

    pagination = None
    results = []
    result_cards = []
    if should_search:
        if is_counted_submission and has_filters and limit_context["limit_reached"]:
            limit_message = build_quota_copy(user, limit_context)["blocked_message"]
            flash(limit_message, "info")
            should_search = False
        if not should_search:
            search_usage = build_user_summary(user)
            return render_template(
                "search.html",
                results=results,
                result_cards=result_cards,
                pagination=pagination,
                q=q,
                town=town,
                accessible=accessible,
                venue_type=venue_type,
                toilets_available=toilets_available,
                step_free=step_free,
                stairs_inside=stairs_inside,
                baby_changing=baby_changing,
                lift_available=lift_available,
                disabled_parking=disabled_parking,
                confidence=confidence,
                verification=verification,
                toilet_distance=toilet_distance,
                sensory_notes=sensory_notes,
                public_comments=public_comments,
                source=source,
                submitted=submitted,
                has_filters=has_filters,
                should_search=should_search,
                active_filter_chips=active_filter_chips,
                pagination_args=pagination_args,
                venue_type_options=public_filter_options["venue_type_options"],
                source_options=public_filter_options["source_options"],
                search_usage=search_usage,
                signal_examples=build_signal_examples(),
                seo=build_seo_payload(
                    title=f"Search | {APP_NAME}",
                    description="Search Planira for place details, then open the venue page when you need more context.",
                    canonical_url=build_absolute_url("search"),
                    robots="noindex, follow",
                ),
            )
        query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)
        if selected_place_id:
            query = query.filter(Place.id == selected_place_id)
        elif q:
            like = f"%{q}%"
            query = query.filter(db.or_(Place.name.ilike(like), Place.postcode.ilike(like), Place.address1.ilike(like)))
        if town:
            query = query.filter(Place.town.ilike(f"%{town}%"))
        if venue_type:
            query = query.filter(db.func.lower(db.func.trim(Place.venue_type)) == venue_type)
        if toilets_available == "unknown":
            query = query.filter(
                db.or_(
                    AccessibilityProfile.id.is_(None),
                    AccessibilityProfile.toilets_available == "unknown",
                )
            )
        elif toilets_available:
            query = query.filter(AccessibilityProfile.toilets_available == toilets_available)
        if accessible:
            query = query.filter(AccessibilityProfile.accessible_toilet == accessible)
        if step_free:
            query = query.filter(AccessibilityProfile.step_free_entrance == step_free)
        if stairs_inside:
            query = query.filter(AccessibilityProfile.stairs_inside == stairs_inside)
        if baby_changing:
            query = query.filter(AccessibilityProfile.baby_changing == baby_changing)
        if lift_available:
            query = query.filter(AccessibilityProfile.lift_available == lift_available)
        if disabled_parking:
            query = query.filter(AccessibilityProfile.disabled_parking == disabled_parking)
        if confidence:
            query = query.filter(AccessibilityProfile.confidence_score.isnot(None), AccessibilityProfile.confidence_score >= int(confidence))
        if verification == "recent":
            # TODO: Promote this search cutoff to a named constant if verification windows need tuning in more than one place.
            recent_cutoff = datetime.now(timezone.utc) - timedelta(days=45)
            query = query.filter(
                AccessibilityProfile.last_verified_at.isnot(None),
                AccessibilityProfile.last_verified_at >= recent_cutoff,
            )
        elif verification == "needs_verification":
            query = query.filter(AccessibilityProfile.last_verified_at.is_(None))
        if toilet_distance == "recorded":
            query = query.filter(
                db.or_(
                    AccessibilityProfile.toilet_distance_from_bar_m.isnot(None),
                    meaningful_text_distance_filter(),
                )
            )
        elif toilet_distance == "short":
            # TODO: Promote this search threshold to a named constant if short-distance rules need to stay consistent across views.
            query = query.filter(
                AccessibilityProfile.toilet_distance_from_bar_m.isnot(None),
                AccessibilityProfile.toilet_distance_from_bar_m <= 10,
            )
        elif toilet_distance == "unknown":
            query = query.filter(
                db.or_(
                    AccessibilityProfile.id.is_(None),
                    db.and_(
                        AccessibilityProfile.toilet_distance_from_bar_m.is_(None),
                        ~meaningful_text_distance_filter(),
                    ),
                )
            )
        if sensory_notes == "has":
            query = query.filter(meaningful_profile_text_filter(AccessibilityProfile.sensory_notes))
        elif sensory_notes == "missing":
            query = query.filter(
                db.or_(
                    AccessibilityProfile.id.is_(None),
                    ~meaningful_profile_text_filter(AccessibilityProfile.sensory_notes),
                )
            )
        if public_comments == "has":
            query = query.filter(meaningful_profile_text_filter(AccessibilityProfile.public_comments))
        elif public_comments == "missing":
            query = query.filter(
                db.or_(
                    AccessibilityProfile.id.is_(None),
                    ~meaningful_profile_text_filter(AccessibilityProfile.public_comments),
                )
            )
        if source:
            query = query.filter(AccessibilityProfile.source == source)
        pagination = query.order_by(Place.name.asc()).paginate(page=page, per_page=per_page, error_out=False)
        results = pagination.items
        result_cards = [build_place_card(place) for place in results]
        if is_counted_submission and has_filters:
            consume_search_credit_if_needed(user, limit_context)
            track_search_event(
                user,
                q,
                town,
                accessible,
                filters_json=filters_payload,
                result_count=pagination.total,
            )
            add_page_analytics_event(
                "search_submitted",
                {
                    "result_count": pagination.total,
                    "has_query": bool(q),
                    "has_town": bool(town),
                    "selected_place": bool(selected_place_id),
                },
            )
            db.session.commit()
    search_usage = build_user_summary(user)
    return render_template(
        "search.html",
        results=results,
        result_cards=result_cards,
        pagination=pagination,
        q=q,
        town=town,
        accessible=accessible,
        venue_type=venue_type,
        toilets_available=toilets_available,
        step_free=step_free,
        stairs_inside=stairs_inside,
        baby_changing=baby_changing,
        lift_available=lift_available,
        disabled_parking=disabled_parking,
        confidence=confidence,
        verification=verification,
        toilet_distance=toilet_distance,
        sensory_notes=sensory_notes,
        public_comments=public_comments,
        source=source,
        submitted=submitted,
        has_filters=has_filters,
        should_search=should_search,
        active_filter_chips=active_filter_chips,
        pagination_args=pagination_args,
        venue_type_options=public_filter_options["venue_type_options"],
        source_options=public_filter_options["source_options"],
        search_usage=search_usage,
        signal_examples=build_signal_examples(),
        seo=build_seo_payload(
            title=f"Search | {APP_NAME}",
            description="Search Planira for place details, then open the venue page when you need more context.",
            canonical_url=build_absolute_url("search"),
            robots="noindex, follow",
        ),
    )


@app.route("/api/autocomplete")
def autocomplete_places():
    enforce_rate_limit("autocomplete", limit=60, window_seconds=60)
    query_text = (request.args.get("q") or "").strip()
    town_context = (request.args.get("town") or "").strip()
    if len(query_text) < 2:
        return jsonify([])

    groups = build_autocomplete_groups(
        query_text,
        town_context=town_context,
        user=current_user_for_optional_request(),
    )
    return jsonify(groups)


@app.route("/place/<slug>")
def place_detail(slug):
    place = Place.query.filter_by(slug=slug).first_or_404()
    profile = get_or_create_profile(place)
    user = current_user_for_optional_request()
    comment_query = Comment.query.filter_by(place_id=place.id, is_public=True)
    if not is_staff_user(user):
        comment_query = comment_query.filter_by(status="approved")
    comments = comment_query.order_by(Comment.created_at.desc()).all()
    place_images = (
        PlaceImage.query.filter_by(place_id=place.id, is_approved=True)
        .order_by(PlaceImage.created_at.desc())
        .all()
    )
    comment_rows = [
        {
            "public_label": build_public_author_label(comment.user_email),
            "body": comment.body,
        }
        for comment in comments
    ]
    signal = build_access_signal(profile)
    add_page_analytics_event(
        "place_viewed",
        {
            "place_id": place.id,
            "venue_type": (place.venue_type or "unknown").lower(),
        },
    )
    return render_template(
        "place.html",
        place=place,
        profile=profile,
        comments=comment_rows,
        place_images=place_images,
        can_upload_place_images=can_upload_place_images(user),
        get_place_image_url=get_place_image_url,
        signal=signal,
        seo=build_seo_payload(
            title=f"{place.name} | {APP_NAME}",
            description=build_place_seo_description(place, profile, signal),
            canonical_url=build_absolute_url("place_detail", slug=place.slug),
            structured_data=[build_place_structured_data(place)],
        ),
        turnstile_comment=build_turnstile_context("comment_submission"),
        turnstile_place_image=build_turnstile_context("place_image_upload"),
        enable_turnstile=bool(user),
    )


@app.route("/place/<int:place_id>/images/upload", methods=["POST"])
@login_required
def upload_place_image(place_id):
    place = db.session.get(Place, place_id)
    if not place:
        abort(404)

    user = current_user()
    if not can_upload_place_images(user):
        flash("Upgrade to a paid Planira plan to add place photos.", "info")
        return redirect(url_for("place_detail", slug=place.slug))
    if not protect_with_turnstile("place_image_upload"):
        return redirect(url_for("place_detail", slug=place.slug))

    caption = (request.form.get("caption") or "").strip()
    if len(caption) > 255:
        flash("Captions must be 255 characters or fewer.", "error")
        return redirect(url_for("place_detail", slug=place.slug))

    upload = request.files.get("place_image")
    try:
        filename, original_filename = save_place_image_upload(upload)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("place_detail", slug=place.slug))

    place_image = PlaceImage(
        place=place,
        uploader=user,
        filename=filename,
        original_filename=original_filename,
        caption=caption or None,
        is_approved=True,
    )
    db.session.add(place_image)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_place_image_file(filename)
        raise

    flash("Place photo added.", "success")
    return redirect(url_for("place_detail", slug=place.slug))


@app.route("/place-images/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_place_image(image_id):
    place_image = db.session.get(PlaceImage, image_id)
    if not place_image:
        abort(404)

    user = current_user()
    if not can_delete_place_image(user, place_image):
        flash("You can only remove your own place photos.", "error")
        return redirect(url_for("place_detail", slug=place_image.place.slug))

    filename = place_image.filename
    place_slug = place_image.place.slug
    db.session.delete(place_image)
    db.session.commit()
    delete_place_image_file(filename)
    flash("Place photo removed.", "success")
    return redirect(url_for("place_detail", slug=place_slug))


@app.route("/place/<slug>/comment", methods=["POST"])
@login_required
def add_comment(slug):
    comment_actor = current_user()
    comment_identifier = f"user:{comment_actor.id}" if comment_actor else request_client_identifier()
    enforce_rate_limit(
        "comment_submit",
        limit=5,
        window_seconds=600,
        identifier=comment_identifier,
        description="You have submitted several comments recently. Please wait a few minutes and try again.",
    )
    place = Place.query.filter_by(slug=slug).first_or_404()
    if not protect_with_turnstile("comment_submission"):
        return redirect(url_for("place_detail", slug=slug))
    body = request.form.get("body", "").strip()
    if not body:
        flash("Please add a comment before submitting.", "error")
        return redirect(url_for("place_detail", slug=slug))

    if len(body) > 2000:
        flash("Comments must be 2000 characters or fewer.", "error")
        return redirect(url_for("place_detail", slug=slug))

    db.session.add(Comment(place=place, user_email=session["user"]["email"], body=body, is_public=True, status="pending"))
    db.session.commit()
    flash("Comment submitted for review.", "success")
    return redirect(url_for("place_detail", slug=slug))


@app.route("/dashboard")
@login_required
@staff_required
def dashboard():
    mission_page = parse_int_field(request.args.get("mission_page"), "Mission page", minimum=1, default=1)
    mission_per_page = 12
    quality_queues = build_dashboard_quality_queues(limit=4)
    stats = {
        "total": Place.query.count(),
        "needs_call": Place.query.filter_by(status="needs_call").count(),
        "verified": Place.query.filter_by(status="verified").count(),
        "callback": Place.query.filter_by(status="callback").count(),
    }
    mission_query = Place.query.filter(Place.status.in_(["needs_call", "callback"])).order_by(Place.priority.desc(), Place.updated_at.asc())
    mission_pagination = mission_query.paginate(page=mission_page, per_page=mission_per_page, error_out=False)
    next_places = mission_pagination.items
    premium_ready = (
        Place.query.join(AccessibilityProfile)
        .filter(
            Place.status == "verified",
            AccessibilityProfile.confidence_score >= 70,
            AccessibilityProfile.last_verified_at.isnot(None),
        )
        .count()
    )
    api_ready = (
        Place.query.join(AccessibilityProfile)
        .filter(
            AccessibilityProfile.last_verified_at.isnot(None),
            AccessibilityProfile.public_comments.isnot(None),
            AccessibilityProfile.confidence_score >= 60,
        )
        .count()
    )
    user_stats = {
        "all_users": User.query.count(),
        "free_users": User.query.filter(User.plan == "free").count(),
        "staff_users": User.query.filter(User.role.in_(["admin", "staff"])).count(),
    }
    pending_comment_count = Comment.query.filter_by(status="pending").count()
    monetisation_stats = {
        "premium_ready": premium_ready,
        "api_ready": api_ready,
        "community_candidates": stats["verified"],
        "mission_queue": mission_pagination.total,
        "pending_comments": pending_comment_count,
    }
    plan_highlights = [
        "Logged-in free users can become a metered search tier here.",
        "Verified high-confidence places can feed premium filters and saved lists.",
        "Structured verified records become the first API lookup packs.",
        "Priority queue doubles as tonight's livestream mission board.",
    ]
    community_ranks = [
        {"name": "Tourist", "rule": "First correction or confirmation"},
        {"name": "Explorer", "rule": "5 useful venue updates"},
        {"name": "Mapper", "rule": "20 useful venue updates"},
        {"name": "Inspector", "rule": "High-trust verifier status"},
    ]
    recent_activity = build_recent_activity()
    recent_search_activity = build_recent_search_activity()
    recent_audit_entries = build_recent_audit_entries()
    api_operations = build_api_operations_summary()
    quick_actions = [
        {"label": "Venue workspace", "href": url_for("admin_venues")},
        {"label": "Streaming control room", "href": url_for("staff_streaming_control_room")},
        {"label": "Add venue", "href": url_for("new_place")},
        {"label": "Open moderation", "href": url_for("admin_moderation")},
        {"label": "Support inbox", "href": url_for("admin_support")},
        {"label": "Manage users", "href": url_for("admin_users")},
    ]
    return render_template(
        "dashboard.html",
        stats=stats,
        next_places=next_places,
        mission_pagination=mission_pagination,
        mission_rows=build_admin_venue_rows(next_places),
        user_stats=user_stats,
        monetisation_stats=monetisation_stats,
        quality_queues=quality_queues,
        plan_highlights=plan_highlights,
        community_ranks=community_ranks,
        recent_activity=recent_activity,
        recent_search_activity=recent_search_activity,
        recent_audit_entries=recent_audit_entries,
        api_operations=api_operations,
        quick_actions=quick_actions,
    )


@app.route("/admin/support")
@login_required
@staff_required
def admin_support():
    status_filter = (request.args.get("status") or "all").strip().lower() or "all"
    query = ContactMessage.query
    if status_filter in CONTACT_MESSAGE_STATUS_VALUES:
        query = query.filter(ContactMessage.status == status_filter)
    messages = query.order_by(ContactMessage.created_at.desc()).all()
    return render_template(
        "admin_support.html",
        support_rows=build_support_rows(messages),
        support_stats=build_support_stats(),
        status_filter=status_filter,
    )


@app.route("/admin/support/<int:message_id>")
@login_required
@staff_required
def admin_support_detail(message_id):
    message = db.session.get(ContactMessage, message_id)
    if not message:
        abort(404)
    return render_template(
        "admin_support_detail.html",
        contact_message=message,
        support_stats=build_support_stats(),
    )


@app.route("/admin/support/<int:message_id>/reply", methods=["POST"])
@login_required
@staff_required
def admin_support_reply(message_id):
    message = db.session.get(ContactMessage, message_id)
    if not message:
        abort(404)

    reply_body = (request.form.get("reply_body") or "").strip()
    if not reply_body:
        flash("Please add a reply before sending.", "error")
        return redirect(url_for("admin_support_detail", message_id=message.id))

    actor = current_user()
    email_sent = send_templated_email(
        f"Re: {message.subject}",
        [message.email],
        "staff_reply",
        reply_to=support_email_address() or None,
        category="support",
        contact_message=message,
        reply_body=reply_body,
        reply_body_html=basic_html_from_text(reply_body),
        staff_user=actor,
    )
    if not email_sent:
        flash("Reply email could not be sent right now. The message status was not changed.", "error")
        return redirect(url_for("admin_support_detail", message_id=message.id))

    now = datetime.now(timezone.utc)
    before_state = {
        "status": message.status,
        "handled_by_user_id": message.handled_by_user_id,
        "handled_at": message.handled_at,
        "reply_sent_at": message.reply_sent_at,
    }
    message.status = "replied"
    message.handled_by_user_id = actor.id if actor else None
    message.handled_at = now
    message.reply_sent_at = now
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="support.reply_sent",
        entity_type="contact_message",
        entity_id=message.id,
        before=before_state,
        after={
            "status": message.status,
            "handled_by_user_id": message.handled_by_user_id,
            "handled_at": message.handled_at,
            "reply_sent_at": message.reply_sent_at,
        },
        reason="Staff reply sent",
    )
    db.session.commit()
    flash("Reply sent.", "success")
    return redirect(url_for("admin_support_detail", message_id=message.id))


@app.route("/admin/support/<int:message_id>/status", methods=["POST"])
@login_required
@staff_required
def admin_support_status(message_id):
    message = db.session.get(ContactMessage, message_id)
    if not message:
        abort(404)

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in CONTACT_MESSAGE_STATUS_VALUES:
        flash("Choose a valid support status.", "error")
        return redirect(url_for("admin_support_detail", message_id=message.id))

    actor = current_user()
    before_state = {
        "status": message.status,
        "handled_by_user_id": message.handled_by_user_id,
        "handled_at": message.handled_at,
    }
    message.status = new_status
    message.handled_by_user_id = actor.id if actor else None
    message.handled_at = datetime.now(timezone.utc)
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="support.status_updated",
        entity_type="contact_message",
        entity_id=message.id,
        before=before_state,
        after={
            "status": message.status,
            "handled_by_user_id": message.handled_by_user_id,
            "handled_at": message.handled_at,
        },
        reason=f"Support status changed to {new_status}",
    )
    db.session.commit()
    flash("Support status updated.", "success")
    return redirect(url_for("admin_support_detail", message_id=message.id))


@app.route("/admin/newsletter")
@login_required
@admin_required
def admin_newsletter():
    subscriber_count = NewsletterSubscriber.query.filter_by(status="subscribed").count()
    subscribers = (
        NewsletterSubscriber.query.order_by(NewsletterSubscriber.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "admin_newsletter.html",
        subscriber_count=subscriber_count,
        subscribers=subscribers,
        draft_rows=build_newsletter_draft_rows(),
        newsletter_enabled=bool(app.config.get("NEWSLETTER_ENABLED")),
        newsletter_status=newsletter_status_for_email(current_user().email),
    )


@app.route("/admin/newsletter/drafts", methods=["POST"])
@login_required
@admin_required
def create_newsletter_draft():
    subject = (request.form.get("subject") or "").strip()[:255]
    body_text = (request.form.get("body_text") or "").strip()
    if not subject:
        flash("Please add a newsletter subject.", "error")
        return redirect(url_for("admin_newsletter"))
    if not body_text:
        flash("Please add newsletter body copy.", "error")
        return redirect(url_for("admin_newsletter"))

    actor = current_user()
    draft = NewsletterDraft(subject=subject, body_text=body_text, status="draft", created_by_user_id=actor.id if actor else None)
    db.session.add(draft)
    db.session.commit()
    flash("Newsletter draft saved.", "success")
    return redirect(url_for("admin_newsletter"))


@app.route("/admin/newsletter/drafts/<int:draft_id>/test", methods=["POST"])
@login_required
@admin_required
def send_newsletter_test(draft_id):
    draft = db.session.get(NewsletterDraft, draft_id)
    if not draft:
        abort(404)

    actor = current_user()
    subscription_state = newsletter_status_for_email(actor.email)
    if subscription_state["status"] == "unsubscribed":
        flash("You are unsubscribed from newsletter mail, so a test email was not sent.", "info")
        return redirect(url_for("admin_newsletter"))

    unsubscribe_url = url_for("newsletter_unsubscribe", token=build_unsubscribe_token(actor.email), _external=True)
    text_body, html_body = render_email_bodies(
        "newsletter",
        heading=draft.subject,
        intro="This is a test send from the Planira newsletter workspace.",
        body_html=basic_html_from_text(draft.body_text),
        body_text=draft.body_text,
        unsubscribe_url=unsubscribe_url,
    )
    email_sent = send_email(
        draft.subject,
        [actor.email],
        text_body,
        html_body=html_body,
        category="newsletter",
    )
    if email_sent:
        flash("Newsletter test sent.", "success")
    else:
        flash("Newsletter test could not be sent right now.", "error")
    return redirect(url_for("admin_newsletter"))


@app.route("/staff/streaming")
@login_required
@staff_required
def staff_streaming_control_room():
    active_call = serialize_obs_current_call(get_obs_active_place())
    progress_payload = build_obs_progress_payload()
    health_payload = build_obs_health_payload()
    return render_template(
        "staff_streaming_control_room.html",
        active_call=active_call,
        progress_payload=progress_payload,
        health_payload=health_payload,
    )


@app.route("/admin/moderation")
@login_required
@staff_required
def admin_moderation():
    moderation_items = build_moderation_items()
    return render_template(
        "admin_moderation.html",
        moderation_items=moderation_items,
    )


@app.route("/admin/moderation/<int:comment_id>", methods=["POST"])
@login_required
@staff_required
def moderate_comment(comment_id):
    comment = db.session.get(Comment, comment_id)
    if not comment:
        abort(404)

    action = request.form.get("action", "").strip().lower()
    moderation_reason = request.form.get("moderation_reason", "").strip() or None
    if action not in {"approve", "reject"}:
        flash("Please choose approve or reject.", "error")
        return redirect(url_for("admin_moderation"))

    actor = current_user()
    before_state = {
        "status": comment.status,
        "reviewed_by_user_id": comment.reviewed_by_user_id,
        "reviewed_at": comment.reviewed_at,
        "moderation_reason": comment.moderation_reason,
    }
    comment.status = "approved" if action == "approve" else "rejected"
    comment.reviewed_by_user_id = actor.id if actor else None
    comment.reviewed_at = datetime.now(timezone.utc)
    comment.moderation_reason = moderation_reason

    after_state = {
        "status": comment.status,
        "reviewed_by_user_id": comment.reviewed_by_user_id,
        "reviewed_at": comment.reviewed_at,
        "moderation_reason": comment.moderation_reason,
    }
    log_audit(
        actor_user_id=actor.id if actor else None,
        action=f"comment.{comment.status}",
        entity_type="comment",
        entity_id=comment.id,
        before=before_state,
        after=after_state,
        reason=moderation_reason,
    )
    db.session.commit()
    flash(
        "Comment approved." if comment.status == "approved" else "Comment rejected.",
        "success",
    )
    return redirect(url_for("admin_moderation"))


@app.route("/admin/users")
@login_required
@staff_required
def admin_users():
    query_text = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "all").strip().lower() or "all"
    plan_filter = request.args.get("plan", "all").strip().lower() or "all"
    manual_override_filter = request.args.get("manual_override", "all").strip().lower() or "all"
    access_filter = request.args.get("access", "all").strip().lower() or "all"
    page = request.args.get("page", 1, type=int) or 1
    per_page = 20
    role_filter_options = [
        {"value": "all", "label": "All roles"},
        {"value": "member", "label": "Members"},
        {"value": "staff", "label": "Staff"},
        {"value": "admin", "label": "Admins"},
    ]
    plan_filter_options = [
        {"value": "all", "label": "All plans"},
        {"value": "free", "label": "Free"},
        {"value": "paid", "label": "Paid"},
        {"value": "business", "label": "Business"},
        {"value": "admin", "label": "Admin"},
    ]
    manual_override_filter_options = [
        {"value": "all", "label": "Any override"},
        {"value": "none", "label": "No override"},
        {"value": "active", "label": "Manual active"},
        {"value": "expired", "label": "Manual expired"},
    ]

    query = build_admin_user_query(
        q=query_text,
        role=role_filter,
        plan=plan_filter,
        manual_override=manual_override_filter,
        access=access_filter,
    )
    pagination = query.order_by(User.created_at.desc(), User.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    user_rows = build_user_rows(pagination.items)

    showing_start = ((pagination.page - 1) * pagination.per_page) + 1 if pagination.total else 0
    showing_end = ((pagination.page - 1) * pagination.per_page) + len(user_rows) if pagination.total else 0
    return render_template(
        "admin_users.html",
        query_text=query_text,
        role_filter=role_filter,
        plan_filter=plan_filter,
        manual_override_filter=manual_override_filter,
        role_filter_options=role_filter_options,
        plan_filter_options=plan_filter_options,
        manual_override_filter_options=manual_override_filter_options,
        admin_stats=build_admin_user_stats(),
        user_rows=user_rows,
        pagination=pagination,
        showing_start=showing_start,
        showing_end=showing_end,
    )


@app.route("/admin/users/<int:user_id>/edit")
@login_required
@admin_required
def admin_user_edit(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    query_text = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "all").strip().lower() or "all"
    plan_filter = request.args.get("plan", "all").strip().lower() or "all"
    manual_override_filter = request.args.get("manual_override", "all").strip().lower() or "all"
    return_page = request.args.get("page", 1, type=int) or 1
    manual_entitlement_options = [
        {"value": "paid_consumer", "label": "Paid consumer"},
        {"value": "api_20", "label": "API pack 20"},
        {"value": "api_50", "label": "API pack 50"},
        {"value": "api_100", "label": "API pack 100"},
        {"value": "business", "label": "Business"},
    ]
    selected_row = build_user_rows([user])[0]
    return render_template(
        "admin_user_edit.html",
        query_text=query_text,
        role_filter=role_filter,
        plan_filter=plan_filter,
        manual_override_filter=manual_override_filter,
        return_page=return_page,
        selected_row=selected_row,
        selected_api_keys=build_api_key_rows_with_usage(user),
        selected_api_events=build_recent_api_lookup_activity(user_id=user.id),
        manual_entitlement_options=manual_entitlement_options,
    )


@app.route("/admin/users/<int:user_id>/credits", methods=["POST"])
@login_required
@admin_required
def update_admin_user_credits(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    try:
        credit_delta = parse_int_field(
            request.form.get("credit_delta"),
            "Credit adjustment",
            minimum=-500,
            maximum=500,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(admin_user_return_url(user_id=user.id))

    reason = (request.form.get("reason") or "").strip() or None
    before_state = {"search_credits": user.search_credits or 0}
    updated_credits = max((user.search_credits or 0) + credit_delta, 0)
    user.search_credits = updated_credits
    actor = current_user()
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="user.search_credits.updated",
        entity_type="user",
        entity_id=user.id,
        before=before_state,
        after={"search_credits": updated_credits, "credit_delta": credit_delta},
        reason=reason,
    )
    db.session.commit()
    flash(f"Updated search credits for {user.email}.", "success")
    return redirect(admin_user_return_url(user_id=user.id))


@app.route("/admin/users/<int:user_id>/staff", methods=["POST"])
@login_required
@admin_required
def update_admin_user_staff(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    if is_admin_email(user.email) or user.role == "admin" or user.plan == "admin":
        flash("Admin access is managed outside this screen.", "error")
        return redirect(admin_user_return_url(user_id=user.id))

    action = (request.form.get("action") or "").strip().lower()
    if action not in {"promote", "demote"}:
        flash("Choose a valid staff action.", "error")
        return redirect(admin_user_return_url(user_id=user.id))

    target_role = "staff" if action == "promote" else DEFAULT_MEMBER_ROLE
    before_state = {"role": user.role}
    user.role = target_role
    actor = current_user()
    log_audit(
        actor_user_id=actor.id if actor else None,
        action=f"user.role.{action}",
        entity_type="user",
        entity_id=user.id,
        before=before_state,
        after={"role": target_role},
        reason=(request.form.get("reason") or "").strip() or None,
    )
    db.session.commit()
    flash(
        f"{user.email} is now {'staff' if target_role == 'staff' else 'a member'}.",
        "success",
    )
    return redirect(admin_user_return_url(user_id=user.id))


@app.route("/admin/users/<int:user_id>/manual-entitlement", methods=["POST"])
@login_required
@admin_required
def update_admin_user_manual_entitlement(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    enabled = bool(request.form.get("manual_entitlement_enabled"))
    plan_key = (request.form.get("manual_entitlement_plan") or "").strip().lower()
    note = (request.form.get("manual_entitlement_note") or "").strip() or None

    try:
        expires_at = parse_optional_datetime_local(
            request.form.get("access_override_until"),
            "Expiry date",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(admin_user_return_url(user_id=user.id))

    if enabled and plan_key not in MANUAL_ENTITLEMENT_ALLOWED_PLANS:
        flash("Choose a valid manual access level.", "error")
        return redirect(admin_user_return_url(user_id=user.id))

    before_state = {
        "manual_entitlement_enabled": user.manual_entitlement_enabled,
        "manual_entitlement_plan": user.manual_entitlement_plan,
        "access_override_until": manual_entitlement_expires_at(user),
        "manual_entitlement_note": user.manual_entitlement_note,
    }

    user.manual_entitlement_enabled = enabled
    user.manual_entitlement_plan = plan_key if enabled else None
    user.access_override_until = expires_at if enabled else None
    user.manual_entitlement_note = note

    after_state = {
        "manual_entitlement_enabled": user.manual_entitlement_enabled,
        "manual_entitlement_plan": user.manual_entitlement_plan,
        "access_override_until": user.access_override_until,
        "manual_entitlement_note": user.manual_entitlement_note,
    }

    actor = current_user()
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="user.manual_entitlement.updated",
        entity_type="user",
        entity_id=user.id,
        before=before_state,
        after=after_state,
        reason=note,
    )
    db.session.commit()

    flash(f"Manual access override updated for {user.email}.", "success")
    return redirect(admin_user_return_url(user_id=user.id))


@app.route("/admin/users/<int:user_id>/api-keys")
@login_required
@admin_required
def admin_user_api_keys(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    return jsonify({"user_id": user.id, "api_keys": build_api_key_rows_with_usage(user)})


@app.route("/admin/users/<int:user_id>/api-keys", methods=["POST"])
@login_required
@admin_required
def create_admin_user_api_key(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    label = request.form.get("label", "").strip() or "Primary key"
    scopes = request.form.get("scopes", "").strip() or None
    raw_lookup_limit = request.form.get("monthly_lookup_limit", "").strip()
    raw_lookup_credits = request.form.get("lookup_credits", "").strip()
    monthly_lookup_limit = parse_int_field(raw_lookup_limit, "Monthly lookup limit", minimum=0) if raw_lookup_limit else None
    lookup_credits = parse_int_field(raw_lookup_credits, "Lookup credits", minimum=0, default=0) if raw_lookup_credits else 0

    try:
        api_key, raw_key = create_api_key_for_user(
            user,
            label=label,
            scopes=scopes,
            monthly_lookup_limit=monthly_lookup_limit,
            lookup_credits=lookup_credits,
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_scope", "message": str(exc)}), 400
    actor = current_user()
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="api_key.created",
        entity_type="api_key",
        entity_id=api_key.id,
        after={
            "user_id": user.id,
            "label": api_key.label,
            "scopes": api_key.scopes_json or [],
            "monthly_lookup_limit": api_key.monthly_lookup_limit,
            "lookup_credits": api_key.lookup_credits,
            "is_active": api_key.is_active,
        },
        reason="Staff-created API key",
    )
    db.session.commit()
    return (
        jsonify(
            {
                "user_id": user.id,
                "api_key": serialize_api_key(api_key, raw_key=raw_key),
                "raw_key": raw_key,
                "copy_warning": "Copy this API key now. The raw key is only shown once.",
            }
        ),
        201,
    )


@app.route("/admin/users/<int:user_id>/api-keys/<int:key_id>", methods=["POST"])
@login_required
@admin_required
def update_admin_user_api_key(user_id, key_id):
    api_key = APIKey.query.filter_by(id=key_id, user_id=user_id).first_or_404()
    action = request.form.get("action", "").strip().lower()
    actor = current_user()
    before_state = {
        "label": api_key.label,
        "is_active": api_key.is_active,
        "lookup_credits": api_key.lookup_credits or 0,
    }
    if action == "deactivate":
        api_key.is_active = False
        audit_action = "api_key.revoked"
    elif action == "rename":
        api_key.label = (request.form.get("label", "").strip() or api_key.label or "API key")[:120]
        audit_action = "api_key.renamed"
    else:
        return jsonify({"error": "invalid_action"}), 400
    log_audit(
        actor_user_id=actor.id if actor else None,
        action=audit_action,
        entity_type="api_key",
        entity_id=api_key.id,
        before=before_state,
        after={
            "label": api_key.label,
            "is_active": api_key.is_active,
            "lookup_credits": api_key.lookup_credits or 0,
        },
        reason=(request.form.get("reason") or "").strip() or None,
    )
    db.session.commit()
    return jsonify({"user_id": user_id, "api_key": serialize_api_key(api_key)})


@app.route("/admin/users/<int:user_id>/api-keys/<int:key_id>/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_admin_user_api_key(user_id, key_id):
    api_key = APIKey.query.filter_by(id=key_id, user_id=user_id).first_or_404()
    actor = current_user()
    before_state = {
        "label": api_key.label,
        "is_active": api_key.is_active,
        "lookup_credits": api_key.lookup_credits or 0,
    }
    if api_key.is_active:
        api_key.is_active = False
        log_audit(
            actor_user_id=actor.id if actor else None,
            action="api_key.revoked",
            entity_type="api_key",
            entity_id=api_key.id,
            before=before_state,
            after={
                "label": api_key.label,
                "is_active": api_key.is_active,
                "lookup_credits": api_key.lookup_credits or 0,
            },
            reason=(request.form.get("reason") or "").strip() or None,
        )
        db.session.commit()
        flash(f"Revoked API key for user #{user_id}.", "success")
    else:
        flash("That API key is already revoked.", "info")
    return redirect(admin_user_return_url(user_id=user_id))


@app.route("/admin/users/<int:user_id>/api-keys/<int:key_id>/credits", methods=["POST"])
@login_required
@admin_required
def update_admin_user_api_key_credits(user_id, key_id):
    api_key = APIKey.query.filter_by(id=key_id, user_id=user_id).first_or_404()
    try:
        credit_delta = parse_int_field(
            request.form.get("credit_delta"),
            "API credit adjustment",
            minimum=-500,
            maximum=500,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(admin_user_return_url(user_id=user_id))

    actor = current_user()
    before_state = {"lookup_credits": api_key.lookup_credits or 0}
    updated_credits = max((api_key.lookup_credits or 0) + credit_delta, 0)
    api_key.lookup_credits = updated_credits
    log_audit(
        actor_user_id=actor.id if actor else None,
        action="api_key.lookup_credits.updated",
        entity_type="api_key",
        entity_id=api_key.id,
        before=before_state,
        after={"lookup_credits": updated_credits, "credit_delta": credit_delta},
        reason=(request.form.get("reason") or "").strip() or None,
    )
    db.session.commit()
    flash(f"Updated API lookup credits for user #{user_id}.", "success")
    return redirect(admin_user_return_url(user_id=user_id))


@app.route("/admin/venues")
@login_required
@staff_required
def admin_venues():
    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    postcode = request.args.get("postcode", "").strip()
    status = request.args.get("status", "all").strip() or "all"
    profile = request.args.get("profile", "all").strip() or "all"
    toilet_distance = request.args.get("toilet_distance", "all").strip() or "all"
    confidence = request.args.get("confidence", "all").strip() or "all"
    verified = request.args.get("verified", "all").strip() or "all"
    quality_queue = request.args.get("quality_queue", "all").strip() or "all"
    sort = request.args.get("sort", "priority").strip() or "priority"
    page = parse_int_field(request.args.get("page"), "Page", minimum=1, default=1)
    per_page = 25

    query = build_admin_venue_query(
        q=q,
        town=town,
        postcode=postcode,
        status=status,
        profile=profile,
        toilet_distance=toilet_distance,
        confidence=confidence,
        verified=verified,
        quality_queue=quality_queue,
        sort=sort,
    )
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    venue_rows = build_admin_venue_rows(pagination.items)
    filters = {
        "q": q,
        "town": town,
        "postcode": postcode,
        "status": status,
        "profile": profile,
        "toilet_distance": toilet_distance,
        "confidence": confidence,
        "verified": verified,
        "quality_queue": quality_queue,
        "sort": sort,
    }
    return render_template(
        "admin_venues.html",
        venue_rows=venue_rows,
        pagination=pagination,
        filters=filters,
        quality_queue_options=ADMIN_QUALITY_QUEUE_OPTIONS,
    )


@app.route("/admin/data")
@login_required
@staff_required
def admin_data():
    data_rows = build_data_rows()
    return render_template(
        "admin_data.html",
        data_rows=data_rows,
    )


@app.route("/admin/place/new", methods=["GET", "POST"])
@login_required
@staff_required
def new_place():
    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("Venue name is required.", "error")
            return render_template("new_place.html", form_values=request.form)

        try:
            priority = parse_int_field(request.form.get("priority"), "Priority", minimum=1, maximum=5, default=3)
            website = normalize_optional_url(request.form.get("website"))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("new_place.html", form_values=request.form)

        base_slug = slugify(name + " " + request.form.get("town", ""))
        slug = base_slug
        i = 2
        while Place.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{i}"
            i += 1
        place = Place(
            name=name,
            slug=slug,
            venue_type=request.form.get("venue_type"),
            phone=request.form.get("phone"),
            website=website,
            address1=request.form.get("address1"),
            town=request.form.get("town"),
            county=request.form.get("county"),
            postcode=request.form.get("postcode"),
            priority=priority,
        )
        db.session.add(place)
        db.session.commit()
        get_or_create_profile(place)
        flash("Place added.", "success")
        return redirect(url_for("call_place", place_id=place.id))
    return render_template("new_place.html", form_values={})


@app.route("/admin/place/<int:place_id>/call", methods=["GET", "POST"])
@login_required
@staff_required
def call_place(place_id):
    place = db.session.get(Place, place_id)
    if not place:
        abort(404)

    profile = get_or_create_profile(place)
    premium_checklist = [
        "Confirm accessible toilet status clearly",
        "Capture exact toilet or baby changing location",
        "Record toilet distance once backend support exists",
        "Add a useful public comment a paying user would trust",
        "Set a realistic confidence score",
        "Mark verified when the call genuinely supports it",
    ]
    stream_prompt = f"Mission: verify {place.name} for toilets, step-free access, baby changing, comments, and confidence."
    toilet_distance_backend_supported = True
    verification = verification_status(profile)
    quality = quality_queue_for_profile(profile)
    if request.method == "POST":
        fields = [
            "toilets_available",
            "toilet_location",
            "accessible_toilet",
            "baby_changing",
            "baby_changing_location",
            "step_free_entrance",
            "stairs_inside",
            "lift_available",
            "disabled_parking",
            "sensory_notes",
            "toilet_distance_from_bar",
            "public_comments",
            "internal_notes",
            "source",
        ]
        for field in fields:
            setattr(profile, field, request.form.get(field))

        try:
            profile.confidence_score = parse_int_field(request.form.get("confidence_score"), "Confidence score", minimum=0, maximum=100, default=0)
            profile.toilet_distance_from_bar_m = parse_float_field(
                request.form.get("toilet_distance_from_bar_m"),
                "Toilet distance in metres",
                minimum=0,
                maximum=5000,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "call.html",
                place=place,
                profile=profile,
                verification=verification,
                quality=quality,
                premium_checklist=premium_checklist,
                stream_prompt=stream_prompt,
                toilet_distance_backend_supported=toilet_distance_backend_supported,
                worksheet_form=request.form,
            )

        if request.form.get("mark_verified") == "yes":
            profile.last_verified_at = datetime.now(timezone.utc)
            profile.last_verified_by = session["user"]["email"]
            actor = current_user()
            profile.verified_by_user_id = actor.id if actor else None
            place.status = "verified"
        else:
            place.status = request.form.get("status", place.status)
        log = CallLog(
            place=place,
            user_email=session["user"]["email"],
            result=request.form.get("call_result"),
            contact_name=request.form.get("contact_name"),
            notes=request.form.get("call_notes"),
        )
        db.session.add(log)
        db.session.commit()
        flash("Worksheet saved.", "success")
        return redirect(url_for("dashboard"))
    return render_template(
        "call.html",
        place=place,
        profile=profile,
        verification=verification_status(profile),
        quality=quality_queue_for_profile(profile),
        premium_checklist=premium_checklist,
        stream_prompt=stream_prompt,
        toilet_distance_backend_supported=toilet_distance_backend_supported,
        worksheet_form={},
    )


@app.route("/obs/current-call")
@staff_session_required
def obs_current_call():
    payload = serialize_obs_current_call(get_obs_active_place())
    if request_prefers_json():
        return jsonify(payload)
    return render_template("obs_current_call.html", obs_call=payload)


@app.route("/obs/progress")
@staff_session_required
def obs_progress():
    payload = build_obs_progress_payload()
    if request_prefers_json():
        return jsonify(payload)
    return render_template("obs_progress.html", progress=payload)


@app.route("/obs/health")
@staff_session_required
def obs_health():
    return jsonify(build_obs_health_payload())


@app.route("/auth/login", methods=["GET", "POST"])
def login():
    enforce_rate_limit("login", limit=20, window_seconds=300)
    if request.method == "POST":
        if not os.getenv("GOOGLE_CLIENT_ID"):
            flash("Google OAuth is not configured yet. Add your keys to .env.", "error")
            return redirect(url_for("index"))
        next_url = request.form.get("next") or session.get("next_url") or safe_next_target_for_request()
        session["next_url"] = normalize_next_target(next_url, default_endpoint="search")
        if not protect_with_turnstile("login"):
            return redirect(url_for("login", next=session["next_url"]))
        return google.authorize_redirect(url_for("auth_callback", _external=True))

    next_url = request.args.get("next") or safe_next_target_for_request()
    session["next_url"] = normalize_next_target(next_url, default_endpoint="search")
    return render_template(
        "login.html",
        next_url=session["next_url"],
        turnstile_login=build_turnstile_context("login"),
        enable_turnstile=True,
        seo=build_seo_payload(
            title=f"Continue with Google | {APP_NAME}",
            description="Continue to Planira with Google sign-in and a server-verified anti-abuse check.",
            canonical_url=build_absolute_url("login"),
            robots="noindex, nofollow",
        ),
    )


@app.route("/auth/google/callback")
def auth_callback():
    try:
        token = google.authorize_access_token()
        info = token.get("userinfo") or google.parse_id_token(token)
    except Exception:
        app.logger.exception("Google OAuth callback failed")
        flash("Sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("index"))

    google_sub = (info or {}).get("sub", "").strip()
    email = (info or {}).get("email", "").lower().strip()
    if not oauth_email_is_verified((info or {}).get("email_verified")):
        flash("Google sign-in requires a verified email address.", "error")
        return redirect(url_for("index"))
    if not google_sub:
        flash("Google sign-in did not return a stable account identifier.", "error")
        return redirect(url_for("index"))
    if not email:
        flash("Google sign-in did not return an email address.", "error")
        return redirect(url_for("index"))

    user = User.query.filter_by(google_sub=google_sub).first()
    if not user:
        user = User.query.filter_by(email=email).first()
        if user and user.google_sub and user.google_sub != google_sub:
            app.logger.warning("Blocked Google login for email=%s due to sub mismatch.", email)
            flash("That Google account could not be matched safely. Contact support if you need help.", "error")
            return redirect(url_for("index"))
    created_new_user = False
    if not user:
        user = User(
            email=email,
            google_sub=google_sub,
            name=info.get("name"),
            picture=info.get("picture"),
            role="admin" if is_admin_email(email) else DEFAULT_MEMBER_ROLE,
        )
        db.session.add(user)
        created_new_user = True
    else:
        if not user.google_sub:
            user.google_sub = google_sub
        user.name = info.get("name")
        user.picture = info.get("picture")
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
    if created_new_user:
        send_welcome_email_for_user(user)
    queue_analytics_event(
        "signup_completed",
        {
            "method": "google",
            "account_status": "new" if created_new_user else "existing",
        },
    )
    refresh_session_user(user)
    return redirect_to_next("search")


@app.route("/auth/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))


@app.cli.command("init-db")
def init_db():
    if should_auto_create_schema():
        db.create_all()
        print("SQLite development database created.")
        return

    upgrade()
    print("Database upgraded via Flask-Migrate.")


@app.cli.command("seed")
def seed():
    if should_auto_create_schema():
        db.create_all()
    if Place.query.count() == 0:
        samples = [
            ("The Example Arms", "pub", "01604 000000", "1 High Street", "Northampton", "NN1 1AA"),
            ("Lavender Hotel", "hotel", "01536 000000", "Station Road", "Kettering", "NN16 8AA"),
            ("Soft Indigo Cafe", "cafe", "01933 000000", "Market Square", "Wellingborough", "NN8 1AT"),
        ]
        for name, venue_type, phone, address, town, postcode in samples:
            place = Place(name=name, slug=slugify(name + " " + town), venue_type=venue_type, phone=phone, address1=address, town=town, postcode=postcode)
            db.session.add(place)
            db.session.flush()
            db.session.add(AccessibilityProfile(place=place))
        db.session.commit()
    print("Seed data ready.")


@app.cli.command("check-config")
def check_config():
    missing = missing_config_keys()
    if missing:
        print("Missing required configuration:", ", ".join(sorted(missing)))
        raise SystemExit(1)
    print("Configuration looks good.")


@app.cli.command("ensure-profiles")
def ensure_profiles():
    created = ensure_accessibility_profiles(commit=True)
    print(f"Accessibility profiles created: {created}")
    print(f"Place count: {Place.query.count()}")
    print(f"Accessibility profile count: {AccessibilityProfile.query.count()}")


@app.cli.command("repair-sequences")
def repair_sequences():
    database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if not is_postgresql_database_uri(database_uri):
        print(f"Skipping sequence repair: database '{database_uri}' does not use PostgreSQL.")
        return

    repaired_count = 0

    try:
        for table in db.metadata.sorted_tables:
            primary_key_columns = list(table.primary_key.columns)
            if len(primary_key_columns) != 1:
                continue

            pk_column = primary_key_columns[0]
            if not isinstance(pk_column.type, Integer):
                continue

            sequence_name = get_postgresql_sequence_name(table.name, pk_column.name)
            if not sequence_name:
                continue

            sync_table_primary_key_sequence(table, pk_column)
            repaired_count += 1
            print(f"Repaired sequence for {table.name}.{pk_column.name}")

        db.session.commit()
        print(f"Sequence repair complete. Repaired {repaired_count} table(s).")
    except Exception as exc:
        db.session.rollback()
        print(f"Sequence repair failed: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    with app.app_context():
        if should_auto_create_schema():
            db.create_all()
    debug_enabled = app.config["ENVIRONMENT"] != "production" and env_flag("FLASK_DEBUG", default=False)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug_enabled)
