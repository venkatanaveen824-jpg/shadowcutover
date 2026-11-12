"""The 15-field account update form used by the legacy system of record."""

FORM_FIELDS = [
    "account_id",
    "first_name",
    "last_name",
    "date_of_birth",
    "phone",
    "email",
    "street",
    "city",
    "state",
    "zip",
    "mail_preference",
    "preferred_language",
    "request_type",
    "effective_date",
    "notes",
]

# Fields that must match exactly for a shadow result to count as correct.
# "notes" is free text, so it is reported but not scored.
SCORED_FIELDS = [f for f in FORM_FIELDS if f != "notes"]


def normalize(field: str, value) -> str:
    """Normalize a value before comparison so formatting noise is not counted as a mismatch."""
    if value is None:
        return ""
    v = str(value).strip()
    if field == "phone":
        return "".join(ch for ch in v if ch.isdigit())[-10:]
    if field in ("email", "mail_preference", "preferred_language", "request_type"):
        return v.lower()
    if field == "state":
        return v.upper()
    return " ".join(v.split()).lower()
