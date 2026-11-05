#!/usr/bin/env python3
"""
ge_mcp_server.py (v2 - Engine-Scoped Meta-Tool Router)

A zero-dependency STDIO Model Context Protocol (MCP) server for Google Antigravity (AGY)
that connects to a specific Gemini Enterprise (Agentspace / Discovery Engine) instance
and dynamically routes tool discovery, action execution, and grounded search strictly
across the connectors and data stores bound to that Engine ID.

API Version Notes:
  - Engine metadata (`engines/{id}`) and grounded assistant queries (`:streamAssist`)
    use the General Availability `v1` Discovery Engine API.
  - Connector MCP invocation (`dataConnector:invokeConnectorMcp`) and widget/connector
    action state metadata (`widgetConfigs`, `dataConnector`) currently require `v1alpha`
    (tested against `v1` and `v1beta`, which return HTTP 404 for `:invokeConnectorMcp`).
"""

import argparse
import datetime
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _api_base(location: str, version: str = "v1") -> str:
    loc = (location or "global").strip().lower()
    if loc in ("us", "eu"):
        return f"https://{loc}-discoveryengine.googleapis.com/{version}"
    return f"https://discoveryengine.googleapis.com/{version}"


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for cafile in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if os.path.exists(cafile):
            try:
                ctx.load_verify_locations(cafile=cafile)
            except Exception:
                pass
    return ctx


_SSL_CTX = _build_ssl_context()


def _discover_gcloud_candidates() -> list[str]:
    candidates: list[str] = []
    env_gcloud = os.environ.get("GCLOUD_BIN")
    if env_gcloud:
        candidates.append(os.path.expanduser(os.path.expandvars(env_gcloud)))
    which_gcloud = shutil.which("gcloud")
    if which_gcloud:
        candidates.append(which_gcloud)
    for p in (
        os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
        os.path.expanduser("~/.local/share/google-cloud-sdk/bin/gcloud"),
        "/opt/homebrew/bin/gcloud",
        "/usr/local/bin/gcloud",
        "/usr/bin/gcloud",
        "/snap/bin/gcloud",
        "gcloud",
    ):
        if p not in candidates:
            candidates.append(p)
    return candidates


_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_ENGINE_SCOPE_CACHE: dict[str, Any] = {"key": "", "data": None, "expires_at": 0.0}


def get_access_token() -> str:
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"]:
        return _TOKEN_CACHE["token"]

    env_token = os.environ.get("GE_MCP_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()

    env = dict(os.environ)
    existing_metrics = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "")
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing_metrics} datacloud.antigravity".strip()
    )

    last_err = ""
    for gcloud_bin in _discover_gcloud_candidates():
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
        f"Failed to obtain Google Cloud access token via gcloud. "
        f"Run 'gcloud auth login' first. Last error: {last_err}"
    )


