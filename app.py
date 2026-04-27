import hashlib
import hmac
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
from werkzeug.middleware.proxy_fix import ProxyFix

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
    return {
        "current_user": user,
        "is_admin": is_admin_email(session.get("user", {}).get("email")),
        "csrf_token": csrf_token,
        "account_state": build_account_state(user),
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


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin_email(session.get("user", {}).get("email")):
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
    if not profile:
        return {
            "tone": "moderate",
            "label": "Needs checking",
            "summary": "We still need a reliable decision for this place.",
        }

    accessible = profile.accessible_toilet
    step_free = profile.step_free_entrance
    stairs = profile.stairs_inside
    confidence = profile.confidence_score or 0

    if accessible == "yes" and step_free == "yes" and stairs in {"no", "unknown"} and confidence >= 70:
        return {
            "tone": "easy",
            "label": "Easy access",
            "summary": "Step-free details look strong and confidence is high.",
        }

    if accessible == "no" or step_free == "no" or stairs == "yes":
        return {
            "tone": "difficult",
            "label": "Challenging",
            "summary": "This one might be tricky, so check the details before you go.",
        }

    return {
        "tone": "moderate",
        "label": "Heads up",
        "summary": "There are a few unknowns or partial answers to keep in mind.",
    }


def build_place_card(place):
    profile = place.accessibility
    signal = build_access_signal(profile)
    verified_text = "Not verified yet"
    if profile and profile.last_verified_at:
        verified_text = f"Last verified {profile.last_verified_at.strftime('%d %b %Y')}"

    key_bits = []
    if profile:
        if profile.accessible_toilet == "yes":
            key_bits.append("Accessible toilet")
        elif profile.accessible_toilet == "no":
            key_bits.append("No accessible toilet")

        if profile.step_free_entrance == "yes":
            key_bits.append("Step-free entrance")
        elif profile.step_free_entrance == "no":
            key_bits.append("Steps at entrance")

        if profile.baby_changing == "yes":
            key_bits.append("Baby changing")

    return {
        "place": place,
        "signal": signal,
        "verified_text": verified_text,
        "key_bits": key_bits[:3],
    }


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
    labels = {
        "visitor": "Visitor",
        "free": "Free",
        "paid": "Paid",
        "business": "Business",
        "admin": "Admin",
    }
    return labels.get(plan_name, (plan_name or "free").replace("_", " ").title())


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


def authenticate_api_key(raw_key=None, authorization_header=None, request_obj=None, required_scopes=None, endpoint=None, query=None, status_code=200, commit=True, record_event=True):
    candidate = resolve_api_key_candidate(raw_key=raw_key, authorization_header=authorization_header, request_obj=request_obj)
    if not candidate:
        return {"ok": False, "error": "missing_api_key", "status_code": 401}
    if is_malformed_api_key(candidate):
        return {"ok": False, "error": "malformed_api_key", "status_code": 401}

    candidate_hash = hash_api_key_value(candidate)
    matched_key = None
    inactive_match = None
    for api_key in APIKey.query.all():
        if hmac.compare_digest(api_key.key_hash, candidate_hash):
            if api_key.is_active:
                matched_key = api_key
            else:
                inactive_match = api_key
            break

    if inactive_match and not matched_key:
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
    plan_details = {
        "free": {
            "name": "Free",
            "price": "PS0",
            "description": "Browse verified venue information with the free member experience.",
            "search_limit": 10,
            "credits_label": "Top up with search credits when you need a few extra checks.",
        },
        "paid": {
            "name": "Paid",
            "price": "PS9/mo idea",
            "description": "More search confidence, richer filters, and premium venue detail.",
            "search_limit": 100,
            "credits_label": "Extra credits can sit on top of the monthly allowance for busy periods.",
        },
        "business": {
            "name": "Business",
            "price": "Usage pack",
            "description": "Developer-ready account with API-pack access.",
            "search_limit": 50,
            "credits_label": "Search credits remain available for staff-style manual checks too.",
        },
    }
    limit_context = search_limit_context(user)
    quota_copy = build_quota_copy(user, limit_context)
    comment_count = Comment.query.filter_by(user_email=user.email).count()
    call_count = CallLog.query.filter_by(user_email=user.email).count()
    verified_count = AccessibilityProfile.query.filter_by(last_verified_by=user.email).count()
    saved_venues = 0
    current_plan = plan_details.get(account_state["plan_name"], plan_details["free"])
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
    progress = int((summary["searches_used"] / max(summary["search_limit"], 1)) * 100)
    api_keys = APIKey.query.filter_by(user_id=user.id).order_by(APIKey.created_at.desc(), APIKey.id.desc()).all()
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
            "keys": [serialize_api_key(api_key) for api_key in api_keys],
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


def build_moderation_items(limit=8):
    comments = Comment.query.filter(Comment.status.in_(["pending", "approved", "rejected"])).order_by(Comment.created_at.desc()).limit(limit).all()
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

    rows = []
    for user in users:
        comment_count = comment_counts.get(user.email, 0)
        call_count = call_counts.get(user.email, 0)
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
        rows.append(
            {
                "user": user,
                "contributions": comment_count + call_count,
                "flags": 0,
                "activity": activity["label"],
                "activity_detail": activity["detail"],
                "joined_date": user.created_at.strftime("%d %b %Y") if user.created_at else "Recently",
                "role_label": "Staff" if user.role == "admin" else user.role.replace("_", " ").title(),
            }
        )
    return rows


def build_api_key_rows_for_user(user):
    if not user:
        return []
    keys = APIKey.query.filter_by(user_id=user.id).order_by(APIKey.created_at.desc(), APIKey.id.desc()).all()
    return [serialize_api_key(api_key) for api_key in keys]


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
    query = Place.query.outerjoin(AccessibilityProfile)

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
        rows.append(
            {
                "place": place,
                "status_label": place.status.replace("_", " ").title(),
                "confidence_score": profile.confidence_score if profile and profile.confidence_score is not None else None,
                "toilet_distance_from_bar_m": profile.toilet_distance_from_bar_m if profile else None,
                "last_verified": profile.last_verified_at.strftime("%d %b %Y") if profile and profile.last_verified_at else "Never verified",
                "profile_missing": profile is None,
            }
        )
    return rows


def build_data_rows(limit=20):
    places = Place.query.outerjoin(AccessibilityProfile).order_by(Place.updated_at.desc(), Place.name.asc()).limit(limit).all()
    rows = []
    for row in build_admin_venue_rows(places):
        rows.append(
            {
                "place": row["place"],
                "confidence": row["confidence_score"] if row["confidence_score"] is not None else 0,
                "verification": row["last_verified"] if row["last_verified"] != "Never verified" else "Not verified",
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
        Place.query.outerjoin(AccessibilityProfile)
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
            "description": "Browse the value proposition and get nudged into login when you want real venue data.",
            "features": [
                "Search starts from the home experience",
                "Google login unlocks venue results",
                "Clear dataset and coverage messaging",
            ],
            "cta": None,
            "checkout_enabled": False,
            "key": "free_visitor",
            "role": "visitor",
            "search_limit_copy": "Preview only until you sign in",
            "credits_copy": "Sign in to start tracked search usage and a monthly search allowance.",
        },
        *[
            {
                **plan,
                "checkout_enabled": stripe_checkout_ready(plan),
                "search_limit_copy": (
                    active_quota_copy["search_limit_copy"]
                    if user and current_plan_catalog_key(user) == plan["key"] and active_quota_copy
                    else format_search_limit_copy(
                        100 if plan["key"] == "paid_consumer" else 50 if plan["key"].startswith("api_") else 10
                    )
                ),
                "credits_copy": (
                    "Extra search credits can top up the monthly allowance whenever you need more checks."
                    if plan["key"] == "paid_consumer"
                    else "Best for teams and structured lookup workflows."
                    if plan["key"].startswith("api_")
                    else "A lighter monthly allowance for occasional venue planning, with extra credits if needed."
                ),
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
        "Verification missions as content",
        "Future sponsor slots",
        "Paid data unlocks during streams",
        "Audience voting on where to verify next",
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
        {"label": "Upgrade plan", "href": url_for("plans"), "style": "secondary"},
    ]
    return render_template(
        "account.html",
        current_access=current_access,
        account_state=account_state,
        account_summary=summary,
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
        apply_plan_role_to_user(user_id, target_role)

    return jsonify({"received": True})


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

    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    accessible = request.args.get("accessible", "").strip()
    submitted = request.args.get("submitted") == "1"
    page = parse_int_field(request.args.get("page"), "Page", minimum=1, default=1)
    per_page = 12

    has_filters = any([q, town, accessible])
    should_search = has_filters
    limit_context = search_limit_context(user)
    filters_payload = {
        "query": q or None,
        "town": town or None,
        "accessible": accessible or None,
    }
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
                submitted=submitted,
                has_filters=has_filters,
                should_search=should_search,
                upgrade_features=[
                    "Verified-only results",
                    "Advanced accessibility filters",
                    "Richer venue summaries",
                    "Confidence score and richer verification detail",
                ],
                search_usage=search_usage,
            )
        query = Place.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Place.name.ilike(like), Place.postcode.ilike(like), Place.address1.ilike(like)))
        if town:
            query = query.filter(Place.town.ilike(f"%{town}%"))
        if accessible == "yes":
            query = query.join(AccessibilityProfile).filter(AccessibilityProfile.accessible_toilet == "yes")
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
    upgrade_features = [
        "Verified-only results",
        "Advanced accessibility filters",
        "Richer venue summaries",
        "Confidence score and richer verification detail",
    ]
    search_usage = build_user_summary(user)
    return render_template(
        "search.html",
        results=results,
        result_cards=result_cards,
        pagination=pagination,
        q=q,
        town=town,
        accessible=accessible,
        submitted=submitted,
        has_filters=has_filters,
        should_search=should_search,
        upgrade_features=upgrade_features,
        search_usage=search_usage,
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
    detail_checks = [
        ("Toilets", profile.toilets_available, profile.toilet_location or "Location unknown"),
        ("Accessible toilet", profile.accessible_toilet, None),
        ("Baby changing", profile.baby_changing, profile.baby_changing_location or "Location unknown"),
        ("Step-free entrance", profile.step_free_entrance, None),
        ("Stairs inside", profile.stairs_inside, None),
        ("Lift", profile.lift_available, None),
        ("Disabled parking", profile.disabled_parking, None),
        ("Toilet distance from bar", profile.toilet_distance_from_bar, None),
    ]
    return render_template(
        "place.html",
        place=place,
        profile=profile,
        comments=comments,
        signal=signal,
        detail_checks=detail_checks,
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
@admin_required
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
    quick_actions = [
        {"label": "Venue workspace", "href": url_for("admin_venues")},
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
        quick_actions=quick_actions,
    )


@app.route("/admin/moderation")
@login_required
@admin_required
def admin_moderation():
    moderation_items = build_moderation_items()
    return render_template(
        "admin_moderation.html",
        moderation_items=moderation_items,
    )


@app.route("/admin/moderation/<int:comment_id>", methods=["POST"])
@login_required
@admin_required
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
@admin_required
def admin_users():
    query_text = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1
    selected_user_id = request.args.get("selected", type=int)
    per_page = 10

    query = User.query
    if query_text:
        like = f"%{query_text}%"
        query = query.filter(db.or_(User.email.ilike(like), User.name.ilike(like)))

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
        user_rows=user_rows,
        pagination=pagination,
        selected_row=selected_row,
        selected_user_id=selected_row["user"].id if selected_row else None,
        selected_api_keys=build_api_key_rows_for_user(selected_row["user"]) if selected_row else [],
        showing_start=showing_start,
        showing_end=showing_end,
    )


@app.route("/admin/users/<int:user_id>/api-keys")
@login_required
@staff_required
def admin_user_api_keys(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    return jsonify({"user_id": user.id, "api_keys": build_api_key_rows_for_user(user)})


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
    if action == "deactivate":
        api_key.is_active = False
    elif action == "rename":
        api_key.label = (request.form.get("label", "").strip() or api_key.label or "API key")[:120]
    else:
        return jsonify({"error": "invalid_action"}), 400
    db.session.commit()
    return jsonify({"user_id": user_id, "api_key": serialize_api_key(api_key)})


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
@admin_required
def admin_data():
    data_rows = build_data_rows()
    return render_template(
        "admin_data.html",
        data_rows=data_rows,
    )


@app.route("/admin/place/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_place():
    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("Venue name is required.", "error")
            return render_template("new_place.html")

        try:
            priority = parse_int_field(request.form.get("priority"), "Priority", minimum=1, maximum=5, default=3)
            website = normalize_optional_url(request.form.get("website"))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("new_place.html")

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
    return render_template("new_place.html")


@app.route("/admin/place/<int:place_id>/call", methods=["GET", "POST"])
@login_required
@admin_required
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
    toilet_distance_backend_supported = False
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
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "call.html",
                place=place,
                profile=profile,
                premium_checklist=premium_checklist,
                stream_prompt=stream_prompt,
                toilet_distance_backend_supported=toilet_distance_backend_supported,
            )

        if request.form.get("mark_verified") == "yes":
            profile.last_verified_at = datetime.now(timezone.utc)
            profile.last_verified_by = session["user"]["email"]
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
    )


@app.route("/obs/current-call")
def obs_current_call():
    place = Place.query.filter(Place.status.in_(["calling", "needs_call", "callback"])).order_by(Place.status.desc(), Place.priority.desc(), Place.updated_at.asc()).first()
    return render_template("obs_current_call.html", place=place)


@app.route("/obs/progress")
def obs_progress():
    today = datetime.now(timezone.utc).date()
    verified_today = AccessibilityProfile.query.filter(db.func.date(AccessibilityProfile.last_verified_at) == str(today)).count()
    total_verified = Place.query.filter_by(status="verified").count()
    return render_template("obs_progress.html", verified_today=verified_today, total_verified=total_verified)


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
