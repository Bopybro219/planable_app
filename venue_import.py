import json
import re

from slugify_fallback import slugify

JSON_FILE = "wetherspoons_locations.json"
PROFILE_FIELD_MAP = {
    "toilets_available": "toilets_available",
    "toilet_location": "toilet_location",
    "toilet_distance_from_bar": "toilet_distance_from_bar",
    "toilet_distance_from_bar_m": "toilet_distance_from_bar_m",
    "accessible_toilet": "accessible_toilet",
    "baby_changing": "baby_changing",
    "baby_changing_location": "baby_changing_location",
    "step_free_entrance": "step_free_entrance",
    "stairs_inside": "stairs_inside",
    "lift_available": "lift_available",
    "disabled_parking": "disabled_parking",
    "sensory_notes": "sensory_notes",
    "public_comments": "public_comments",
    "internal_notes": "internal_notes",
    "source": "source",
}


def parse_address(address):
    parts = [p.strip() for p in (address or "").split(",")]
    postcode_match = re.search(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", address or "", re.I)
    postcode = postcode_match.group(0).upper() if postcode_match else ""

    county = parts[-2] if len(parts) >= 3 else ""
    town = parts[-3] if len(parts) >= 4 else ""
    address1 = ", ".join(parts[:-3]) if len(parts) >= 4 else (address or "").strip()

    return address1, town, county, postcode


def normalize_identity_value(value):
    return " ".join((value or "").strip().lower().split())


def build_place_identity(name, address1, postcode):
    return (
        normalize_identity_value(name),
        normalize_identity_value(address1),
        normalize_identity_value(postcode),
    )


def extract_profile_payload(item):
    payload = {}
    for item_key, model_key in PROFILE_FIELD_MAP.items():
        value = item.get(item_key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if value == "" or value == [] or value == {}:
            continue
        payload[model_key] = value
    return payload


def load_places_from_json(json_file):
    with open(json_file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def print_import_summary(summary):
    print(f"Mode: {'apply' if summary['apply'] else 'dry-run'}")
    print(f"Input file: {summary['json_file']}")
    print(f"Rows scanned: {summary['scanned']}")
    print(f"Would import: {summary['would_import']}")
    print(f"Skipped duplicates/invalid: {summary['skipped']}")
    print(f"Place count: {summary['before']['place']} -> {summary['after']['place']}")
    print(
        "Accessibility profile count: "
        f"{summary['before']['accessibility_profile']} -> {summary['after']['accessibility_profile']}"
    )


def import_venues(
    *,
    db_session,
    place_model,
    profile_model,
    json_file=JSON_FILE,
    apply=False,
):
    places = load_places_from_json(json_file)
    before_place_count = db_session.query(place_model).count()
    before_profile_count = db_session.query(profile_model).count()

    existing_places = db_session.query(
        place_model.name,
        place_model.address1,
        place_model.postcode,
        place_model.slug,
    ).all()
    existing_keys = {
        build_place_identity(name, address1, postcode)
        for name, address1, postcode, _slug in existing_places
    }
    existing_slugs = {slug for _name, _address1, _postcode, slug in existing_places if slug}

    would_import = 0
    skipped = 0
    profile_creations = 0

    try:
        for item in places:
            name = (item.get("name") or "").strip()
            address = (item.get("address") or "").strip()
            phone = (item.get("phone") or "").strip()

            if not name or not address or phone == "</a>":
                skipped += 1
                continue

            address1, town, county, postcode = parse_address(address)
            identity = build_place_identity(name, address1, postcode)
            if identity in existing_keys:
                skipped += 1
                continue

            base_slug = slugify(f"{name} {town} {postcode}".strip())
            slug = base_slug
            suffix = 2
            while slug in existing_slugs:
                slug = f"{base_slug}-{suffix}"
                suffix += 1

            profile_payload = extract_profile_payload(item)
            would_import += 1
            existing_keys.add(identity)
            existing_slugs.add(slug)

            if not apply:
                if profile_payload:
                    profile_creations += 1
                continue

            place = place_model(
                name=name,
                slug=slug,
                venue_type=(item.get("venue_type") or "pub").strip() or "pub",
                phone=phone or None,
                address1=address1 or None,
                town=town or None,
                county=county or None,
                postcode=postcode or None,
                priority=3,
                status="needs_call",
            )
            db_session.add(place)
            db_session.flush()

            if profile_payload:
                db_session.add(profile_model(place=place, **profile_payload))
                profile_creations += 1

        if apply:
            db_session.commit()
        else:
            db_session.rollback()
    except Exception:
        db_session.rollback()
        raise

    after_place_count = db_session.query(place_model).count() if apply else before_place_count + would_import
    after_profile_count = (
        db_session.query(profile_model).count() if apply else before_profile_count + profile_creations
    )

    return {
        "apply": apply,
        "json_file": json_file,
        "scanned": len(places),
        "would_import": would_import,
        "skipped": skipped,
        "before": {
            "place": before_place_count,
            "accessibility_profile": before_profile_count,
        },
        "after": {
            "place": after_place_count,
            "accessibility_profile": after_profile_count,
        },
    }
