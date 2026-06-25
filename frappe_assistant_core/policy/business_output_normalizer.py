# Frappe Assistant Core - Platia 360 Business Output Normalizer
# Copyright (C) 2026 Paul Clinton

import re
from typing import Any
import frappe
from frappe.utils import now

HIDDEN_FIELDS = {
    "source_app",
    "package",
    "branch",
    "git_branch",
    "technical_module",
    "implementation_path",
    "stack",
    "internal_app_name",
    "raw_app_name",
    "repo",
    "repository",
}

RAW_TO_SAFE_TOOL_NAMES = {
    "get_document": "get_business_record",
    "list_documents": "list_business_records",
    "generate_report": "generate_business_report",
    "get_doctype_info": "platia_object_type_summary",
}

SAFE_TO_RAW_TOOL_NAMES = {v: k for k, v in RAW_TO_SAFE_TOOL_NAMES.items()}

# Matches a fenced code block (```...```) or inline code (`...`).
# Fenced blocks are matched non-greedily across newlines; inline code
# excludes backticks and newlines so it doesn't swallow whole paragraphs.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

# Collapses two-or-more consecutive copies of a brand token that may have
# been produced by earlier (already-stored) corruption or by future alias
# overlap, e.g. "Platia 360 Platia 360 Platia 360 Insights" -> "Platia 360 Insights".
_BRAND = "Platia 360"
_BRAND_REPEAT_RE = re.compile(
    r"(?:" + re.escape(_BRAND) + r"\s+){2,}", re.IGNORECASE
)


def get_business_alias_map():
    """Build a comprehensive dictionary of legacy/technical names to business-safe terms."""
    alias_map = {}

    def add_variations(key, val):
        if not key:
            return
        variants = set()
        variants.add(key)
        variants.add(key.lower())
        variants.add(key.upper())

        spaced = key.replace("_", " ").replace("-", " ")
        variants.update({spaced, spaced.lower(), spaced.upper(), spaced.title()})

        kebab = key.replace("_", "-").replace(" ", "-")
        variants.update({kebab, kebab.lower(), kebab.upper()})

        snake = key.replace("-", "_").replace(" ", "_")
        variants.update({snake, snake.lower(), snake.upper()})

        for v in variants:
            # Don't let a shorter/earlier alias's OWN replacement value collide
            # with a later key. We only register raw legacy text as a key here;
            # replacement values are never fed back into the map.
            alias_map[v] = val

    base_rules = [
        ("frappe_assistant_core", "Platia 360 AI Assistant"),
        ("frappe", "Platia 360"),
        ("erpnext", "Platia 360"),
        ("doctype", "object type"),
        ("doctypes", "object types"),
        ("doc_type", "object type"),
        ("doc_types", "object types"),
        ("frapp", "Platia 360"),
        ("frap", "Platia 360"),
        ("erpnxt", "Platia 360"),
        ("erp next", "Platia 360"),
    ]

    for key, val in base_rules:
        add_variations(key, val)

    # Installed apps. Business name is built WITHOUT reusing words that are
    # already brand-mapped (e.g. don't produce "Platia 360 Frappe Insights" -
    # produce "Platia 360 Insights"), so the replacement value can never
    # re-trigger another rule in this same map.
    try:
        already_mapped_words = {"frappe", "erpnext", "platia", "360"}
        for app in frappe.get_installed_apps():
            if app in ("frappe", "erpnext", "frappe_assistant_core"):
                continue
            parts = app.split("_")
            cleaned_parts = []
            for p in parts:
                if p.lower() in already_mapped_words:
                    continue  # drop "frappe" out of "frappe_insights" etc.
                if p.lower() in ("ai", "crm", "hr", "mcp"):
                    cleaned_parts.append(p.upper())
                else:
                    cleaned_parts.append(p.capitalize())
            title_case = " ".join(cleaned_parts).strip()
            business_name = f"Platia 360 {title_case}".strip() if title_case else "Platia 360"
            add_variations(app, business_name)
    except Exception:
        pass

    return alias_map


