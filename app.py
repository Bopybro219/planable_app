import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urljoin, urlparse

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_migrate import Migrate, upgrade
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect
from sqlalchemy.orm import selectinload
from werkzeug.middleware.proxy_fix import ProxyFix

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
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", str(1024 * 1024)))
app.config["PREFERRED_URL_SCHEME"] = "https" if app.config["SESSION_COOKIE_SECURE"] else "http"

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
    name = db.Column(db.String(255))
    picture = db.Column(db.Text)
    role = db.Column(db.String(50), default="user")
    plan = db.Column(db.String(50), default="free", nullable=False)
    monthly_search_limit = db.Column(db.Integer)
    search_credits = db.Column(db.Integer, default=0, nullable=False)
    community_points = db.Column(db.Integer, default=0, nullable=False)
    rank_title = db.Column(db.String(120))
    age_verification_status = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime)


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


def is_production():
    return app.config["ENVIRONMENT"] == "production"


def should_auto_create_schema():
    return (
        app.config["ENVIRONMENT"] in {"development", "testing"}
        and is_sqlite_database_uri(app.config["SQLALCHEMY_DATABASE_URI"])
    )


def missing_config_keys():
    missing = []
    secret_key = app.config["SECRET_KEY"]
    if not secret_key or secret_key == "dev-change-me":
        missing.append("SECRET_KEY")

    if is_production():
        if not os.getenv("GOOGLE_CLIENT_ID", "").strip():
            missing.append("GOOGLE_CLIENT_ID")
        if not os.getenv("GOOGLE_CLIENT_SECRET", "").strip():
            missing.append("GOOGLE_CLIENT_SECRET")
        if not ADMIN_EMAILS:
            missing.append("ADMIN_EMAILS")
    return missing


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
    }


def is_admin_email(email):
    return bool(email and email.lower() in ADMIN_EMAILS)


