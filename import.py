import argparse

from flask_migrate import upgrade

from app import AccessibilityProfile, Place, app, db, should_auto_create_schema
from venue_import import JSON_FILE, import_venues, print_import_summary


def bootstrap_database():
    if should_auto_create_schema():
        db.create_all()
        return

    # Manual review: PostgreSQL and production imports should run against an
    # Alembic-managed schema rather than creating tables ad hoc.
    upgrade()


def build_parser():
    parser = argparse.ArgumentParser(description="Dry-run or apply a venue import from JSON.")
    parser.add_argument("--json-file", default=JSON_FILE, help="Path to the venue JSON file.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the import to the database. Without this flag the script runs as a dry-run only.",
    )
    return parser


def main():
    args = build_parser().parse_args()
    with app.app_context():
        bootstrap_database()
        summary = import_venues(
            db_session=db.session,
            place_model=Place,
            profile_model=AccessibilityProfile,
            json_file=args.json_file,
            apply=args.apply,
        )
        print_import_summary(summary)


if __name__ == "__main__":
    main()
