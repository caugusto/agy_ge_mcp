#!/usr/bin/env python3
"""
ge_mcp_server.py (v1 backup - single-connector + project-level list)
----------------
Standalone Model Context Protocol (MCP) stdio server for Plain Antigravity that connects
directly to Google Cloud Gemini Enterprise (Discovery Engine) connectors and engines
using `gcloud auth print-access-token` (zero dependency on Spark Desktop or npm).

Capabilities exposed via MCP:
  1. Dynamic tool discovery & forwarding via Discovery Engine `:invokeConnectorMcp`
     for the configured `--connector` (e.g. `jira-connector_1790881435408`).
  2. `gemini_enterprise_assist`: Calls the Gemini Enterprise Engine `streamAssist` API
     to execute actions or queries across attached connectors (Jira, M365, BigQuery, etc.).
  3. `list_ge_connectors`: Lists all Gemini Enterprise Data Connectors in the project,
     their active state, data source, and enabled actions.
  4. `invoke_ge_connector_mcp`: Sends an MCP tool call or JSON-RPC method to any
     Gemini Enterprise connector collection in the project.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API_BASE = "https://discoveryengine.googleapis.com/v1alpha"


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(cafile):
            try:
                ctx.load_verify_locations(cafile=cafile)
            except Exception:
                pass
    return ctx


_SSL_CTX = _build_ssl_context()
GCLOUD_PATHS = [
    os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
    "/opt/homebrew/bin/gcloud",
    "/usr/local/bin/gcloud",
    "gcloud",
]

_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


def get_access_token() -> str:
    """Obtains a Google Cloud access token via gcloud with a 5-minute in-memory cache."""
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"]:
        return _TOKEN_CACHE["token"]

    env = dict(os.environ)
    existing_metrics = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "")
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing_metrics} datacloud.antigravity".strip()
    )

    last_err = ""
    for gcloud_bin in GCLOUD_PATHS:
        try:
            token = subprocess.check_output(
                [gcloud_bin, "auth", "print-access-token"],
                text=True,
                stderr=subprocess.PIPE,
                env=env,
                timeout=15,
            ).strip()
            if token:
                _TOKEN_CACHE["token"] = token
                _TOKEN_CACHE["expires_at"] = now + 300
                return token
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Failed to obtain gcloud access token: {last_err}")


def de_request(
    method: str,
    url: str,
    project: str,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    """Performs an authenticated HTTP request to Google Cloud Discovery Engine."""
    token = get_access_token()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Goog-User-Project": str(project),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": {"message": raw[:500], "code": e.code}}
    except Exception as e:
        return 0, {"error": {"message": str(e)}}


def clean_collection_id(connector: str) -> str:
    return connector.removeprefix("collections/").strip()


def invoke_connector_mcp(
    project: str,
    location: str,
    connector: str,
    mcp_payload: dict[str, Any],
    timeout: int = 60,
) -> tuple[int, Any]:
    """Calls Discovery Engine dataConnector:invokeConnectorMcp."""
    col_id = clean_collection_id(connector)
    url = (
        f"{API_BASE}/projects/{project}/locations/{location}/"
        f"collections/{col_id}/dataConnector:invokeConnectorMcp"
    )
    body = {k: v for k, v in mcp_payload.items() if k not in ("jsonrpc", "id")}
    return de_request("POST", url, project, body=body, timeout=timeout)


def _normalize_json_schema(schema: Any) -> Any:
    """Normalizes Discovery Engine's array-typed JSON schemas (e.g. ['object'] -> 'object') for MCP clients."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    t = out.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        out["type"] = non_null[0] if non_null else "string"
    if isinstance(out.get("properties"), dict):
        out["properties"] = {
            k: _normalize_json_schema(v) for k, v in out["properties"].items()
        }
    if isinstance(out.get("items"), dict):
        out["items"] = _normalize_json_schema(out["items"])
    return out