def is_safe_redirect_target(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in {"http", "https"} and ref_url.netloc == test_url.netloc


def redirect_to_next(default_endpoint="search"):
    target = session.pop("next_url", None)
    if target and is_safe_redirect_target(target):
        return redirect(target)
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


def parse_int_field(raw_value, field_name, minimum=None, maximum=None, default=None):
    value = (raw_value or "").strip()
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
            flash("Please log in with Google to view results.", "info")
            return redirect(url_for("login", next=request.path))
        if not current_user():
            session.clear()
            flash("Your session expired, so please sign in again.", "info")
            return redirect(url_for("login", next=request.path))
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

    for place in missing_places:
        db.session.add(AccessibilityProfile(place_id=place.id))

    if commit and missing_places:
        db.session.commit()
    elif not commit:
        db.session.flush()

    return len(missing_places)


def build_access_signal(profile):
    return present_access_signal(profile)


def build_place_card(place):
    return present_place_card(place)


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


def apply_plan_role_to_user(user_id, target_role):
    if not user_id or not target_role:
        return False

    user = db.session.get(User, int(user_id))
    if not user:
        return False

    if user.role != target_role:
        user.role = target_role
        if target_role == "paid_consumer":
            user.plan = "paid"
        elif target_role == "api_buyer":
            user.plan = "business"
        db.session.commit()
    return True


def is_staff_user(user):
    if not user:
        return False
    return user.role in {"admin", "staff"} or user.plan == "admin" or is_admin_email(user.email)


def current_role_key(user):
    if not user:
        return "free_visitor"
    if user.plan == "paid":
        return "paid_consumer"
    if user.plan == "business":
        return "api_buyer"
    if is_staff_user(user):
        return "paid_consumer"
    if user.role == "paid_consumer":
        return "paid_consumer"
    if user.role == "api_buyer":
        return "api_buyer"
    return "logged_in_free"


def normalize_plan_name(user):
    if not user:
        return "visitor"
    if user.plan:
        return user.plan
    if user.role == "paid_consumer":
        return "paid"
    if user.role == "api_buyer":
        return "business"
    if is_staff_user(user):
        return "admin"
    return "free"


def humanize_plan_name(plan_name):
    return present_plan_name(plan_name)


def normalize_billing_plan_name(user):
    if not user:
        return "free"
    if user.plan in {"paid", "business"}:
        return user.plan
    if user.role == "paid_consumer":
        return "paid"
    if user.role == "api_buyer":
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
    plan_name = normalize_billing_plan_name(user)
    if plan_name == "paid":
        return "paid_consumer"
    if plan_name == "business":
        return "api_buyer"
    return "logged_in_free"


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


def normalize_scope_list(scopes):
    if scopes is None:
        return None
    if isinstance(scopes, str):
        values = [item.strip() for item in scopes.split(",")]
    else:
        values = [str(item).strip() for item in scopes]
    cleaned = sorted({value for value in values if value})
    return cleaned or None


def generate_api_key_value(prefix=None):
    return f"{prefix or current_api_key_prefix()}{secrets.token_urlsafe(32)}"


def create_api_key_for_user(user, label=None, scopes=None, monthly_lookup_limit=None, lookup_credits=None, prefix=None):
    normalized_label = (label or "Primary key").strip() or "Primary key"
    normalized_scopes = normalize_scope_list(scopes)
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


def api_key_limit_context(api_key):
    monthly_limit = api_key.monthly_lookup_limit
    if monthly_limit is None:
        monthly_limit = default_api_lookup_limit_for_user(api_key.user)
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
    candidate = resolve_api_key_candidate(raw_key=raw_key, authorization_header=authorization_header, request_obj=request_obj)
    if not candidate:
        return {"ok": False, "error": "missing_api_key", "status_code": 401}
    if is_malformed_api_key(candidate):
        return {"ok": False, "error": "malformed_api_key", "status_code": 401}

    candidate_hash = hash_api_key_value(candidate)
    matched_key = APIKey.query.filter_by(key_hash=candidate_hash).first()
    if matched_key and not matched_key.is_active:
        return {"ok": False, "error": "inactive_api_key", "status_code": 403}
    if not matched_key:
        return {"ok": False, "error": "invalid_api_key", "status_code": 401}
    if not api_key_has_required_scopes(matched_key, required_scopes=required_scopes):
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
        limit_context = api_key_limit_context(api_key)
        rows.append(
            {
                **serialize_api_key(api_key),
                "lookups_used": monthly_counts.get(api_key.id, 0),
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


def serialize_place_for_api(place):
    profile = getattr(place, "accessibility", None)
    signal = build_access_signal(profile)
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
    return [
        {"label": "Dashboard", "endpoint": "dashboard"},
        {"label": "Streaming", "endpoint": "staff_streaming_control_room"},
        {"label": "Venues", "endpoint": "admin_venues"},
        {"label": "Moderation", "endpoint": "admin_moderation"},
        {"label": "Users", "endpoint": "admin_users"},
        {"label": "Legacy data view", "endpoint": "admin_data"},
        {"label": "Add venue", "endpoint": "new_place"},
    ]


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
            "avatar_initials": "".join(part[:1] for part in (user.name or user.email).split()[:2]).upper() or "PA",
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


def build_admin_user_query(*, q="", access="all"):
    query = User.query

    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like), User.name.ilike(like)))

    normalized_access = (access or "all").strip().lower() or "all"
    admin_email_list = sorted(ADMIN_EMAILS)
    admin_email_filter = db.func.lower(User.email).in_(admin_email_list) if admin_email_list else db.false()

    if normalized_access == "member":
        query = query.filter(
            ~db.or_(
                User.role.in_(["admin", "staff"]),
                User.plan == "admin",
                admin_email_filter,
            )
        )
    elif normalized_access == "staff":
        query = query.filter(User.role == "staff")
    elif normalized_access == "admin":
        query = query.filter(db.or_(User.role == "admin", User.plan == "admin", admin_email_filter))
    elif normalized_access == "paid":
        query = query.filter(User.plan == "paid")
    elif normalized_access == "business":
        query = query.filter(User.plan == "business")

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


