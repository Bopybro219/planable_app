import sqlite3
from app import app, db, Place, AccessibilityProfile

SQLITE_PATH = "./instance/planable.db"

def row_to_dict(row):
    return {key: row[key] for key in row.keys()}

with app.app_context():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row

    places = conn.execute("SELECT * FROM place").fetchall()
    print(f"Found {len(places)} places in SQLite")

    for row in places:
        data = row_to_dict(row)

        if Place.query.get(data["id"]):
            continue

        allowed = {c.name for c in Place.__table__.columns}
        clean = {k: v for k, v in data.items() if k in allowed}

        db.session.add(Place(**clean))

    db.session.commit()
    print("Places migrated")

    try:
        profiles = conn.execute("SELECT * FROM accessibility_profile").fetchall()
        print(f"Found {len(profiles)} profiles in SQLite")

        for row in profiles:
            data = row_to_dict(row)

            allowed = {c.name for c in AccessibilityProfile.__table__.columns}
            clean = {k: v for k, v in data.items() if k in allowed}

            existing = AccessibilityProfile.query.filter_by(place_id=clean.get("place_id")).first()
            if existing:
                continue

            db.session.add(AccessibilityProfile(**clean))

        db.session.commit()
        print("Accessibility profiles migrated")

    except sqlite3.OperationalError as e:
        print(f"Skipped accessibility_profile: {e}")
