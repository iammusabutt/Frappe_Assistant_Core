# Frappe Assistant Core - Platia 360 Identity Policy Tests
# Copyright (C) 2026 Paul Clinton

import json
from unittest.mock import MagicMock

import frappe
from frappe_assistant_core.mcp.server import MCPServer
from frappe_assistant_core.policy.identity_policy import PolicyError
from frappe_assistant_core.policy.business_output_normalizer import (
    normalize_for_business_user,
    enforce_final_output_policy,
)
from frappe_assistant_core.tests.base_test import BaseAssistantTest


class TestIdentityPolicy(BaseAssistantTest):
    def setUp(self):
        super().setUp()
        self.server = MCPServer("platia-mcp-test")
        
        # Ensure we have a clean settings document for testing
        self.settings = frappe.get_doc("Assistant Core Settings")
        self.settings.mandatory_identity_skill_id = "platia_identity"
        self.settings.fail_if_identity_skill_missing = 1
        self.settings.enable_business_output_normalizer = 1
        self.settings.save(ignore_permissions=True)
        frappe.db.commit()

        # Clean cache
        frappe.cache.delete_key("fac_policy_identity_platia_identity")
        
        # Create a standard mock identity skill
        self.create_identity_skill(status="Published")

    def tearDown(self):
        # Clean up skills created
        frappe.db.delete("FAC Skill", {"skill_id": "platia_identity"})
        frappe.db.delete("FAC Context File", {"title": ["like", "TEST_%"]})
        frappe.db.commit()
        frappe.cache.delete_key("fac_policy_identity_platia_identity")
        super().tearDown()

    def create_identity_skill(self, status="Published", content=None):
        frappe.db.delete("FAC Skill", {"skill_id": "platia_identity"})
        frappe.db.commit()

        if content is None:
            content = (
                "Platia 360 Identity Policy:\n"
                "- System is Platia 360\n"
                "- Assistant is Platia 360 AI\n"
                "- Owned by Platia 360 LLC FZ"
            )

        skill = frappe.get_doc({
            "doctype": "FAC Skill",
            "skill_id": "platia_identity",
            "title": "Platia 360 Identity & Behaviour",
            "status": status,
            "skill_type": "Workflow",
            "description": "Mandatory identity policy",
            "content": content,
            "visibility": "Public",
        })
        skill.insert(ignore_permissions=True)
        frappe.db.commit()
        return skill

    def test_initialize_includes_platia_identity(self):
        """Test initialize response result instructions contain identity policy."""
        result = self.server._handle_initialize({"clientInfo": {"name": "test-client"}})
        
        self.assertIn("instructions", result)
        instructions = result["instructions"]
        self.assertIn("Platia 360", instructions)
        self.assertIn("Platia 360 AI", instructions)
        self.assertIn("Platia 360 LLC FZ", instructions)
        self.assertEqual(result["serverInfo"]["name"], "Platia 360 MCP")

    def test_initialize_fails_when_identity_skill_missing(self):
        """Test server fails initialization when identity skill is missing."""
        frappe.db.delete("FAC Skill", {"skill_id": "platia_identity"})
        frappe.db.commit()
        frappe.cache.delete_key("fac_policy_identity_platia_identity")

        with self.assertRaises(PolicyError) as context:
            self.server._handle_initialize({})
        
        self.assertIn("identity policy is missing", str(context.exception))

    def test_initialize_fails_when_identity_skill_not_published(self):
        """Test server fails initialization when identity skill is not Published (e.g. Draft)."""
        self.create_identity_skill(status="Draft")
        frappe.cache.delete_key("fac_policy_identity_platia_identity")

        with self.assertRaises(PolicyError) as context:
            self.server._handle_initialize({})
            
        self.assertIn("not Published", str(context.exception))

    def test_tools_list_has_no_internal_terms(self):
        """Test that list of tools normalizes names, schemas, and descriptions."""
        mock_registry = {
            "get_document": {
                "name": "get_document",
                "description": "Retrieve detailed information about a specific Frappe DocType document.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"doctype": {"type": "string"}},
                },
                "fn": lambda: "doc"
            }
        }
        
        result = self.server._handle_tools_list({}, tool_registry=mock_registry)
        tools = result["tools"]
        
        # Verify get_document was renamed to get_business_record
        business_record_tool = next((t for t in tools if t["name"] == "get_business_record"), None)
        self.assertIsNotNone(business_record_tool)
        
        # Verify description and schema normalized
        self.assertNotIn("Frappe", business_record_tool["description"])
        self.assertIn("Platia 360", business_record_tool["description"])
        self.assertNotIn("doctype", business_record_tool["description"])
        self.assertIn("object type", business_record_tool["description"])
        self.assertIn("object type", str(business_record_tool["inputSchema"]))
        
        # Verify platia_environment_summary was appended
        env_summary = next((t for t in tools if t["name"] == "platia_environment_summary"), None)
        self.assertIsNotNone(env_summary)

    def test_tool_result_has_no_internal_terms(self):
        """Test execution of platia_environment_summary directly."""
        result = self.server._handle_tools_call({"name": "platia_environment_summary", "arguments": {}})
        
        self.assertFalse(result["isError"])
        content_text = result["content"][0]["text"]
        self.assertIn("Platia 360", content_text)
        self.assertIn("Platia 360 AI Assistant", content_text)
        self.assertNotIn("frappe", content_text.lower())
        self.assertNotIn("erpnext", content_text.lower())

    def test_internal_app_identifier_is_replaced(self):
        """Test normalizer recursively cleans dictionary results, hides fields, and replaces legacy terms."""
        raw_result = {
            "app_name": "frappe_assistant_core",
            "branch": "main",
            "source_app": "frappe",
            "nested": {
                "technical_module": "metadata",
                "doctype": "Sales Invoice",
            }
        }
        
        normalized = normalize_for_business_user(raw_result)
        
        # Verify hidden fields are removed
        self.assertNotIn("branch", normalized)
        self.assertNotIn("source_app", normalized)
        self.assertNotIn("technical_module", normalized["nested"])
        
        # Verify replacements
        self.assertEqual(normalized["app_name"], "Platia 360 AI Assistant")
        self.assertEqual(normalized["nested"]["object type"], "Sales Invoice")

    def test_error_message_is_business_safe(self):
        """Test execution failures wrap internal exceptions into safe business user error messages."""
        def fail_fn(**kwargs):
            raise Exception("Internal database query error in DocType Customer: column 'name' not found in tabDocType")
            
        mock_registry = {
            "get_document": {
                "name": "get_document",
                "description": "Get document",
                "inputSchema": {},
                "fn": fail_fn
            }
        }
        
        # Call safe name get_business_record which maps back to get_document
        result = self.server._handle_tools_call({"name": "get_business_record", "arguments": {}}, tool_registry=mock_registry)
        
        self.assertTrue(result["isError"])
        err_msg = result["content"][0]["text"]
        
        # Safe message should not leak database fields or raw names
        self.assertNotIn("tabDocType", err_msg)
        self.assertNotIn("column", err_msg)
        self.assertNotIn("DocType", err_msg)
        self.assertIn("Platia 360", err_msg)
        self.assertIn("support", err_msg)

    def test_final_output_guard(self):
        """Test final output guard throws PolicyError if violations exist after normalization."""
        # This string contains "frappe" inside a word with boundary or exact
        text_with_violation = "This server runs on Frappe Framework."
        
        # Normalization replaces "Frappe" -> "Platia 360", so final text should be safe and not throw
        safe_text = enforce_final_output_policy(text_with_violation)
        self.assertIn("Platia 360", safe_text)
        self.assertNotIn("Frappe", safe_text)
        
        # If a violation still leaks or is bypassable (e.g. explicitly writing forbidden terms in lowercase with boundaries)
        with self.assertRaises(PolicyError):
            # We mock the normalizer mapping to not match but let the pattern trigger
            from unittest.mock import patch
            with patch("frappe_assistant_core.policy.business_output_normalizer.normalize_text", return_value="frappe"):
                enforce_final_output_policy("some raw text")