def de_request(
    method: str,
    url: str,
    project: str,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
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


def resolve_engine_and_location(
    project: str, req_engine: str = "", req_location: str = ""
) -> tuple[str, str, str]:
    """
    Resolves or validates (engine_id, location, display_name) using the GA v1
    Discovery Engine API across global, us, and eu.
    """
    req_engine = (req_engine or "").strip()
    req_location = (req_location or "").strip().lower()
    locations_to_scan = [req_location] if req_location else ["global", "us", "eu"]

    if req_engine:
        for loc in locations_to_scan:
            url = (
                f"{_api_base(loc, 'v1')}/projects/{project}/locations/{loc}/"
                f"collections/default_collection/engines/{req_engine}"
            )
            code, data = de_request("GET", url, project, timeout=20)
            if code == 200 and isinstance(data, dict) and data.get("name"):
                return req_engine, loc, data.get("displayName", req_engine)

        other_locs = [l for l in ("global", "us", "eu") if l not in locations_to_scan]
        for loc in other_locs:
            url = (
                f"{_api_base(loc, 'v1')}/projects/{project}/locations/{loc}/"
                f"collections/default_collection/engines/{req_engine}"
            )
            code, data = de_request("GET", url, project, timeout=20)
            if code == 200 and isinstance(data, dict) and data.get("name"):
                sys.stderr.write(
                    f"WARN: Engine '{req_engine}' was not found in location '{req_location}', "
                    f"but WAS found in location '{loc}'. Using location='{loc}'.\n"
                )
                return req_engine, loc, data.get("displayName", req_engine)

        if req_location:
            return req_engine, req_location, req_engine
        raise RuntimeError(
            f"Could not find Gemini Enterprise engine '{req_engine}' in project '{project}' "
            "across locations: global, us, eu."
        )

    found: list[tuple[str, str, str, str]] = []
    for loc in locations_to_scan:
        url = (
            f"{_api_base(loc, 'v1')}/projects/{project}/locations/{loc}/"
            "collections/default_collection/engines"
        )
        code, data = de_request("GET", url, project, timeout=20)
        if code == 200 and isinstance(data, dict):
            for eng in data.get("engines", []):
                eid = eng.get("name", "").split("/")[-1]
                if eid:
                    found.append(
                        (
                            eid,
                            loc,
                            eng.get("displayName", eid),
                            eng.get("solutionType", ""),
                        )
                    )

    if not found:
        raise RuntimeError(
            f"No Gemini Enterprise engines found in project '{project}' "
            f"(scanned locations: {', '.join(locations_to_scan)})."
        )
    if len(found) == 1:
        eid, loc, dname, _ = found[0]
        return eid, loc, dname

    lines = ["Multiple Gemini Enterprise engines found in project:"]
    for eid, loc, dname, stype in found:
        lines.append(f"  --engine {eid} --location {loc}   ({dname}) [{stype}]")
    raise RuntimeError("\n".join(lines))


def write_dynamic_mcp_configs(
    project: str, engine: str, location: str, server_name: str = "gemini-enterprise"
) -> list[str]:
    """
    Safely merges the dynamic $HOME/$PATH 'gemini-enterprise' MCP server entry into
    ~/.gemini/config/mcp_config.json and ~/.gemini/antigravity/mcp_config.json.
    """
    config_paths = [
        os.path.expanduser("~/.gemini/config/mcp_config.json"),
        os.path.expanduser("~/.gemini/antigravity/mcp_config.json"),
    ]
    server_entry = {
        "command": "sh",
        "args": [
            "-c",
            'PATH="$HOME/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" exec python3 "$HOME/.gemini/config/ge_mcp_server.py"',
        ],
        "env": {
            "GE_MCP_PROJECT": str(project),
            "GE_MCP_ENGINE": str(engine),
            "GE_MCP_LOCATION": str(location),
            "PYTHONUNBUFFERED": "1",
        },
    }
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    updated: list[str] = []

    for cfg_path in config_paths:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        data: dict[str, Any] = {"mcpServers": {}}
        if os.path.exists(cfg_path):
            backup_path = f"{cfg_path}.bak_{timestamp}"
            shutil.copy2(cfg_path, backup_path)
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            except Exception:
                pass
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}

        data["mcpServers"][server_name] = server_entry
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        updated.append(cfg_path)
    return updated


