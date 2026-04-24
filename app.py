import os
from datetime import datetime, timezone
from functools import wraps
from slugify_fallback import slugify
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///planable.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

db = SQLAlchemy(app)
oauth = OAuth(app)

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
    role = db.Column(db.String(50), default="member")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

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
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


def current_user():
    email = session.get("user", {}).get("email")
    return User.query.filter_by(email=email).first() if email else None

@app.context_processor
def inject_user():
    return {"current_user": current_user(), "is_admin": is_admin_email(session.get("user", {}).get("email"))}

def is_admin_email(email):
    return bool(email and email.lower() in ADMIN_EMAILS)

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("Please log in with Google to view results.", "info")
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

def get_or_create_profile(place):
    if not place.accessibility:
        db.session.add(AccessibilityProfile(place=place))
        db.session.commit()
    return place.accessibility

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    return render_template("index.html", q=q, town=town)

@app.route("/search")
def search():
    if not session.get("user"):
        flash("Search results are available after Google login.", "info")
        return redirect(url_for("login", next=request.full_path))
    q = request.args.get("q", "").strip()
    town = request.args.get("town", "").strip()
    accessible = request.args.get("accessible")
    query = Place.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Place.name.ilike(like), Place.postcode.ilike(like), Place.address1.ilike(like)))
    if town:
        query = query.filter(Place.town.ilike(f"%{town}%"))
    if accessible == "yes":
        query = query.join(AccessibilityProfile).filter(AccessibilityProfile.accessible_toilet == "yes")
    results = query.order_by(Place.name.asc()).limit(100).all()
    return render_template("search.html", results=results, q=q, town=town, accessible=accessible)

@app.route("/place/<slug>")
@login_required
def place_detail(slug):
    place = Place.query.filter_by(slug=slug).first_or_404()
    profile = get_or_create_profile(place)
    comments = Comment.query.filter_by(place_id=place.id, is_public=True).order_by(Comment.created_at.desc()).all()
    return render_template("place.html", place=place, profile=profile, comments=comments)

@app.route("/place/<slug>/comment", methods=["POST"])
@login_required
def add_comment(slug):
    place = Place.query.filter_by(slug=slug).first_or_404()
    body = request.form.get("body", "").strip()
    if body:
        db.session.add(Comment(place=place, user_email=session["user"]["email"], body=body, is_public=True))
        db.session.commit()
        flash("Comment added.", "success")
    return redirect(url_for("place_detail", slug=slug))

@app.route("/dashboard")
@login_required
@admin_required
def dashboard():
    stats = {
        "total": Place.query.count(),
        "needs_call": Place.query.filter_by(status="needs_call").count(),
        "verified": Place.query.filter_by(status="verified").count(),
        "callback": Place.query.filter_by(status="callback").count(),
    }
    next_places = Place.query.filter(Place.status.in_(["needs_call", "callback"])).order_by(Place.priority.desc(), Place.updated_at.asc()).limit(20).all()
    return render_template("dashboard.html", stats=stats, next_places=next_places)

@app.route("/admin/place/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_place():
    if request.method == "POST":
        name = request.form["name"].strip()
        base_slug = slugify(name + " " + request.form.get("town", ""))
        slug = base_slug
        i = 2
        while Place.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{i}"
            i += 1
        place = Place(
            name=name, slug=slug, venue_type=request.form.get("venue_type"), phone=request.form.get("phone"),
            website=request.form.get("website"), address1=request.form.get("address1"), town=request.form.get("town"),
            county=request.form.get("county"), postcode=request.form.get("postcode"), priority=int(request.form.get("priority", 3)),
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
    place = Place.query.get_or_404(place_id)
    profile = get_or_create_profile(place)
    if request.method == "POST":
        fields = ["toilets_available","toilet_location","accessible_toilet","baby_changing","baby_changing_location","step_free_entrance","stairs_inside","lift_available","disabled_parking","sensory_notes","public_comments","internal_notes","source"]
        for f in fields:
            setattr(profile, f, request.form.get(f))
        profile.confidence_score = int(request.form.get("confidence_score", 0))
        if request.form.get("mark_verified") == "yes":
            profile.last_verified_at = datetime.now(timezone.utc)
            profile.last_verified_by = session["user"]["email"]
            place.status = "verified"
        else:
            place.status = request.form.get("status", place.status)
        log = CallLog(place=place, user_email=session["user"]["email"], result=request.form.get("call_result"), contact_name=request.form.get("contact_name"), notes=request.form.get("call_notes"))
        db.session.add(log)
        db.session.commit()
        flash("Worksheet saved.", "success")
        return redirect(url_for("dashboard"))
    return render_template("call.html", place=place, profile=profile)

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
    session["next_url"] = request.args.get("next") or url_for("search")
    return google.authorize_redirect(url_for("auth_callback", _external=True))

@app.route("/auth/google/callback")
def auth_callback():
    token = google.authorize_access_token()
    info = token.get("userinfo") or google.parse_id_token(token)
    email = info["email"].lower()
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=info.get("name"), picture=info.get("picture"), role="admin" if is_admin_email(email) else "member")
        db.session.add(user)
    else:
        user.name = info.get("name")
        user.picture = info.get("picture")
    db.session.commit()
    session["user"] = {"email": email, "name": user.name, "picture": user.picture}
    return redirect(session.pop("next_url", url_for("search")))

@app.route("/auth/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))

@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database created.")

@app.cli.command("seed")
def seed():
    db.create_all()
    if Place.query.count() == 0:
        samples = [
            ("The Example Arms", "pub", "01604 000000", "1 High Street", "Northampton", "NN1 1AA"),
            ("Lavender Hotel", "hotel", "01536 000000", "Station Road", "Kettering", "NN16 8AA"),
            ("Soft Indigo Cafe", "cafe", "01933 000000", "Market Square", "Wellingborough", "NN8 1AT"),
        ]
        for name, vt, phone, addr, town, pc in samples:
            p = Place(name=name, slug=slugify(name + " " + town), venue_type=vt, phone=phone, address1=addr, town=town, postcode=pc)
            db.session.add(p)
            db.session.flush()
            db.session.add(AccessibilityProfile(place=p))
        db.session.commit()
    print("Seed data ready.")

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
