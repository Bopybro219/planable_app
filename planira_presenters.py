from datetime import datetime, timezone


APP_NAME = "Planira"
TAGLINE = "Know before you go."

PLAN_LABELS = {
    "visitor": "Visitor",
    "free": "Free",
    "paid": "Paid",
    "business": "Business",
    "admin": "Admin",
}

PLAN_DETAILS = {
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

PLACE_STATUS_LABELS = {
    "needs_call": "Needs phone check",
    "needs_review": "Needs review",
    "pending_review": "Pending review",
    "callback": "Callback needed",
    "calling": "In progress",
    "verified": "Verified",
}

VERIFICATION_SOURCE_LABELS = {
    "phone_verified": "Phone verified",
    "owner_verified": "Owner verified",
    "user_submitted": "Visitor note",
    "not_verified": "Needs verification",
}

PROFILE_VALUE_LABELS = {
    "yes": "Yes",
    "no": "No",
    "unknown": "Unknown",
}

SIGNAL_EXAMPLES = [
    {
        "label": "Looks straightforward",
        "tone": "easy",
        "copy": "Clear, recent details that suggest the visit should feel easier to manage.",
    },
    {
        "label": "Worth checking",
        "tone": "moderate",
        "copy": "Useful context when some details are partial, older, or still a little unclear.",
    },
    {
        "label": "Might be tricky",
        "tone": "difficult",
        "copy": "More careful planning may help here, especially if step-free access or facilities are limited.",
    },
]


def humanize_label(value, labels=None):
    mapping = labels or {}
    cleaned = (value or "").strip()
    if not cleaned:
        return "Unknown"
    if cleaned in mapping:
        return mapping[cleaned]
    return cleaned.replace("_", " ").title()


def humanize_plan_name(plan_name):
    return PLAN_LABELS.get(plan_name, humanize_label(plan_name))


def humanize_profile_value(value):
    return humanize_label(value, PROFILE_VALUE_LABELS)


def format_short_date(value):
    if not value:
        return None
    return value.strftime("%d %b %Y")


def build_verification_state(profile):
    verified_at = getattr(profile, "last_verified_at", None) if profile else None
    if not verified_at:
        return {
            "label": "Needs verification",
            "short_label": "Not verified yet",
            "tone": "moderate",
            "date": None,
        }

    now = datetime.now(timezone.utc)
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    age_days = max((now - verified_at).days, 0)
    if age_days <= 45:
        label = "Recently verified"
        tone = "easy"
    elif age_days <= 120:
        label = "Verified earlier"
        tone = "moderate"
    else:
        label = "Verification is getting old"
        tone = "difficult"

    date_label = format_short_date(verified_at)
    return {
        "label": label,
        "short_label": f"Verified {date_label}",
        "tone": tone,
        "date": date_label,
    }


def build_confidence_state(score):
    numeric = 0 if score is None else max(0, min(int(score), 100))
    if numeric >= 75:
        return {
            "label": "High confidence",
            "tone": "easy",
            "percent": numeric,
            "copy": "Recent details line up well, so this should take less extra checking.",
        }
    if numeric >= 45:
        return {
            "label": "Some confidence",
            "tone": "moderate",
            "percent": numeric,
            "copy": "There is useful detail here, but one or two answers still deserve a check.",
        }
    return {
        "label": "Low confidence",
        "tone": "difficult",
        "percent": numeric,
        "copy": "Important details are still thin or uncertain, so a backup plan may help.",
    }


def build_toilet_distance_summary(profile):
    if not profile:
        return {
            "label": "Toilet distance from bar",
            "value": "Unknown",
            "highlight": None,
            "warning": "Toilet distance from bar still needs confirming.",
        }

    raw_text = (getattr(profile, "toilet_distance_from_bar", "") or "").strip()
    metres = getattr(profile, "toilet_distance_from_bar_m", None)

    if metres is not None:
        rounded = round(float(metres), 1)
        if rounded <= 10:
            highlight = "Short walk to toilet"
        elif rounded <= 25:
            highlight = "Moderate walk to toilet"
        else:
            highlight = "Longer walk to toilet"
        return {
            "label": "Toilet distance from bar",
            "value": f"{rounded:g}m from bar",
            "highlight": highlight,
            "warning": None,
        }

    if raw_text:
        lowered = raw_text.lower()
        if any(term in lowered for term in ["near", "close", "short", "few steps"]):
            highlight = "Short walk to toilet"
        else:
            highlight = "Toilet distance from bar"
        return {
            "label": "Toilet distance from bar",
            "value": raw_text,
            "highlight": highlight,
            "warning": None,
        }

    return {
        "label": "Toilet distance from bar",
        "value": "Unknown",
        "highlight": None,
        "warning": "Toilet distance from bar still needs confirming.",
    }


def build_access_signal(profile):
    if not profile:
        confidence = build_confidence_state(0)
        verification = build_verification_state(None)
        return {
            "tone": "moderate",
            "label": "Worth checking",
            "summary": "We still need reliable accessibility details before this place feels easy to trust.",
            "explanation": "There is not enough verified information yet to make a confident decision.",
            "confidence": confidence,
            "confidence_label": confidence["label"],
            "verification": verification,
            "verification_label": verification["label"],
            "source_label": "Needs verification",
            "highlights": [],
            "warnings": ["Toilet distance from bar still needs confirming."],
            "distance": build_toilet_distance_summary(None),
            "detail_checks": [],
        }

    accessible = getattr(profile, "accessible_toilet", "unknown")
    step_free = getattr(profile, "step_free_entrance", "unknown")
    stairs = getattr(profile, "stairs_inside", "unknown")
    confidence = build_confidence_state(getattr(profile, "confidence_score", 0))
    verification = build_verification_state(profile)
    distance = build_toilet_distance_summary(profile)

    highlights = []
    warnings = []

    if distance["highlight"]:
        highlights.append(distance["highlight"])
    if accessible == "yes":
        highlights.append("Accessible toilet confirmed")
    elif accessible == "no":
        warnings.append("No accessible toilet confirmed")

    if step_free == "yes":
        highlights.append("Step-free entrance")
    elif step_free == "no":
        warnings.append("Steps at the entrance")

    if stairs == "yes":
        warnings.append("Stairs inside")

    if getattr(profile, "baby_changing", "unknown") == "yes":
        highlights.append("Baby changing available")

    if distance["warning"]:
        warnings.append(distance["warning"])

    if verification["tone"] == "difficult":
        warnings.append("Verification is older, so it is worth double-checking.")
    elif verification["tone"] == "moderate" and verification["label"] != "Needs verification":
        warnings.append("Useful record, but the last verification is not especially recent.")
    elif verification["label"] == "Needs verification":
        warnings.append("Needs a fresh verification before it feels dependable.")

    if accessible == "yes" and step_free == "yes" and stairs in {"no", "unknown"} and confidence["percent"] >= 75:
        label = "Looks straightforward"
        tone = "easy"
        summary = "Step-free entry and toilet details line up well, so this looks simpler to plan around."
    elif accessible == "no" or step_free == "no" or stairs == "yes":
        label = "Might be tricky"
        tone = "difficult"
        summary = "Some access details suggest this visit could take more planning before you set off."
    else:
        label = "Worth checking"
        tone = "moderate"
        summary = "There is useful guidance here, but a few details still need checking before you rely on it."

    detail_checks = [
        {
            "label": "Toilet distance from bar",
            "value": distance["value"],
            "note": distance["highlight"] or distance["warning"],
            "emphasis": "priority",
        },
        {
            "label": "Toilets",
            "value": humanize_profile_value(getattr(profile, "toilets_available", "unknown")),
            "note": getattr(profile, "toilet_location", None) or "Location unknown",
            "emphasis": None,
        },
        {
            "label": "Accessible toilet",
            "value": humanize_profile_value(accessible),
            "note": None,
            "emphasis": None,
        },
        {
            "label": "Baby changing",
            "value": humanize_profile_value(getattr(profile, "baby_changing", "unknown")),
            "note": getattr(profile, "baby_changing_location", None) or "Location unknown",
            "emphasis": None,
        },
        {
            "label": "Step-free entrance",
            "value": humanize_profile_value(step_free),
            "note": None,
            "emphasis": None,
        },
        {
            "label": "Stairs inside",
            "value": humanize_profile_value(stairs),
            "note": None,
            "emphasis": None,
        },
        {
            "label": "Lift",
            "value": humanize_profile_value(getattr(profile, "lift_available", "unknown")),
            "note": None,
            "emphasis": None,
        },
        {
            "label": "Disabled parking",
            "value": humanize_profile_value(getattr(profile, "disabled_parking", "unknown")),
            "note": None,
            "emphasis": None,
        },
    ]

    return {
        "tone": tone,
        "label": label,
        "summary": summary,
        "explanation": summary,
        "confidence": confidence,
        "confidence_label": confidence["label"],
        "verification": verification,
        "verification_label": verification["label"],
        "source_label": humanize_label(getattr(profile, "source", "not_verified"), VERIFICATION_SOURCE_LABELS),
        "highlights": highlights[:4],
        "warnings": warnings[:4],
        "distance": distance,
        "detail_checks": detail_checks,
    }


def build_place_card(place):
    profile = getattr(place, "accessibility", None)
    signal = build_access_signal(profile)
    key_bits = list(signal["highlights"])[:3]

    return {
        "place": place,
        "signal": signal,
        "verified_text": signal["verification"]["short_label"],
        "key_bits": key_bits,
    }


def build_signal_examples():
    return SIGNAL_EXAMPLES