def reverse_tool_name_mapping(name):
    """Map a business safe tool name back to the internal tool name."""
    return SAFE_TO_RAW_TOOL_NAMES.get(name, name)


def is_hidden_field(key: str) -> bool:
    """Check if the key is in the forbidden hidden fields list."""
    if not key:
        return False
    normalized = str(key).strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in HIDDEN_FIELDS


def _build_alias_regex(alias_map: dict):
    """
    Build ONE compiled regex covering every alias key, longest-first so
    multi-word phrases win over single-word substrings.
    """
    keys = [k for k in alias_map.keys() if k and len(k) >= 2]
    if not keys:
        return None
    keys.sort(key=len, reverse=True)
    parts = []
    for k in keys:
        escaped = re.escape(k)
        prefix = r"\b" if (k[0].isalnum() or k[0] == "_") else ""
        suffix = r"\b" if (k[-1].isalnum() or k[-1] == "_") else ""
        parts.append(prefix + escaped + suffix)
    pattern = "|".join(parts)
    return re.compile(pattern, flags=re.IGNORECASE)


def _normalize_prose(text: str, alias_map: dict, regex) -> str:
    if not text or regex is None:
        return text

    def _replace(match):
        matched = match.group(0)
        # Case-insensitive lookup: try exact, then lowercase fallback.
        return alias_map.get(matched, alias_map.get(matched.lower(), matched))

    result = regex.sub(_replace, text)
    # Safety net: collapse any accidental repeated brand prefix.
    result = _BRAND_REPEAT_RE.sub(_BRAND + " ", result)
    return result


def normalize_text(value: str) -> str:
    """
    Replace legacy/technical terms with business aliases in prose,
    while leaving fenced/inline code spans untouched so code samples
    (e.g. frappe.db.sql(...)) stay syntactically valid.
    """
    if not value:
        return value

    text = str(value)
    alias_map = get_business_alias_map()
    regex = _build_alias_regex(alias_map)
    if regex is None:
        return text

    # Split into alternating [prose, code, prose, code, ...] segments.
    # Code spans (fenced or inline) are passed through verbatim.
    pieces = []
    last_end = 0
    for m in _CODE_SPAN_RE.finditer(text):
        prose_chunk = text[last_end:m.start()]
        pieces.append(_normalize_prose(prose_chunk, alias_map, regex))
        pieces.append(m.group(0))  # code span, untouched
        last_end = m.end()
    pieces.append(_normalize_prose(text[last_end:], alias_map, regex))

    return "".join(pieces)

def normalize_for_business_user(value):
    """Recursively clean dictionaries, lists, and strings of technical terminology."""
    if isinstance(value, str):
        return normalize_text(value)

    if isinstance(value, list):
        return [normalize_for_business_user(item) for item in value]

    if isinstance(value, tuple):
        return tuple(normalize_for_business_user(item) for item in value)

    if isinstance(value, dict):
        cleaned = {}
        for key, val in value.items():
            if is_hidden_field(key):
                continue

            safe_key = normalize_text(str(key))
            if is_hidden_field(safe_key):
                continue

            cleaned[safe_key] = normalize_for_business_user(val)
        return cleaned

    return value

def find_forbidden_terms(text: str) -> list:
    """Verify if any core forbidden terminology is present in text."""
    if not text:
        return []
    text_lower = text.lower()
    violations = []
    
    forbidden_patterns = ["frappe", "erpnext", "doctype", "frappe_assistant_core"]
    for pattern in forbidden_patterns:
        # Match pattern case-insensitively with boundaries or dashes/underscores
        regex = r'(?:\b|_|-)' + re.escape(pattern) + r'(?:\b|_|-)'
        if re.search(regex, text_lower):
            violations.append(pattern)
            
    return violations