def discover_engine_bound_resources(
    project: str,
    location: str,
    engine_id: str,
    force_refresh: bool = False,
    timeout: int = 15,
) -> dict[str, Any]:
    now = time.time()
    cache_key = f"{project}:{location}:{engine_id}"
    if (
        not force_refresh
        and _ENGINE_SCOPE_CACHE.get("key") == cache_key
        and _ENGINE_SCOPE_CACHE.get("data") is not None
        and now < _ENGINE_SCOPE_CACHE.get("expires_at", 0.0)
    ):
        return _ENGINE_SCOPE_CACHE["data"]

    base_v1 = f"{_api_base(location, 'v1')}/projects/{project}/locations/{location}"
    base_v1alpha = f"{_api_base(location, 'v1alpha')}/projects/{project}/locations/{location}"

    eng_url = f"{base_v1}/collections/default_collection/engines/{engine_id}"
    widget_url = f"{base_v1alpha}/collections/default_collection/engines/{engine_id}/widgetConfigs/default_search_widget_config"

    eng_code, eng_data = de_request("GET", eng_url, project, timeout=timeout)
    wid_code, wid_data = de_request("GET", widget_url, project, timeout=timeout)

    bound_datastore_ids: list[str] = (
        eng_data.get("dataStoreIds", [])
        if eng_code == 200 and isinstance(eng_data, dict)
        else []
    )

    connectors_by_id: dict[str, dict[str, Any]] = {}
    consumed_datastores: set[str] = set()

    if wid_code == 200 and isinstance(wid_data, dict):
        for comp in wid_data.get("collectionComponents", []):
            cid = clean_collection_id(
                comp.get("id") or comp.get("name", "").split("/")[-1]
            )
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

    for cid, info in list(connectors_by_id.items()):
        dc_url = f"{base_v1alpha}/collections/{cid}/dataConnector"
        dc_code, dc_data = de_request("GET", dc_url, project, timeout=timeout)
        if dc_code == 200 and isinstance(dc_data, dict):
            enabled_actions = dc_data.get("bapConfig", {}).get("enabledActions", [])
            info["state"] = dc_data.get("state", "ACTIVE")
            info["actionState"] = dc_data.get("actionState", "")
            info["connectorModes"] = dc_data.get("connectorModes", [])
            info["enabledActionsCount"] = len(enabled_actions)
            info["enabledActions"] = enabled_actions

    bound_search_datastores = [
        ds_id for ds_id in bound_datastore_ids if ds_id not in consumed_datastores
    ]

    result = {
        "project": project,
        "location": location,
        "engineId": engine_id,
        "engineDisplayName": (
            eng_data.get("displayName", engine_id)
            if eng_code == 200 and isinstance(eng_data, dict)
            else engine_id
        ),
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

    allowed_summary = (
        ", ".join(
            f"'{c['connectorId']}' ({c.get('dataSourceDisplayName') or c.get('dataSource')})"
            for c in allowlisted
        )
        or "none"
    )
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
    col_id = clean_collection_id(connector_id)
    # :invokeConnectorMcp currently requires v1alpha (returns 404 in v1/v1beta)
    url = (
        f"{_api_base(location, 'v1alpha')}/projects/{project}/locations/{location}/"
        f"collections/{col_id}/dataConnector:invokeConnectorMcp"
    )
    body = {k: v for k, v in mcp_payload.items() if k not in ("jsonrpc", "id")}
    return de_request("POST", url, project, body=body, timeout=timeout)


def _normalize_json_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    t = out.get("type")
    if isinstance(t, list):
        non_null = [str(x).lower() for x in t if x != "null"]
        out["type"] = non_null[0] if non_null else "string"
    elif isinstance(t, str):
        out["type"] = t.lower()
    if isinstance(out.get("properties"), dict):
        out["properties"] = {
            k: _normalize_json_schema(v) for k, v in out["properties"].items()
        }
    if isinstance(out.get("items"), dict):
        out["items"] = _normalize_json_schema(out["items"])
    return out


_HTML_TAG_RE = re.compile(
    r"<\s*(?:html|body|div|p|br|ul|ol|li|table|thead|tbody|tr|th|td|h[1-6]|strong|b|em|i|a|span|pre|code|hr|blockquote)\b",
    re.IGNORECASE,
)


def _format_inline_html(text: str) -> str:
    escaped = html.escape(text)
    link_placeholders: list[str] = []

    def _save_md_link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        idx = len(link_placeholders)
        link_placeholders.append(
            f'<a href="{url}" style="color:#1a73e8;text-decoration:none;">{label}</a>'
        )
        return f"@@MDLINK_{idx}@@"

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _save_md_link, escaped
    )
    escaped = re.sub(
        r"(https?://[^\s<]+)",
        lambda m: (
            f'<a href="{m.group(1).rstrip(".,;:)")}" style="color:#1a73e8;text-decoration:none;">'
            f'{m.group(1).rstrip(".,;:)")}</a>'
            + m.group(1)[len(m.group(1).rstrip(".,;:)")) :]
        ),
        escaped,
    )
    for idx, anchor in enumerate(link_placeholders):
        escaped = escaped.replace(f"@@MDLINK_{idx}@@", anchor)

    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#f1f3f4;padding:1px 4px;border-radius:3px;font-family:monospace;">\1</code>',
        escaped,
    )
    return escaped


