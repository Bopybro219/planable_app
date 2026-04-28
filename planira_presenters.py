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
        "label": "Easy",
        "tone": "easy",
        "copy": "Clear, recent details that suggest the visit should feel easier to manage.",
    },
    {
        "label": "Worth checking",
        "tone": "moderate",
        "copy": "Useful context when some details are partial, older, or still a little unclear.",
    },
    {
        "label": "Tricky",
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


def format_relative_time(value, *, now=None):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    delta = current - value
    total_seconds = max(int(delta.total_seconds()), 0)
    days = total_seconds // 86400

    if days == 0:
        if total_seconds < 3600:
            minutes = max(total_seconds // 60, 1)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = max(total_seconds // 3600, 1)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days == 1:
        return "1 day ago"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        weeks = max(round(days / 7), 1)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = max(round(days / 30), 1)
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = max(round(days / 365), 1)
    return f"{years} year{'s' if years != 1 else ''} ago"


def verification_status(profile):
    verified_at = getattr(profile, "last_verified_at", None) if profile else None
    if not verified_at:
        return {
            "status": "Not verified yet",
            "label": "Not verified yet",
            "badge_label": "Not verified yet",
            "short_label": "Not verified yet",
            "tone": "neutral",
            "badge_class": "badge-neutral",
            "verified": False,
            "date": None,
            "relative_time": None,
            "last_checked_copy": "Not checked yet",
        }

    now = datetime.now(timezone.utc)
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    age_days = max((now - verified_at).days, 0)
    if age_days <= 45:
        status = "Verified"
        tone = "easy"
        badge_class = "badge-verified"
    elif age_days <= 120:
        status = "Checked recently"
        tone = "moderate"
        badge_class = "badge-warning"
    else:
        status = "Needs checking"
        tone = "difficult"
        badge_class = "badge-warning"

    date_label = format_short_date(verified_at)
    relative_time = format_relative_time(verified_at, now=now)
    return {
        "status": status,
        "label": status,
        "badge_label": status,
        "short_label": status if not relative_time else f"{status} · {relative_time}",
        "tone": tone,
        "badge_class": badge_class,
        "verified": status == "Verified",
        "date": date_label,
        "relative_time": relative_time,
        "last_checked_copy": f"Last checked {relative_time}" if relative_time else f"Last checked {date_label}",
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


def build_quick_answer_items(profile, distance):
    if not profile:
        return [
            {"tone": "warning", "icon": "?", "label": "Entrance still needs checking"},
            {"tone": "warning", "icon": "?", "label": "Toilet details still need checking"},
            {"tone": "warning", "icon": "!", "label": "Layout may vary"},
        ]

    step_free = getattr(profile, "step_free_entrance", "unknown")
    accessible = getattr(profile, "accessible_toilet", "unknown")
    toilets_available = getattr(profile, "toilets_available", "unknown")
    stairs = getattr(profile, "stairs_inside", "unknown")

    items = []
    if step_free == "yes":
        items.append({"tone": "positive", "icon": "check", "label": "Step-free entrance"})
    elif step_free == "no":
        items.append({"tone": "warning", "icon": "warning", "label": "Entrance has steps"})
    else:
        items.append({"tone": "warning", "icon": "?", "label": "Entrance still needs checking"})

    if accessible == "yes":
        items.append({"tone": "positive", "icon": "check", "label": "Accessible toilet confirmed"})
    elif toilets_available == "yes":
        items.append({"tone": "positive", "icon": "check", "label": "Toilet available"})
    elif accessible == "no":
        items.append({"tone": "warning", "icon": "warning", "label": "No accessible toilet confirmed"})
    else:
        items.append({"tone": "warning", "icon": "?", "label": "Toilet details still need checking"})

    if stairs == "yes":
        items.append({"tone": "warning", "icon": "warning", "label": "Stairs inside"})
    elif distance["value"] != "Unknown":
        items.append({"tone": "neutral", "icon": "info", "label": distance["value"]})
    else:
        items.append({"tone": "warning", "icon": "warning", "label": "Layout may vary"})

    return items[:3]


def build_key_facts(profile, distance):
    if not profile:
        return ["Details still being checked"]

    facts = []
    step_free = getattr(profile, "step_free_entrance", "unknown")
    accessible = getattr(profile, "accessible_toilet", "unknown")
    toilets_available = getattr(profile, "toilets_available", "unknown")

    if step_free == "yes":
        facts.append("Step-free entrance")
    elif step_free == "no":
        facts.append("Entrance has steps")

    if accessible == "yes":
        facts.append("Accessible toilet")
    elif accessible == "no":
        facts.append("No accessible toilet confirmed")
    elif toilets_available == "yes":
        facts.append("Toilet available")

    if distance["value"] != "Unknown":
        facts.append(distance["value"])

    if not facts:
        facts.append("More access detail needed")
    return facts[:3]


def build_access_signal(profile):
    if not profile:
        confidence = build_confidence_state(0)
        verification = verification_status(None)
        distance = build_toilet_distance_summary(None)
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
            "distance": distance,
            "quick_answer_items": build_quick_answer_items(None, distance),
            "key_facts": build_key_facts(None, distance),
            "detail_checks": [],
        }

    accessible = getattr(profile, "accessible_toilet", "unknown")
    step_free = getattr(profile, "step_free_entrance", "unknown")
    stairs = getattr(profile, "stairs_inside", "unknown")
    confidence = build_confidence_state(getattr(profile, "confidence_score", 0))
    verification = verification_status(profile)
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
        label = "Easy"
        tone = "easy"
        summary = "Step-free entry and toilet details line up well, so this looks simpler to plan around."
    elif accessible == "no" or step_free == "no" or stairs == "yes":
        label = "Tricky"
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
        "quick_answer_items": build_quick_answer_items(profile, distance),
        "key_facts": build_key_facts(profile, distance),
        "detail_checks": detail_checks,
    }


def build_place_card(place):
    profile = getattr(place, "accessibility", None)
    signal = build_access_signal(profile)

    return {
        "place": place,
        "signal": signal,
        "verified_text": signal["verification"]["badge_label"],
        "key_bits": signal["key_facts"],
    }


def build_signal_examples():
    return SIGNAL_EXAMPLES
