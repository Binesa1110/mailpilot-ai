import re
from dataclasses import dataclass

from pydantic import EmailStr, TypeAdapter, ValidationError

from .gmail import create_mime_message

SUPPORTED_CATEGORIES = {
    "professional",
    "formal",
    "academic",
    "business",
    "personal",
    "job application",
    "complaint",
    "apology",
    "follow-up",
}
ALWAYS_MANUAL_CATEGORIES = {"complaint", "apology"}


def normalize_email_address(value: str) -> str:
    local, separator, domain = value.rpartition("@")
    if not separator:
        return value
    return f"{local}@{domain.lower()}"

_PLACEHOLDER_PATTERN = re.compile(
    r"\[[^\]]{0,100}\]|\{\{?[^}]{1,100}\}?\}|<[^>]{1,100}>|\b(?:TODO|TBD|INSERT\s+(?:NAME|DETAIL|HERE)|YOUR\s+(?:NAME|COMPANY))\b",
    re.IGNORECASE,
)
_SENSITIVE_OR_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:social security|ssn|passport number|credit card|bank account|routing number|"
    r"account number|phone number|mobile number|home address|date of birth|dob|"
    r"password|medical record|diagnos\w*|prescription|attorney|lawyer|legal advice|"
    r"lawsuit|liability|contract|binding agreement|terms and conditions|refund|"
    r"compensation|invoice|payment|wire transfer|salary|guarantee|warranty|"
    r"commit(?:ment)?|promise|shall|agree to|accept(?:ing)? (?:the )?offer|"
    r"investment|loan|tax advice|send funds|repay|charge|pay you)\b",
    re.IGNORECASE,
)
_SENSITIVE_NUMBER_PATTERN = re.compile(
    r"(?<!\d)(?:\d{3}[- ]?\d{2}[- ]?\d{4}|\d{13,19})(?!\d)"
)
_UNSUPPORTED_CLAIM_PATTERN = re.compile(
    r"\b(?:as (?:we )?agreed|as promised|as discussed|i have attached|we have completed|"
    r"i confirm that|guaranteed|will deliver by|i have \d+\+? years|"
    r"our records show|you owe|your account is)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons)}


def evaluate_auto_send(
    draft: dict,
    settings: dict,
    daily_count: int,
    gmail_connected: bool,
    kill_switch_enabled: bool,
) -> PolicyDecision:
    reasons: list[str] = []
    if settings["mode"] != "auto_send":
        reasons.append("account_requires_manual_approval")
    if not settings["enabled"]:
        reasons.append("auto_send_not_enabled")
    if kill_switch_enabled:
        reasons.append("global_kill_switch_enabled")
    if draft.get("status") != "draft":
        reasons.append("draft_not_new")
    if not gmail_connected:
        reasons.append("gmail_send_permission_missing")

    recipient = draft.get("recipient", "")
    try:
        validated_recipient = TypeAdapter(EmailStr).validate_python(recipient)
    except (ValidationError, TypeError):
        validated_recipient = None
    if not validated_recipient or normalize_email_address(recipient) not in settings["recipient_allowlist"]:
        reasons.append("recipient_not_allowlisted_or_invalid")
    if "," in recipient or ";" in recipient or "\n" in recipient or "\r" in recipient:
        reasons.append("multiple_or_unusual_recipients")

    category = (draft.get("category") or "").strip().casefold()
    if category not in SUPPORTED_CATEGORIES:
        reasons.append("category_unclassified")
    if category not in settings["categories"]:
        reasons.append("category_not_allowlisted")
    if category in ALWAYS_MANUAL_CATEGORIES:
        reasons.append("high_risk_category_requires_manual_review")

    review = draft.get("review")
    if not isinstance(review, dict):
        reasons.append("review_missing")
    else:
        if review.get("risk_flags"):
            reasons.append("review_risk_flags_present")
        if review.get("notes"):
            reasons.append("review_notes_require_manual_review")
        review_text = " ".join(
            item for key in ("notes", "risk_flags") for item in review.get(key, [])
            if isinstance(item, str)
        )
        if re.search(r"\b(?:unsupported|unverified|unsubstantiated|fact|claim|verify|uncertain)\b", review_text, re.I):
            reasons.append("review_indicates_uncertainty")

    content = f"{draft.get('subject', '')}\n{draft.get('body', '')}"
    if _PLACEHOLDER_PATTERN.search(content):
        reasons.append("placeholder_detected")
    if _SENSITIVE_OR_HIGH_RISK_PATTERN.search(content):
        reasons.append("sensitive_or_high_risk_content")
    if _SENSITIVE_NUMBER_PATTERN.search(content):
        reasons.append("sensitive_number_detected")
    if _UNSUPPORTED_CLAIM_PATTERN.search(content):
        reasons.append("potential_unsupported_claim")

    if not draft.get("subject", "").strip() or not draft.get("body", "").strip():
        reasons.append("empty_message")
    try:
        create_mime_message(recipient, draft.get("subject", ""), draft.get("body", ""))
    except (ValueError, UnicodeError):
        reasons.append("message_invalid_or_oversized")

    if daily_count >= settings["daily_limit"]:
        reasons.append("daily_auto_send_limit_reached")

    return PolicyDecision(allowed=not reasons, reasons=tuple(dict.fromkeys(reasons)))
