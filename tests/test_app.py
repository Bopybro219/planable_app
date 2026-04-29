import io
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module
import venue_import
from app import ADMIN_EMAILS, APILookupEvent, APIKey, AccessibilityProfile, AuditLog, Comment, Place, PlaceImage, SearchEvent, User, app, db


def rebind_sqlalchemy_for_current_config():
    extension = app.extensions["sqlalchemy"]
    engines = extension._app_engines.setdefault(app, {})
    for engine in engines.values():
        engine.dispose()
    engines.clear()

    basic_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    basic_engine_options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
    echo = app.config.get("SQLALCHEMY_ECHO", False)
    engine_options = {}

    if basic_uri is not None:
        basic_engine_options["url"] = basic_uri
        engine_options[None] = basic_engine_options

    for key, options in engine_options.items():
        extension._make_metadata(key)
        options.setdefault("echo", echo)
        options.setdefault("echo_pool", echo)
        extension._apply_driver_defaults(options, app)
        engines[key] = extension._make_engine(key, options, app)


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self._original_uri = app.config["SQLALCHEMY_DATABASE_URI"]
        self._original_testing = app.config.get("TESTING", False)
        self._original_environment = app.config.get("ENVIRONMENT")
        self._original_schema_ready = app.config.get("_DB_SCHEMA_READY", False)
        self._original_upload_dir = app.config.get("PROFILE_IMAGE_UPLOAD_DIR")
        self._original_place_upload_dir = app.config.get("PLACE_IMAGE_UPLOAD_DIR")
        self._original_max_content_length = app.config.get("MAX_CONTENT_LENGTH")
        self._original_profile_image_max_bytes = app.config.get("PROFILE_IMAGE_MAX_BYTES")
        self._original_place_image_max_bytes = app.config.get("PLACE_IMAGE_MAX_BYTES")
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self._db_fd)
        self._upload_dir = tempfile.mkdtemp(prefix="planira-profile-images-")
        self._place_upload_dir = tempfile.mkdtemp(prefix="planira-place-images-")

        app.config["TESTING"] = True
        app.config["ENVIRONMENT"] = "testing"
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{self._db_path}"
        app.config["_DB_SCHEMA_READY"] = False
        app.config["PROFILE_IMAGE_UPLOAD_DIR"] = self._upload_dir
        app.config["PLACE_IMAGE_UPLOAD_DIR"] = self._place_upload_dir
        app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
        app.config["PROFILE_IMAGE_MAX_BYTES"] = 2 * 1024 * 1024
        app.config["PLACE_IMAGE_MAX_BYTES"] = 2 * 1024 * 1024
        self.client = app.test_client()

        with app.app_context():
            rebind_sqlalchemy_for_current_config()
            db.session.remove()
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

        app.config["SQLALCHEMY_DATABASE_URI"] = self._original_uri
        app.config["TESTING"] = self._original_testing
        app.config["ENVIRONMENT"] = self._original_environment
        app.config["_DB_SCHEMA_READY"] = self._original_schema_ready
        app.config["PROFILE_IMAGE_UPLOAD_DIR"] = self._original_upload_dir
        app.config["PLACE_IMAGE_UPLOAD_DIR"] = self._original_place_upload_dir
        app.config["MAX_CONTENT_LENGTH"] = self._original_max_content_length
        app.config["PROFILE_IMAGE_MAX_BYTES"] = self._original_profile_image_max_bytes
        app.config["PLACE_IMAGE_MAX_BYTES"] = self._original_place_image_max_bytes

        with app.app_context():
            rebind_sqlalchemy_for_current_config()

        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        shutil.rmtree(self._upload_dir, ignore_errors=True)
        shutil.rmtree(self._place_upload_dir, ignore_errors=True)

    def login_session(self, email, name, picture=""):
        with self.client.session_transaction() as session:
            session["user"] = {"email": email, "name": name, "picture": picture}
            session["_csrf_token"] = "token123"

    def png_bytes(self):
        return (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
            b"\x90wS\xde"
            b"\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
            b"\x18\xdd\x8d\xb1"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def gif_bytes(self):
        return (
            b"GIF89a"
            b"\x01\x00\x01\x00"
            b"\x80\x00\x00"
            b"\x00\x00\x00\xff\xff\xff"
            b"!\xf9\x04\x01\x00\x00\x00\x00"
            b",\x00\x00\x00\x00\x01\x00\x01\x00\x00"
            b"\x02\x02D\x01\x00;"
        )

    def test_health_endpoint(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_legal_info_routes_render(self):
        for path, snippet in [
            ("/privacy", b"How Planira handles your data"),
            ("/terms", b"Using Planira fairly"),
            ("/data-rights", b"Your data choices"),
            ("/cookies", b"Session and sign-in basics"),
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(snippet, response.data)

    def test_sqlite_fallback_still_works_when_database_url_missing(self):
        self.assertEqual(app_module.normalize_database_url(None), "sqlite:///planable.db")

    def test_postgresql_database_url_is_normalized(self):
        normalized = app_module.normalize_database_url("postgres://user:pass@localhost:5432/planira")

        self.assertEqual(normalized, "postgresql://user:pass@localhost:5432/planira")

    def test_secret_key_validation_rejects_placeholder_secret(self):
        self.assertIn("placeholder", app_module.secret_key_issues("change-me"))
        self.assertIn("too_short", app_module.secret_key_issues("change-me"))

    def test_flask_migrate_is_initialized(self):
        self.assertIsNotNone(app_module.migrate)
        self.assertIs(app.extensions["migrate"].db, db)

    def test_production_postgres_mode_does_not_auto_create_tables(self):
        app.config["ENVIRONMENT"] = "production"
        app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql+psycopg2://planira_user:password@localhost:5432/planira"
        app.config["_DB_SCHEMA_READY"] = False

        with patch.object(app_module.db, "create_all", side_effect=AssertionError("create_all should not run")):
            with app.test_request_context("/health"):
                result = app_module.ensure_database_ready()

        self.assertIsNone(result)
        self.assertTrue(app.config["_DB_SCHEMA_READY"])

    def test_production_missing_config_reports_hardening_requirements(self):
        original_environment = app.config["ENVIRONMENT"]
        original_server_name = app.config.get("SERVER_NAME")
        original_trusted_hosts = app.config.get("TRUSTED_HOSTS")
        original_cookie_secure = app.config.get("SESSION_COOKIE_SECURE")
        original_scheme = app.config.get("PREFERRED_URL_SCHEME")

        app.config["ENVIRONMENT"] = "production"
        app.config["SERVER_NAME"] = None
        app.config["TRUSTED_HOSTS"] = None
        app.config["SESSION_COOKIE_SECURE"] = False
        app.config["PREFERRED_URL_SCHEME"] = "http"

        try:
            missing = app_module.missing_config_keys()
        finally:
            app.config["ENVIRONMENT"] = original_environment
            app.config["SERVER_NAME"] = original_server_name
            app.config["TRUSTED_HOSTS"] = original_trusted_hosts
            app.config["SESSION_COOKIE_SECURE"] = original_cookie_secure
            app.config["PREFERRED_URL_SCHEME"] = original_scheme

        self.assertTrue(any(item.startswith("SECRET_KEY") for item in missing))
        self.assertIn("SESSION_COOKIE_SECURE=true", missing)
        self.assertIn("TRUSTED_HOSTS", missing)
        self.assertIn("SERVER_NAME", missing)

    def test_v2_schema_models_create_cleanly(self):
        with app.app_context():
            user = User(
                email="v2@example.com",
                name="V2 User",
                picture="",
                role="user",
                plan="free",
                search_credits=8,
                community_points=13,
                rank_title="Mapper",
                age_verification_status="verified_adult",
            )
            place = Place(
                name="Geo Venue",
                slug="geo-venue",
                town="Geo Town",
                postcode="GT1 1AA",
                latitude=52.24,
                longitude=-0.89,
            )
            db.session.add_all([user, place])
            db.session.flush()

            profile = AccessibilityProfile(
                place=place,
                toilet_distance_from_bar="About ten steps",
                toilet_distance_from_bar_m=7.5,
                verified_by_user=user,
            )
            comment = Comment(
                place=place,
                user_email=user.email,
                body="Useful note",
                status="pending",
            )
            search_event = SearchEvent(
                query_text="Geo",
                town="Geo Town",
                accessible="yes",
                filters_json={"verified_only": True},
                result_count=1,
            )
            api_key = APIKey(
                user=user,
                key_hash="hash-v2-key",
                label="Primary",
                scopes_json=["search:read"],
                lookup_credits=25,
            )
            db.session.add_all([profile, comment, search_event, api_key])
            db.session.flush()

            api_lookup = APILookupEvent(
                api_key=api_key,
                user=user,
                endpoint="/api/search",
                query="Geo Venue",
                status_code=200,
            )
            audit_log = AuditLog(
                actor_user=user,
                action="comment.created",
                entity_type="comment",
                entity_id=str(comment.id),
                before_json={"status": None},
                after_json={"status": "pending"},
                reason="Initial community submission",
            )
            db.session.add_all([api_lookup, audit_log])
            db.session.commit()

            saved_user = User.query.filter_by(email="v2@example.com").first()
            saved_search = SearchEvent.query.filter_by(query_text="Geo").first()
            saved_api_key = APIKey.query.filter_by(key_hash="hash-v2-key").first()
            saved_audit = AuditLog.query.filter_by(action="comment.created").first()

        self.assertEqual(saved_user.plan, "free")
        self.assertEqual(saved_user.search_credits, 8)
        self.assertEqual(saved_user.community_points, 13)
        self.assertEqual(saved_search.filters_json["verified_only"], True)
        self.assertIsNone(saved_search.user_id)
        self.assertEqual(saved_api_key.lookup_credits, 25)
        self.assertEqual(saved_audit.entity_type, "comment")

    def test_account_state_labels_separate_plan_from_access(self):
        with app.app_context():
            member = User(email="member-state@example.com", name="Member", picture="", role="member", plan="free")
            staff = User(email="staff-state@example.com", name="Staff", picture="", role="staff", plan="free")
            admin = User(email="admin-state@example.com", name="Admin", picture="", role="admin", plan="business")
            db.session.add_all([member, staff, admin])
            db.session.commit()

            member_state = app_module.build_account_state(member)
            staff_state = app_module.build_account_state(staff)
            admin_state = app_module.build_account_state(admin)

        self.assertEqual(member_state["plan_label"], "Free")
        self.assertEqual(member_state["access_label"], "Member")
        self.assertEqual(staff_state["plan_label"], "Free")
        self.assertEqual(staff_state["access_label"], "Staff")
        self.assertEqual(admin_state["plan_label"], "Business")
        self.assertEqual(admin_state["access_label"], "Admin")

    def test_google_callback_rejects_unverified_email(self):
        fake_google = SimpleNamespace(
            authorize_access_token=unittest.mock.Mock(
                return_value={
                    "userinfo": {
                        "sub": "google-unverified",
                        "email": "oauth-unverified@example.com",
                        "email_verified": False,
                        "name": "OAuth Unverified",
                    }
                }
            )
        )

        with patch.object(app_module, "google", fake_google):
            response = self.client.get("/auth/google/callback", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"requires a verified email address", response.data)
        with app.app_context():
            self.assertIsNone(User.query.filter_by(email="oauth-unverified@example.com").first())

    def test_google_callback_links_existing_user_to_google_sub(self):
        with app.app_context():
            user = User(email="oauth-link@example.com", name="Old Name", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        fake_google = SimpleNamespace(
            authorize_access_token=unittest.mock.Mock(
                return_value={
                    "userinfo": {
                        "sub": "google-linked-sub",
                        "email": "oauth-link@example.com",
                        "email_verified": True,
                        "name": "Linked User",
                        "picture": "https://example.com/avatar.png",
                    }
                }
            )
        )

        with patch.object(app_module, "google", fake_google):
            response = self.client.get("/auth/google/callback")

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            refreshed = User.query.filter_by(email="oauth-link@example.com").first()
        self.assertEqual(refreshed.google_sub, "google-linked-sub")
        self.assertEqual(refreshed.name, "Linked User")
        self.assertIsNotNone(refreshed.last_login_at)

    def test_user_has_api_access_matches_paid_business_and_staff_states(self):
        with app.app_context():
            free_user = User(email="free-api@example.com", name="Free", picture="", role="member", plan="free")
            paid_user = User(email="paid-api@example.com", name="Paid", picture="", role="member", plan="paid")
            business_user = User(email="business-api@example.com", name="Business", picture="", role="member", plan="business")
            staff_user = User(email="staff-api@example.com", name="Staff", picture="", role="staff", plan="free")

            self.assertFalse(app_module.user_has_api_access(free_user))
            self.assertTrue(app_module.user_has_api_access(paid_user))
            self.assertTrue(app_module.user_has_api_access(business_user))
            self.assertTrue(app_module.user_has_api_access(staff_user))

    def test_free_plan_overrides_stale_paid_or_api_roles_for_entitlements(self):
        with app.app_context():
            stale_paid_user = User(email="stale-paid@example.com", name="Stale Paid", picture="", role="paid_consumer", plan="free")
            stale_api_user = User(email="stale-api@example.com", name="Stale API", picture="", role="api_buyer", plan="free")

            self.assertFalse(app_module.user_has_api_access(stale_paid_user))
            self.assertFalse(app_module.user_has_api_access(stale_api_user))
            self.assertEqual(app_module.normalize_billing_plan_name(stale_paid_user), "free")
            self.assertEqual(app_module.normalize_billing_plan_name(stale_api_user), "free")
            self.assertEqual(app_module.get_monthly_search_limit(stale_paid_user), 10)
            self.assertEqual(app_module.get_monthly_search_limit(stale_api_user), 10)

    def test_apply_plan_role_to_user_repairs_plan_when_role_already_matches(self):
        with app.app_context():
            user = User(email="repair-plan@example.com", name="Repair Plan", picture="", role="api_buyer", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

            applied = app_module.apply_plan_role_to_user(user_id, "api_buyer")
            repaired = db.session.get(User, user_id)

        self.assertTrue(applied)
        self.assertEqual(repaired.role, "api_buyer")
        self.assertEqual(repaired.plan, "business")

    def test_post_routes_require_csrf(self):
        response = self.client.post("/admin/place/new", data={"name": "Test venue"})

        self.assertEqual(response.status_code, 400)

    def test_new_routes_require_auth(self):
        settings_response = self.client.get("/account/settings")
        moderation_response = self.client.get("/admin/moderation")

        self.assertEqual(settings_response.status_code, 302)
        self.assertEqual(moderation_response.status_code, 302)

    def test_comment_post_with_csrf_reaches_view(self):
        with app.app_context():
            if not User.query.filter_by(email="user@example.com").first():
                db.session.add(User(email="user@example.com", name="User", picture="", role="member"))
                db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": "user@example.com", "name": "User", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.post(
            "/place/missing-place/comment",
            data={"body": "Useful note", "csrf_token": "token123"},
        )

        self.assertEqual(response.status_code, 404)

    def test_place_page_masks_comment_author_email(self):
        with app.app_context():
            user = User(email="visible@example.com", name="Visible User", picture="", role="member", plan="free")
            place = Place(name="Masked Cafe", slug="masked-cafe", town="Mask Town")
            db.session.add_all([user, place])
            db.session.flush()
            db.session.add(Comment(place=place, user_email=user.email, body="Helpful mask note", is_public=True, status="approved"))
            db.session.commit()

        self.login_session("visible@example.com", "Visible User")
        response = self.client.get("/place/masked-cafe")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"vis...", response.data)
        self.assertNotIn(b"visible@example.com", response.data)

    def test_search_submission_tracks_usage(self):
        with app.app_context():
            user = User.query.filter_by(email="usage@example.com").first()
            if not user:
                user = User(email="usage@example.com", name="Usage User", picture="", role="member", plan="free")
                db.session.add(user)
                db.session.commit()
            if Place.query.count() == 0:
                db.session.add(Place(name="Tracked Venue", slug="tracked-venue", town="Test Town"))
                db.session.commit()
            start_count = SearchEvent.query.filter_by(user_id=user.id).count()

        self.login_session("usage@example.com", "Usage User")

        response = self.client.get("/search?q=Tracked&town=Test+Town&accessible=yes&submitted=1")

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            user = User.query.filter_by(email="usage@example.com").first()
            end_count = SearchEvent.query.filter_by(user_id=user.id).count()
            event = SearchEvent.query.filter_by(user_id=user.id).order_by(SearchEvent.id.desc()).first()
        self.assertEqual(end_count, start_count + 1)
        self.assertEqual(event.query_text, "Tracked")
        self.assertEqual(event.town, "Test Town")
        self.assertEqual(event.accessible, "yes")
        self.assertEqual(event.filters_json["accessible"], "yes")
        self.assertEqual(event.result_count, 0)

    def test_free_user_limit_blocks_additional_searches(self):
        with app.app_context():
            user = User(
                email="limited@example.com",
                name="Limited User",
                picture="",
                role="member",
                plan="free",
                monthly_search_limit=1,
                search_credits=0,
            )
            db.session.add(user)
            db.session.add(Place(name="Limited Venue", slug="limited-venue", town="Quiet Town"))
            db.session.commit()
            db.session.add(SearchEvent(user_id=user.id, query_text="Used", result_count=1))
            db.session.commit()
            start_count = SearchEvent.query.filter_by(user_id=user.id).count()

        self.login_session("limited@example.com", "Limited User")
        response = self.client.get("/search?q=Limited&submitted=1", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You&#39;ve used all free plan searches for this month.", response.data)
        with app.app_context():
            user = User.query.filter_by(email="limited@example.com").first()
            end_count = SearchEvent.query.filter_by(user_id=user.id).count()
        self.assertEqual(end_count, start_count)
        self.assertEqual(user.search_credits, 0)

    def test_staff_admin_search_bypass_still_tracks_usage(self):
        admin_email = "limit-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            user = User(
                email=admin_email,
                name="Limit Admin",
                picture="",
                role="admin",
                plan="admin",
                monthly_search_limit=0,
                search_credits=0,
            )
            db.session.add(user)
            db.session.add(Place(name="Admin Venue", slug="admin-venue", town="Admin Town"))
            db.session.commit()
            start_count = SearchEvent.query.filter_by(user_id=user.id).count()

        self.login_session(admin_email, "Limit Admin")
        response = self.client.get("/search?q=Admin&submitted=1")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"monthly search limit", response.data)
        with app.app_context():
            user = User.query.filter_by(email=admin_email).first()
            end_count = SearchEvent.query.filter_by(user_id=user.id).count()
        self.assertEqual(end_count, start_count + 1)
        self.assertEqual(user.search_credits, 0)

    def test_account_usage_display_shows_v2_fields(self):
        with app.app_context():
            user = User(
                email="account@example.com",
                name="Account User",
                picture="",
                role="member",
                plan="free",
                monthly_search_limit=12,
                search_credits=4,
                community_points=22,
                rank_title="Explorer",
            )
            db.session.add(user)
            db.session.commit()
            db.session.add_all(
                [
                    SearchEvent(user_id=user.id, query_text="One", result_count=1),
                    SearchEvent(user_id=user.id, query_text="Two", result_count=2),
                ]
            )
            db.session.commit()

        self.login_session("account@example.com", "Account User")
        response = self.client.get("/account")
        settings_response = self.client.get("/account/settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings_response.status_code, 200)
        self.assertIn(b"Current plan", response.data)
        self.assertIn(b"Free", response.data)
        self.assertIn(b"12", response.data)
        self.assertIn(b"4", response.data)
        self.assertIn(b"22", response.data)
        self.assertIn(b"Explorer", response.data)
        self.assertIn(b"2 of 12 searches used this month", response.data)
        self.assertIn(b"4 extra search credits remaining", response.data)
        self.assertIn(b"Extra credits", settings_response.data)
        self.assertIn(b"Community points", settings_response.data)

    def test_account_settings_profile_avatar_fallback_and_helper_copy_render(self):
        with app.app_context():
            user = User(email="avatar-fallback@example.com", name="Avatar Fallback", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("avatar-fallback@example.com", "Avatar Fallback")
        response = self.client.get("/account/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Profile picture", response.data)
        self.assertIn(b"PNG, JPG, WEBP or GIF. Max 2MB.", response.data)
        self.assertIn(b"AF", response.data)
        self.assertIn(b"planira-avatar-fallback", response.data)

    def test_profile_image_upload_replace_and_remove_flow(self):
        with app.app_context():
            user = User(email="avatar-upload@example.com", name="Avatar Upload", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("avatar-upload@example.com", "Avatar Upload")

        upload_response = self.client.post(
            "/account/settings/profile-image",
            data={
                "csrf_token": "token123",
                "profile_image": (io.BytesIO(self.png_bytes()), "avatar.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(upload_response.status_code, 200)
        self.assertIn(b"Profile picture updated.", upload_response.data)
        with app.app_context():
            user = User.query.filter_by(email="avatar-upload@example.com").first()
            first_filename = user.profile_image_filename
            first_path = os.path.join(self._upload_dir, first_filename)
        self.assertTrue(first_filename.endswith(".png"))
        self.assertTrue(os.path.exists(first_path))
        self.assertIn(f"uploads/profile_pics/{first_filename}".encode(), upload_response.data)

        replace_response = self.client.post(
            "/account/settings/profile-image",
            data={
                "csrf_token": "token123",
                "profile_image": (io.BytesIO(self.gif_bytes()), "avatar.gif"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(replace_response.status_code, 200)
        with app.app_context():
            user = User.query.filter_by(email="avatar-upload@example.com").first()
            second_filename = user.profile_image_filename
        self.assertNotEqual(first_filename, second_filename)
        self.assertFalse(os.path.exists(first_path))
        self.assertTrue(os.path.exists(os.path.join(self._upload_dir, second_filename)))

        remove_response = self.client.post(
            "/account/settings/profile-image",
            data={"csrf_token": "token123", "action": "remove"},
            follow_redirects=True,
        )

        self.assertEqual(remove_response.status_code, 200)
        self.assertIn(b"Profile picture removed.", remove_response.data)
        with app.app_context():
            user = User.query.filter_by(email="avatar-upload@example.com").first()
            self.assertIsNone(user.profile_image_filename)
            self.assertIsNone(user.avatar_url)
        self.assertFalse(os.path.exists(os.path.join(self._upload_dir, second_filename)))
        self.assertIn(b"planira-avatar-fallback", remove_response.data)

    def test_profile_image_upload_rejects_non_image_files(self):
        with app.app_context():
            user = User(email="avatar-bad@example.com", name="Avatar Bad", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("avatar-bad@example.com", "Avatar Bad")
        response = self.client.post(
            "/account/settings/profile-image",
            data={
                "csrf_token": "token123",
                "profile_image": (io.BytesIO(b"<svg></svg>"), "avatar.svg"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Use a PNG, JPG, WEBP or GIF image.", response.data)
        with app.app_context():
            user = User.query.filter_by(email="avatar-bad@example.com").first()
            self.assertIsNone(user.profile_image_filename)

    def test_logged_out_and_free_users_can_view_existing_place_images(self):
        with app.app_context():
            free_user = User(email="gallery-free@example.com", name="Gallery Free", picture="", role="member", plan="free")
            place = Place(name="Gallery Cafe", slug="gallery-cafe", town="Gallery Town")
            uploader = User(email="gallery-paid@example.com", name="Gallery Paid", picture="", role="member", plan="paid")
            db.session.add_all([free_user, place, uploader])
            db.session.flush()
            image = PlaceImage(place=place, uploader=uploader, filename="gallery.png", caption="Front entrance")
            db.session.add(image)
            db.session.commit()

        with open(os.path.join(self._place_upload_dir, "gallery.png"), "wb") as file_obj:
            file_obj.write(self.png_bytes())

        logged_out_response = self.client.get("/place/gallery-cafe")
        self.assertEqual(logged_out_response.status_code, 200)
        self.assertIn(b"Front entrance", logged_out_response.data)
        self.assertIn(b"uploads/place_images/gallery.png", logged_out_response.data)
        self.assertNotIn(b"Add a place photo", logged_out_response.data)

        self.login_session("gallery-free@example.com", "Gallery Free")
        free_response = self.client.get("/place/gallery-cafe")
        self.assertEqual(free_response.status_code, 200)
        self.assertIn(b"Front entrance", free_response.data)
        self.assertIn(b"Upgrade to add your own place photos.", free_response.data)
        self.assertNotIn(b"Upload photo", free_response.data)

    def test_free_user_cannot_upload_place_image_or_see_upload_form(self):
        with app.app_context():
            user = User(email="free-photo@example.com", name="Free Photo", picture="", role="member", plan="free")
            place = Place(name="Free Photo Place", slug="free-photo-place", town="Quiet Town")
            db.session.add_all([user, place])
            db.session.commit()
            place_id = place.id

        self.login_session("free-photo@example.com", "Free Photo")
        page_response = self.client.get("/place/free-photo-place")
        self.assertEqual(page_response.status_code, 200)
        self.assertNotIn(b"Add a place photo", page_response.data)
        self.assertIn(b"Upgrade to add your own place photos.", page_response.data)

        upload_response = self.client.post(
            f"/place/{place_id}/images/upload",
            data={
                "csrf_token": "token123",
                "caption": "Blocked upload",
                "place_image": (io.BytesIO(self.png_bytes()), "blocked.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(upload_response.status_code, 200)
        self.assertIn(b"Upgrade to a paid Planira plan to add place photos.", upload_response.data)
        with app.app_context():
            self.assertEqual(PlaceImage.query.count(), 0)

    def test_paid_user_can_upload_place_image_and_place_page_renders_it(self):
        with app.app_context():
            user = User(email="paid-photo@example.com", name="Paid Photo", picture="", role="member", plan="paid")
            place = Place(name="Paid Photo Place", slug="paid-photo-place", town="Photo Town")
            db.session.add_all([user, place])
            db.session.commit()
            place_id = place.id

        self.login_session("paid-photo@example.com", "Paid Photo")
        page_response = self.client.get("/place/paid-photo-place")
        self.assertEqual(page_response.status_code, 200)
        self.assertIn(b"Add a place photo", page_response.data)

        upload_response = self.client.post(
            f"/place/{place_id}/images/upload",
            data={
                "csrf_token": "token123",
                "caption": "Side entrance",
                "place_image": (io.BytesIO(self.png_bytes()), "venue.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(upload_response.status_code, 200)
        self.assertIn(b"Place photo added.", upload_response.data)
        self.assertIn(b"Side entrance", upload_response.data)
        with app.app_context():
            image = PlaceImage.query.one()
            saved_path = os.path.join(self._place_upload_dir, image.filename)
        self.assertTrue(image.filename.endswith(".png"))
        self.assertTrue(os.path.exists(saved_path))
        self.assertIn(f"uploads/place_images/{image.filename}".encode(), upload_response.data)

    def test_place_image_upload_rejects_invalid_types_and_svg(self):
        with app.app_context():
            user = User(email="invalid-photo@example.com", name="Invalid Photo", picture="", role="member", plan="business")
            place = Place(name="Invalid Photo Place", slug="invalid-photo-place", town="Photo Town")
            db.session.add_all([user, place])
            db.session.commit()
            place_id = place.id

        self.login_session("invalid-photo@example.com", "Invalid Photo")
        bad_type_response = self.client.post(
            f"/place/{place_id}/images/upload",
            data={
                "csrf_token": "token123",
                "place_image": (io.BytesIO(b"not-an-image"), "notes.txt"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(bad_type_response.status_code, 200)
        self.assertIn(b"Use a PNG, JPG, WEBP or GIF image.", bad_type_response.data)

        svg_response = self.client.post(
            f"/place/{place_id}/images/upload",
            data={
                "csrf_token": "token123",
                "place_image": (io.BytesIO(b"<svg></svg>"), "photo.svg"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(svg_response.status_code, 200)
        self.assertIn(b"Use a PNG, JPG, WEBP or GIF image.", svg_response.data)
        with app.app_context():
            self.assertEqual(PlaceImage.query.count(), 0)

    def test_uploader_can_delete_own_place_image(self):
        with app.app_context():
            user = User(email="delete-own@example.com", name="Delete Own", picture="", role="member", plan="paid")
            place = Place(name="Delete Own Place", slug="delete-own-place", town="Delete Town")
            db.session.add_all([user, place])
            db.session.flush()
            image = PlaceImage(place=place, uploader=user, filename="delete-own.png", caption="Own photo")
            db.session.add(image)
            db.session.commit()
            image_id = image.id

        file_path = os.path.join(self._place_upload_dir, "delete-own.png")
        with open(file_path, "wb") as file_obj:
            file_obj.write(self.png_bytes())

        self.login_session("delete-own@example.com", "Delete Own")
        response = self.client.post(
            f"/place-images/{image_id}/delete",
            data={"csrf_token": "token123"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Place photo removed.", response.data)
        self.assertFalse(os.path.exists(file_path))
        with app.app_context():
            self.assertIsNone(db.session.get(PlaceImage, image_id))

    def test_non_owner_and_free_user_cannot_delete_another_users_place_image(self):
        with app.app_context():
            owner = User(email="photo-owner@example.com", name="Photo Owner", picture="", role="member", plan="paid")
            free_user = User(email="photo-free@example.com", name="Photo Free", picture="", role="member", plan="free")
            place = Place(name="Ownership Place", slug="ownership-place", town="Owner Town")
            db.session.add_all([owner, free_user, place])
            db.session.flush()
            image = PlaceImage(place=place, uploader=owner, filename="ownership.png", caption="Owner photo")
            db.session.add(image)
            db.session.commit()
            image_id = image.id

        with open(os.path.join(self._place_upload_dir, "ownership.png"), "wb") as file_obj:
            file_obj.write(self.png_bytes())

        self.login_session("photo-free@example.com", "Photo Free")
        response = self.client.post(
            f"/place-images/{image_id}/delete",
            data={"csrf_token": "token123"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You can only remove your own place photos.", response.data)
        with app.app_context():
            self.assertIsNotNone(db.session.get(PlaceImage, image_id))

    def test_staff_can_delete_any_place_image_even_if_file_is_missing(self):
        staff_email = "photo-staff@example.com"
        ADMIN_EMAILS.add(staff_email)
        with app.app_context():
            owner = User(email="photo-owner-2@example.com", name="Photo Owner Two", picture="", role="member", plan="paid")
            staff = User(email=staff_email, name="Photo Staff", picture="", role="staff", plan="free")
            place = Place(name="Staff Delete Place", slug="staff-delete-place", town="Staff Town")
            db.session.add_all([owner, staff, place])
            db.session.flush()
            image = PlaceImage(place=place, uploader=owner, filename="missing-file.png", caption="Staff delete")
            db.session.add(image)
            db.session.commit()
            image_id = image.id

        self.login_session(staff_email, "Photo Staff")
        response = self.client.post(
            f"/place-images/{image_id}/delete",
            data={"csrf_token": "token123"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Place photo removed.", response.data)
        with app.app_context():
            self.assertIsNone(db.session.get(PlaceImage, image_id))

    def test_nav_avatar_renders_for_logged_in_member_and_staff_users(self):
        with app.app_context():
            member = User(email="nav-member@example.com", name="Nav Member", picture="", role="member", plan="free")
            staff = User(email="nav-staff@example.com", name="Nav Staff", picture="", role="staff", plan="free")
            db.session.add_all([member, staff])
            db.session.commit()

        self.login_session("nav-member@example.com", "Nav Member")
        member_response = self.client.get("/account")
        self.assertEqual(member_response.status_code, 200)
        self.assertIn(b"account-avatar", member_response.data)
        self.assertIn(b"NM", member_response.data)
        self.assertNotIn(b"Staff workspace", member_response.data)

        self.login_session("nav-staff@example.com", "Nav Staff")
        staff_response = self.client.get("/account")
        self.assertEqual(staff_response.status_code, 200)
        self.assertIn(b"account-avatar", staff_response.data)
        self.assertIn(b"NS", staff_response.data)
        self.assertIn(b"Staff", staff_response.data)

    def test_logged_out_pages_still_render_without_account_avatar(self):
        response = self.client.get("/plans")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Continue with Google", response.data)
        self.assertNotIn(b"account-avatar", response.data)

    def test_plans_page_uses_consistent_quota_language_for_current_account(self):
        with app.app_context():
            user = User(
                email="plans@example.com",
                name="Plans User",
                picture="",
                role="member",
                plan="free",
                monthly_search_limit=8,
                search_credits=3,
            )
            db.session.add(user)
            db.session.commit()

        self.login_session("plans@example.com", "Plans User")
        response = self.client.get("/plans")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"on the Free plan", response.data)
        self.assertIn(b"Free plan includes 8 searches per month.", response.data)

    def test_checkout_route_metadata_contains_target_role_user_id_and_email(self):
        with app.app_context():
            user = User(email="checkout-fields@example.com", name="Checkout Fields", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        self.login_session("checkout-fields@example.com", "Checkout Fields")
        session_create = unittest.mock.Mock(return_value=SimpleNamespace(url="https://checkout.stripe.test/session/fields"))
        fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(create=session_create)))

        with patch.dict(os.environ, {"STRIPE_PRICE_API_20": "price_api_20_test"}, clear=False):
            with patch.object(app_module, "stripe", fake_stripe):
                with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123"}, clear=False):
                    response = self.client.post("/billing/checkout/api_20", data={"csrf_token": "token123"})

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(session_create.call_args)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        self.assertTrue(any("temporarily disabled" in message for _, message in flashes))

    def test_subscription_checkout_includes_subscription_metadata(self):
        with app.app_context():
            user = User(email="checkout-subscription@example.com", name="Checkout Subscription", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        self.login_session("checkout-subscription@example.com", "Checkout Subscription")
        session_create = unittest.mock.Mock(return_value=SimpleNamespace(url="https://checkout.stripe.test/subscription"))
        fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(create=session_create)))

        with patch.dict(os.environ, {"STRIPE_PRICE_PAID_CONSUMER": "price_paid_test"}, clear=False):
            with patch.object(app_module, "stripe", fake_stripe):
                with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123"}, clear=False):
                    response = self.client.post("/billing/checkout/paid_consumer", data={"csrf_token": "token123"})

        self.assertEqual(response.status_code, 303)
        kwargs = session_create.call_args.kwargs
        self.assertEqual(kwargs["subscription_data"]["metadata"]["user_id"], str(user_id))
        self.assertEqual(kwargs["subscription_data"]["metadata"]["target_role"], "paid_consumer")

    def test_billing_success_for_paid_consumer_grants_paid_plan_and_api_access(self):
        with app.app_context():
            user = User(email="paid-success@example.com", name="Paid Success", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        self.login_session("paid-success@example.com", "Paid Success")
        fake_session = SimpleNamespace(
            metadata={"plan_key": "paid_consumer", "target_role": "paid_consumer"},
            payment_status="paid",
            status="complete",
            client_reference_id=str(user_id),
            customer="cus_paid_success",
            subscription="sub_paid_success",
        )
        fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(retrieve=unittest.mock.Mock(return_value=fake_session))))

        with patch.dict(os.environ, {"STRIPE_PRICE_PAID_CONSUMER": "price_paid_test"}, clear=False):
            with patch.object(app_module, "stripe", fake_stripe):
                with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123"}, clear=False):
                    response = self.client.get("/billing/success?session_id=cs_test_paid", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Paid consumer is now active on your account.", response.data)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "paid_consumer")
        self.assertEqual(refreshed.plan, "paid")
        self.assertEqual(refreshed.stripe_customer_id, "cus_paid_success")
        self.assertEqual(refreshed.stripe_subscription_id, "sub_paid_success")
        self.assertIsNone(refreshed.subscription_status)
        self.assertTrue(app_module.user_has_api_access(refreshed))

    def test_stripe_webhook_for_api_buyer_does_not_grant_business_plan_or_api_access(self):
        with app.app_context():
            user = User(email="api-webhook@example.com", name="API Webhook", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "api_buyer",
                    },
                    "customer": "cus_api_webhook",
                    "subscription": None,
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "member")
        self.assertEqual(refreshed.plan, "free")
        self.assertEqual(refreshed.stripe_customer_id, "cus_api_webhook")
        self.assertFalse(app_module.user_has_api_access(refreshed))

    def test_stripe_subscription_deleted_downgrades_paid_member_to_free(self):
        with app.app_context():
            user = User(email="subscription-deleted@example.com", name="Subscription Deleted", picture="", role="paid_consumer", plan="paid")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "paid_consumer",
                        "user_email": "subscription-deleted@example.com",
                    },
                    "status": "canceled",
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
            audit = AuditLog.query.filter_by(action="billing.entitlement.revoked", entity_type="user", entity_id=str(user_id)).first()
        self.assertEqual(refreshed.role, "member")
        self.assertEqual(refreshed.plan, "free")
        self.assertFalse(app_module.user_has_api_access(refreshed))
        self.assertIsNotNone(audit)

    def test_subscription_deleted_can_resolve_user_by_stored_subscription_id(self):
        with app.app_context():
            user = User(
                email="stored-subscription@example.com",
                name="Stored Subscription",
                picture="",
                role="paid_consumer",
                plan="paid",
                stripe_customer_id="cus_stored_subscription",
                stripe_subscription_id="sub_stored_subscription",
                subscription_status="active",
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_stored_subscription",
                    "object": "subscription",
                    "customer": "cus_stored_subscription",
                    "status": "canceled",
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "member")
        self.assertEqual(refreshed.plan, "free")
        self.assertEqual(refreshed.stripe_subscription_id, "sub_stored_subscription")
        self.assertEqual(refreshed.subscription_status, "canceled")

    def test_invoice_payment_failed_downgrades_paid_member_to_free(self):
        with app.app_context():
            user = User(email="invoice-failed@example.com", name="Invoice Failed", picture="", role="paid_consumer", plan="paid")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "paid_consumer",
                    }
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "member")
        self.assertEqual(refreshed.plan, "free")
        self.assertFalse(app_module.user_has_api_access(refreshed))

    def test_subscription_updated_only_revokes_terminal_statuses(self):
        with app.app_context():
            user = User(email="subscription-active@example.com", name="Subscription Active", picture="", role="paid_consumer", plan="paid")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "paid_consumer",
                    },
                    "status": "active",
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "paid_consumer")
        self.assertEqual(refreshed.plan, "paid")
        self.assertTrue(app_module.user_has_api_access(refreshed))

    def test_subscription_lifecycle_webhooks_do_not_demote_staff_or_admin_accounts(self):
        staff_email = "staff-subscription@example.com"
        ADMIN_EMAILS.add(staff_email)
        with app.app_context():
            staff = User(email=staff_email, name="Staff Subscription", picture="", role="admin", plan="admin")
            db.session.add(staff)
            db.session.commit()
            user_id = staff.id

        fake_event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "paid_consumer",
                    },
                    "status": "canceled",
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "admin")
        self.assertEqual(refreshed.plan, "admin")

    def test_subscription_lifecycle_does_not_revoke_one_off_api_pack_entitlement(self):
        with app.app_context():
            user = User(email="api-pack-owner@example.com", name="API Pack Owner", picture="", role="api_buyer", plan="business")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        fake_event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "metadata": {
                        "user_id": str(user_id),
                        "target_role": "paid_consumer",
                    },
                    "status": "canceled",
                }
            },
        }
        fake_stripe = SimpleNamespace(Webhook=SimpleNamespace(construct_event=unittest.mock.Mock(return_value=fake_event)))

        with patch.object(app_module, "stripe", fake_stripe):
            with patch.dict(app.config, {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_test_123"}, clear=False):
                response = self.client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig_test"})

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "api_buyer")
        self.assertEqual(refreshed.plan, "business")
        self.assertFalse(app_module.user_has_api_access(refreshed))

    def test_search_first_load_stays_empty(self):
        with app.app_context():
            user = User.query.filter_by(email="presearch@example.com").first()
            if not user:
                user = User(email="presearch@example.com", name="Pre Search", picture="", role="member")
                db.session.add(user)
            if not Place.query.filter_by(slug="first-load-venue").first():
                db.session.add(Place(name="First Load Venue", slug="first-load-venue", town="Quiet Town"))
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": "presearch@example.com", "name": "Pre Search", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/search")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Start with a place search", response.data)
        self.assertNotIn(b"First Load Venue", response.data)

    def test_search_submitted_empty_fields_stays_empty(self):
        with app.app_context():
            user = User.query.filter_by(email="blanksearch@example.com").first()
            if not user:
                user = User(email="blanksearch@example.com", name="Blank Search", picture="", role="member")
                db.session.add(user)
            if not Place.query.filter_by(slug="blank-search-venue").first():
                db.session.add(Place(name="Blank Search Venue", slug="blank-search-venue", town="Quiet Town"))
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": "blanksearch@example.com", "name": "Blank Search", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/search?submitted=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add a venue name, town, or accessibility filter to show results.", response.data)
        self.assertNotIn(b"Blank Search Venue", response.data)

    def test_autocomplete_empty_query_returns_empty_list(self):
        response = self.client.get("/api/autocomplete?q=")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, [])

    def test_autocomplete_is_available_without_login(self):
        with app.app_context():
            db.session.add(Place(name="Open Venue", slug="open-venue", town="Northampton"))
            db.session.commit()

        response = self.client.get("/api/autocomplete?q=Open")

        self.assertEqual(response.status_code, 200)
        place_group = next(group for group in response.json if group["key"] == "places")
        self.assertEqual(len(place_group["items"]), 1)
        self.assertEqual(place_group["items"][0]["name"], "Open Venue")
        self.assertEqual(place_group["items"][0]["town"], "Northampton")

    def test_autocomplete_selected_place_search_returns_exact_result(self):
        with app.app_context():
            user = User(email="railway@example.com", name="Railway User", picture="", role="member", plan="free")
            place = Place(name="The Railway Inn", slug="the-railway-inn-rushden", town="Rushden")
            db.session.add_all([user, place])
            db.session.commit()
            place_id = place.id

        self.login_session("railway@example.com", "Railway User")
        autocomplete_response = self.client.get("/api/autocomplete?q=railway")

        self.assertEqual(autocomplete_response.status_code, 200)
        place_group = next(group for group in autocomplete_response.json if group["key"] == "places")
        railway_item = next(item for item in place_group["items"] if item["name"] == "The Railway Inn")
        self.assertEqual(railway_item["town"], "Rushden")
        self.assertEqual(railway_item["selected_place_id"], place_id)

        search_response = self.client.get(
            f"/search?q=The+Railway+Inn&town=Rushden&selected_place_id={place_id}&submitted=1"
        )

        self.assertEqual(search_response.status_code, 200)
        self.assertIn(b"The Railway Inn", search_response.data)
        self.assertIn(b"Rushden", search_response.data)
        self.assertNotIn(b"Nothing matched that search this time.", search_response.data)

    def test_autocomplete_prioritises_name_matches_and_limits_results(self):
        with app.app_context():
            for index in range(1, 6):
                db.session.add(Place(name=f"Alpha Match {index}", slug=f"alpha-match-{index}", town="Elsewhere"))
            db.session.add(Place(name="Quiet Room", slug="quiet-room", town="Alpha Town"))
            db.session.add(Place(name="Another Quiet Room", slug="another-quiet-room", town="Alpha Village"))
            db.session.commit()

        response = self.client.get("/api/autocomplete?q=Alpha")

        self.assertEqual(response.status_code, 200)
        place_group = next(group for group in response.json if group["key"] == "places")
        popular_group = next(group for group in response.json if group["key"] == "popular")
        self.assertEqual(len(place_group["items"]), 4)
        self.assertEqual([item["name"] for item in place_group["items"]], [f"Alpha Match {index}" for index in range(1, 5)])
        self.assertTrue(all(item["name"] != place_group["items"][0]["name"] for item in popular_group["items"]))

    def test_autocomplete_recent_searches_do_not_leak_between_users(self):
        with app.app_context():
            first_user = User(email="recent-one@example.com", name="Recent One", picture="", role="member", plan="free")
            second_user = User(email="recent-two@example.com", name="Recent Two", picture="", role="member", plan="free")
            db.session.add_all([first_user, second_user])
            db.session.flush()
            db.session.add_all(
                [
                    SearchEvent(user_id=first_user.id, query_text="Railway", town="Rushden", result_count=1),
                    SearchEvent(user_id=second_user.id, query_text="Railway Secret", town="Hidden Town", result_count=1),
                ]
            )
            db.session.commit()

        self.login_session("recent-one@example.com", "Recent One")
        response = self.client.get("/api/autocomplete?q=rail")

        self.assertEqual(response.status_code, 200)
        recent_group = next(group for group in response.json if group["key"] == "recent")
        recent_titles = [item["title"] for item in recent_group["items"]]
        recent_towns = [item["town"] for item in recent_group["items"]]
        self.assertIn("Railway", recent_titles)
        self.assertIn("Rushden", recent_towns)
        self.assertNotIn("Railway Secret", recent_titles)
        self.assertNotIn("Hidden Town", recent_towns)

    def test_autocomplete_badges_only_appear_when_data_exists(self):
        with app.app_context():
            clear_place = Place(name="Clear Route", slug="clear-route", town="Northampton")
            quiet_place = Place(name="Quiet Corner", slug="quiet-corner", town="Northampton")
            db.session.add_all([clear_place, quiet_place])
            db.session.flush()
            db.session.add(
                AccessibilityProfile(
                    place_id=clear_place.id,
                    accessible_toilet="yes",
                    step_free_entrance="yes",
                    confidence_score=86,
                    last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=7),
                )
            )
            db.session.commit()

        response = self.client.get("/api/autocomplete?q=North")

        self.assertEqual(response.status_code, 200)
        place_group = next(group for group in response.json if group["key"] == "places")
        items_by_name = {item["name"]: item for item in place_group["items"]}
        self.assertIn("Verified", items_by_name["Clear Route"]["badges"])
        self.assertIn("Step-free", items_by_name["Clear Route"]["badges"])
        self.assertIn("Accessible toilet", items_by_name["Clear Route"]["badges"])
        self.assertIn("Looks straightforward", items_by_name["Clear Route"]["badges"])
        self.assertEqual(items_by_name["Quiet Corner"]["badges"], [])

    def test_search_results_paginate(self):
        with app.app_context():
            user = User.query.filter_by(email="pager@example.com").first()
            if not user:
                user = User(email="pager@example.com", name="Pager User", picture="", role="member")
                db.session.add(user)
            existing = Place.query.filter(Place.slug.like("pager-venue-%")).count()
            if existing < 15:
                for index in range(existing + 1, 16):
                    db.session.add(
                        Place(
                            name=f"Pager Venue {index}",
                            slug=f"pager-venue-{index}",
                            town="Pagination Town",
                        )
                    )
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": "pager@example.com", "name": "Pager User", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/search?q=Pager&page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Page 2 of", response.data)
        self.assertIn(b"Previous", response.data)

    def test_verification_status_helper_covers_public_states(self):
        now = app_module.datetime.now(app_module.timezone.utc)

        recent_profile = AccessibilityProfile(last_verified_at=now - app_module.timedelta(days=14))
        checked_profile = AccessibilityProfile(last_verified_at=now - app_module.timedelta(days=70))
        stale_profile = AccessibilityProfile(last_verified_at=now - app_module.timedelta(days=160))

        recent = app_module.verification_status(recent_profile)
        checked = app_module.verification_status(checked_profile)
        stale = app_module.verification_status(stale_profile)
        missing = app_module.verification_status(None)

        self.assertEqual(recent["status"], "Verified")
        self.assertEqual(recent["relative_time"], "2 weeks ago")
        self.assertTrue(recent["verified"])
        self.assertEqual(checked["status"], "Checked recently")
        self.assertEqual(stale["status"], "Needs checking")
        self.assertEqual(missing["status"], "Not verified yet")
        self.assertEqual(missing["last_checked_copy"], "Not checked yet")

    def test_search_and_place_pages_prioritise_quick_answer_and_verification(self):
        with app.app_context():
            user = User(email="clarity@example.com", name="Clarity User", picture="", role="member", plan="free")
            place = Place(
                name="Clear Cafe",
                slug="clear-cafe",
                address1="12 Broad Street",
                town="Northampton",
                postcode="NN1 4AA",
            )
            db.session.add_all([user, place])
            db.session.flush()
            db.session.add(
                AccessibilityProfile(
                    place_id=place.id,
                    toilets_available="yes",
                    accessible_toilet="yes",
                    step_free_entrance="yes",
                    stairs_inside="no",
                    toilet_distance_from_bar_m=8,
                    confidence_score=84,
                    last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=14),
                )
            )
            db.session.commit()

        self.login_session("clarity@example.com", "Clarity User")
        search_response = self.client.get("/search?q=Clear&submitted=1")
        place_response = self.client.get("/place/clear-cafe")

        self.assertEqual(search_response.status_code, 200)
        self.assertIn(b"Verified", search_response.data)
        self.assertIn(b"Last checked 2 weeks ago", search_response.data)
        self.assertIn(b"Step-free entrance", search_response.data)
        self.assertIn(b"Accessible toilet", search_response.data)

        self.assertEqual(place_response.status_code, 200)
        self.assertIn(b"Quick answer", place_response.data)
        self.assertIn(b"Last checked 2 weeks ago", place_response.data)
        self.assertIn(b"Accessible toilet confirmed", place_response.data)

    def test_developers_page_shows_upgrade_message_without_api_access(self):
        with app.app_context():
            user = User(email="developer-view@example.com", name="Developer View", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("developer-view@example.com", "Developer View")
        response = self.client.get("/developers")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Upgrade before creating keys", response.data)
        self.assertIn(b"Free search access does not unlock the API.", response.data)
        self.assertNotIn(b"Create new key", response.data)


    def test_dashboard_mission_board_paginates(self):
        admin_email = "admin-pagination@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User.query.filter_by(email=admin_email).first()
            if not admin:
                admin = User(email=admin_email, name="Admin User", picture="", role="admin")
                db.session.add(admin)
            existing = Place.query.filter(Place.slug.like("mission-page-%")).count()
            if existing < 13:
                for index in range(existing + 1, 14):
                    db.session.add(
                        Place(
                            name=f"Mission Page Venue {index}",
                            slug=f"mission-page-{index}",
                            town="Mission Town",
                            status="needs_call",
                        )
                    )
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": admin_email, "name": "Admin User", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/dashboard?mission_page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mission page 2 of", response.data)
        self.assertIn(b"Showing", response.data)

    def test_admin_users_paginates(self):
        admin_email = "admin-users@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User.query.filter_by(email=admin_email).first()
            if not admin:
                admin = User(email=admin_email, name="Admin Users", picture="", role="admin")
                db.session.add(admin)

            existing = User.query.filter(User.email.like("staff-page-user-%@example.com")).count()
            if existing < 22:
                for index in range(existing + 1, 23):
                    db.session.add(
                        User(
                            email=f"staff-page-user-{index}@example.com",
                            name=f"Staff Page User {index}",
                            picture="",
                            role="member",
                        )
                    )
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": admin_email, "name": "Admin Users", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/admin/users?q=Staff+Page+User&page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Showing 21-22 of 22 users.".replace("-", "\u2013").encode(), response.data)
        self.assertIn(b"Page 2 of 2", response.data)
        self.assertIn(b"Previous", response.data)

    def test_admin_users_pagination_preserves_search_query(self):
        admin_email = "admin-users-search@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User.query.filter_by(email=admin_email).first()
            if not admin:
                admin = User(email=admin_email, name="Admin Search", picture="", role="admin")
                db.session.add(admin)

            existing = User.query.filter(User.email.like("persisted-query-user-%@example.com")).count()
            if existing < 21:
                for index in range(existing + 1, 22):
                    db.session.add(
                        User(
                            email=f"persisted-query-user-{index}@example.com",
                            name=f"Persisted Query User {index}",
                            picture="",
                            role="member",
                        )
                    )
            db.session.commit()

        with self.client.session_transaction() as session:
            session["user"] = {"email": admin_email, "name": "Admin Search", "picture": ""}
            session["_csrf_token"] = "token123"

        response = self.client.get("/admin/users?q=Persisted&page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Showing 21-21 of 21 users.".replace("-", "\u2013").encode(), response.data)
        self.assertIn(b"q=Persisted", response.data)
        self.assertIn(b"Page 2 of 2", response.data)

    def test_comment_approval_updates_status_and_creates_audit_log(self):
        admin_email = "moderator@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            moderator = User(email=admin_email, name="Moderator", picture="", role="admin", plan="admin")
            place = Place(name="Moderation Venue", slug="moderation-venue", town="Review Town")
            comment = Comment(
                place=place,
                user_email="guest@example.com",
                body="Needs review",
                status="pending",
            )
            db.session.add_all([moderator, place, comment])
            db.session.commit()
            comment_id = comment.id

        self.login_session(admin_email, "Moderator")
        response = self.client.post(
            f"/admin/moderation/{comment_id}",
            data={
                "csrf_token": "token123",
                "action": "approve",
                "moderation_reason": "Verified by staff",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            comment = db.session.get(Comment, comment_id)
            audit = AuditLog.query.filter_by(action="comment.approved", entity_type="comment", entity_id=str(comment_id)).first()
        self.assertEqual(comment.status, "approved")
        self.assertIsNotNone(comment.reviewed_by_user_id)
        self.assertEqual(comment.moderation_reason, "Verified by staff")
        self.assertIsNotNone(comment.reviewed_at)
        self.assertIsNotNone(audit)
        self.assertEqual(audit.reason, "Verified by staff")

    def test_comment_rejection_hides_public_comment_and_creates_audit_log(self):
        admin_email = "rejector@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            moderator = User(email=admin_email, name="Rejector", picture="", role="admin", plan="admin")
            visitor = User(email="visitor@example.com", name="Visitor", picture="", role="member", plan="free")
            place = Place(name="Hidden Venue", slug="hidden-venue", town="Hidden Town")
            approved = Comment(place=place, user_email="approved@example.com", body="Approved note", status="approved")
            pending = Comment(place=place, user_email=visitor.email, body="Pending note", status="pending")
            db.session.add_all([moderator, visitor, place, approved, pending])
            db.session.commit()
            pending_id = pending.id

        self.login_session(admin_email, "Rejector")
        response = self.client.post(
            f"/admin/moderation/{pending_id}",
            data={
                "csrf_token": "token123",
                "action": "reject",
                "moderation_reason": "Not specific enough",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)

        self.login_session("visitor@example.com", "Visitor")
        place_response = self.client.get("/place/hidden-venue")

        self.assertEqual(place_response.status_code, 200)
        self.assertIn(b"Approved note", place_response.data)
        self.assertNotIn(b"Pending note", place_response.data)
        with app.app_context():
            comment = db.session.get(Comment, pending_id)
            audit = AuditLog.query.filter_by(action="comment.rejected", entity_id=str(pending_id)).first()
        self.assertEqual(comment.status, "rejected")
        self.assertEqual(comment.moderation_reason, "Not specific enough")
        self.assertIsNotNone(audit)

    def test_staff_dashboard_shows_pending_moderation_and_recent_staff_activity(self):
        admin_email = "staffdash@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            moderator = User(email=admin_email, name="Staff Dash", picture="", role="admin", plan="admin")
            user = User(email="searcher@example.com", name="Searcher", picture="", role="member", plan="free")
            place = Place(name="Dash Venue", slug="dash-venue", town="Dash Town", status="needs_call")
            pending = Comment(place=place, user_email="pending@example.com", body="Pending moderation item", status="pending")
            stale_place = Place(name="Stale Venue", slug="stale-venue", town="Old Town", status="verified")
            missing_place = Place(name="Missing Venue", slug="missing-venue", town="Gap Town", status="needs_call")
            db.session.add_all([moderator, user, place, pending, stale_place, missing_place])
            db.session.flush()
            db.session.add_all(
                [
                    AccessibilityProfile(
                        place_id=stale_place.id,
                        confidence_score=30,
                        last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=120),
                    ),
                    AccessibilityProfile(
                        place_id=missing_place.id,
                        toilets_available="unknown",
                        accessible_toilet="unknown",
                        step_free_entrance="unknown",
                        stairs_inside="unknown",
                    ),
                ]
            )
            db.session.commit()
            db.session.add(SearchEvent(user_id=user.id, query_text="Dash Search", result_count=3))
            db.session.add(
                AuditLog(
                    actor_user_id=moderator.id,
                    action="comment.approved",
                    entity_type="comment",
                    entity_id=str(pending.id),
                    after_json={"status": "approved"},
                )
            )
            db.session.commit()

        self.login_session(admin_email, "Staff Dash")
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pending submissions", response.data)
        self.assertIn(b"Recent searches", response.data)
        self.assertIn(b"Audit trail", response.data)
        self.assertIn(b"Needs checking", response.data)
        self.assertIn(b"Stale verification", response.data)
        self.assertIn(b"Missing key accessibility fields", response.data)
        self.assertIn(b"<strong>1</strong>", response.data)
        self.assertIn(b"Dash Search", response.data)
        self.assertIn(b"Comment Approved", response.data)

    def test_staff_user_can_access_dashboard_and_user_directory(self):
        with app.app_context():
            staff = User(email="ops-staff@example.com", name="Ops Staff", picture="", role="staff", plan="free")
            member = User(email="member-one@example.com", name="Member One", picture="", role="member", plan="free")
            db.session.add_all([staff, member])
            db.session.commit()

        self.login_session("ops-staff@example.com", "Ops Staff")

        dashboard_response = self.client.get("/dashboard")
        users_response = self.client.get("/admin/users")

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertIn(b"Live preview usage", dashboard_response.data)
        self.assertEqual(users_response.status_code, 200)
        self.assertIn(b"Manage Planira users", users_response.data)
        self.assertIn(b"Manual override", users_response.data)

    def test_admin_staff_demote_uses_member_role_name(self):
        admin_email = "admin-demote@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Admin Demote", picture="", role="admin", plan="admin")
            staff_user = User(email="to-demote@example.com", name="To Demote", picture="", role="staff", plan="free")
            db.session.add_all([admin, staff_user])
            db.session.commit()
            user_id = staff_user.id

        self.login_session(admin_email, "Admin Demote")
        response = self.client.post(
            f"/admin/users/{user_id}/staff",
            data={"csrf_token": "token123", "action": "demote"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, user_id)
        self.assertEqual(refreshed.role, "member")
        self.assertIn(b"is now a member", response.data)

    def test_admin_can_enable_manual_entitlement(self):
        admin_email = "manual-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Manual Admin", picture="", role="admin", plan="admin")
            member = User(email="manual-member@example.com", name="Manual Member", picture="", role="member", plan="free")
            db.session.add_all([admin, member])
            db.session.commit()
            admin_id = admin.id
            member_id = member.id

        self.login_session(admin_email, "Manual Admin")
        response = self.client.post(
            f"/admin/users/{member_id}/manual-entitlement",
            data={
                "csrf_token": "token123",
                "manual_entitlement_enabled": "1",
                "manual_entitlement_plan": "api_20",
                "access_override_until": "2099-12-31T23:59",
                "manual_entitlement_note": "Support grant",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Manual access override updated", response.data)
        self.assertIn(b"Manual active", response.data)
        with app.app_context():
            refreshed = db.session.get(User, member_id)
            audit = AuditLog.query.filter_by(
                action="user.manual_entitlement.updated",
                entity_type="user",
                entity_id=str(member_id),
            ).first()
        self.assertTrue(refreshed.manual_entitlement_enabled)
        self.assertEqual(refreshed.manual_entitlement_plan, "api_20")
        self.assertEqual(refreshed.manual_entitlement_note, "Support grant")
        self.assertIsNotNone(refreshed.access_override_until)
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_user_id, admin_id)
        self.assertEqual(audit.reason, "Support grant")

    def test_non_admin_cannot_enable_manual_entitlement(self):
        with app.app_context():
            staff = User(email="manual-staff@example.com", name="Manual Staff", picture="", role="staff", plan="free")
            member = User(email="manual-target@example.com", name="Manual Target", picture="", role="member", plan="free")
            db.session.add_all([staff, member])
            db.session.commit()
            member_id = member.id

        self.login_session("manual-staff@example.com", "Manual Staff")
        response = self.client.post(
            f"/admin/users/{member_id}/manual-entitlement",
            data={
                "csrf_token": "token123",
                "manual_entitlement_enabled": "1",
                "manual_entitlement_plan": "paid_consumer",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin access required.", response.data)
        with app.app_context():
            refreshed = db.session.get(User, member_id)
        self.assertFalse(refreshed.manual_entitlement_enabled)

    def test_manual_entitlement_grants_access_without_fake_stripe_records(self):
        admin_email = "manual-grant-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Grant Admin", picture="", role="admin", plan="admin")
            member = User(email="manual-grant@example.com", name="Manual Grant", picture="", role="member", plan="free")
            db.session.add_all([admin, member])
            db.session.commit()
            member_id = member.id

        self.login_session(admin_email, "Grant Admin")
        response = self.client.post(
            f"/admin/users/{member_id}/manual-entitlement",
            data={
                "csrf_token": "token123",
                "manual_entitlement_enabled": "1",
                "manual_entitlement_plan": "business",
                "manual_entitlement_note": "Manual API access",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            refreshed = db.session.get(User, member_id)
            has_api_access = app_module.user_has_api_access(refreshed)
            plan_name = app_module.normalize_billing_plan_name(refreshed)
        self.assertTrue(has_api_access)
        self.assertEqual(plan_name, "business")
        self.assertIsNone(refreshed.stripe_customer_id)
        self.assertIsNone(refreshed.stripe_subscription_id)
        self.assertIsNone(refreshed.subscription_status)

    def test_expired_manual_entitlement_does_not_grant_access(self):
        with app.app_context():
            user = User(
                email="manual-expired@example.com",
                name="Manual Expired",
                picture="",
                role="member",
                plan="free",
                manual_entitlement_enabled=True,
                manual_entitlement_plan="business",
                access_override_until=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(hours=1),
            )
            db.session.add(user)
            db.session.commit()

            self.assertFalse(app_module.manual_entitlement_is_active(user))
            self.assertFalse(app_module.user_has_api_access(user))
            self.assertEqual(app_module.normalize_billing_plan_name(user), "free")

    def test_disabling_manual_entitlement_removes_override_access(self):
        admin_email = "manual-disable-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Disable Admin", picture="", role="admin", plan="admin")
            member = User(
                email="manual-disable@example.com",
                name="Manual Disable",
                picture="",
                role="member",
                plan="free",
                manual_entitlement_enabled=True,
                manual_entitlement_plan="paid_consumer",
                manual_entitlement_note="Temporary",
            )
            db.session.add_all([admin, member])
            db.session.commit()
            member_id = member.id

        self.login_session(admin_email, "Disable Admin")
        response = self.client.post(
            f"/admin/users/{member_id}/manual-entitlement",
            data={
                "csrf_token": "token123",
                "manual_entitlement_plan": "paid_consumer",
                "manual_entitlement_note": "",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No manual override", response.data)
        with app.app_context():
            refreshed = db.session.get(User, member_id)
        self.assertFalse(refreshed.manual_entitlement_enabled)
        self.assertIsNone(refreshed.manual_entitlement_plan)
        self.assertFalse(app_module.user_has_api_access(refreshed))

    def test_invalid_manual_plan_is_rejected(self):
        admin_email = "manual-invalid-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Invalid Admin", picture="", role="admin", plan="admin")
            member = User(email="manual-invalid@example.com", name="Manual Invalid", picture="", role="member", plan="free")
            db.session.add_all([admin, member])
            db.session.commit()
            member_id = member.id

        self.login_session(admin_email, "Invalid Admin")
        response = self.client.post(
            f"/admin/users/{member_id}/manual-entitlement",
            data={
                "csrf_token": "token123",
                "manual_entitlement_enabled": "1",
                "manual_entitlement_plan": "admin",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a valid manual access level.", response.data)
        with app.app_context():
            refreshed = db.session.get(User, member_id)
        self.assertFalse(refreshed.manual_entitlement_enabled)

    def test_admin_user_filters_work_for_role_plan_and_manual_override(self):
        admin_email = "filters-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Filters Admin", picture="", role="admin", plan="admin")
            staff_user = User(email="filters-staff@example.com", name="Filters Staff", picture="", role="staff", plan="free")
            business_user = User(
                email="filters-business@example.com",
                name="Filters Business",
                picture="",
                role="member",
                plan="free",
                manual_entitlement_enabled=True,
                manual_entitlement_plan="business",
            )
            free_user = User(email="filters-free@example.com", name="Filters Free", picture="", role="member", plan="free")
            db.session.add_all([admin, staff_user, business_user, free_user])
            db.session.commit()

        self.login_session(admin_email, "Filters Admin")
        response = self.client.get("/admin/users?role=staff&plan=free&manual_override=all&q=Filters")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"filters-staff@example.com", response.data)
        self.assertNotIn(b"filters-business@example.com", response.data)

        manual_response = self.client.get("/admin/users?manual_override=active&q=Filters")
        self.assertEqual(manual_response.status_code, 200)
        self.assertIn(b"filters-business@example.com", manual_response.data)
        self.assertNotIn(b"filters-free@example.com", manual_response.data)

    def test_admin_user_edit_page_loads(self):
        admin_email = "edit-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            admin = User(email=admin_email, name="Edit Admin", picture="", role="admin", plan="admin")
            member = User(email="edit-member@example.com", name="Edit Member", picture="", role="member", plan="free")
            db.session.add_all([admin, member])
            db.session.commit()
            member_id = member.id

        self.login_session(admin_email, "Edit Admin")
        response = self.client.get(f"/admin/users/{member_id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"User editor", response.data)
        self.assertIn(b"Manual access override", response.data)

    def test_non_staff_cannot_access_admin_users_pages(self):
        with app.app_context():
            user = User(email="not-staff-users@example.com", name="Not Staff Users", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        self.login_session("not-staff-users@example.com", "Not Staff Users")
        list_response = self.client.get("/admin/users", follow_redirects=True)
        edit_response = self.client.get(f"/admin/users/{user_id}/edit", follow_redirects=True)

        self.assertEqual(list_response.status_code, 200)
        self.assertIn(b"Staff access required.", list_response.data)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn(b"Admin access required.", edit_response.data)

    def test_non_staff_cannot_see_staff_only_audit_or_search_activity(self):
        with app.app_context():
            user = User(email="plainuser@example.com", name="Plain User", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("plainuser@example.com", "Plain User")
        response = self.client.get("/dashboard", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Recent searches", response.data)
        self.assertNotIn(b"Audit trail", response.data)

    def test_account_and_plans_pages_use_unified_plan_and_access_labels(self):
        with app.app_context():
            staff = User(email="ui-staff@example.com", name="UI Staff", picture="", role="staff", plan="free")
            db.session.add(staff)
            db.session.commit()

        self.login_session("ui-staff@example.com", "UI Staff")

        account_response = self.client.get("/account")
        settings_response = self.client.get("/account/settings")
        plans_response = self.client.get("/plans")
        venues_response = self.client.get("/admin/venues")

        self.assertEqual(account_response.status_code, 200)
        self.assertIn(b"Free", account_response.data)
        self.assertIn(b"Staff", account_response.data)

        self.assertEqual(settings_response.status_code, 200)
        self.assertIn(b"Plan: Free", settings_response.data)
        self.assertIn(b"Access: Staff", settings_response.data)

        self.assertEqual(plans_response.status_code, 200)
        self.assertIn(b"on the Free plan with Staff access", plans_response.data)

        self.assertEqual(venues_response.status_code, 200)
        self.assertIn(b"planira-staff-dropdown-toggle planira-nav-link-active", venues_response.data)
        self.assertIn(b"Legacy data view", venues_response.data)

    def test_staff_user_can_access_admin_venues(self):
        with app.app_context():
            staff = User(email="staff-venues@example.com", name="Staff Venues", picture="", role="staff", plan="free")
            place = Place(name="Workspace Venue", slug="workspace-venue", town="Workspace Town", postcode="WK1 1AA")
            db.session.add_all([staff, place])
            db.session.commit()

        self.login_session("staff-venues@example.com", "Staff Venues")
        response = self.client.get("/admin/venues")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Search and review venue records", response.data)
        self.assertIn(b"Workspace Venue", response.data)
        self.assertIn(b"Staff workspace", response.data)
        self.assertIn(b"Venues", response.data)

    def test_non_staff_cannot_access_admin_venues(self):
        with app.app_context():
            user = User(email="member-venues@example.com", name="Member Venues", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("member-venues@example.com", "Member Venues")
        response = self.client.get("/admin/venues", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Staff access required.", response.data)
        self.assertNotIn(b"Search and review venue records", response.data)

    def test_admin_venues_filters_match_v2_fields(self):
        staff_email = "filter-staff@example.com"
        with app.app_context():
            staff = User(email=staff_email, name="Filter Staff", picture="", role="staff", plan="free")
            alpha = Place(
                name="Alpha Arms",
                slug="alpha-arms",
                address1="1 River Road",
                town="Northampton",
                postcode="NN1 1AA",
                status="verified",
                priority=5,
            )
            beta = Place(
                name="Beta Bar",
                slug="beta-bar",
                address1="2 Hill Street",
                town="Bedford",
                postcode="MK40 1BB",
                status="needs_call",
                priority=3,
            )
            gamma = Place(
                name="Gamma Gate",
                slug="gamma-gate",
                address1="3 Quiet Lane",
                town="Leicester",
                postcode="LE1 2CC",
                status="callback",
                priority=1,
            )
            db.session.add_all([staff, alpha, beta, gamma])
            db.session.flush()
            db.session.add_all(
                [
                    AccessibilityProfile(
                        place_id=alpha.id,
                        toilet_distance_from_bar_m=4.5,
                        confidence_score=82,
                        last_verified_at=app_module.datetime.now(app_module.timezone.utc),
                    ),
                    AccessibilityProfile(
                        place_id=gamma.id,
                        confidence_score=25,
                        last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=120),
                    ),
                ]
            )
            db.session.commit()

        self.login_session(staff_email, "Filter Staff")

        profile_response = self.client.get("/admin/venues?profile=missing_profile")
        self.assertEqual(profile_response.status_code, 200)
        self.assertIn(b"Beta Bar", profile_response.data)
        self.assertNotIn(b"Alpha Arms", profile_response.data)

        distance_response = self.client.get("/admin/venues?toilet_distance=known")
        self.assertEqual(distance_response.status_code, 200)
        self.assertIn(b"Alpha Arms", distance_response.data)
        self.assertIn(b"4.5m from bar", distance_response.data)
        self.assertNotIn(b"Beta Bar", distance_response.data)

        confidence_response = self.client.get("/admin/venues?confidence=high")
        self.assertEqual(confidence_response.status_code, 200)
        self.assertIn(b"Alpha Arms", confidence_response.data)
        self.assertNotIn(b"Gamma Gate", confidence_response.data)

        verified_response = self.client.get("/admin/venues?verified=stale")
        self.assertEqual(verified_response.status_code, 200)
        self.assertIn(b"Gamma Gate", verified_response.data)
        self.assertNotIn(b"Alpha Arms", verified_response.data)

        query_response = self.client.get("/admin/venues?q=River&town=Northampton&postcode=NN1&status=verified&sort=name")
        self.assertEqual(query_response.status_code, 200)
        self.assertIn(b"Alpha Arms", query_response.data)
        self.assertNotIn(b"Beta Bar", query_response.data)
        self.assertNotIn(b"Gamma Gate", query_response.data)

        quality_response = self.client.get("/admin/venues?quality_queue=missing_accessibility")
        self.assertEqual(quality_response.status_code, 200)
        self.assertIn(b"Beta Bar", quality_response.data)
        self.assertIn(b"Alpha Arms", quality_response.data)
        self.assertIn(b"key fields missing", quality_response.data)

    def test_staff_views_show_verification_metadata_without_public_simplification(self):
        staff_email = "metadata-staff@example.com"
        with app.app_context():
            staff = User(email=staff_email, name="Metadata Staff", picture="", role="staff", plan="free")
            place = Place(
                name="Metadata Arms",
                slug="metadata-arms",
                address1="8 Audit Lane",
                town="Leicester",
                postcode="LE1 8ZZ",
                status="verified",
            )
            db.session.add_all([staff, place])
            db.session.flush()
            db.session.add(
                AccessibilityProfile(
                    place_id=place.id,
                    confidence_score=91,
                    last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=10),
                    last_verified_by="auditor@example.com",
                    verified_by_user_id=staff.id,
                )
            )
            db.session.commit()

        self.login_session(staff_email, "Metadata Staff")
        venues_response = self.client.get("/admin/venues")
        data_response = self.client.get("/admin/data")

        self.assertEqual(venues_response.status_code, 200)
        self.assertIn(b"Metadata Arms", venues_response.data)
        self.assertIn(b"Confidence 91", venues_response.data)
        self.assertIn(b"Verified by: auditor@example.com", venues_response.data)
        self.assertIn(b"Last verified:", venues_response.data)

        self.assertEqual(data_response.status_code, 200)
        self.assertIn(b"Confidence 91", data_response.data)
        self.assertIn(b"Verified by: auditor@example.com", data_response.data)

    def test_call_worksheet_shows_quality_and_verification_cues(self):
        staff_email = "worksheet-staff@example.com"
        with app.app_context():
            staff = User(email=staff_email, name="Worksheet Staff", picture="", role="staff", plan="free")
            place = Place(name="Worksheet Venue", slug="worksheet-venue", town="Worksheet Town", status="needs_call")
            db.session.add_all([staff, place])
            db.session.flush()
            db.session.add(
                AccessibilityProfile(
                    place_id=place.id,
                    confidence_score=25,
                    last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=150),
                    toilets_available="unknown",
                    accessible_toilet="unknown",
                    step_free_entrance="unknown",
                    stairs_inside="unknown",
                )
            )
            db.session.commit()
            place_id = place.id

        self.login_session(staff_email, "Worksheet Staff")
        response = self.client.get(f"/admin/place/{place_id}/call")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Needs next action", response.data)
        self.assertIn(b"Needs checking", response.data)
        self.assertIn(b"Missing key fields", response.data)
        self.assertIn(b"Last checked", response.data)

    def test_account_api_key_creation_stores_hash_only_and_returns_raw_once(self):
        with app.app_context():
            user = User(email="api-owner@example.com", name="API Owner", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()

        self.login_session("api-owner@example.com", "API Owner")
        create_response = self.client.post(
            "/account/api-keys",
            data={"csrf_token": "token123", "label": "Primary developer key", "scopes": "places:read"},
        )

        self.assertEqual(create_response.status_code, 201)
        payload = create_response.get_json()
        raw_key = payload["raw_key"]
        self.assertTrue(raw_key.startswith("plnr_test_"))
        self.assertEqual(payload["copy_warning"], "Copy this API key now. The raw key is only shown once.")

        with app.app_context():
            saved_key = APIKey.query.filter_by(label="Primary developer key").first()

        self.assertIsNotNone(saved_key)
        self.assertNotEqual(saved_key.key_hash, raw_key)
        self.assertEqual(saved_key.key_hash, app_module.hash_api_key_value(raw_key))
        self.assertNotIn(raw_key.encode(), saved_key.key_hash.encode())

        list_response = self.client.get("/account/api-keys")

        self.assertEqual(list_response.status_code, 200)
        self.assertIn(b"Primary developer key", list_response.data)
        self.assertNotIn(raw_key.encode(), list_response.data)
        self.assertNotIn(saved_key.key_hash.encode(), list_response.data)

    def test_account_api_key_creation_requires_api_access_plan(self):
        with app.app_context():
            user = User(email="blocked-api-owner@example.com", name="Blocked Owner", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("blocked-api-owner@example.com", "Blocked Owner")
        response = self.client.post(
            "/account/api-keys",
            data={"csrf_token": "token123", "label": "Blocked key", "scopes": "places:read"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json(),
            {
                "error": "api_access_required",
                "message": "API access requires an active Planira API or Early Access plan.",
            },
        )
        with app.app_context():
            self.assertIsNone(APIKey.query.filter_by(label="Blocked key").first())

    def test_api_key_list_responses_do_not_expose_hashes(self):
        admin_email = "api-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            owner = User(email="hash-hide@example.com", name="Hash Hide", picture="", role="member", plan="business")
            admin = User(email=admin_email, name="API Admin", picture="", role="admin", plan="admin")
            db.session.add_all([owner, admin])
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(owner, label="Private key", scopes=["places:read"])
            db.session.commit()
            owner_id = owner.id
            key_hash = api_key.key_hash

        self.login_session(admin_email, "API Admin")
        response = self.client.get(f"/admin/users/{owner_id}/api-keys")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Private key", response.data)
        self.assertNotIn(key_hash.encode(), response.data)
        self.assertNotIn(raw_key.encode(), response.data)

    def test_valid_api_key_authenticates_and_records_lookup(self):
        with app.app_context():
            user = User(email="lookup@example.com", name="Lookup User", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Lookup key", scopes=["places:read"])
            db.session.commit()
            key_id = api_key.id

            result = app_module.authenticate_api_key(
                authorization_header=f"Bearer {raw_key}",
                required_scopes=["places:read"],
                endpoint="/internal/api/search",
                query="Blue venue",
                status_code=200,
            )

            saved_key = db.session.get(APIKey, key_id)
            lookup_event = db.session.query(APILookupEvent).filter_by(api_key_id=key_id).order_by(APILookupEvent.id.desc()).first()

        self.assertTrue(result["ok"])
        self.assertIsNotNone(saved_key.last_used_at)
        self.assertIsNotNone(lookup_event)
        self.assertEqual(lookup_event.endpoint, "/internal/api/search")
        self.assertEqual(lookup_event.query, "Blue venue")
        self.assertEqual(lookup_event.status_code, 200)

    def test_authenticate_api_key_requires_owner_api_access(self):
        with app.app_context():
            user = User(email="lost-access@example.com", name="Lost Access", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Former access key", scopes=["places:read"])
            db.session.commit()

            user.plan = "free"
            user.role = "member"
            db.session.commit()

            result = app_module.authenticate_api_key(raw_key=raw_key, endpoint="/internal/api/search", query="Two", status_code=200)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "api_access_required")

    def test_place_search_api_exposes_public_verification_fields_only(self):
        with app.app_context():
            user = User(email="public-api@example.com", name="Public API", picture="", role="member", plan="business")
            place = Place(
                name="API Arms",
                slug="api-arms",
                address1="44 Station Road",
                town="Northampton",
                postcode="NN1 9ZZ",
            )
            db.session.add_all([user, place])
            db.session.flush()
            db.session.add(
                AccessibilityProfile(
                    place_id=place.id,
                    accessible_toilet="yes",
                    step_free_entrance="yes",
                    confidence_score=82,
                    last_verified_at=app_module.datetime.now(app_module.timezone.utc) - app_module.timedelta(days=8),
                    last_verified_by="staff@example.com",
                    verified_by_user_id=user.id,
                )
            )
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Search key", scopes=["places:read"])
            db.session.add(api_key)
            db.session.commit()

        response = self.client.get(
            "/api/v1/places/search?q=API",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_status"], "Verified")
        self.assertIsNotNone(result["last_verified_at"])
        self.assertNotIn("verified_by_user_id", result)
        self.assertNotIn("last_verified_by", result)

    def test_place_search_api_blocks_keys_when_owner_lacks_api_access(self):
        with app.app_context():
            user = User(email="blocked-public-api@example.com", name="Blocked Public API", picture="", role="member", plan="business")
            place = Place(name="Blocked API Arms", slug="blocked-api-arms", town="Northampton")
            db.session.add_all([user, place])
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Soon blocked key", scopes=["places:read"])
            db.session.commit()

            user.plan = "free"
            user.role = "member"
            db.session.commit()

        response = self.client.get(
            "/api/v1/places/search?q=Blocked",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json(),
            {
                "error": "api_access_required",
                "message": "API access requires an active Planira API or Early Access plan.",
            },
        )

    def test_staff_api_key_can_create_and_update_place_accessibility_data(self):
        with app.app_context():
            staff = User(email="writer-staff@example.com", name="Writer Staff", picture="", role="staff", plan="free")
            db.session.add(staff)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(staff, label="Staff write key")
            db.session.commit()

        create_response = self.client.post(
            "/api/v1/places",
            json={
                "place": {
                    "name": "Writable Venue",
                    "town": "Northampton",
                    "venue_type": "pub",
                    "status": "needs_call",
                },
                "accessibility": {
                    "accessible_toilet": "yes",
                    "step_free_entrance": "no",
                    "source": "phone_verified",
                    "confidence_score": 74,
                },
                "mark_verified": True,
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(create_response.status_code, 201)
        created_payload = create_response.get_json()["place"]
        place_id = created_payload["id"]
        self.assertEqual(created_payload["name"], "Writable Venue")
        self.assertTrue(created_payload["verified"])

        update_response = self.client.patch(
            f"/api/v1/places/{place_id}",
            json={
                "accessibility": {
                    "step_free_entrance": "yes",
                    "public_comments": "Recently verified by staff",
                    "confidence_score": 91,
                }
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(update_response.status_code, 200)
        with app.app_context():
            saved_place = db.session.get(Place, place_id)
            saved_profile = saved_place.accessibility
        self.assertEqual(saved_place.status, "verified")
        self.assertEqual(saved_profile.accessible_toilet, "yes")
        self.assertEqual(saved_profile.step_free_entrance, "yes")
        self.assertEqual(saved_profile.source, "phone_verified")
        self.assertEqual(saved_profile.confidence_score, 91)
        self.assertEqual(saved_profile.public_comments, "Recently verified by staff")
        self.assertEqual(saved_profile.last_verified_by, "writer-staff@example.com")

    def test_staff_api_key_accepts_nested_verification_payload(self):
        with app.app_context():
            staff = User(email="nested-writer@example.com", name="Nested Writer", picture="", role="staff", plan="free")
            db.session.add(staff)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(staff, label="Nested staff write key")
            db.session.commit()

        response = self.client.post(
            "/api/v1/places",
            json={
                "place": {
                    "name": "Nested Venue",
                    "town": "Northampton",
                },
                "verification": {
                    "mark_verified": True,
                    "source": "staff_api",
                    "confidence_score": 88,
                },
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 201)
        place_id = response.get_json()["place"]["id"]
        with app.app_context():
            saved_place = db.session.get(Place, place_id)
            saved_profile = saved_place.accessibility
        self.assertEqual(saved_place.status, "verified")
        self.assertEqual(saved_profile.source, "staff_api")
        self.assertEqual(saved_profile.confidence_score, 88)
        self.assertEqual(saved_profile.last_verified_by, "nested-writer@example.com")

    def test_invalid_verification_payload_returns_400(self):
        with app.app_context():
            staff = User(email="invalid-verification@example.com", name="Invalid Verification", picture="", role="staff", plan="free")
            db.session.add(staff)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(staff, label="Invalid verification key")
            db.session.commit()

        response = self.client.post(
            "/api/v1/places",
            json={
                "place": {
                    "name": "Broken Verification Venue",
                    "town": "Northampton",
                },
                "verification": {
                    "mark_verified": True,
                    "unexpected": "nope",
                },
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {
                "error": "invalid_payload",
                "message": "Unknown verification field(s): unexpected.",
            },
        )

    def test_normal_api_key_cannot_write_place_data(self):
        with app.app_context():
            user = User(email="readonly-business@example.com", name="Readonly Business", picture="", role="member", plan="business")
            place = Place(name="Readonly Venue", slug="readonly-venue", town="Northampton")
            db.session.add_all([user, place])
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Readonly key")
            db.session.commit()
            place_id = place.id

        response = self.client.patch(
            f"/api/v1/places/{place_id}",
            json={"accessibility": {"accessible_toilet": "yes"}},
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json(),
            {
                "error": "invalid_api_key",
                "message": "This API key does not have access to this write endpoint.",
            },
        )

    def test_invalid_api_key_is_rejected_for_write_endpoint(self):
        response = self.client.patch(
            "/api/v1/places/999",
            json={"accessibility": {"accessible_toilet": "yes"}},
            headers={"Authorization": "Bearer plnr_test_abcdefghijklmnopqrstuvwxyz123456"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {
                "error": "invalid_api_key",
                "message": "The API key could not be verified.",
            },
        )

    def test_invalid_api_key_is_rejected(self):
        with app.app_context():
            result = app_module.authenticate_api_key(raw_key="plnr_test_abcdefghijklmnopqrstuvwxyz123456")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_api_key")

    def test_malformed_api_key_is_rejected(self):
        with app.app_context():
            result = app_module.authenticate_api_key(raw_key="not-a-real-key")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "malformed_api_key")

    def test_inactive_api_key_is_rejected(self):
        with app.app_context():
            user = User(email="inactive@example.com", name="Inactive User", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Inactive key")
            api_key.is_active = False
            db.session.commit()

            result = app_module.authenticate_api_key(raw_key=raw_key)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "inactive_api_key")

    def test_monthly_lookup_limit_blocks_usage_when_configured(self):
        with app.app_context():
            user = User(email="limited-api@example.com", name="Limited API", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Limited key", monthly_lookup_limit=1, lookup_credits=0)
            db.session.commit()
            db.session.add(APILookupEvent(api_key_id=api_key.id, user_id=user.id, endpoint="/internal/api/search", query="One", status_code=200))
            db.session.commit()

            result = app_module.authenticate_api_key(raw_key=raw_key, endpoint="/internal/api/search", query="Two", status_code=200)
            event_count = db.session.query(APILookupEvent).filter_by(api_key_id=api_key.id).count()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "monthly_lookup_limit_reached")
        self.assertEqual(event_count, 1)

    def test_lookup_credits_allow_extra_usage(self):
        with app.app_context():
            user = User(email="credit-api@example.com", name="Credit API", picture="", role="member", plan="business")
            db.session.add(user)
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Credit key", monthly_lookup_limit=1, lookup_credits=1)
            db.session.commit()
            db.session.add(APILookupEvent(api_key_id=api_key.id, user_id=user.id, endpoint="/internal/api/search", query="One", status_code=200))
            db.session.commit()

            result = app_module.authenticate_api_key(raw_key=raw_key, endpoint="/internal/api/search", query="Two", status_code=200)
            saved_key = db.session.get(APIKey, api_key.id)
            event_count = db.session.query(APILookupEvent).filter_by(api_key_id=api_key.id).count()

        self.assertTrue(result["ok"])
        self.assertEqual(saved_key.lookup_credits, 0)
        self.assertEqual(event_count, 2)

    def test_non_staff_cannot_manage_another_users_api_keys(self):
        with app.app_context():
            owner = User(email="key-owner@example.com", name="Key Owner", picture="", role="member", plan="business")
            outsider = User(email="outsider@example.com", name="Outsider", picture="", role="member", plan="free")
            db.session.add_all([owner, outsider])
            db.session.commit()
            owner_id = owner.id

        self.login_session("outsider@example.com", "Outsider")
        response = self.client.post(
            f"/admin/users/{owner_id}/api-keys",
            data={"csrf_token": "token123", "label": "Bad idea"},
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(APIKey.query.filter_by(user_id=owner_id).count(), 0)

    def test_developer_key_creation_requires_api_access_plan(self):
        with app.app_context():
            user = User(email="developer-blocked@example.com", name="Developer Blocked", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("developer-blocked@example.com", "Developer Blocked")
        response = self.client.post(
            "/developers/api-keys",
            data={"csrf_token": "token123", "label": "Blocked dev key"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"API access requires an active Planira API or Early Access plan.", response.data)
        with app.app_context():
            self.assertIsNone(APIKey.query.filter_by(label="Blocked dev key").first())

    def test_customer_api_key_management_is_admin_only(self):
        with app.app_context():
            owner = User(email="managed@example.com", name="Managed User", picture="", role="member", plan="business")
            admin = User(email="admin-managed@example.com", name="Admin User", picture="", role="admin", plan="admin")
            staff = User(email="staff@example.com", name="Staff User", picture="", role="staff", plan="free")
            db.session.add_all([owner, admin, staff])
            db.session.commit()
            owner_id = owner.id

        self.login_session("staff@example.com", "Staff User")
        denied_response = self.client.post(
            f"/admin/users/{owner_id}/api-keys",
            data={
                "csrf_token": "token123",
                "label": "Managed key",
                "scopes": "places:read",
                "monthly_lookup_limit": "5",
                "lookup_credits": "2",
            },
            follow_redirects=True,
        )
        self.assertEqual(denied_response.status_code, 200)
        self.assertIn(b"Admin access required.", denied_response.data)

        self.login_session("admin-managed@example.com", "Admin User")
        create_response = self.client.post(
            f"/admin/users/{owner_id}/api-keys",
            data={
                "csrf_token": "token123",
                "label": "Managed key",
                "scopes": "places:read",
                "monthly_lookup_limit": "5",
                "lookup_credits": "2",
            },
        )

        self.assertEqual(create_response.status_code, 201)
        payload = create_response.get_json()
        raw_key = payload["raw_key"]
        self.assertTrue(raw_key.startswith("plnr_test_"))
        key_id = payload["api_key"]["id"]

        list_response = self.client.get(f"/admin/users/{owner_id}/api-keys")
        self.assertEqual(list_response.status_code, 200)
        self.assertIn(b"Managed key", list_response.data)
        self.assertNotIn(raw_key.encode(), list_response.data)

        rename_response = self.client.post(
            f"/admin/users/{owner_id}/api-keys/{key_id}",
            data={"csrf_token": "token123", "action": "rename", "label": "Renamed key"},
        )
        deactivate_response = self.client.post(
            f"/admin/users/{owner_id}/api-keys/{key_id}",
            data={"csrf_token": "token123", "action": "deactivate"},
        )

        self.assertEqual(rename_response.status_code, 200)
        self.assertEqual(deactivate_response.status_code, 200)
        with app.app_context():
            saved_key = db.session.get(APIKey, key_id)
        self.assertEqual(saved_key.label, "Renamed key")
        self.assertFalse(saved_key.is_active)

    def test_venue_import_dry_run_reports_counts_without_writing(self):
        sample_fd, sample_path = tempfile.mkstemp(suffix=".json")
        os.close(sample_fd)
        with open(sample_path, "w", encoding="utf-8") as handle:
            handle.write(
                """
[
  {"name": "Pub One", "address": "1 High Street, Test Quarter, Test Town, Test County, NN1 1AA", "phone": "01234 000001"},
  {"name": "Pub Two", "address": "2 High Street, Test Quarter, Test Town, Test County, NN1 1AB", "phone": "01234 000002", "accessible_toilet": "yes"}
]
                """.strip()
            )

        try:
            with app.app_context():
                summary = venue_import.import_venues(
                    db_session=db.session,
                    place_model=Place,
                    profile_model=AccessibilityProfile,
                    json_file=sample_path,
                    apply=False,
                )

                self.assertEqual(summary["before"]["place"], 0)
                self.assertEqual(summary["after"]["place"], 2)
                self.assertEqual(summary["before"]["accessibility_profile"], 0)
                self.assertEqual(summary["after"]["accessibility_profile"], 1)
                self.assertEqual(Place.query.count(), 0)
                self.assertEqual(AccessibilityProfile.query.count(), 0)
        finally:
            os.unlink(sample_path)

    def test_venue_import_apply_inserts_places_and_skips_duplicates(self):
        sample_fd, sample_path = tempfile.mkstemp(suffix=".json")
        os.close(sample_fd)
        with open(sample_path, "w", encoding="utf-8") as handle:
            handle.write(
                """
[
  {"name": "Pub One", "address": "1 High Street, Test Quarter, Test Town, Test County, NN1 1AA", "phone": "01234 000001"},
  {"name": "Pub One", "address": "1 High Street, Test Quarter, Test Town, Test County, NN1 1AA", "phone": "01234 000099"},
  {"name": "Pub Two", "address": "2 High Street, Test Quarter, Test Town, Test County, NN1 1AB", "phone": "01234 000002", "accessible_toilet": "yes", "source": "seed"}
]
                """.strip()
            )

        try:
            with app.app_context():
                summary = venue_import.import_venues(
                    db_session=db.session,
                    place_model=Place,
                    profile_model=AccessibilityProfile,
                    json_file=sample_path,
                    apply=True,
                )

                self.assertEqual(summary["before"]["place"], 0)
                self.assertEqual(summary["after"]["place"], 2)
                self.assertEqual(summary["skipped"], 1)
                self.assertEqual(Place.query.count(), 2)
                self.assertEqual(AccessibilityProfile.query.count(), 1)
                pub_two = Place.query.filter_by(name="Pub Two").first()
                self.assertIsNotNone(pub_two)
                self.assertEqual(pub_two.status, "needs_call")
                self.assertEqual(pub_two.accessibility.accessible_toilet, "yes")
                self.assertEqual(User.query.count(), 0)
                self.assertEqual(APIKey.query.count(), 0)
                self.assertEqual(AuditLog.query.count(), 0)
        finally:
            os.unlink(sample_path)

    def test_ensure_accessibility_profiles_handles_empty_db(self):
        with app.app_context():
            created = app_module.ensure_accessibility_profiles(commit=True)

            self.assertEqual(created, 0)
            self.assertEqual(Place.query.count(), 0)
            self.assertEqual(AccessibilityProfile.query.count(), 0)

    def test_ensure_accessibility_profiles_matches_place_count(self):
        with app.app_context():
            place_one = Place(name="Profile Venue One", slug="profile-venue-one", town="Profile Town")
            place_two = Place(name="Profile Venue Two", slug="profile-venue-two", town="Profile Town")
            db.session.add_all([place_one, place_two])
            db.session.commit()

            created = app_module.ensure_accessibility_profiles(commit=True)

            self.assertEqual(created, 2)
            self.assertEqual(Place.query.count(), 2)
            self.assertEqual(AccessibilityProfile.query.count(), 2)
            self.assertTrue(all(place.accessibility is not None for place in Place.query.order_by(Place.id).all()))

    def test_ensure_accessibility_profiles_is_idempotent(self):
        with app.app_context():
            place_one = Place(name="Idempotent Venue One", slug="idempotent-venue-one", town="Repeat Town")
            place_two = Place(name="Idempotent Venue Two", slug="idempotent-venue-two", town="Repeat Town")
            db.session.add_all([place_one, place_two])
            db.session.commit()

            first_run = app_module.ensure_accessibility_profiles(commit=True)
            second_run = app_module.ensure_accessibility_profiles(commit=True)

            self.assertEqual(first_run, 2)
            self.assertEqual(second_run, 0)
            self.assertEqual(Place.query.count(), 2)
            self.assertEqual(AccessibilityProfile.query.count(), 2)


if __name__ == "__main__":
    unittest.main()
