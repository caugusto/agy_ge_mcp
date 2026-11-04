#!/usr/bin/env python3
"""
ge_mcp_server.py (v2 - Portable Engine-Scoped Meta-Tool Router)
---------------------------------------------------------------
Standalone Model Context Protocol (MCP) stdio server for Google Antigravity (AGY)
that connects to a specific Google Cloud Gemini Enterprise Engine (`--engine`) and
exposes ONLY the connectors and data stores bound to that Engine instance.

Zero external Python dependencies (uses Python 3 standard library + `gcloud auth`).

Meta-Tools Exposed:
  1. `ge_list_connectors`: Lists ONLY the connectors and data stores bound to `--engine`.
  2. `ge_search_tools`: Lists/searches MCP tools and schemas on an engine-bound connector.
  3. `ge_call_tool`: Executes an MCP tool on an engine-bound connector (rejects unlinked connectors).
  4. `ge_assist_query`: Queries the Gemini Enterprise Engine (`streamAssist`) across bound data stores.
"""

import argparse
import json
import os
import shutil
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


def _candidate_gcloud_paths() -> list[str]:
    paths = []
    which_gcloud = shutil.which("gcloud")
    if which_gcloud:
        paths.append(which_gcloud)
    for p in (
        os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
        "/opt/homebrew/bin/gcloud",
        "/usr/local/bin/gcloud",
        "/usr/bin/gcloud",
        "gcloud",
    ):
        if p not in paths:
            paths.append(p)
    return paths


_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_ENGINE_SCOPE_CACHE: dict[str, Any] = {"data": None, "expires_at": 0.0}


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
    for gcloud_bin in _candidate_gcloud_paths():
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

    raise RuntimeError(
        f"Failed to obtain gcloud access token. Run `gcloud auth login` first. ({last_err})"
    )


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


def discover_engine_bound_resources(
    project: str,
    location: str,
    engine_id: str,
    force_refresh: bool = False,
    timeout: int = 15,
) -> dict[str, Any]:
    """
    Returns ONLY the connectors and data stores bound to the specified `engine_id`
    by inspecting:
      1. `engines/{engine_id}/widgetConfigs/default_search_widget_config` (`collectionComponents`)
      2. `engines/{engine_id}` (`dataStoreIds`)
    Unlinked project-level collections are never included.
    """
    now = time.time()
    cache_key = f"{project}:{location}:{engine_id}"
    if (
        not force_refresh
        and _ENGINE_SCOPE_CACHE.get("key") == cache_key
        and _ENGINE_SCOPE_CACHE.get("data") is not None
        and now < _ENGINE_SCOPE_CACHE.get("expires_at", 0.0)
    ):
        return _ENGINE_SCOPE_CACHE["data"]

    base = f"{API_BASE}/projects/{project}/locations/{location}"
    eng_url = f"{base}/collections/default_collection/engines/{engine_id}"
    widget_url = f"{eng_url}/widgetConfigs/default_search_widget_config"

    eng_code, eng_data = de_request("GET", eng_url, project, timeout=timeout)
    wid_code, wid_data = de_request("GET", widget_url, project, timeout=timeout)

    bound_datastore_ids: list[str] = (
        eng_data.get("dataStoreIds", []) if eng_code == 200 and isinstance(eng_data, dict) else []
    )

    connectors_by_id: dict[str, dict[str, Any]] = {}
    consumed_datastores: set[str] = set()

    # 1. Authoritative action/federated connectors from widgetConfig.collectionComponents
    if wid_code == 200 and isinstance(wid_data, dict):
        for comp in wid_data.get("collectionComponents", []):
            cid = clean_collection_id(comp.get("id") or comp.get("name", "").split("/")[-1])
            if not cid or cid == "default_collection":
                continue
            auth_state = comp.get("connectorAuthState", {})
            ds_comps = [
                d.get("id") or d.get("name", "").split("/")[-1]
                for d in comp.get("dataStoreComponents", [])
                if d.get("id") or d.get("name")
            ]
            for ds_id in ds_comps:
                consumed_datastores.add(ds_id)
            for b_ds in bound_datastore_ids:
                if b_ds == cid or b_ds.startswith(f"{cid}_"):
                    consumed_datastores.add(b_ds)

            connectors_by_id[cid] = {
                "connectorId": cid,
                "displayName": comp.get("displayName", cid),
                "dataSource": comp.get("dataSource", ""),
                "dataSourceDisplayName": comp.get("dataSourceDisplayName", ""),
                "authorized": auth_state.get("authState") == "AUTHORIZED",
                "authState": auth_state.get("authState", "UNKNOWN"),
                "boundDataStores": ds_comps,
            }

    # 2. Enrich engine-bound connectors with enabledActionsCount from their dataConnector resource
    for cid, info in list(connectors_by_id.items()):
        dc_url = f"{base}/collections/{cid}/dataConnector"
        dc_code, dc_data = de_request("GET", dc_url, project, timeout=timeout)
        if dc_code == 200 and isinstance(dc_data, dict):
            enabled_actions = dc_data.get("bapConfig", {}).get("enabledActions", [])
            info["state"] = dc_data.get("state", "ACTIVE")
            info["actionState"] = dc_data.get("actionState", "")
            info["connectorModes"] = dc_data.get("connectorModes", [])
            info["enabledActionsCount"] = len(enabled_actions)
            info["enabledActions"] = enabled_actions

    # 3. Remaining bound dataStoreIds on the engine (e.g. GCS / BQ search data stores attached to this engine)
    bound_search_datastores = [
        ds_id for ds_id in bound_datastore_ids if ds_id not in consumed_datastores
    ]

    result = {
        "project": project,
        "engineId": engine_id,
        "engineDisplayName": eng_data.get("displayName", engine_id) if eng_code == 200 else engine_id,
        "connectors": list(connectors_by_id.values()),
        "allowlistedConnectorIds": sorted(connectors_by_id.keys()),
        "boundSearchDataStores": bound_search_datastores,
    }

    if eng_code == 200 or wid_code == 200:
        _ENGINE_SCOPE_CACHE["key"] = cache_key
        _ENGINE_SCOPE_CACHE["data"] = result
        _ENGINE_SCOPE_CACHE["expires_at"] = now + 120

    return result