SEARCH_BINARY_FILTER_VALUES = {"", "yes", "no"}
SEARCH_CONFIDENCE_FILTERS = {"", "70", "85", "95"}
SEARCH_VERIFICATION_FILTERS = {"", "recent", "needs_verification"}
SEARCH_TOILET_DISTANCE_FILTERS = {"", "recorded", "short", "unknown"}
SEARCH_TEXT_PRESENCE_FILTERS = {"", "has", "missing"}
SEARCH_PUBLIC_SOURCE_FILTERS = ("phone_verified", "owner_verified", "user_submitted", "not_verified")


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
        "submitted": "1" if submitted else None,
    }


def meaningful_text_distance_filter():
    return db.func.length(db.func.trim(db.func.coalesce(AccessibilityProfile.toilet_distance_from_bar, ""))) > 0


def meaningful_profile_text_filter(column):
    return db.func.length(db.func.trim(db.func.coalesce(column, ""))) > 0


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

    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
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

    sort_map = {
        "priority": [Place.priority.desc(), Place.updated_at.desc(), Place.name.asc()],
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
        rows.append(
            {
                "place": place,
                "status_label": humanize_label(place.status, PLACE_STATUS_LABELS),
                "confidence_score": profile.confidence_score if profile and profile.confidence_score is not None else None,
                "toilet_distance_from_bar_m": profile.toilet_distance_from_bar_m if profile else None,
                "last_verified": signal["verification"]["short_label"],
                "verification_label": signal["verification"]["label"],
                "signal_label": signal["label"],
                "profile_missing": profile is None,
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
                "confidence": row["confidence_score"] if row["confidence_score"] is not None else 0,
                "verification": row["verification_label"],
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
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
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
        }
    )


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
    )


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/data-rights")
def data_rights():
    return render_template("data_rights.html")


@app.route("/cookies")
def cookies():
    return render_template("cookies.html")


@app.route("/plans")
def plans():
    user = current_user()
    account_state = build_account_state(user)
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
    )


@app.route("/account/api-keys")
@login_required
def account_api_keys():
    user = current_user()
    return jsonify({"api_keys": build_api_key_rows_for_user(user)})


@app.route("/account/api-keys", methods=["POST"])
@login_required
def create_account_api_key():
    user = current_user()
    label = request.form.get("label", "").strip() or "Primary key"
    scopes = request.form.get("scopes", "").strip() or None
    api_key, raw_key = create_api_key_for_user(user, label=label, scopes=scopes)
    db.session.commit()
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
    developer_summary = build_developer_summary(user) if user else None
    example_prefix = current_api_key_prefix()
    example_response = {
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
                "last_verified_at": "2026-04-28T10:30:00+00:00",
            }
        ],
    }
    return render_template(
        "developers.html",
        developer_summary=developer_summary,
        raw_api_key=None,
        raw_api_key_label=None,
        example_api_key=f"{example_prefix}replace_me",
        api_search_url=url_for("api_places_search", _external=True),
        example_response=example_response,
    )


