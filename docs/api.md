# Planira API

Planira currently exposes a small JSON API for place lookup and staff-managed venue data updates.

## Base behavior

- All API requests use a Bearer token in the `Authorization` header.
- API keys are account-scoped.
- Raw API keys are shown only once when created. The server stores only a hash.
- Public/read access and write/edit access are intentionally separate.

Example header:

```http
Authorization: Bearer plnr_live_your_key_here
```

## Access model

### Read access

Users can use read endpoints when their account has API access:

- `paid`
- `business`
- `staff`
- `admin`

Free/member accounts do not have API access.

### Write access

Write endpoints are stricter:

- `staff` and `admin` API keys can create and update place data
- paid/business API users are read-only

This mirrors the staff dashboard permission model used in the web app.

## Authentication errors

Common auth responses:

### `401 Unauthorized`

Missing key:

```json
{
  "error": "missing_api_key",
  "message": "Send an API key using the Authorization Bearer header."
}
```

Invalid key:

```json
{
  "error": "invalid_api_key",
  "message": "The API key could not be verified."
}
```

### `403 Forbidden`

No API entitlement:

```json
{
  "error": "api_access_required",
  "message": "API access requires an active Planira API or Early Access plan."
}
```

Read key trying to write:

```json
{
  "error": "write_access_forbidden",
  "message": "This API key can read data but does not have permission to edit it."
}
```

Revoked key:

```json
{
  "error": "revoked_api_key",
  "message": "This API key is no longer active."
}
```

## Rate and usage behavior

- Lookups are tracked per API key.
- Read lookups update usage counters and create `APILookupEvent` records.
- Staff and some business access can have unlimited lookup allowance.
- Limited keys can consume extra lookup credits after the monthly allowance is used.
- If a key has no remaining allowance, the API returns `429`.

Example:

```json
{
  "error": "limit_reached",
  "message": "This API key has used its available lookup allowance."
}
```

## Endpoints

## `GET /api/v1/places/search`

Searches for matching places and returns public place/accessibility fields.

### Query parameters

At least one of these is required:

- `q`
- `town`
- `postcode`

### Example request

```bash
curl \
  -H "Authorization: Bearer plnr_live_your_key_here" \
  "http://127.0.0.1:5000/api/v1/places/search?q=pub&town=Northampton"
```

### Success response

```json
{
  "count": 1,
  "results": [
    {
      "id": 12,
      "name": "Example Arms",
      "slug": "example-arms",
      "address1": "1 High Street",
      "town": "Northampton",
      "county": "Northamptonshire",
      "postcode": "NN1 1AA",
      "phone": "01604 000000",
      "website": "https://example.com",
      "status": "verified",
      "venue_type": "pub",
      "latitude": 52.24,
      "longitude": -0.89,
      "accessible_toilet": "yes",
      "step_free_entrance": "yes",
      "toilets_available": "yes",
      "stairs_inside": "no",
      "baby_changing": "unknown",
      "lift_available": "unknown",
      "disabled_parking": "unknown",
      "toilet_location": null,
      "toilet_distance_from_bar": null,
      "toilet_distance_from_bar_m": null,
      "public_comments": null,
      "sensory_notes": null,
      "source": "phone_verified",
      "confidence_score": 82,
      "verified": true,
      "verification_status": "Verified",
      "last_verified_at": "2026-04-20T12:00:00+00:00"
    }
  ],
  "usage": {
    "lookups_used": 3,
    "lookup_credits_remaining": 0,
    "lookup_limit": 100
  }
}
```

### Notes

- Results are limited to 25 places.
- The response intentionally excludes staff-only verification details like `verified_by_user_id` and `last_verified_by`.

### Error responses

Missing query:

```json
{
  "error": "missing_query",
  "message": "Add a place query, town, or postcode before calling this endpoint."
}
```

No results:

```json
{
  "error": "no_results",
  "message": "No places matched that lookup."
}
```

## `POST /api/v1/places`

Creates a new `Place` and an `AccessibilityProfile`.

### Access

- staff/admin only

### Request body

Top-level keys are strictly validated. Allowed keys:

- `place`
- `accessibility`
- `verification`
- `mark_verified`

Unknown top-level fields return `400`.

### Minimal create payload

```json
{
  "place": {
    "name": "Writable Venue",
    "town": "Northampton"
  }
}
```

### Full create payload

