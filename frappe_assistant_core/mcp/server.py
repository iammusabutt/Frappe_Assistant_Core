# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Custom MCP Server Implementation

A streamlined MCP server that fixes serialization issues and provides
full control over the implementation. Based on the MCP specification
with Frappe-specific optimizations.

Key improvements over frappe-mcp:
- Proper JSON serialization with `default=str` (handles datetime, Decimal, etc.)
- No Pydantic dependency (simpler, faster)
- Full error tracebacks for debugging
- Optional Bearer token authentication
- Frappe-native integration
"""

import json
import traceback
from collections import OrderedDict
from typing import Any, Dict, Optional

from werkzeug.wrappers import Request, Response

from frappe_assistant_core.policy.identity_policy import (
    PolicyError,
    load_mandatory_identity_policy,
    load_context_files_for_current_user,
    build_tool_usage_guidance,
    build_mcp_instructions,
    audit_policy_loaded,
)
from frappe_assistant_core.policy.business_output_normalizer import (
    HIDDEN_FIELDS,
    RAW_TO_SAFE_TOOL_NAMES,
    normalize_text,
    normalize_for_business_user,
    business_safe_error_message,
    reverse_tool_name_mapping,
    audit_tool_result_normalized,
)


class MCPServer:
    """
    Lightweight MCP server for Frappe.

    This class implements the Model Context Protocol (MCP) specification
    for tool calling with StreamableHTTP transport.

    Example:
        ```python
        from frappe_assistant_core.mcp.server import MCPServer
        from frappe_assistant_core.mcp.tool_adapter import register_base_tool
        from frappe_assistant_core.plugins.core.tools.list_documents import DocumentList

        mcp = MCPServer("my-server")

        @mcp.register()
        def handle_mcp():
            # Import and register BaseTool instances
            register_base_tool(mcp, DocumentList())
        ```

    Note:
        Tools are implemented as BaseTool subclasses and registered using
        the tool_adapter. The @mcp.tool decorator pattern is not supported.
    """

    def __init__(self, name: str = "frappe-assistant-core"):
        """
        Initialize MCP server.

        Args:
            name: Server name for identification
        """
        self.name = name
        self._tool_registry = OrderedDict()
        self._entry_fn = None

    def register(
        self,
        allow_guest: bool = False,
        xss_safe: bool = True,
        methods: list = None,
    ):
        """
        Decorator to register MCP endpoint with Frappe.

        This creates a whitelisted Frappe endpoint that handles MCP requests.

        Args:
            allow_guest: If True, allows unauthenticated access
            xss_safe: If True, response will not be sanitized for XSS
            methods: List of allowed HTTP methods (default: ["POST"])

        Example:
            ```python
            @mcp.register()
            def handle_mcp():
                # Import tool modules here
                pass
            ```
        """
        import frappe

        if methods is None:
            methods = ["POST"]

        whitelister = frappe.whitelist(
            allow_guest=allow_guest,
            xss_safe=xss_safe,
            methods=methods,
        )

        def decorator(fn):
            if self._entry_fn is not None:
                raise Exception("Only one MCP endpoint allowed per MCPServer instance")

            self._entry_fn = fn

            def wrapper() -> Response:
                # Run user's function to perform auth checks and build the
                # per-request tool registry. The registry is returned to keep it
                # off any shared/global state, so concurrent requests stay
                # isolated (see issue #197).
                result = fn()

                # If fn() returned a Response (e.g., 401 auth failure), use that.
                if isinstance(result, Response):
                    return result

                # Otherwise fn() returns the per-request tool registry (a dict),
                # or None to fall back to the shared registry.
                tool_registry = result if isinstance(result, dict) else None

                # Handle MCP request
                request = frappe.request
                response = Response()
                return self.handle(request, response, tool_registry=tool_registry)

            return whitelister(wrapper)

        return decorator

    def handle(self, request: Request, response: Response, tool_registry: Optional[Dict] = None) -> Response:
        """
        Handle MCP request - main entry point.

        Processes JSON-RPC 2.0 requests according to MCP specification.

        Args:
            request: Werkzeug Request object
            response: Werkzeug Response object
            tool_registry: Per-request tool registry (name -> tool_dict). When
                provided, all tool routing for this request reads from it instead
                of the shared ``self._tool_registry``. This is what keeps
                concurrent requests isolated: each request builds its own
                registry on the call stack rather than mutating a process-global
                one. Falls back to ``self._tool_registry`` when not supplied
                (e.g. tools registered directly via ``add_tool``).

        Returns:
            Populated Response object with MCP response
        """
        import frappe

        try:
            settings = frappe.get_single("Assistant Core Settings")
        except Exception:
            settings = None
        enable_normalizer = getattr(settings, "enable_business_output_normalizer", True) if settings else True

        # Per-request registry isolates concurrent requests. Never mutate the
        # shared singleton during request handling.
        if tool_registry is None:
            tool_registry = self._tool_registry

        # Only POST allowed
        if request.method != "POST":
            response.status_code = 405
            return response

        # Parse JSON request
        try:
            data = request.get_json(force=True)
            # Log incoming request for debugging
            frappe.logger().debug(f"MCP Request: method={data.get('method')}, id={data.get('id')}")
        except Exception as e:
            frappe.logger().error(
                f"MCP Parse Error: {str(e)}, Raw data: {request.get_data(as_text=True)[:500]}"
            )
            return self._error_response(response, None, -32700, f"Parse error: {str(e)}")

        # Populate correlation ids on frappe.local so downstream audit logging
        # can tag every tool execution with the MCP session and client. See
        # _populate_correlation_ids for header/initialize param fallback order.
        self._populate_correlation_ids(request, data)

        # Check if notification (no response needed)
        if self._is_notification(data):
            response.status_code = 202  # Accepted
            # Echo MCP-Protocol-Version header if present (2025-06-18 spec)
            incoming_version = frappe.request.headers.get("mcp-protocol-version")
            if incoming_version:
                response.headers["mcp-protocol-version"] = incoming_version
            return response

        # Get request ID
        request_id = data.get("id")
        if request_id is None:
            return self._error_response(response, None, -32600, "Invalid Request: missing id")

        # Route method
        method = data.get("method")
        params = data.get("params", {})

        result = None

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list(params, tool_registry)
            elif method == "tools/call":
                frappe.logger().info(
                    f"MCP tools/call: tool={params.get('name')}, args={json.dumps(params.get('arguments', {}), default=str)[:200]}"
                )
                result = self._handle_tools_call(params, tool_registry)
            elif method == "resources/list":
                result = self._handle_resources_list(params, request_id)
                if enable_normalizer:
                    result = normalize_for_business_user(result)
            elif method == "resources/read":
                result = self._handle_resources_read(params, request_id)
                if enable_normalizer:
                    result = normalize_for_business_user(result)
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = self._handle_prompts_list(params, request_id)
                if enable_normalizer:
                    result = normalize_for_business_user(result)
            elif method == "prompts/get":
                result = self._handle_prompts_get(params, request_id)
                if enable_normalizer:
                    result = normalize_for_business_user(result)
            elif method == "ping":
                result = {}
            else:
                frappe.logger().warning(f"MCP Unknown method: {method}")
                return self._error_response(response, request_id, -32601, f"Method not found: {method}")
        except PolicyError as e:
            frappe.logger().warning(f"MCP Policy Error: {str(e)}")
            return self._error_response(response, request_id, -32000, str(e))
        except Exception as e:
            # Log unexpected errors
            frappe.logger().error(
                f"MCP Handler Error for method '{method}': {str(e)}\n{traceback.format_exc()}"
            )
            return self._error_response(response, request_id, -32603, business_safe_error_message("jsonrpc", e))

        # Success response
        return self._success_response(response, request_id, result)

    def add_tool(self, tool_dict: Dict):
        """
        Programmatically add a tool.

        Used by tool_adapter to register BaseTool instances.

        Args:
            tool_dict: Dict with keys: name, description, inputSchema, fn, annotations
        """
        self._tool_registry[tool_dict["name"]] = tool_dict

    def _populate_correlation_ids(self, request: Request, data: Dict):
        """
        Set `frappe.local.assistant_session_id` and `assistant_client_id`.

        Resolution order for session id:
            1. `Mcp-Session-Id` request header (MCP streamable HTTP transport)
            2. `X-Assistant-Session-Id` request header (explicit override)
            3. A freshly-generated UUID4 (per-request fallback)

        Resolution order for client id:
            1. `X-Assistant-Client-Id` request header
            2. `clientInfo.name` from the `initialize` params when present
            3. `None`
        """
        import uuid

        import frappe

        session_id = (
            request.headers.get("Mcp-Session-Id")
            or request.headers.get("X-Assistant-Session-Id")
            or str(uuid.uuid4())
        )

        client_id = request.headers.get("X-Assistant-Client-Id")
        if not client_id:
            params = data.get("params") or {}
            client_info = params.get("clientInfo") or {}
            client_id = client_info.get("name")

        frappe.local.assistant_session_id = session_id
        frappe.local.assistant_client_id = client_id

    def _handle_initialize(self, params: Dict) -> Dict:
        """
        Handle initialize request.
        Loads the mandatory platia_identity policy and builds initialize response instructions.
        """
        import frappe

        # Load policy first (fails-closed if missing or not Published)
        policy = load_mandatory_identity_policy()

        # Get settings
        protocol_version = "2025-06-18"
        try:
            settings = frappe.get_single("Assistant Core Settings")
            protocol_version = settings.mcp_protocol_version or protocol_version
        except Exception:
            pass

        # Build instructions
        optional_context = load_context_files_for_current_user()
        tool_usage_guidance = build_tool_usage_guidance()
        instructions = build_mcp_instructions(
            identity_policy=policy["content"],
            optional_context=optional_context,
            tool_usage_guidance=tool_usage_guidance,
        )

        # Audit policy loading
        client_info = params.get("clientInfo", {})
        client_name = client_info.get("name", "unknown")
        request_id = getattr(frappe.local, "assistant_session_id", None) or "unknown"
        
        audit_policy_loaded(
            event="mcp_initialize_policy_loaded",
            skill_id=policy["skill_id"],
            record_name=policy["record_name"],
            policy_hash=policy["version_hash"],
            request_id=request_id,
            user=frappe.session.user,
            client=client_name,
        )

        # Business-safe server info
        server_info = {
            "name": "Platia 360 MCP",
            "version": "2.0.0",
        }

        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": True},
                "prompts": {},
                "resources": {"subscribe": True, "listChanged": True},
            },
            "serverInfo": server_info,
            "instructions": instructions,
        }

    def _handle_tools_list(self, params: Dict, tool_registry: Optional[Dict] = None) -> Dict:
        """Handle tools/list request with optional token optimization and policy normalization."""
        import frappe

        if tool_registry is None:
            tool_registry = self._tool_registry

        tools_list = []

        # Check skill_mode for token optimization
        skill_replace_map = {}
        try:
            settings = frappe.get_single("Assistant Core Settings")
            if getattr(settings, "skill_mode", "supplementary") == "replace":
                from frappe_assistant_core.api.handlers.resources import get_skill_manager

                skill_replace_map = get_skill_manager().get_tool_skill_map()
        except Exception:
            settings = None

        enable_normalizer = getattr(settings, "enable_business_output_normalizer", True) if settings else True

        # Build tools list
        for tool in tool_registry.values():
            description = tool["description"]

            # In replace mode, minimize descriptions for tools with linked skills
            if skill_replace_map and tool["name"] in skill_replace_map:
                skill_info = skill_replace_map[tool["name"]]
                description = f"{tool['name']}: {skill_info['description']}. Detailed guidance: fac://skills/{skill_info['skill_id']}"

            tool_spec = {
                "name": tool["name"],
                "description": description,
                "inputSchema": tool["inputSchema"],
            }

            if tool.get("annotations"):
                tool_spec["annotations"] = tool.get("annotations")

            # Normalization
            if enable_normalizer:
                # Hide raw app inventory tools (which leak raw app information)
                if tool["name"] in ("list_installed_apps", "get_installed_apps"):
                    continue

                # Rename and normalize
                name = tool_spec["name"]
                safe_name = RAW_TO_SAFE_TOOL_NAMES.get(name, name)
                
                tool_spec["name"] = safe_name
                tool_spec["description"] = normalize_text(tool_spec["description"])
                tool_spec["inputSchema"] = normalize_for_business_user(tool_spec["inputSchema"])

            tools_list.append(tool_spec)

        # Expose platia_environment_summary tool
        if enable_normalizer:
            platia_env_tool = {
                "name": "platia_environment_summary",
                "description": "Get a summary of the Platia 360 environment including active business areas and ownership information.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                }
            }
            tools_list.append(platia_env_tool)

        return {"tools": tools_list}

    def _handle_tools_call(self, params: Dict, tool_registry: Optional[Dict] = None) -> Dict:
        """
        Handle tools/call request with name reverse mapping and business user normalization.
        """
        import frappe

        if tool_registry is None:
            tool_registry = self._tool_registry

        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        frappe.logger().debug(f"MCP _handle_tools_call: tool={tool_name}, args={arguments}")

        try:
            settings = frappe.get_single("Assistant Core Settings")
        except Exception:
            settings = None

        # Direct execution for platia_environment_summary
        if tool_name == "platia_environment_summary":
            env_result = {
                "platform": "Platia 360",
                "assistant": "Platia 360 AI Assistant",
                "owner": "Platia 360 LLC FZ",
                "business_areas": [
                    "Accounting",
                    "Financial Reports",
                    "Receivables",
                    "Payables",
                    "Buying",
                    "Selling",
                    "Stock",
                    "Manufacturing",
                    "CRM",
                    "HR",
                    "Payroll",
                    "Projects",
                    "Quality",
                    "Assets",
                    "Support",
                    "Compliance",
                    "AI Assistant"
                ]
            }
            return {
                "content": [{"type": "text", "text": json.dumps(env_result, indent=2)}],
                "isError": False
            }

        # Reverse map name (e.g. get_business_record -> get_document)
        real_tool_name = reverse_tool_name_mapping(tool_name)

        # Check tool exists
        if real_tool_name not in tool_registry:
            safe_available = [RAW_TO_SAFE_TOOL_NAMES.get(n, n) for n in tool_registry.keys()]
            error_msg = f"Tool '{tool_name}' not found. Available tools: {safe_available}"
            frappe.logger().error(f"MCP Tool Not Found: {error_msg}")
            return {
                "content": [{"type": "text", "text": error_msg}],
                "isError": True,
            }

        tool = tool_registry[real_tool_name]
        fn = tool["fn"]

        try:
            # Execute tool
            frappe.logger().info(f"MCP Executing tool: {real_tool_name}")
            result = fn(**arguments)
            frappe.logger().info(
                f"MCP Tool {real_tool_name} executed successfully, result type: {type(result).__name__}"
            )

            # Extract image content
            image_content = None
            if isinstance(result, dict):
                inner = result.get("result")
                if isinstance(inner, dict) and "_image_content" in inner:
                    image_content = inner.pop("_image_content")

            # Output Normalization
            fields_hidden = set()
            terms_replaced = 0
            
            enable_normalizer = getattr(settings, "enable_business_output_normalizer", True) if settings else True
            if enable_normalizer:
                # Count normalizations/hidden fields before converting to text
                if isinstance(result, dict):
                    for hidden in HIDDEN_FIELDS:
                        if hidden in result:
                            fields_hidden.add(hidden)
                
                # Capture pre-normalization string representation
                pre_str = str(result)
                
                # Normalize result
                result = normalize_for_business_user(result)

                # Capture post-normalization string representation
                post_str = str(result)
                terms_replaced = 1 if pre_str != post_str else 0

            # Serialize result
            if isinstance(result, str):
                result_text = result
            else:
                result_text = json.dumps(result, default=str, indent=2)

            # Build MCP content blocks
            content = [{"type": "text", "text": result_text}]

            if image_content and isinstance(image_content, dict):
                mime_map = {
                    "jpeg": "image/jpeg",
                    "jpg": "image/jpeg",
                    "png": "image/png",
                    "gif": "image/gif",
                    "webp": "image/webp",
                }
                fmt = image_content.get("format", "jpeg")
                content.append(
                    {
                        "type": "image",
                        "mimeType": mime_map.get(fmt, f"image/{fmt}"),
                        "data": image_content["data"],
                    }
                )

            # Audit normalization
            if enable_normalizer:
                try:
                    user = frappe.session.user
                except Exception:
                    user = "System"
                request_id = getattr(frappe.local, "assistant_session_id", None) or "unknown"
                audit_tool_result_normalized(
                    tool_name=tool_name,
                    user=user,
                    request_id=request_id,
                    fields_hidden=fields_hidden,
                    terms_replaced=terms_replaced,
                )

            return {"content": content, "isError": False}

        except Exception as e:
            # Wrap error to business-safe message
            safe_error_text = business_safe_error_message(tool_name, e)
            frappe.logger().error(f"MCP Tool Execution Error wrapped: {safe_error_text}")

            return {"content": [{"type": "text", "text": safe_error_text}], "isError": True}

    def _success_response(self, response: Response, request_id: Any, result: Dict) -> Response:
        """Create JSON-RPC success response."""
        import frappe

        response_data = {"jsonrpc": "2.0", "id": request_id, "result": result}

        # Use default=str here too for consistency
        response.data = json.dumps(response_data, default=str)
        response.mimetype = "application/json"
        response.status_code = 200

        # Echo MCP-Protocol-Version header if present (2025-06-18 spec)
        incoming_version = frappe.request.headers.get("mcp-protocol-version")
        if incoming_version:
            response.headers["mcp-protocol-version"] = incoming_version

        return response

    def _error_response(
        self, response: Response, request_id: Optional[Any], code: int, message: str
    ) -> Response:
        """Create JSON-RPC error response."""
        import frappe

        response_data = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

        response.data = json.dumps(response_data)
        response.mimetype = "application/json"
        response.status_code = 400

        # Echo MCP-Protocol-Version header if present (2025-06-18 spec)
        incoming_version = frappe.request.headers.get("mcp-protocol-version")
        if incoming_version:
            response.headers["mcp-protocol-version"] = incoming_version

        return response

    def _handle_prompts_list(self, params: Dict, request_id: Any) -> Dict:
        """
        Handle prompts/list request.

        Returns available prompt templates from the database.
        """
        from frappe_assistant_core.api.handlers.prompts import handle_prompts_list

        # The handler returns a full JSON-RPC response, extract just the result
        response = handle_prompts_list(request_id)
        if "result" in response:
            return response["result"]
        # If there's an error, return empty prompts list
        return {"prompts": []}

    def _handle_prompts_get(self, params: Dict, request_id: Any) -> Dict:
        """
        Handle prompts/get request.

        Returns a specific prompt template rendered with provided arguments.
        """
        from frappe_assistant_core.api.handlers.prompts import handle_prompts_get

        # The handler returns a full JSON-RPC response, extract just the result
        response = handle_prompts_get(params, request_id)
        if "result" in response:
            return response["result"]
        # If there's an error, re-raise it
        if "error" in response:
            raise Exception(response["error"].get("message", "Unknown prompt error"))
        return {}

    def _handle_resources_list(self, params: Dict, request_id: Any) -> Dict:
        """
        Handle resources/list request.

        Returns available skill documents as MCP resources.
        """
        from frappe_assistant_core.api.handlers.resources import handle_resources_list

        return handle_resources_list(request_id)

    def _handle_resources_read(self, params: Dict, request_id: Any) -> Dict:
        """
        Handle resources/read request.

        Returns the content of a specific skill resource by URI.
        """
        from frappe_assistant_core.api.handlers.resources import handle_resources_read

        return handle_resources_read(params, request_id)

    def _is_notification(self, data: Dict) -> bool:
        """Check if request is a notification (no response needed)."""
        method = data.get("method", "")
        return isinstance(method, str) and method.startswith("notifications/")
