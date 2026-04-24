import json
import re
from app import app, db, Place, AccessibilityProfile
from slugify_fallback import slugify

JSON_FILE = "wetherspoons_locations.json"

def parse_address(address):
    parts = [p.strip() for p in address.split(",")]
    postcode_match = re.search(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", address, re.I)
    postcode = postcode_match.group(0).upper() if postcode_match else ""

    county = parts[-2] if len(parts) >= 3 else ""
    town = parts[-3] if len(parts) >= 4 else ""
    address1 = ", ".join(parts[:-3]) if len(parts) >= 4 else address

    return address1, town, county, postcode

with app.app_context():
    db.create_all()

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        places = json.load(f)

    imported = 0
    skipped = 0

    for item in places:
        name = item.get("name", "").strip()
        address = item.get("address", "").strip()
        phone = item.get("phone", "").strip()

        if not name or not address or phone == "</a>":
            skipped += 1
            continue

        existing = Place.query.filter_by(name=name, phone=phone).first()
        if existing:
            skipped += 1
            continue

        address1, town, county, postcode = parse_address(address)

        base_slug = slugify(f"{name} {town} {postcode}")
        slug = base_slug
        i = 2
        while Place.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{i}"
            i += 1

        place = Place(
            name=name,
            slug=slug,
            venue_type="pub",
            phone=phone,
            address1=address1,
            town=town,
            county=county,
            postcode=postcode,
            priority=3,
            status="needs_call",
        )

        db.session.add(place)
        db.session.flush()
        db.session.add(AccessibilityProfile(place=place))
        imported += 1

    db.session.commit()
    print(f"Imported: {imported}")
    print(f"Skipped: {skipped}")