def format_email_content_as_html(content: str) -> str:
    """
    Gmail's Discovery Engine connector (`send_message` / `reply_to_mail_message`)
    sends emails with `Content-Type: text/html; charset=UTF-8`. If a caller passes
    plain text or Markdown without HTML tags, Gmail collapses all newlines into a
    single unformatted line. This helper converts plain text / Markdown into clean,
    readable HTML while leaving existing HTML untouched.
    """
    if not content or not isinstance(content, str):
        return content
    if _HTML_TAG_RE.search(content):
        return content

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html_parts: list[str] = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#202124;">'
    ]
    in_ul = False
    in_ol = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            html_parts.append("</ul>")
            in_ul = False
        if in_ol:
            html_parts.append("</ol>")
            in_ol = False

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()

        if not stripped:
            _close_lists()
            i += 1
            continue

        # Markdown table detection
        if (
            "|" in stripped
            and i + 1 < len(lines)
            and re.match(r"^\s*\|?[\s:-]+\|[\s|:-]*$", lines[i + 1])
        ):
            _close_lists()
            headers = [
                c.strip() for c in stripped.strip("|").split("|")
            ]
            html_parts.append(
                '<table style="border-collapse:collapse;width:100%;margin:12px 0;font-size:13px;">'
                "<thead><tr>"
            )
            for h in headers:
                html_parts.append(
                    f'<th style="border:1px solid #dadce0;background:#f8f9fa;padding:8px 10px;text-align:left;font-weight:600;">{_format_inline_html(h)}</th>'
                )
            html_parts.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i].strip():
                row_cells = [
                    c.strip() for c in lines[i].strip().strip("|").split("|")
                ]
                html_parts.append("<tr>")
                for c in row_cells:
                    html_parts.append(
                        f'<td style="border:1px solid #dadce0;padding:8px 10px;vertical-align:top;">{_format_inline_html(c)}</td>'
                    )
                html_parts.append("</tr>")
                i += 1
            html_parts.append("</tbody></table>")
            continue

        # Headings
        heading_match = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading_match:
            _close_lists()
            level = len(heading_match.group(1))
            sizes = {1: "18px", 2: "16px", 3: "15px", 4: "14px"}
            html_parts.append(
                f'<h{level} style="font-size:{sizes.get(level, "14px")};margin:14px 0 6px 0;color:#202124;">'
                f"{_format_inline_html(heading_match.group(2))}</h{level}>"
            )
            i += 1
            continue

        # Ordered list item
        ol_match = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if ol_match:
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            if not in_ol:
                html_parts.append('<ol style="margin:8px 0;padding-left:22px;">')
                in_ol = True
            item_html = _format_inline_html(ol_match.group(2))
            sub_items: list[str] = []
            while i + 1 < len(lines):
                next_line = lines[i + 1]
                sub_match = re.match(r"^\s{2,}[-*•]\s+(.+)$", next_line)
                if sub_match:
                    sub_items.append(
                        f'<li style="margin:2px 0;">{_format_inline_html(sub_match.group(1))}</li>'
                    )
                    i += 1
                else:
                    break
            if sub_items:
                item_html += (
                    '<ul style="margin:4px 0 6px 0;padding-left:18px;">'
                    + "".join(sub_items)
                    + "</ul>"
                )
            html_parts.append(f'<li style="margin:6px 0;">{item_html}</li>')
            i += 1
            continue

        # Unordered list item
        ul_match = re.match(r"^[-*•]\s+(.+)$", stripped)
        if ul_match:
            if in_ol:
                html_parts.append("</ol>")
                in_ol = False
            if not in_ul:
                html_parts.append('<ul style="margin:8px 0;padding-left:22px;">')
                in_ul = True
            html_parts.append(
                f'<li style="margin:4px 0;">{_format_inline_html(ul_match.group(1))}</li>'
            )
            i += 1
            continue

        # Standard paragraph
        _close_lists()
        html_parts.append(
            f'<p style="margin:8px 0;">{_format_inline_html(stripped)}</p>'
        )
        i += 1

    _close_lists()
    html_parts.append("</div>")
    return "".join(html_parts)