```json
{
  "place": {
    "name": "Writable Venue",
    "town": "Northampton",
    "venue_type": "pub",
    "status": "needs_call",
    "phone": "01604 000000",
    "website": "https://example.com",
    "address1": "1 High Street",
    "county": "Northamptonshire",
    "postcode": "NN1 1AA",
    "priority": 3,
    "latitude": 52.24,
    "longitude": -0.89
  },
  "accessibility": {
    "toilets_available": "yes",
    "toilet_location": "Ground floor rear",
    "toilet_distance_from_bar": "About 10 metres",
    "toilet_distance_from_bar_m": 10,
    "accessible_toilet": "yes",
    "baby_changing": "no",
    "baby_changing_location": "Upstairs",
    "step_free_entrance": "yes",
    "stairs_inside": "no",
    "lift_available": "unknown",
    "disabled_parking": "partial",
    "sensory_notes": "Quiet on weekday afternoons.",
    "public_comments": "Staff confirmed access by phone.",
    "internal_notes": "Double-check event nights.",
    "source": "phone_verified",
    "confidence_score": 74
  },
  "mark_verified": true
}
```

### Nested verification payload

This is also supported:

```json
{
  "place": {
    "name": "Nested Venue",
    "town": "Northampton"
  },
  "verification": {
    "mark_verified": true,
    "source": "staff_api",
    "confidence_score": 88
  }
}
```

### Success response

- `201 Created`

```json
{
  "place": {
    "id": 25,
    "name": "Writable Venue",
    "slug": "writable-venue-northampton",
    "town": "Northampton",
    "status": "verified",
    "verified": true
  }
}
```

## `PATCH /api/v1/places/<id>`

Updates an existing `Place` and/or its `AccessibilityProfile`.

### Access

- staff/admin only

### Example request

```bash
curl -X PATCH \
  -H "Authorization: Bearer plnr_live_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "accessibility": {
      "step_free_entrance": "yes",
      "public_comments": "Recently verified by staff",
      "confidence_score": 91
    }
  }' \
  "http://127.0.0.1:5000/api/v1/places/25"
```

### Success response

- `200 OK`

```json
{
  "place": {
    "id": 25,
    "name": "Writable Venue",
    "status": "verified",
    "verified": true
  }
}
```

### Not found

```json
{
  "error": "not_found",
  "message": "Place not found."
}
```

## Write payload rules

## `place` fields

Allowed:

- `name`
- `venue_type`
- `phone`
- `website`
- `address1`
- `town`
- `county`
- `postcode`
- `priority`
- `status`
- `latitude`
- `longitude`

Rules:

- `name` is required when creating a place
- `status` must be one of `needs_call`, `calling`, `callback`, `verified`
- `priority` must be an integer from `1` to `5`
- `latitude` must be between `-90` and `90`
- `longitude` must be between `-180` and `180`

## `accessibility` fields

Allowed:

- `toilets_available`
- `toilet_location`
- `toilet_distance_from_bar`
- `toilet_distance_from_bar_m`
- `accessible_toilet`
- `baby_changing`
- `baby_changing_location`
- `step_free_entrance`
- `stairs_inside`
- `lift_available`
- `disabled_parking`
- `sensory_notes`
- `public_comments`
- `internal_notes`
- `source`
- `confidence_score`

Rules:

- choice fields must be one of `yes`, `no`, `unknown`, `partial`
- `confidence_score` must be an integer from `0` to `100`
- `toilet_distance_from_bar_m` must be a number from `0` to `5000`

## Blank and null handling

Write validation is intentionally strict:

- omit a field to leave the existing value unchanged
- send `null` only when a field is intentionally being cleared and that field supports nulls
- blank strings are rejected instead of silently overwriting good data

Example invalid payload:

```json
{
  "accessibility": {
    "public_comments": ""
  }
}
```

Response:

```json
{
  "error": "invalid_payload",
  "message": "public_comments cannot be blank. Omit it to keep the existing value or send null to clear it."
}
```

## Verification behavior

When `mark_verified` is `true`:

- `place.status` is set to `verified`
- `accessibility_profile.last_verified_at` is set to the current timestamp
- `accessibility_profile.last_verified_by` is set to the API key owner email
- `accessibility_profile.verified_by_user_id` is set to the API key owner user id

`mark_verified` must be a JSON boolean:

```json
{
  "verification": {
    "mark_verified": true
  }
}
```

If `mark_verified` is present but not a boolean, the API returns `400`.

## Validation failures

Write endpoints return `400` for invalid JSON or invalid field usage.

Examples:

Unknown place field:

```json
{
  "error": "invalid_payload",
  "message": "Unknown place field(s): city."
}
```

Unknown verification field:

```json
{
  "error": "invalid_payload",
  "message": "Unknown verification field(s): unexpected."
}
```

Conflicting verification values:

```json
{
  "error": "invalid_payload",
  "message": "confidence_score cannot differ between accessibility and verification payloads."
}
```

## Current implementation notes

- The API is defined in [app.py](/home/bopybro/Desktop/planable_app/app.py).
- Read endpoint: `api_places_search`
- Write endpoints: `api_create_place`, `api_update_place`
- Auth helper: `authenticate_api_key`
- Staff write guard: `authenticate_api_write_request`