def enforce_final_output_policy(text: str) -> str:
    """Enforce the final output guidelines, blocking text if violations remain."""
    from frappe_assistant_core.policy.identity_policy import PolicyError
    
    safe_text = normalize_text(text)
    violations = find_forbidden_terms(safe_text)
    
    if violations:
        audit_final_output_blocked(len(violations), frappe.session.user)
        raise PolicyError(f"Response blocked because internal terminology was detected: {', '.join(violations)}")

    return safe_text

def contains_internal_details(message: str) -> bool:
    """Check if the message contains technical stack traces or query dumps."""
    message_lower = message.lower()
    if "traceback" in message_lower or "sql" in message_lower or "select " in message_lower:
        return True
    
    # Check for general database/technical leakage terms
    tech_terms = [
        "frappe", "erpnext", "doctype", "mysql", "mariadb", "postgresql",
        "column", "table", "relation", "database", "query", "syntax", "tab"
    ]
    for term in tech_terms:
        if term in message_lower:
            return True
    return False

def business_safe_error_message(tool_name: str, exc: Exception) -> str:
    """Formulate a business-safe error string from a raw exception."""
    internal_message = str(exc)
    safe_message = normalize_text(internal_message)

    if contains_internal_details(internal_message) or contains_internal_details(safe_message):
        safe_message = (
            "The request could not be completed because the required Platia 360 "
            "configuration or permission was not available. Please contact Platia 360 support."
        )
    return safe_message

def business_safe_error_response(request_id: Any, exc: Exception) -> dict:
    """Build a business-safe JSON-RPC error response."""
    safe_message = business_safe_error_message("jsonrpc", exc)
    
    # Audit rewritten error
    try:
        audit_doc = frappe.get_doc({
            "doctype": "Assistant Audit Log",
            "action": "mcp_business_error_rewritten",
            "user": frappe.session.user or "System",
            "status": "Success",
            "timestamp": now(),
            "client_id": getattr(frappe.local, "assistant_client_id", None),
            "session_id": getattr(frappe.local, "assistant_session_id", None),
            "output_data": frappe.as_json({
                "request_id": request_id,
                "original_error_class": exc.__class__.__name__,
                "original_error_message": str(exc),
                "safe_message": safe_message,
            }),
        })
        audit_doc.insert(ignore_permissions=True)
    except Exception:
        pass

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32000,
            "message": safe_message,
        },
    }

def audit_tool_result_normalized(tool_name, user, request_id, fields_hidden, terms_replaced):
    """Write tool-call normalization details to the audit log."""
    try:
        details = {
            "event": "mcp_tool_result_normalized",
            "tool": tool_name,
            "user": user,
            "fields_hidden": list(fields_hidden),
            "terms_replaced_count": terms_replaced,
            "request_id": request_id,
        }
        audit_doc = frappe.get_doc({
            "doctype": "Assistant Audit Log",
            "action": "mcp_tool_result_normalized",
            "user": user or "System",
            "status": "Success",
            "timestamp": now(),
            "client_id": getattr(frappe.local, "assistant_client_id", None),
            "session_id": getattr(frappe.local, "assistant_session_id", None),
            "output_data": frappe.as_json(details),
        })
        audit_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.logger("mcp_policy").warning(f"Failed to log tool result normalization audit: {str(e)}")

def audit_final_output_blocked(violations_count, user):
    """Write final output policy block details to the audit log."""
    try:
        details = {
            "event": "assistant_final_output_blocked",
            "violations_count": violations_count,
            "action": "blocked_for_regeneration",
            "user": user,
            "timestamp": now(),
        }
        audit_doc = frappe.get_doc({
            "doctype": "Assistant Audit Log",
            "action": "assistant_final_output_blocked",
            "user": user or "System",
            "status": "Success",
            "timestamp": now(),
            "client_id": getattr(frappe.local, "assistant_client_id", None),
            "session_id": getattr(frappe.local, "assistant_session_id", None),
            "output_data": frappe.as_json(details),
        })
        audit_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.logger("mcp_policy").warning(f"Failed to log final output blocked audit: {str(e)}")
