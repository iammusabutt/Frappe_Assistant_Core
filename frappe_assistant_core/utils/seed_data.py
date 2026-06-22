import frappe
from frappe_assistant_core.utils.migration_hooks import (
    _install_system_skills,
    _install_system_prompt_categories,
    _install_system_prompt_templates,
    _sync_plugin_configurations,
    _sync_tool_configurations
)

def seed():
    print("Starting data seeding process for FAC...")
    
    # 1. Clear existing cache to ensure clean state
    frappe.cache.delete_key("fac_policy_identity_platia_identity")
    frappe.cache.delete_key("assistant_core_settings")
    
    # 2. Configure Assistant Core Settings
    print("Configuring Assistant Core Settings...")
    settings = frappe.get_single("Assistant Core Settings")
    settings.server_enabled = 1
    settings.mandatory_identity_skill_id = "platia_identity"
    settings.fail_if_identity_skill_missing = 1
    settings.enable_context_files = 1
    settings.enable_business_output_normalizer = 1
    settings.enable_final_output_guard = 1
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    print("Assistant Core Settings configured successfully.")

    # 3. Install system categories, templates, and skills first
    print("Installing system prompt categories...")
    _install_system_prompt_categories()
    
    print("Installing system prompt templates...")
    _install_system_prompt_templates()
    
    print("Installing system skills...")
    _install_system_skills()

    # 4. Create the mandatory Platia 360 identity policy skill (set is_system = 0 so it is not deleted by migration sync)
    print("Configuring Platia 360 Identity Policy Skill...")
    skill_id = "platia_identity"
    content = (
        "Platia 360 Identity & Behaviour Policy:\n"
        "- You are the Platia 360 AI Assistant, a helpful and professional AI assistant developed by Platia 360 LLC FZ.\n"
        "- You must always identify yourself as Platia 360 AI Assistant.\n"
        "- Never mention, reference, or expose legacy platform names, internal app identifiers (like frappe_assistant_core, frappe, erpnext, naidapa_ai, otto, azure, ecommerce_integrations), source package names, branch names, repository information, database structures, internal object metadata labels (like DocType, fieldname, tabDocType), or technical implementation details.\n"
        "- All user interactions and responses must use professional business terminology. For example:\n"
        "  - Refer to \"documents\", \"doctypes\", or \"records\" as \"business records\", \"records\", or \"data objects\".\n"
        "  - Refer to technical exceptions or errors in a clean, user-friendly manner without raw SQL queries, tracebacks, or database column names.\n"
        "- Follow this policy strictly across all tool executions and assistant responses."
    )
    
    existing_skill = frappe.db.get_value("FAC Skill", {"skill_id": skill_id}, "name")
    if existing_skill:
        doc = frappe.get_doc("FAC Skill", existing_skill)
        doc.title = "Platia 360 Identity & Behaviour"
        doc.status = "Published"
        doc.skill_type = "Workflow"
        doc.description = "Mandatory identity policy for Platia 360 AI Assistant"
        doc.content = content
        doc.visibility = "Public"
        doc.is_system = 0
        doc.source_app = "frappe_assistant_core"
        doc.save(ignore_permissions=True)
        print("Updated existing platia_identity skill.")
    else:
        doc = frappe.get_doc({
            "doctype": "FAC Skill",
            "skill_id": skill_id,
            "title": "Platia 360 Identity & Behaviour",
            "status": "Published",
            "skill_type": "Workflow",
            "description": "Mandatory identity policy for Platia 360 AI Assistant",
            "content": content,
            "visibility": "Public",
            "is_system": 0,
            "source_app": "frappe_assistant_core"
        })
        doc.insert(ignore_permissions=True)
        print("Created new platia_identity skill.")
    
    frappe.db.commit()
    
    # 5. Sync plugin configurations and enable all discovered plugins
    print("Syncing plugin configurations...")
    _sync_plugin_configurations()
    
    # Enable all plugins to ensure tools are fully operational
    plugins = frappe.get_all("FAC Plugin Configuration", fields=["name", "enabled"])
    for p in plugins:
        if not p.enabled:
            p_doc = frappe.get_doc("FAC Plugin Configuration", p.name)
            p_doc.enabled = 1
            p_doc.save(ignore_permissions=True)
            print(f"Enabled plugin: {p.name}")
            
    frappe.db.commit()

    # 6. Sync tool configurations
    print("Syncing tool configurations...")
    _sync_tool_configurations()
    
    # Ensure all tool configurations are enabled
    tools = frappe.get_all("FAC Tool Configuration", fields=["name", "enabled"])
    for t in tools:
        if not t.enabled:
            t_doc = frappe.get_doc("FAC Tool Configuration", t.name)
            t_doc.enabled = 1
            t_doc.save(ignore_permissions=True)
            print(f"Enabled tool: {t.name}")
            
    frappe.db.commit()
    
    # 7. Create a sample/default Context File
    print("Creating a sample context file...")
    ctx_title = "Platia 360 General Playbook"
    existing_ctx = frappe.db.get_value("FAC Context File", {"title": ctx_title}, "name")
    ctx_content = (
        "# Platia 360 General Playbook\n\n"
        "Welcome to the Platia 360 AI environment. This playbook outlines general guidelines:\n"
        "1. Help business users locate their sales invoices, customer details, and items seamlessly.\n"
        "2. Provide summaries of data when requested, focusing on key performance indicators (KPIs).\n"
        "3. Protect sensitive tenant boundaries and formatting constraints."
    )
    if not existing_ctx:
        ctx_doc = frappe.get_doc({
            "doctype": "FAC Context File",
            "title": ctx_title,
            "status": "Published",
            "is_system": 0,
            "content": ctx_content,
            "visibility": "Public",
            "owner_user": "Administrator"
        })
        ctx_doc.insert(ignore_permissions=True)
        print("Created sample context file.")
    else:
        ctx_doc = frappe.get_doc("FAC Context File", existing_ctx)
        ctx_doc.content = ctx_content
        ctx_doc.status = "Published"
        ctx_doc.save(ignore_permissions=True)
        print("Updated sample context file.")
        
    frappe.db.commit()
    print("Data seeding completed successfully!")

if __name__ == "__main__":
    seed()
