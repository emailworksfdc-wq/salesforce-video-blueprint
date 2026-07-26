"""
Data protection layer for Salesforce recording pipeline.

This module is the ONLY data-protection control in the project. An audit finding
noted "no PII redaction anywhere; the HTML embeds real record ids and field values
verbatim." This module provides detection and redaction at three points: raw
capture ingest, spec emission, and HTML rendering.

Detection strategy: bias toward false positives (annoying) over false negatives
(security failures). When in doubt, redact.

CRITICAL: Never log, print, or include the sensitive value itself in error
messages. An exception message containing the secret defeats the entire module.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Field-name patterns that always indicate sensitive data
_SENSITIVE_FIELD_PATTERNS = [
    r"pass(?:word)?",
    r"passwd",
    r"secret",
    r"token",
    r"bearer",
    r"ssn",
    r"social[_\s-]?security",
    r"credit[_\s-]?card",
    r"card[_\s-]?number",
    r"cardnum",
    r"cvv",
    r"cvc",
    r"\bpin\b",
    r"auth",
    r"api[_\s-]?key",
    r"apikey",
    r"private[_\s-]?key",
    r"routing",
    r"account[_\s-]?number",
    r"iban",
    r"swift",
    r"sort[_\s-]?code",
    r"tax[_\s-]?id",
    r"ein",
    r"nin",
    r"licen[cs]e",
    r"passport",
]

_SENSITIVE_FIELD_RE = re.compile(
    "|".join(f"(?:{p})" for p in _SENSITIVE_FIELD_PATTERNS),
    re.IGNORECASE,
)

# Value-based patterns
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)

# US SSN: 3-2-4 format with sanity exclusions
# Area (first 3): cannot be 000, 666, or 9xx
# Group (middle 2): cannot be 00
# Serial (last 4): cannot be 0000
_SSN_RE = re.compile(
    r"\b(?!000|666|9\d{2})([0-8]\d{2})-(?!00)(\d{2})-(?!0000)(\d{4})\b"
)

# E.164 phone (international) or US 10-digit
_PHONE_RE = re.compile(
    r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)

# Salesforce session/access token: 00D (org id prefix) followed by !
_SF_SESSION_TOKEN_RE = re.compile(r"\b00D[A-Za-z0-9]{12,15}![A-Za-z0-9._-]+\b")

# JWT: three base64url segments separated by dots
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)

# Private key blocks
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]+-----END [A-Z ]*PRIVATE KEY-----"
)

# AWS keys
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

# GitHub tokens
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")

# Slack tokens
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b")

# Bearer token pattern
_BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b")

# Credit card digits (13-19 digits with optional spaces/hyphens)
# This pattern alone is NOT sufficient — must be followed by Luhn check
_CARD_CANDIDATE_RE = re.compile(r"\b[\d\s-]{13,23}\b")


def _luhn_check(card_number: str) -> bool:
    """
    Validate a credit card number using the Luhn algorithm.

    The Luhn algorithm (mod-10 checksum):
    1. Starting from the rightmost digit (check digit), double every second digit.
    2. If doubling results in a two-digit number, sum those digits (e.g., 16 -> 1+6=7).
    3. Sum all the digits.
    4. If the total modulo 10 is 0, the number is valid.

    This is the difference between a usable redaction tool and one that flags
    every 16-digit order number or Salesforce id as a credit card.

    Args:
        card_number: Digit string (spaces/hyphens already stripped)

    Returns:
        True if the number passes Luhn check
    """
    if not card_number.isdigit():
        return False

    digits = [int(d) for d in card_number]
    # Reverse for easier indexing from the right
    digits.reverse()

    checksum = 0
    for i, digit in enumerate(digits):
        if i % 2 == 1:  # Every second digit from the right (0-indexed)
            doubled = digit * 2
            checksum += doubled if doubled < 10 else (doubled - 9)
        else:
            checksum += digit

    return checksum % 10 == 0


def _sf_id_checksum_valid(candidate: str) -> bool:
    """
    Validate a Salesforce 18-character id's checksum.

    The 18-char form has a 3-character base-32 checksum suffix encoding the
    uppercase-ness of the preceding 15 characters. Each checksum character
    encodes 5 bits (the case of 5 preceding chars).

    This validation prevents false positives: a random 18-char word in prose
    will not pass, so we do not mangle sentences.

    Algorithm:
    - Split the 15-char prefix into 3 chunks of 5.
    - For each chunk, compute a 5-bit value where bit i is 1 if char i is uppercase.
    - Map that 5-bit value to a base-32 character (A-Z, 0-5).
    - Compare the three computed characters to the actual suffix.

    Args:
        candidate: 18-character string

    Returns:
        True if the checksum is valid
    """
    if len(candidate) != 18:
        return False

    prefix = candidate[:15]
    suffix = candidate[15:]

    # Base-32 alphabet used by Salesforce
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

    computed = []
    for chunk_idx in range(3):
        chunk = prefix[chunk_idx * 5 : (chunk_idx + 1) * 5]
        bits = 0
        for i, char in enumerate(chunk):
            if char.isupper():
                bits |= 1 << i
        computed.append(alphabet[bits])

    return "".join(computed) == suffix


def _is_sf_id(candidate: str) -> bool:
    """
    Detect a valid Salesforce record id (15 or 18 characters).

    For 18-char ids, validates the checksum to avoid false positives.
    For 15-char ids, checks that it's alphanumeric and case-sensitive.

    A valid Salesforce id has a 3-character object key prefix (e.g., 500 for Case,
    001 for Account). The prefix is preserved in masked output for auditability.

    Args:
        candidate: String to test

    Returns:
        True if candidate is a valid Salesforce id
    """
    if not candidate or not candidate.isalnum():
        return False

    length = len(candidate)
    if length == 15:
        # 15-char ids are case-sensitive alphanumeric
        return True
    elif length == 18:
        # 18-char ids have a checksum — validate it
        return _sf_id_checksum_valid(candidate)
    else:
        return False


def is_sensitive_field(
    name: str | None,
    *,
    aria_label: str | None = None,
    input_type: str | None = None,
) -> bool:
    """
    Detect if a field name or label indicates sensitive data.

    Checks field name, aria-label, and input type against patterns for passwords,
    tokens, SSN, credit cards, API keys, and other secrets.

    Args:
        name: Field name (e.g., "password", "apiKey", "ssn")
        aria_label: ARIA label if available
        input_type: HTML input type (e.g., "password")

    Returns:
        True if the field is sensitive
    """
    if input_type == "password":
        return True

    for text in (name, aria_label):
        if text and _SENSITIVE_FIELD_RE.search(text):
            return True

    return False


def looks_like_secret_value(value: str | None) -> tuple[bool, str | None]:
    """
    Detect if a value looks like a secret, independent of field name.

    A field called "notes" can still contain a credit card number or token.
    This function checks value patterns: credit cards (Luhn-validated), SSNs,
    JWTs, Salesforce session tokens, private keys, AWS/GitHub/Slack tokens, etc.

    Args:
        value: String to inspect

    Returns:
        (is_sensitive, category) where category is e.g. "credit_card", "ssn", "jwt"
    """
    if not value:
        return (False, None)

    # Salesforce session token (highest value secret in this project)
    if _SF_SESSION_TOKEN_RE.search(value):
        return (True, "sf_session_token")

    # JWT
    if _JWT_RE.search(value):
        # Verify it decodes to something starting with {"alg"
        match = _JWT_RE.search(value)
        if match:
            try:
                import base64
                import json

                header = match.group(0).split(".")[0]
                # Add padding if needed
                padding = 4 - len(header) % 4
                if padding != 4:
                    header += "=" * padding
                decoded = base64.urlsafe_b64decode(header).decode("utf-8")
                data = json.loads(decoded)
                if "alg" in data:
                    return (True, "jwt")
            except Exception:
                # If decoding fails, not a valid JWT
                pass

    # Private key
    if _PRIVATE_KEY_RE.search(value):
        return (True, "private_key")

    # AWS key
    if _AWS_KEY_RE.search(value):
        return (True, "aws_key")

    # GitHub token
    if _GITHUB_TOKEN_RE.search(value):
        return (True, "github_token")

    # Slack token
    if _SLACK_TOKEN_RE.search(value):
        return (True, "slack_token")

    # Bearer token
    if _BEARER_TOKEN_RE.search(value):
        return (True, "bearer_token")

    # Credit card (Luhn-validated)
    for match in _CARD_CANDIDATE_RE.finditer(value):
        candidate = match.group(0)
        digits = re.sub(r"[\s-]", "", candidate)
        if 13 <= len(digits) <= 19 and _luhn_check(digits):
            return (True, "credit_card")

    # SSN (with sanity checks to avoid test data)
    if _SSN_RE.search(value):
        return (True, "ssn")

    return (False, None)


def redact_value(
    value: str | None,
    *,
    field_name: str | None = None,
    input_type: str | None = None,
) -> tuple[str | None, bool, str | None]:
    """
    Redact a field value if it's sensitive.

    Checks both field-name-based and value-based patterns. Returns the original
    value if not sensitive, or a redacted placeholder if sensitive.

    Args:
        value: The value to inspect
        field_name: Optional field name for field-name-based detection
        input_type: Optional input type (e.g., "password")

    Returns:
        (redacted_or_original, was_redacted, category)
    """
    if value is None:
        return (None, False, None)

    # Field-name-based detection
    if is_sensitive_field(field_name, input_type=input_type):
        category = "password_field" if input_type == "password" else "sensitive_field"
        return ("[REDACTED:{}]".format(category), True, category)

    # Value-based detection
    is_sensitive, category = looks_like_secret_value(value)
    if is_sensitive:
        return ("[REDACTED:{}]".format(category), True, category)

    return (value, False, None)


@dataclass
class RedactionPolicy:
    """
    Configurable redaction policy.

    Controls which categories of data are redacted and how. Default is strict
    (redact everything) to prevent accidental leaks. An unsalted hash of a
    low-entropy value (phone number) is trivially reversible by brute force,
    so hash_salt is mandatory when mode="hash".
    """

    redact_record_ids: bool = True
    redact_emails: bool = True
    redact_phones: bool = True
    redact_names: bool = False  # Name detection is hard; opt-in only
    mode: str = "mask"  # "mask" | "hash" | "drop"
    hash_salt: str = field(default_factory=lambda: "default-salt-change-me")

    @classmethod
    def strict(cls) -> RedactionPolicy:
        """Redact everything, mask mode (non-reversible)."""
        return cls(
            redact_record_ids=True,
            redact_emails=True,
            redact_phones=True,
            redact_names=False,
            mode="mask",
        )

    @classmethod
    def permissive(cls) -> RedactionPolicy:
        """Redact only secrets, keep record ids and emails."""
        return cls(
            redact_record_ids=False,
            redact_emails=False,
            redact_phones=False,
            redact_names=False,
            mode="mask",
        )


def redact_text(
    text: str, policy: RedactionPolicy | None = None
) -> tuple[str, list[str]]:
    """
    Redact sensitive data from free text.

    Scans for emails, phones, Salesforce record ids, credit cards, SSNs, tokens,
    and other secrets. Useful for validation messages, OCR output, and prose.

    Args:
        text: The text to scrub
        policy: Redaction policy (default: strict)

    Returns:
        (scrubbed_text, categories_found)
    """
    if policy is None:
        policy = RedactionPolicy.strict()

    categories: list[str] = []
    result = text

    # Collect all patterns to redact in one pass to avoid interaction
    replacements: list[tuple[int, int, str, str]] = []  # (start, end, replacement, category)

    # Salesforce session tokens (always redacted, highest priority)
    for match in _SF_SESSION_TOKEN_RE.finditer(result):
        category = "sf_session_token"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # JWTs (always redacted)
    for match in _JWT_RE.finditer(result):
        # Verify it's a valid JWT
        try:
            import base64
            import json

            header = match.group(0).split(".")[0]
            padding = 4 - len(header) % 4
            if padding != 4:
                header += "=" * padding
            decoded = base64.urlsafe_b64decode(header).decode("utf-8")
            data = json.loads(decoded)
            if "alg" in data:
                category = "jwt"
                categories.append(category)
                if policy.mode == "drop":
                    replacement = ""
                elif policy.mode == "hash":
                    hashed = hmac.new(
                        policy.hash_salt.encode("utf-8"),
                        match.group(0).encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()[:12]
                    replacement = f"[HASH:{category}:{hashed}]"
                else:  # mask
                    replacement = f"[REDACTED:{category}]"
                replacements.append((match.start(), match.end(), replacement, category))
        except Exception:
            pass

    # Private keys (always redacted)
    for match in _PRIVATE_KEY_RE.finditer(result):
        category = "private_key"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # AWS keys (always redacted)
    for match in _AWS_KEY_RE.finditer(result):
        category = "aws_key"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # GitHub tokens (always redacted)
    for match in _GITHUB_TOKEN_RE.finditer(result):
        category = "github_token"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # Slack tokens (always redacted)
    for match in _SLACK_TOKEN_RE.finditer(result):
        category = "slack_token"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # Bearer tokens (always redacted)
    for match in _BEARER_TOKEN_RE.finditer(result):
        category = "bearer_token"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # Credit cards (Luhn-validated, always redacted)
    for match in _CARD_CANDIDATE_RE.finditer(result):
        candidate = match.group(0)
        digits = re.sub(r"[\s-]", "", candidate)
        if 13 <= len(digits) <= 19 and _luhn_check(digits):
            category = "credit_card"
            categories.append(category)
            if policy.mode == "drop":
                replacement = ""
            elif policy.mode == "hash":
                hashed = hmac.new(
                    policy.hash_salt.encode("utf-8"),
                    digits.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:12]
                replacement = f"[HASH:{category}:{hashed}]"
            else:  # mask
                replacement = f"[REDACTED:{category}]"
            replacements.append((match.start(), match.end(), replacement, category))

    # SSNs (always redacted)
    for match in _SSN_RE.finditer(result):
        category = "ssn"
        categories.append(category)
        if policy.mode == "drop":
            replacement = ""
        elif policy.mode == "hash":
            hashed = hmac.new(
                policy.hash_salt.encode("utf-8"),
                match.group(0).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            replacement = f"[HASH:{category}:{hashed}]"
        else:  # mask
            replacement = f"[REDACTED:{category}]"
        replacements.append((match.start(), match.end(), replacement, category))

    # Emails (policy-gated)
    if policy.redact_emails:
        for match in _EMAIL_RE.finditer(result):
            category = "email"
            categories.append(category)
            if policy.mode == "drop":
                replacement = ""
            elif policy.mode == "hash":
                hashed = hmac.new(
                    policy.hash_salt.encode("utf-8"),
                    match.group(0).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:12]
                replacement = f"[HASH:{category}:{hashed}]"
            else:  # mask
                replacement = f"[REDACTED:{category}]"
            replacements.append((match.start(), match.end(), replacement, category))

    # Phones (policy-gated)
    if policy.redact_phones:
        for match in _PHONE_RE.finditer(result):
            category = "phone"
            categories.append(category)
            if policy.mode == "drop":
                replacement = ""
            elif policy.mode == "hash":
                hashed = hmac.new(
                    policy.hash_salt.encode("utf-8"),
                    match.group(0).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:12]
                replacement = f"[HASH:{category}:{hashed}]"
            else:  # mask
                replacement = f"[REDACTED:{category}]"
            replacements.append((match.start(), match.end(), replacement, category))

    # Salesforce record ids (policy-gated)
    if policy.redact_record_ids:
        for match in re.finditer(r"\b[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?\b", result):
            candidate = match.group(0)
            if _is_sf_id(candidate):
                category = "sf_record_id"
                categories.append(category)
                prefix = candidate[:3]
                if policy.mode == "drop":
                    replacement = ""
                elif policy.mode == "hash":
                    hashed = hmac.new(
                        policy.hash_salt.encode("utf-8"),
                        candidate.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()[:12]
                    replacement = f"{prefix}[HASH:{category}:{hashed}]"
                else:  # mask
                    replacement = f"{prefix}[REDACTED:{category}]"
                replacements.append((match.start(), match.end(), replacement, category))

    # Sort replacements by start position (descending) to preserve indices
    # Remove overlapping replacements (keep the first one found, which is typically more specific)
    non_overlapping = []
    for i, (start, end, replacement, category) in enumerate(sorted(replacements, key=lambda x: x[0])):
        # Check if this replacement overlaps with any already added
        overlaps = False
        for other_start, other_end, _, _ in non_overlapping:
            if (start < other_end and end > other_start):
                overlaps = True
                break
        if not overlaps:
            non_overlapping.append((start, end, replacement, category))

    # Sort by start position (descending) for safe replacement
    non_overlapping.sort(key=lambda x: x[0], reverse=True)

    # Apply all replacements
    for start, end, replacement, _ in non_overlapping:
        result = result[:start] + replacement + result[end:]

    # Deduplicate categories
    return (result, list(dict.fromkeys(categories)))


def redact_mapping(
    data: Mapping[str, Any], policy: RedactionPolicy | None = None
) -> tuple[dict, list[str]]:
    """
    Recursively redact sensitive data in a mapping.

    Scans keys and values, recursing into nested dicts and lists. Useful for
    scrubbing telemetry payloads, object snapshots, and JSON responses.

    Args:
        data: Mapping to scrub (dict, object snapshot, etc.)
        policy: Redaction policy (default: strict)

    Returns:
        (scrubbed_dict, categories_found)
    """
    if policy is None:
        policy = RedactionPolicy.strict()

    categories: list[str] = []
    result: dict[str, Any] = {}

    for key, value in data.items():
        # Check if key is sensitive
        if is_sensitive_field(key):
            categories.append("sensitive_field")
            if policy.mode == "drop":
                continue  # Skip this key entirely
            elif policy.mode == "hash":
                if isinstance(value, str):
                    hashed = hmac.new(
                        policy.hash_salt.encode("utf-8"),
                        value.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()[:12]
                    result[key] = f"[HASH:sensitive_field:{hashed}]"
                else:
                    result[key] = "[REDACTED:sensitive_field]"
            else:  # mask
                result[key] = "[REDACTED:sensitive_field]"
            continue

        # Recurse into nested structures
        if isinstance(value, dict):
            nested, nested_categories = redact_mapping(value, policy)
            result[key] = nested
            categories.extend(nested_categories)
        elif isinstance(value, list):
            redacted_list = []
            for item in value:
                if isinstance(item, dict):
                    nested, nested_categories = redact_mapping(item, policy)
                    redacted_list.append(nested)
                    categories.extend(nested_categories)
                elif isinstance(item, str):
                    redacted_str, str_categories = redact_text(item, policy)
                    redacted_list.append(redacted_str)
                    categories.extend(str_categories)
                else:
                    redacted_list.append(item)
            result[key] = redacted_list
        elif isinstance(value, str):
            redacted_str, str_categories = redact_text(value, policy)
            result[key] = redacted_str
            categories.extend(str_categories)
        else:
            result[key] = value

    # Deduplicate categories
    return (result, list(dict.fromkeys(categories)))


@dataclass
class RedactionReport:
    """
    Summary of redaction activity.

    Tracks counts per category so a run can prove what it scrubbed.
    """

    categories: dict[str, int] = field(default_factory=dict)

    def record(self, category: str) -> None:
        """Record that a value in this category was redacted."""
        self.categories[category] = self.categories.get(category, 0) + 1

    def summary(self) -> str:
        """Human-readable summary of redactions."""
        if not self.categories:
            return "No redactions performed."

        total = sum(self.categories.values())
        lines = [f"Redacted {total} value(s):"]
        for cat, count in sorted(self.categories.items()):
            lines.append(f"  - {cat}: {count}")
        return "\n".join(lines)


# ============================================================================
# URL query-parameter redaction
# ============================================================================

# Query/fragment parameters whose VALUE is a credential regardless of shape.
# `cli.py::_redact_sensitive_url` already covers sid/access_token/session on the
# operator-supplied --org-url. This is the same discipline applied to the URLs the
# recorder captured, which that function never sees, plus the parameters it misses.
_SENSITIVE_URL_PARAMS = (
    "sid",
    "access_token",
    "refresh_token",
    "id_token",
    "code",
    "session",
    "sessionid",
    "session_id",
    "sid_client",
    "assertion",
    "client_secret",
    "apikey",
    "api_key",
    "signature",
    "sig",
    "token",
    "auth",
    "password",
    "pw",
)

_URL_PARAM_RE = re.compile(
    r"([?&#])(" + "|".join(_SENSITIVE_URL_PARAMS) + r")=([^&#\s]+)",
    re.IGNORECASE,
)


def redact_url(url: str | None) -> tuple[str | None, list[str]]:
    """Strip credential-bearing query parameters from a URL.

    A captured `frontdoor.jsp?sid=...` URL is a live credential, not a location.
    Pattern-based value detection is not enough here: an OAuth `code` or an opaque
    `access_token` has no distinguishing shape, so the PARAMETER NAME is the signal
    and the value is replaced wholesale.

    The parameter name is preserved so the audit trail still shows which credential
    was present — consistent with `cli.py::_redact_sensitive_url`, which redacts
    rather than omits for the same reason.

    Args:
        url: The URL to scrub. None passes through.

    Returns:
        (scrubbed_url, categories_found)
    """
    if not url:
        return (url, [])

    categories: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        categories.append("url_credential")
        return f"{match.group(1)}{match.group(2)}=[REDACTED:url_credential]"

    scrubbed = _URL_PARAM_RE.sub(_replace, url)
    return (scrubbed, list(dict.fromkeys(categories)))


def pipeline_policy() -> RedactionPolicy:
    """The policy applied to captured evidence on its way into artifacts.

    Redacts secrets (tokens, keys, cards, SSNs) and emails. Deliberately does NOT
    redact Salesforce record ids or phone-shaped digits:

    - **Record ids are retained on purpose.** They are the audit trail. A blueprint
      that cannot say which record a step touched is not evidence, and the ids are
      already scoped to a single org that the report's reader has access to.
    - **Phone redaction is off because it is measurably wrong here.** The phone
      pattern matches any 10 consecutive digits, so `RedactionPolicy.strict()`
      rewrites `step 1234567890` to `step [REDACTED:phone]`. Corrupting step
      identifiers to hide digits that are usually not phone numbers trades a real
      defect for a speculative one.

    `RedactionPolicy.strict()` remains available for callers who want the maximal
    setting and accept the false positives.
    """
    return RedactionPolicy(
        redact_record_ids=False,
        redact_emails=True,
        redact_phones=False,
        redact_names=False,
        mode="mask",
    )


def scrub_collected_telemetry(
    events: Sequence[Any], snapshots: Sequence[Any]
) -> list[str]:
    """Scrub org records fetched *after* extraction, in place.

    Closes the live-org leak channel that the extraction choke point structurally
    cannot see. `ObjectSnapshot.before`/`.after` are whole records returned by
    `SalesforceRestClient.get_record`, and `TelemetryEvent.payload` holds raw SOQL
    rows — all obtained after extraction has finished. `spec_builder._derive_entities`
    interpolates those field values straight into entity evidence details, so with
    `--mode live --track-record Case:<id>` a token sitting in a Case field reached
    `agent-spec.json` verbatim. Measured, not hypothetical.

    `TelemetryRegistry` now scrubs on ingest, which is the stronger boundary — it
    covers every caller rather than the ones that remember. This function remains as
    defence in depth, still called from the CLI after collection and before
    `correlate_all`, for anything appended to a registry outside its ingest methods.

    It is idempotent: re-scrubbing already-clean data changes nothing and finds no
    categories. Callers that report what was redacted must therefore use the UNION of
    this return value and `TelemetryRegistry.redaction_categories`, or the run will
    stop saying the control fired once ingest has already done the work. `cli.py`
    does exactly that.

    Takes duck-typed sequences rather than importing the telemetry models, so this
    module stays free of a dependency on `telemetry.py` (which already imports
    nothing from here) and cannot create an import cycle.

    Returns the redaction categories found, for the run to report. Mutates the
    passed objects: the caller's registry IS the thing that must end up clean, and
    every downstream consumer reads from it.
    """
    policy = pipeline_policy()
    categories: list[str] = []

    for event in events:
        payload = getattr(event, "payload", None)
        if payload:
            event.payload, found = redact_mapping(payload, policy)
            categories.extend(found)

    for snapshot in snapshots:
        for attr in ("before", "after"):
            record = getattr(snapshot, attr, None)
            if record:
                scrubbed, found = redact_mapping(record, policy)
                setattr(snapshot, attr, scrubbed)
                categories.extend(found)

    return list(dict.fromkeys(categories))