@app.route("/developers/api-keys", methods=["POST"])
@login_required
def create_developer_api_key():
    user = current_user()
    label = request.form.get("label", "").strip() or "Primary key"
    api_key, raw_key = create_api_key_for_user(user, label=label)
    db.session.commit()
    flash("API key created. Copy it now because it will not be shown again.", "success")
    developer_summary = build_developer_summary(user)
    example_prefix = current_api_key_prefix()
    example_response = {
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
                "last_verified_at": "2026-04-28T10:30:00+00:00",
            }
        ],
    }
    return render_template(
        "developers.html",
        developer_summary=developer_summary,
        raw_api_key=raw_key,
        raw_api_key_label=label,
        example_api_key=f"{example_prefix}replace_me",
        api_search_url=url_for("api_places_search", _external=True),
        example_response=example_response,
    )


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

    try:
        session_checkout = stripe.checkout.Session.create(
            mode=plan["checkout_mode"],
            line_items=[{"price": plan["price_id"], "quantity": 1}],
            success_url=url_for("billing_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("billing_cancel", _external=True),
            customer_email=user.email,
            client_reference_id=str(user.id),
            metadata={
                "plan_key": plan["key"],
                "user_id": str(user.id),
                "user_email": user.email,
                "target_role": plan["role"],
            },
        )
    except Exception as exc:  # pragma: no cover - third-party API path
        app.logger.exception("Stripe checkout creation failed")
        flash(f"Stripe could not start checkout: {exc}", "error")
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
    except Exception as exc:  # pragma: no cover - third-party API path
        app.logger.exception("Stripe checkout confirmation failed")
        flash(f"Stripe could not confirm the checkout session: {exc}", "error")
        return redirect(url_for("plans"))

    plan_key = (checkout_session.metadata or {}).get("plan_key")
    target_role = (checkout_session.metadata or {}).get("target_role")
    plan = get_plan(plan_key)

    if not plan:
        flash("Payment completed, but the selected plan could not be matched.", "warn")
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

    if apply_plan_role_to_user(user.id, target_role):
        db.session.refresh(user)
        refresh_session_user(user)

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

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        metadata = checkout_session.get("metadata", {}) or {}
        user_id = metadata.get("user_id")
        target_role = metadata.get("target_role")
        # TODO: API pack fulfilment still needs a durable credit-assignment flow.
        # For early testing, API lookup credits remain manually assignable per key.
        apply_plan_role_to_user(user_id, target_role)

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
        required_scopes=None,
        endpoint="/api/v1/places/search",
        query=f"q={q}&town={town}&postcode={postcode}",
        apply_usage=False,
        record_event=False,
        commit=False,
    )
    if not auth_result["ok"]:
        error_map = {
            "missing_api_key": ("missing_api_key", "Send an API key using the Authorization Bearer header.", 401),
            "malformed_api_key": ("invalid_api_key", "The API key format is not valid for this environment.", 401),
            "invalid_api_key": ("invalid_api_key", "The API key could not be verified.", 401),
            "inactive_api_key": ("revoked_api_key", "This API key is no longer active.", 403),
            "insufficient_scope": ("invalid_api_key", "This API key does not have access to this endpoint.", 403),
            "monthly_lookup_limit_reached": ("limit_reached", "This API key has used its available lookup allowance.", 429),
        }
        error_key, message, status_code = error_map.get(
            auth_result["error"],
            ("invalid_api_key", "The API request could not be authorized.", auth_result.get("status_code", 401)),
        )
        return api_error_response(error_key, message, status_code)

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


@app.route("/search")
def search():
    if not session.get("user"):
        flash("Search results are available after Google login.", "info")
        return redirect(url_for("login", next=request.full_path))

    user = current_user()
    if not user:
        session.clear()
        flash("Your session expired, so please sign in again.", "info")
        return redirect(url_for("login", next=request.full_path))

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
    submitted = request.args.get("submitted") == "1"
    try:
        page = parse_int_field(request.args.get("page"), "Page", minimum=1, default=1)
    except ValueError:
        page = 1
    per_page = 12

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
        ]
    )
    should_search = has_filters
    limit_context = search_limit_context(user)
    filters_payload = build_search_filter_payload(filters)
    active_filter_chips = build_search_active_filters(filters)
    pagination_args = build_search_pagination_args(filters, submitted)
    limit_message = None

    pagination = None
    results = []
    result_cards = []
    if should_search:
        if submitted and has_filters and limit_context["limit_reached"]:
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
            )
        query = Place.query.options(selectinload(Place.accessibility)).outerjoin(AccessibilityProfile)
        if q:
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
        if submitted and has_filters:
            consume_search_credit_if_needed(user, limit_context)
            track_search_event(
                user,
                q,
                town,
                accessible,
                filters_json=filters_payload,
                result_count=pagination.total,
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
    )


