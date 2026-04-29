import os
import json
import argparse
import requests

BASE_URL = os.getenv("PLANIRA_BASE_URL", "http://127.0.0.1:5000")
API_KEY = os.getenv("PLANIRA_API_KEY", "").strip()
EXPORT_FILE = "planira_export.json"

if not API_KEY:
    raise SystemExit("Missing PLANIRA_API_KEY environment variable.")

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def api_request(method, path, **kwargs):
    url = f"{BASE_URL}{path}"
    response = requests.request(method, url, headers=headers, timeout=20, **kwargs)

    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}

    if response.status_code >= 400:
        print(f"ERROR {response.status_code}: {data}")

    return response.status_code, data


def export_places(q="", limit=100):
    status, data = api_request(
        "GET",
        "/api/v1/places/search",
        params={"q": q, "limit": limit},
    )

    if status != 200:
        return

    results = data.get("results", [])

    with open(EXPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(results)} places to {EXPORT_FILE}")


def clean_payload(record):
    place_id = record.get("id")
    if not place_id:
        return None, None

    editable_place_fields = {
        "name",
        "town",
        "venue_type",
        "status",
    }

    editable_accessibility_fields = {
        "accessible_toilet",
        "step_free_entrance",
        "stairs_inside",
        "toilets_available",
        "baby_changing",
        "lift_available",
        "disabled_parking",
        "toilet_distance_from_bar",
        "public_comments",
        "sensory_notes",
        "source",
        "confidence_score",
    }

    place = {}
    accessibility = {}

    for key in editable_place_fields:
        value = record.get(key)
        if value not in ("", None):
            place[key] = value

    # Supports either flat export or nested accessibility export later
    source_accessibility = record.get("accessibility", record)

    for key in editable_accessibility_fields:
        value = source_accessibility.get(key)
        if value not in ("", None):
            accessibility[key] = value

    payload = {}
    if place:
        payload["place"] = place
    if accessibility:
        payload["accessibility"] = accessibility

    return place_id, payload


def update_places(apply=False):
    with open(EXPORT_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)

    updated = 0
    skipped = 0
    failed = 0

    for record in records:
        place_id, payload = clean_payload(record)

        if not place_id or not payload:
            skipped += 1
            continue

        print(f"\nPlace ID {place_id}")
        print(json.dumps(payload, indent=2))

        if not apply:
            continue

        status, data = api_request(
            "PATCH",
            f"/api/v1/places/{place_id}",
            json=payload,
        )

        if status == 200:
            updated += 1
        else:
            failed += 1

    if not apply:
        print("\nPreview only. Run with --apply to upload changes.")
    else:
        print(f"\nUpdated: {updated}, skipped: {skipped}, failed: {failed}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export")
    export_cmd.add_argument("--q", default="")
    export_cmd.add_argument("--limit", type=int, default=100)

    update_cmd = sub.add_parser("update")
    update_cmd.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    if args.command == "export":
        export_places(q=args.q, limit=args.limit)

    if args.command == "update":
        update_places(apply=args.apply)


if __name__ == "__main__":
    main()