def fetch_dynamic_connector_tools(
    project: str, location: str, connector: str
) -> list[dict[str, Any]]:
    """Attempts to dynamically list tools from the configured connector via invokeConnectorMcp."""
    if not connector:
        return []
    try:
        code, resp = invoke_connector_mcp(
            project,
            location,
            connector,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            timeout=10,
        )
        if code == 200 and isinstance(resp, dict):
            tools = (
                resp.get("result", {}).get("tools")
                or resp.get("tools")
                or []
            )
            if isinstance(tools, list):
                normalized = []
                for t in tools:
                    if isinstance(t, dict) and t.get("name"):
                        nt = {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "inputSchema": _normalize_json_schema(
                                t.get("inputSchema") or {"type": "object", "properties": {}}
                            ),
                        }
                        normalized.append(nt)
                return normalized
    except Exception:
        pass
    return []


def build_builtin_tools(default_connector: str, default_engine: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "gemini_enterprise_assist",
            "description": (
                "Execute a natural-language query or action (e.g., search/create/update Jira issues, "
                "query BigQuery, search enterprise docs) through the Google Cloud Gemini Enterprise "
                f"Engine (default: '{default_engine}') with all attached Gemini Enterprise connectors."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The prompt or action request to send to Gemini Enterprise (e.g., 'List open Jira issues in project X').",
                    },
                    "engine_id": {
                        "type": "string",
                        "description": f"Optional Gemini Enterprise Engine ID (defaults to '{default_engine}').",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "list_ge_connectors",
            "description": (
                "List all Gemini Enterprise (Discovery Engine) Data Connectors in the GCP project, "
                "including their collection ID, data source (e.g. jira, custom_mcp), state, and enabled actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_ge_connector_details",
            "description": (
                "Inspect the configuration, state, entities, and enabled actions of a specific "
                f"Gemini Enterprise connector (defaults to '{default_connector}')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_id": {
                        "type": "string",
                        "description": f"Collection ID of the connector (defaults to '{default_connector}').",
                    }
                },
            },
        },
        {
            "name": "invoke_ge_connector_mcp",
            "description": (
                "Directly invoke an MCP method (`tools/list` or `tools/call`) on a Gemini Enterprise "
                f"connector via Discovery Engine `:invokeConnectorMcp` (defaults to '{default_connector}')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_id": {
                        "type": "string",
                        "description": f"Connector collection ID (defaults to '{default_connector}').",
                    },
                    "mcp_method": {
                        "type": "string",
                        "enum": ["tools/list", "tools/call"],
                        "description": "MCP JSON-RPC method to invoke on the connector.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Tool name (required when mcp_method is 'tools/call').",
                    },
                    "tool_arguments": {
                        "type": "object",
                        "description": "Arguments object for the tool (used when mcp_method is 'tools/call').",
                    },
                },
                "required": ["mcp_method"],
            },
        },
    ]


def handle_tool_call(
    name: str,
    arguments: dict[str, Any],
    project: str,
    location: str,
    default_engine: str,
    default_connector: str,
) -> dict[str, Any]:
    """Executes an MCP tool call against Google Cloud Discovery Engine."""
    try:
        if name == "list_ge_connectors":
            url = f"{API_BASE}/projects/{project}/locations/{location}/collections"
            code, data = de_request("GET", url, project)
            if code != 200:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                }
            connectors = []
            for col in data.get("collections", []):
                dc = col.get("dataConnector") or {}
                connectors.append(
                    {
                        "collectionId": col.get("name", "").split("/")[-1],
                        "displayName": col.get("displayName", ""),
                        "dataSource": dc.get("dataSource", ""),
                        "state": dc.get("state", ""),
                        "actionState": dc.get("actionState", ""),
                        "enabledActionsCount": len(
                            dc.get("bapConfig", {}).get("enabledActions", [])
                        ),
                    }
                )
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"connectors": connectors}, indent=2)}
                ]
            }

        if name == "get_ge_connector_details":
            cid = clean_collection_id(arguments.get("connector_id") or default_connector)
            url = (
                f"{API_BASE}/projects/{project}/locations/{location}/"
                f"collections/{cid}/dataConnector"
            )
            code, data = de_request("GET", url, project)
            return {
                "isError": code != 200,
                "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
            }

        if name == "gemini_enterprise_assist":
            query_text = arguments.get("query", "")
            engine_id = arguments.get("engine_id") or default_engine
            url = (
                f"{API_BASE}/projects/{project}/locations/{location}/"
                f"collections/default_collection/engines/{engine_id}/"
                f"assistants/default_assistant:streamAssist"
            )
            code, data = de_request(
                "POST",
                url,
                project,
                body={"query": {"text": query_text}},
                timeout=90,
            )
            return {
                "isError": code != 200,
                "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
            }

        if name == "invoke_ge_connector_mcp":
            cid = clean_collection_id(arguments.get("connector_id") or default_connector)
            mcp_method = arguments.get("mcp_method", "tools/list")
            params: dict[str, Any] = {}
            if mcp_method == "tools/call":
                params = {
                    "name": arguments.get("tool_name", ""),
                    "arguments": arguments.get("tool_arguments") or {},
                }
            code, data = invoke_connector_mcp(
                project,
                location,
                cid,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": mcp_method,
                    "params": params,
                },
            )
            return {
                "isError": code != 200,
                "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
            }

        # Otherwise, forward as a direct tool call to default_connector via invokeConnectorMcp
        code, data = invoke_connector_mcp(
            project,
            location,
            default_connector,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        if code == 200 and isinstance(data, dict) and "result" in data:
            res = data["result"]
            if isinstance(res, dict) and "content" in res:
                return res
        return {
            "isError": code != 200,
            "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
        }
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Error executing tool '{name}': {e}"}],
        }