def resolve_and_validate_connector(
    requested_connector: str,
    project: str,
    location: str,
    engine_id: str,
) -> tuple[str | None, str | None]:
    """
    Validates that `requested_connector` (by collection ID, dataSource name, or displayName)
    is strictly bound to `engine_id`.
    Returns `(resolved_collection_id, error_message)`.
    """
    scope = discover_engine_bound_resources(project, location, engine_id)
    allowlisted = scope.get("connectors", [])
    target = clean_collection_id(requested_connector)
    target_lower = target.lower()

    for c in allowlisted:
        if c["connectorId"].lower() == target_lower:
            return c["connectorId"], None

    for c in allowlisted:
        if (
            c.get("dataSource", "").lower() == target_lower
            or c.get("displayName", "").lower() == target_lower
            or c.get("dataSourceDisplayName", "").lower() == target_lower
            or target_lower in c["connectorId"].lower()
        ):
            return c["connectorId"], None

    allowed_summary = ", ".join(
        f"'{c['connectorId']}' ({c.get('dataSourceDisplayName') or c.get('dataSource')})"
        for c in allowlisted
    ) or "none"
    return (
        None,
        (
            f"Access Denied: Connector '{requested_connector}' is NOT linked to Gemini Enterprise "
            f"instance '{engine_id}'. Only connectors bound to '{engine_id}' are permitted: "
            f"{allowed_summary}."
        ),
    )


def invoke_connector_mcp(
    project: str,
    location: str,
    connector_id: str,
    mcp_payload: dict[str, Any],
    timeout: int = 60,
) -> tuple[int, Any]:
    """Calls Discovery Engine dataConnector:invokeConnectorMcp."""
    col_id = clean_collection_id(connector_id)
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


def build_meta_tools(project: str, location: str, engine_id: str) -> list[dict[str, Any]]:
    """Builds the 4 engine-scoped meta-tools, embedding the live engine allowlist into their descriptions."""
    connector_summary = "call `ge_list_connectors` to view bound connectors"
    try:
        scope = discover_engine_bound_resources(project, location, engine_id, timeout=6)
        conns = scope.get("connectors", [])
        if conns:
            connector_summary = ", ".join(
                f"{c['connectorId']} ({c.get('dataSourceDisplayName') or c.get('dataSource')})"
                for c in conns
            )
    except Exception:
        pass

    connector_prop: dict[str, Any] = {
        "type": "string",
        "description": (
            f"Connector ID or data source name bound to engine '{engine_id}'. "
            f"Bound connectors: {connector_summary}."
        ),
    }

    return [
        {
            "name": "ge_list_connectors",
            "description": (
                f"List ONLY the connectors and search data stores attached to Gemini Enterprise "
                f"instance '{engine_id}' (project '{project}'). Unlinked project connectors are hidden."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "Set true to bypass cache and re-fetch the engine's bound connectors.",
                    }
                },
            },
        },
        {
            "name": "ge_search_tools",
            "description": (
                f"Discover available MCP tools and their input schemas on a connector bound to "
                f"Gemini Enterprise instance '{engine_id}'. Bound connectors: {connector_summary}. "
                "Always call this before calling `ge_call_tool` for a tool whose schema you haven't seen."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector": connector_prop,
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Optional keyword filter to match tool names or descriptions "
                            "(e.g. 'issue', 'project', 'board', 'mail', 'calendar', 'file', 'sql'). "
                            "Leave empty to list all tools."
                        ),
                    },
                    "include_schemas": {
                        "type": "boolean",
                        "description": (
                            "Whether to include full JSON inputSchema for matching tools (default: true)."
                        ),
                    },
                },
                "required": ["connector"],
            },
        },
        {
            "name": "ge_call_tool",
            "description": (
                f"Execute an MCP tool on a connector bound to Gemini Enterprise instance '{engine_id}'. "
                f"Bound connectors: {connector_summary}. Calls to connectors not linked to '{engine_id}' "
                "are strictly blocked."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector": connector_prop,
                    "tool_name": {
                        "type": "string",
                        "description": "Exact MCP tool name (from `ge_search_tools`, e.g. 'search_issues', 'create_issue').",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "JSON object of arguments matching the tool's inputSchema.",
                    },
                },
                "required": ["connector", "tool_name"],
            },
        },
        {
            "name": "ge_assist_query",
            "description": (
                f"Run a natural-language search or assistant query against Gemini Enterprise Engine "
                f"'{engine_id}' (`streamAssist`), grounded across the engine's bound search data stores."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question or search query for the Gemini Enterprise engine.",
                    }
                },
                "required": ["query"],
            },
        },
    ]


