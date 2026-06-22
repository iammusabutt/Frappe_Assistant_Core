# Frappe Assistant Core - Platia 360 Identity Policy Enforcement
# Copyright (C) 2026 Paul Clinton

import hashlib
import frappe
from frappe.utils import now

class PolicyError(Exception):
    pass

POLICY_CACHE_KEY = "fac_policy_identity_platia_identity"
POLICY_CACHE_TTL_SECONDS = 300

def load_mandatory_identity_policy():
    """
    Load the required Platia 360 identity policy from FAC Skill.
    Lookup must use skill_id = platia_identity.
    """
    # Check cache first
    cached = frappe.cache.get_value(POLICY_CACHE_KEY)
    if cached:
        return cached

    try:
        settings = frappe.get_single("Assistant Core Settings")
        required_skill_id = getattr(settings, "mandatory_identity_skill_id", None) or "platia_identity"
    except Exception:
        required_skill_id = "platia_identity"

    skill_name = frappe.db.get_value("FAC Skill", {"skill_id": required_skill_id}, "name")
    if not skill_name:
        raise PolicyError("Mandatory Platia 360 identity policy is missing.")

    skill = frappe.get_doc("FAC Skill", skill_name)

    if skill.status != "Published":
        raise PolicyError("Mandatory Platia 360 identity policy is not Published.")

    content = (skill.content or "").strip()
    if not content:
        raise PolicyError("Mandatory Platia 360 identity policy is empty.")

    policy = {
        "skill_id": skill.skill_id,
        "record_name": skill.name,
        "title": skill.title,
        "status": skill.status,
        "modified": skill.modified,
        "version_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }

    # Cache policy
    frappe.cache.set_value(POLICY_CACHE_KEY, policy, expires_in_sec=POLICY_CACHE_TTL_SECONDS)
    return policy

def load_context_files_for_current_user():
    """
    Load and return list of markdown content from accessible published context files.
    """
    try:
        settings = frappe.get_single("Assistant Core Settings")
        if not getattr(settings, "enable_context_files", False):
            return []
    except Exception:
        return []

    if not frappe.db.table_exists("FAC Context File"):
        return []

    user = frappe.session.user
    user_roles = frappe.get_roles(user)

    # Base query for public, system, or owned context files
    base = frappe.get_all(
        "FAC Context File",
        filters={},
        or_filters=[
            ["owner_user", "=", user],
            ["visibility", "=", "Public"],
            ["is_system", "=", 1],
        ],
        fields=["name", "title", "content", "status", "owner_user", "visibility"],
    )

    # Filter for Published status unless user is the owner
    context_files = [c for c in base if c.owner_user == user or c.status == "Published"]

    # Shared context files
    if user_roles:
        shared = frappe.db.sql(
            """
            SELECT DISTINCT c.name, c.title, c.content, c.status, c.owner_user, c.visibility
            FROM `tabFAC Context File` c
            INNER JOIN `tabHas Role` hr
                ON hr.parent = c.name AND hr.parenttype = 'FAC Context File'
            WHERE c.status = 'Published'
              AND c.visibility = 'Shared'
              AND hr.role IN %(roles)s
              AND c.owner_user != %(user)s
            """,
            {"roles": user_roles, "user": user},
            as_dict=True,
        )
        seen_names = {c.name for c in context_files}
        for c in shared:
            if c.name not in seen_names:
                context_files.append(c)
                seen_names.add(c.name)

    # Dedup and get contents
    contents = []
    seen_names = set()
    for c in context_files:
        if c.name in seen_names:
            continue
        seen_names.add(c.name)
        if c.content:
            contents.append(c.content.strip())

    return contents

def build_tool_usage_guidance():
    """Build standard tool usage guidelines for the assistant."""
    return (
        "Tool Usage Guidance:\n"
        "- Use the tools provided to interact with the environment.\n"
        "- Follow tool schemas and constraints strictly.\n"
        "- Do not guess or assume parameter values; ask the user if needed."
    )

def build_mcp_instructions(identity_policy, optional_context, tool_usage_guidance):
    """
    Assemble instructions in strict order:
    1. Mandatory identity policy
    2. Optional context files
    3. Tool usage guidance
    """
    parts = []
    if identity_policy:
        parts.append(identity_policy)
    if optional_context:
        parts.append("\n\n".join(optional_context))
    if tool_usage_guidance:
        parts.append(tool_usage_guidance)
    return "\n\n---\n\n".join(parts)

def audit_policy_loaded(event, skill_id, record_name, policy_hash, request_id, user, client):
    """Log an initialize policy loaded event to the audit trail."""
    try:
        details = {
            "skill_id": skill_id,
            "skill_record": record_name,
            "policy_loaded": True,
            "policy_hash": policy_hash,
            "skill_status": "Published",
            "client": client,
        }
        
        audit_doc = frappe.get_doc({
            "doctype": "Assistant Audit Log",
            "action": event,
            "user": user or "System",
            "status": "Success",
            "timestamp": now(),
            "client_id": getattr(frappe.local, "assistant_client_id", None),
            "session_id": getattr(frappe.local, "assistant_session_id", None),
            "output_data": frappe.as_json(details),
        })
        audit_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.logger("mcp_policy").warning(f"Failed to audit policy load: {str(e)}")