def send_response(msg_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Enterprise MCP Stdio Server for Antigravity")
    parser.add_argument("--project", default=os.environ.get("GE_MCP_PROJECT", ""), help="GCP Project ID or Number")
    parser.add_argument("--location", default="global", help="Discovery Engine location")
    parser.add_argument(
        "--engine",
        default="test1-agentspace_1741135345115",
        help="Gemini Enterprise Engine ID",
    )
    parser.add_argument(
        "--connector",
        default=os.environ.get("GE_MCP_CONNECTOR", ""),
        help="Default Gemini Enterprise connector collection ID",
    )
    parser.add_argument(
        "--call-tool",
        default=None,
        help="Optional one-shot tool name to execute and exit (for CLI testing)",
    )
    parser.add_argument(
        "--tool-args",
        default="{}",
        help="Optional JSON arguments string for --call-tool",
    )
    parser.add_argument(
        "--tool-args-file",
        default=None,
        help="Optional path to JSON arguments file for --call-tool",
    )
    args = parser.parse_args()

    if args.call_tool:
        if args.tool_args_file:
            with open(args.tool_args_file, encoding="utf-8") as f:
                tool_args = json.load(f)
        else:
            tool_args = json.loads(args.tool_args) if args.tool_args else {}
        res = handle_tool_call(
            args.call_tool,
            tool_args,
            args.project,
            args.location,
            args.engine,
            args.connector,
        )
        print(json.dumps(res, indent=2))
        return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue

        msg_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        # Notifications (no id) do not receive a response
        if msg_id is None:
            continue

        if method == "initialize":
            protocol_version = params.get("protocolVersion", "2024-11-05")
            send_response(
                msg_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "gemini-enterprise-jira",
                        "version": "1.0.0",
                    },
                },
            )
        elif method == "ping":
            send_response(msg_id, {})
        elif method == "tools/list":
            tools = build_builtin_tools(args.connector, args.engine)
            dynamic_tools = fetch_dynamic_connector_tools(
                args.project, args.location, args.connector
            )
            existing_names = {t["name"] for t in tools}
            for dt in dynamic_tools:
                if isinstance(dt, dict) and dt.get("name") and dt["name"] not in existing_names:
                    tools.append(dt)
                    existing_names.add(dt["name"])
            send_response(msg_id, {"tools": tools})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            res = handle_tool_call(
                tool_name,
                tool_args,
                args.project,
                args.location,
                args.engine,
                args.connector,
            )
            send_response(msg_id, res)
        elif method == "resources/list":
            send_response(msg_id, {"resources": []})
        elif method == "prompts/list":
            send_response(msg_id, {"prompts": []})
        else:
            send_response(
                msg_id,
                error={"code": -32601, "message": f"Method not found: {method}"},
            )


if __name__ == "__main__":
    main()