def build_meta_tools(
    project: str, location: str, engine_id: str
) -> list[dict[str, Any]]:
    connector_summary = "call `ge_list_connectors` to view bound connectors"
    try:
        scope = discover_engine_bound_resources(
            project, location, engine_id, timeout=6
        )
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
                f"instance '{engine_id}' (project '{project}', location '{location}'). Unlinked project connectors are hidden."
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
                            "(e.g. 'issue', 'project', 'mail', 'calendar', 'file', 'sql'). Leave empty to list all tools."
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


_FALLBACK_CONNECTOR_TOOLS: dict[str, dict[str, dict[str, Any]]] = {
    "google_mail": {
        "send_message": {
            "name": "send_message",
            "description": (
                "Send an email message to one or more recipients using Gmail. "
                "Supports plain text, Markdown, or HTML in `Content` (plain text and Markdown "
                "are automatically converted to clean HTML so line breaks and formatting are preserved)."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["To", "Subject", "Content"],
                "properties": {
                    "To": {
                        "type": "string",
                        "description": "Primary recipient email address(es), comma-separated.",
                    },
                    "Subject": {
                        "type": "string",
                        "description": "Subject line for the email.",
                    },
                    "Content": {
                        "type": "string",
                        "description": "Main body content of the email (HTML, Markdown, or multi-line plain text).",
                    },
                    "CC": {
                        "type": "string",
                        "description": "Optional CC recipient email address(es), comma-separated.",
                    },
                    "BCC": {
                        "type": "string",
                        "description": "Optional BCC recipient email address(es), comma-separated.",
                    },
                    "AttachmentFileName": {
                        "type": "string",
                        "description": "Optional attachment file name (e.g. 'report.pdf').",
                    },
                    "AttachmentContent": {
                        "type": "string",
                        "description": "Optional text content for a single text attachment.",
                    },
                    "AttachmentContentBytes": {
                        "type": "string",
                        "description": "Optional Base64-encoded binary content for a single attachment.",
                    },
                    "AttachmentHasBytes": {
                        "type": "boolean",
                        "description": "Set true when providing AttachmentContentBytes, false for AttachmentContent.",
                    },
                },
            },
        },
        "reply_to_mail_message": {
            "name": "reply_to_mail_message",
            "description": (
                "Send a reply to a specific email message in Gmail identified by its `MessageId`. "
                "Supports plain text, Markdown, or HTML in `Content`."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["MessageId", "To", "Content"],
                "properties": {
                    "MessageId": {
                        "type": "string",
                        "description": "Unique identifier of the email message being replied to.",
                    },
                    "To": {
                        "type": "string",
                        "description": "Primary recipient(s) of the email reply, comma-separated.",
                    },
                    "Content": {
                        "type": "string",
                        "description": "Main body of the email reply (HTML, Markdown, or multi-line plain text).",
                    },
                    "CC": {
                        "type": "string",
                        "description": "Optional CC recipient(s), comma-separated.",
                    },
                    "BCC": {
                        "type": "string",
                        "description": "Optional BCC recipient(s), comma-separated.",
                    },
                    "From": {
                        "type": "string",
                        "description": "Optional sender email address.",
                    },
                },
            },
        },
        "create_label": {
            "name": "create_label",
            "description": "Create a new organizational label in Gmail.",
            "inputSchema": {
                "type": "object",
                "required": ["Name"],
                "properties": {
                    "Name": {
                        "type": "string",
                        "description": "Display name of the label.",
                    },
                    "LabelListVisibility": {
                        "type": "string",
                        "description": "Acceptable values: LabelHide, LabelShow, LabelShowIfUnread.",
                    },
                    "MessageListVisibility": {
                        "type": "string",
                        "description": "Acceptable values: HIDE, SHOW.",
                    },
                },
            },
        },
    }
}