@app.route("/place/<slug>")
@login_required
def place_detail(slug):
    place = Place.query.filter_by(slug=slug).first_or_404()
    profile = get_or_create_profile(place)
    user = current_user()
    comment_query = Comment.query.filter_by(place_id=place.id, is_public=True)
    if not is_staff_user(user):
        comment_query = comment_query.filter_by(status="approved")
    comments = comment_query.order_by(Comment.created_at.desc()).all()
    signal = build_access_signal(profile)
    return render_template(
        "place.html",
        place=place,
        profile=profile,
        comments=comments,
        signal=signal,
    )


@app.route("/place/<slug>/comment", methods=["POST"])
@login_required
def add_comment(slug):
    place = Place.query.filter_by(slug=slug).first_or_404()
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
        {"label": "Manage users", "href": url_for("admin_users")},
    ]
    return render_template(
        "dashboard.html",
        stats=stats,
        next_places=next_places,
        mission_pagination=mission_pagination,
        user_stats=user_stats,
        monetisation_stats=monetisation_stats,
        plan_highlights=plan_highlights,
        community_ranks=community_ranks,
        recent_activity=recent_activity,
        recent_search_activity=recent_search_activity,
        recent_audit_entries=recent_audit_entries,
        api_operations=api_operations,
        quick_actions=quick_actions,
    )


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
    access_filter = request.args.get("access", "all").strip().lower() or "all"
    page = request.args.get("page", 1, type=int) or 1
    selected_user_id = request.args.get("selected", type=int)
    per_page = 10
    access_filter_options = [
        {"value": "all", "label": "All users"},
        {"value": "member", "label": "Members"},
        {"value": "staff", "label": "Staff"},
        {"value": "admin", "label": "Admins"},
        {"value": "paid", "label": "Paid plan"},
        {"value": "business", "label": "Business plan"},
    ]

    query = build_admin_user_query(q=query_text, access=access_filter)
    pagination = query.order_by(User.created_at.desc(), User.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    user_rows = build_user_rows(pagination.items)
    selected_row = next((row for row in user_rows if row["user"].id == selected_user_id), None)
    if not selected_row and user_rows:
        selected_row = user_rows[0]

    showing_start = ((pagination.page - 1) * pagination.per_page) + 1 if pagination.total else 0
    showing_end = ((pagination.page - 1) * pagination.per_page) + len(user_rows) if pagination.total else 0
    return render_template(
        "admin_users.html",
        query_text=query_text,
        access_filter=access_filter,
        access_filter_options=access_filter_options,
        active_access_label=next((option["label"] for option in access_filter_options if option["value"] == access_filter), "All users"),
        admin_stats=build_admin_user_stats(),
        user_rows=user_rows,
        pagination=pagination,
        selected_row=selected_row,
        selected_user_id=selected_row["user"].id if selected_row else None,
        selected_api_keys=build_api_key_rows_with_usage(selected_row["user"]) if selected_row else [],
        selected_api_events=build_recent_api_lookup_activity(user_id=selected_row["user"].id) if selected_row else [],
        showing_start=showing_start,
        showing_end=showing_end,
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
        return redirect(
            url_for(
                "admin_users",
                q=request.form.get("return_q") or None,
                access=request.form.get("return_access") or "all",
                page=request.form.get("return_page", type=int) or 1,
                selected=user.id,
            )
        )

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
    return redirect(
        url_for(
            "admin_users",
            q=request.form.get("return_q") or None,
            access=request.form.get("return_access") or "all",
            page=request.form.get("return_page", type=int) or 1,
            selected=user.id,
        )
    )


@app.route("/admin/users/<int:user_id>/staff", methods=["POST"])
@login_required
@admin_required
def update_admin_user_staff(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    if is_admin_email(user.email) or user.role == "admin" or user.plan == "admin":
        flash("Admin access is managed outside this screen.", "error")
        return redirect(
            url_for(
                "admin_users",
                q=request.form.get("return_q") or None,
                access=request.form.get("return_access") or "all",
                page=request.form.get("return_page", type=int) or 1,
                selected=user.id,
            )
        )

    action = (request.form.get("action") or "").strip().lower()
    if action not in {"promote", "demote"}:
        flash("Choose a valid staff action.", "error")
        return redirect(url_for("admin_users", selected=user.id))

    target_role = "staff" if action == "promote" else "user"
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
    return redirect(
        url_for(
            "admin_users",
            q=request.form.get("return_q") or None,
            access=request.form.get("return_access") or "all",
            page=request.form.get("return_page", type=int) or 1,
            selected=user.id,
        )
    )


@app.route("/admin/users/<int:user_id>/api-keys")
@login_required
@staff_required
def admin_user_api_keys(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    return jsonify({"user_id": user.id, "api_keys": build_api_key_rows_with_usage(user)})


@app.route("/admin/users/<int:user_id>/api-keys", methods=["POST"])
@login_required
@staff_required
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

    api_key, raw_key = create_api_key_for_user(
        user,
        label=label,
        scopes=scopes,
        monthly_lookup_limit=monthly_lookup_limit,
        lookup_credits=lookup_credits,
    )
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
@staff_required
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
@staff_required
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
    return redirect(
        url_for(
            "admin_users",
            q=request.form.get("return_q") or None,
            access=request.form.get("return_access") or "all",
            page=request.form.get("return_page", type=int) or 1,
            selected=user_id,
        )
    )


@app.route("/admin/users/<int:user_id>/api-keys/<int:key_id>/credits", methods=["POST"])
@login_required
@staff_required
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
        return redirect(
            url_for(
                "admin_users",
                q=request.form.get("return_q") or None,
                access=request.form.get("return_access") or "all",
                page=request.form.get("return_page", type=int) or 1,
                selected=user_id,
            )
        )

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
    return redirect(
        url_for(
            "admin_users",
            q=request.form.get("return_q") or None,
            access=request.form.get("return_access") or "all",
            page=request.form.get("return_page", type=int) or 1,
            selected=user_id,
        )
    )


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
        "sort": sort,
    }
    return render_template(
        "admin_venues.html",
        venue_rows=venue_rows,
        pagination=pagination,
        filters=filters,
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


@app.route("/auth/login")
def login():
    if not os.getenv("GOOGLE_CLIENT_ID"):
        flash("Google OAuth is not configured yet. Add your keys to .env.", "error")
        return redirect(url_for("index"))
    next_url = request.args.get("next") or url_for("search")
    session["next_url"] = next_url if is_safe_redirect_target(next_url) else url_for("search")
    return google.authorize_redirect(url_for("auth_callback", _external=True))


@app.route("/auth/google/callback")
def auth_callback():
    try:
        token = google.authorize_access_token()
        info = token.get("userinfo") or google.parse_id_token(token)
    except Exception as exc:
        app.logger.exception("Google OAuth callback failed")
        flash(f"Google sign-in could not be completed: {exc}", "error")
        return redirect(url_for("index"))

    email = (info or {}).get("email", "").lower().strip()
    if not email:
        flash("Google sign-in did not return an email address.", "error")
        return redirect(url_for("index"))

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            name=info.get("name"),
            picture=info.get("picture"),
            role="admin" if is_admin_email(email) else "member",
        )
        db.session.add(user)
    else:
        user.name = info.get("name")
        user.picture = info.get("picture")
    db.session.commit()
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


if __name__ == "__main__":
    with app.app_context():
        if should_auto_create_schema():
            db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=app.config["ENVIRONMENT"] == "development")