def handle_tool_call(
    name: str,
    arguments: dict[str, Any],
    project: str,
    location: str,
    engine_id: str,
) -> dict[str, Any]:
    """Executes one of the 4 engine-scoped meta-tools."""
    try:
        if name == "ge_list_connectors":
            refresh = bool(arguments.get("refresh", False))
            scope = discover_engine_bound_resources(
                project, location, engine_id, force_refresh=refresh
            )
            return {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(scope, indent=2)}],
            }

        if name == "ge_search_tools":
            raw_conn = arguments.get("connector", "")
            cid, err = resolve_and_validate_connector(raw_conn, project, location, engine_id)
            if err or not cid:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": err or "Invalid connector"}],
                }

            keyword = (arguments.get("keyword") or "").strip().lower()
            include_schemas = arguments.get("include_schemas", True)

            code, data = invoke_connector_mcp(
                project,
                location,
                cid,
                {"method": "tools/list", "params": {}},
            )
            if code != 200 or not isinstance(data, dict):
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                }

            raw_tools = data.get("result", {}).get("tools") or data.get("tools") or []
            matched = []
            for t in raw_tools:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                t_name = t["name"]
                t_desc = t.get("description", "")
                if keyword and keyword not in t_name.lower() and keyword not in t_desc.lower():
                    continue
                entry: dict[str, Any] = {
                    "name": t_name,
                    "description": t_desc,
                }
                if include_schemas:
                    entry["inputSchema"] = _normalize_json_schema(
                        t.get("inputSchema") or {"type": "object", "properties": {}}
                    )
                matched.append(entry)

            return {
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "engineId": engine_id,
                                "connectorId": cid,
                                "totalToolsOnConnector": len(raw_tools),
                                "matchingToolsCount": len(matched),
                                "tools": matched,
                            },
                            indent=2,
                        ),
                    }
                ],
            }

        if name == "ge_call_tool":
            raw_conn = arguments.get("connector", "")
            cid, err = resolve_and_validate_connector(raw_conn, project, location, engine_id)
            if err or not cid:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": err or "Invalid connector"}],
                }

            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments") or {}
            code, data = invoke_connector_mcp(
                project,
                location,
                cid,
                {
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": tool_args},
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

        if name == "ge_assist_query":
            query_text = arguments.get("query", "")
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

        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Unknown meta-tool '{name}'. Use `ge_list_connectors`, "
                        "`ge_search_tools`, `ge_call_tool`, or `ge_assist_query`."
                    ),
                }
            ],
        }
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Error executing '{name}': {e}"}],
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
    parser = argparse.ArgumentParser(
        description="Engine-Scoped Gemini Enterprise MCP Router for Antigravity"
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GE_MCP_PROJECT", "16933400417"),
        help="GCP Project ID or Project Number",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("GE_MCP_LOCATION", "global"),
        help="Discovery Engine location (default: global)",
    )
    parser.add_argument(
        "--engine",
        default=os.environ.get("GE_MCP_ENGINE", "test1-agentspace_1741135345115"),
        help="Gemini Enterprise Engine ID to scope connectors and data stores to",
    )
    parser.add_argument(
        "--call-tool",
        default=None,
        help="Optional one-shot meta-tool name to execute and exit (for CLI testing)",
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
                        "name": "gemini-enterprise",
                        "version": "2.0.0",
                    },
                },
            )
        elif method == "ping":
            send_response(msg_id, {})
        elif method == "tools/list":
            tools = build_meta_tools(args.project, args.location, args.engine)
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