def handle_tool_call(
    name: str,
    arguments: dict[str, Any],
    project: str,
    location: str,
    engine_id: str,
) -> dict[str, Any]:
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
            cid, err = resolve_and_validate_connector(
                raw_conn, project, location, engine_id
            )
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

            raw_tools = list(
                data.get("result", {}).get("tools") or data.get("tools") or []
            )
            existing_names = {
                t.get("name") for t in raw_tools if isinstance(t, dict) and t.get("name")
            }

            scope = discover_engine_bound_resources(project, location, engine_id)
            conn_info = next(
                (c for c in scope.get("connectors", []) if c["connectorId"] == cid),
                {},
            )
            data_source = conn_info.get("dataSource", "")
            enabled_actions = conn_info.get("enabledActions", [])
            fallbacks = _FALLBACK_CONNECTOR_TOOLS.get(data_source, {})
            for act in enabled_actions:
                if act not in existing_names and act in fallbacks:
                    raw_tools.append(fallbacks[act])
                    existing_names.add(act)

            matched = []
            for t in raw_tools:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                t_name = t["name"]
                t_desc = t.get("description", "")
                if (
                    keyword
                    and keyword not in t_name.lower()
                    and keyword not in t_desc.lower()
                ):
                    continue
                entry: dict[str, Any] = {
                    "name": t_name,
                    "description": t_desc,
                }
                if include_schemas:
                    raw_schema = (
                        t.get("inputSchema")
                        or t.get("parameters")
                        or {"type": "object", "properties": {}}
                    )
                    entry["inputSchema"] = _normalize_json_schema(raw_schema)
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
            cid, err = resolve_and_validate_connector(
                raw_conn, project, location, engine_id
            )
            if err or not cid:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": err or "Invalid connector"}],
                }

            tool_name = arguments.get("tool_name", "")
            tool_args = dict(arguments.get("arguments") or {})

            if tool_name in ("send_message", "reply_to_mail_message") and isinstance(
                tool_args.get("Content"), str
            ):
                tool_args["Content"] = format_email_content_as_html(
                    tool_args["Content"]
                )

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
            # :streamAssist is available on the GA v1 endpoint
            url = (
                f"{_api_base(location, 'v1')}/projects/{project}/locations/{location}/"
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


def send_response(
    msg_id: Any, result: Any = None, error: dict[str, Any] | None = None
) -> None:
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
        default=os.environ.get("GE_MCP_PROJECT", ""),
        help="GCP Project ID or Number (or set GE_MCP_PROJECT env var)",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("GE_MCP_LOCATION", ""),
        help="Location of the Gemini Enterprise instance: global, us, or eu (or set GE_MCP_LOCATION env var)",
    )
    parser.add_argument(
        "--engine",
        default=os.environ.get("GE_MCP_ENGINE", ""),
        help="Gemini Enterprise Engine ID to scope connectors and data stores to (or set GE_MCP_ENGINE env var)",
    )
    parser.add_argument(
        "--resolve-engine",
        action="store_true",
        help="Resolve/validate engine ID and its location via Discovery Engine v1 API and print ENGINE|LOCATION|DISPLAY_NAME",
    )
    parser.add_argument(
        "--write-mcp-config",
        action="store_true",
        help="Write the dynamic $HOME/$PATH 'gemini-enterprise' entry into ~/.gemini/config/mcp_config.json and ~/.gemini/antigravity/mcp_config.json",
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

    if not args.project:
        sys.stderr.write("ERROR: --project (or GE_MCP_PROJECT) is required.\n")
        sys.exit(1)

    if args.resolve_engine:
        try:
            eid, loc, dname = resolve_engine_and_location(
                args.project, args.engine, args.location
            )
            print(f"{eid}|{loc}|{dname}")
            return
        except Exception as e:
            sys.stderr.write(f"ERROR: {e}\n")
            sys.exit(2)

    location = args.location or "global"

    if not args.engine:
        sys.stderr.write("ERROR: --engine (or GE_MCP_ENGINE) is required.\n")
        sys.exit(1)

    if args.write_mcp_config:
        updated = write_dynamic_mcp_configs(args.project, args.engine, location)
        for p in updated:
            print(f"      Updated {p}")
        return

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
            location,
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
            tools = build_meta_tools(args.project, location, args.engine)
            send_response(msg_id, {"tools": tools})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            res = handle_tool_call(
                tool_name,
                tool_args,
                args.project,
                location,
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
