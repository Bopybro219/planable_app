import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module
import venue_import
from app import ADMIN_EMAILS, APILookupEvent, APIKey, AccessibilityProfile, AuditLog, Comment, Place, SearchEvent, User, app, db


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
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self._db_fd)

        app.config["TESTING"] = True
        app.config["ENVIRONMENT"] = "testing"
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{self._db_path}"
        app.config["_DB_SCHEMA_READY"] = False
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

        with app.app_context():
            rebind_sqlalchemy_for_current_config()

        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def login_session(self, email, name, picture=""):
        with self.client.session_transaction() as session:
            session["user"] = {"email": email, "name": name, "picture": picture}
            session["_csrf_token"] = "token123"

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
        self.assertIn(b"Before you search", response.data)
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
        self.assertIn(b"Start with a venue, town, or accessibility filter.", response.data)
        self.assertNotIn(b"Blank Search Venue", response.data)

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
            if existing < 12:
                for index in range(existing + 1, 13):
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
        self.assertIn("Showing 11-12 of 12 users.".replace("-", "\u2013").encode(), response.data)
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
            if existing < 11:
                for index in range(existing + 1, 12):
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
        self.assertIn("Showing 11-11 of 11 users.".replace("-", "\u2013").encode(), response.data)
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
            db.session.add_all([moderator, user, place, pending])
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
        self.assertIn(b"Only admins can change member search credits.", users_response.data)

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

    def test_account_api_key_creation_stores_hash_only_and_returns_raw_once(self):
        with app.app_context():
            user = User(email="api-owner@example.com", name="API Owner", picture="", role="member", plan="free")
            db.session.add(user)
            db.session.commit()

        self.login_session("api-owner@example.com", "API Owner")
        create_response = self.client.post(
            "/account/api-keys",
            data={"csrf_token": "token123", "label": "Primary developer key", "scopes": "search:read"},
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

    def test_api_key_list_responses_do_not_expose_hashes(self):
        admin_email = "api-admin@example.com"
        ADMIN_EMAILS.add(admin_email)
        with app.app_context():
            owner = User(email="hash-hide@example.com", name="Hash Hide", picture="", role="member", plan="business")
            admin = User(email=admin_email, name="API Admin", picture="", role="admin", plan="admin")
            db.session.add_all([owner, admin])
            db.session.commit()
            api_key, raw_key = app_module.create_api_key_for_user(owner, label="Private key", scopes=["search:read"])
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
            api_key, raw_key = app_module.create_api_key_for_user(user, label="Lookup key", scopes=["search:read"])
            db.session.commit()
            key_id = api_key.id

            result = app_module.authenticate_api_key(
                authorization_header=f"Bearer {raw_key}",
                required_scopes=["search:read"],
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

    def test_staff_can_manage_user_api_keys(self):
        with app.app_context():
            owner = User(email="managed@example.com", name="Managed User", picture="", role="member", plan="business")
            staff = User(email="staff@example.com", name="Staff User", picture="", role="staff", plan="free")
            db.session.add_all([owner, staff])
            db.session.commit()
            owner_id = owner.id

        self.login_session("staff@example.com", "Staff User")
        create_response = self.client.post(
            f"/admin/users/{owner_id}/api-keys",
            data={
                "csrf_token": "token123",
                "label": "Managed key",
                "scopes": "search:read,places:read",
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
