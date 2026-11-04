#!/usr/bin/env bash
# ==============================================================================
# install_ge_mcp.sh
#
# Automated installer for the Engine-Scoped Gemini Enterprise MCP Server
# in Google Antigravity (AGY).
#
# Usage:
#   bash install_ge_mcp.sh --project <PROJECT_ID_OR_NUMBER> [--engine <ENGINE_ID>] [--location global]
# ==============================================================================

set -euo pipefail

PROJECT=""
ENGINE=""
LOCATION="global"
SERVER_NAME="gemini-enterprise"

usage() {
  cat <<EOF
Usage:
  bash $(basename "$0") --project <GCP_PROJECT_ID_OR_NUMBER> [--engine <ENGINE_ID>] [--location <LOCATION>]

Options:
  --project <PROJECT>     (Required) GCP Project ID or Project Number hosting Gemini Enterprise.
  --engine <ENGINE_ID>    (Optional) Gemini Enterprise Engine / App ID.
                          If omitted, the installer queries Discovery Engine and auto-selects if 1 engine exists,
                          or lists all engines in the project so you can choose one.
  --location <LOCATION>   (Optional) Discovery Engine location: global (default), us, or eu.
  -h, --help              Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT="${2:-}"
      shift 2
      ;;
    --engine)
      ENGINE="${2:-}"
      shift 2
      ;;
    --location)
      LOCATION="${2:-global}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: --project <GCP_PROJECT_ID_OR_NUMBER> is required." >&2
  echo ""
  usage
  exit 1
fi

echo "=================================================================="
echo " Gemini Enterprise MCP Router Installer for Antigravity (AGY)"
echo "=================================================================="

# 1. Detect python3 dynamically
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: python3 not found in PATH. Please install Python 3.9+." >&2
  exit 1
fi
echo "[1/6] Found Python:  $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# 2. Detect gcloud dynamically
GCLOUD_BIN="$(command -v gcloud || true)"
if [[ -z "$GCLOUD_BIN" && -x "$HOME/google-cloud-sdk/bin/gcloud" ]]; then
  GCLOUD_BIN="$HOME/google-cloud-sdk/bin/gcloud"
fi
if [[ -z "$GCLOUD_BIN" ]]; then
  echo "ERROR: gcloud CLI not found in PATH or \$HOME/google-cloud-sdk/bin/gcloud." >&2
  exit 1
fi
echo "[2/6] Found gcloud:  $GCLOUD_BIN"

# 3. Verify gcloud auth token
echo "[3/6] Verifying Google Cloud authentication..."
TOKEN="$("$GCLOUD_BIN" auth print-access-token 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: No active gcloud session. Run 'gcloud auth login' first and re-run this script." >&2
  exit 1
fi

# Resolve numeric project number if a project ID string was provided
RESOLVED_PROJECT_NUM="$("$GCLOUD_BIN" projects describe "$PROJECT" --format="value(projectNumber)" 2>/dev/null || true)"
if [[ -n "$RESOLVED_PROJECT_NUM" ]]; then
  echo "      Resolved project '$PROJECT' -> Project Number: $RESOLVED_PROJECT_NUM"
  PROJECT_TARGET="$RESOLVED_PROJECT_NUM"
else
  PROJECT_TARGET="$PROJECT"
fi

# 4. Auto-discover or validate --engine
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PY="$SCRIPT_DIR/ge_mcp_server.py"
if [[ ! -f "$SOURCE_PY" ]]; then
  echo "ERROR: Could not find ge_mcp_server.py in $SCRIPT_DIR" >&2
  exit 1
fi

if [[ -z "$ENGINE" ]]; then
  echo "[4/6] No --engine specified. Discovering Gemini Enterprise engines in project $PROJECT_TARGET (location=$LOCATION)..."
  ENGINE="$("$PYTHON_BIN" - "$PROJECT_TARGET" "$LOCATION" "$TOKEN" <<'PYEOF'
import json, ssl, sys, urllib.request

project, location, token = sys.argv[1], sys.argv[2], sys.argv[3]
prefix = f"{location}-" if location in ("us", "eu") else ""
url = f"https://{prefix}discoveryengine.googleapis.com/v1alpha/projects/{project}/locations/{location}/collections/default_collection/engines"

ctx = ssl.create_default_context()
for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
    try:
        ctx.load_verify_locations(cafile=cafile)
    except Exception:
        pass

req = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {token}",
    "X-Goog-User-Project": str(project),
})
try:
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception as e:
    print(f"ERROR_FETCH:{e}", file=sys.stderr)
    sys.exit(2)

engines = data.get("engines", [])
if not engines:
    print("NONE", file=sys.stderr)
    sys.exit(3)
elif len(engines) == 1:
    eng_id = engines[0].get("name", "").split("/")[-1]
    print(eng_id)
else:
    print("MULTIPLE", file=sys.stderr)
    for eng in engines:
        eid = eng.get("name", "").split("/")[-1]
        dname = eng.get("displayName", "")
        stype = eng.get("solutionType", "")
        print(f"  - {eid}  ({dname}) [{stype}]", file=sys.stderr)
    sys.exit(4)
PYEOF
  )" || {
    echo "" >&2
    echo "Please re-run with --engine <ENGINE_ID> from the list above." >&2
    exit 1
  }
  echo "      Auto-selected Engine ID: $ENGINE"
else
  echo "[4/6] Using Gemini Enterprise Engine ID: $ENGINE (location=$LOCATION)"
fi

# 5. Copy ge_mcp_server.py to $HOME/.gemini/config/ge_mcp_server.py and write dynamic mcp_config.json
TARGET_DIR="$HOME/.gemini/config"
TARGET_PY="$TARGET_DIR/ge_mcp_server.py"
mkdir -p "$TARGET_DIR"
mkdir -p "$HOME/.gemini/antigravity"

cp "$SOURCE_PY" "$TARGET_PY"
chmod +x "$TARGET_PY"
echo "[5/6] Installed MCP server script to: \$HOME/.gemini/config/ge_mcp_server.py"

"$PYTHON_BIN" - "$PROJECT_TARGET" "$ENGINE" "$LOCATION" "$SERVER_NAME" <<'PYEOF'
import datetime, json, os, shutil, sys

project, engine, location, server_name = sys.argv[1:5]

config_paths = [
    os.path.expanduser("~/.gemini/config/mcp_config.json"),
    os.path.expanduser("~/.gemini/antigravity/mcp_config.json"),
]

# Dynamic shell-based invocation so $HOME and $PATH resolve at runtime on any OS
# (macOS /Users/..., Linux /home/..., Cloudtop, etc.) without hardcoded user paths.
server_entry = {
    "command": "sh",
    "args": [
        "-c",
        'PATH="$HOME/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" exec python3 "$HOME/.gemini/config/ge_mcp_server.py"',
    ],
    "env": {
        "GE_MCP_PROJECT": project,
        "GE_MCP_ENGINE": engine,
        "GE_MCP_LOCATION": location,
        "PYTHONUNBUFFERED": "1",
    },
}

timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

for cfg_path in config_paths:
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    data = {"mcpServers": {}}
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
    print(f"      Updated {cfg_path}")
PYEOF

# 6. Run live smoke test
echo "[6/6] Running smoke test (ge_list_connectors) against engine '$ENGINE'..."
"$PYTHON_BIN" "$TARGET_PY" \
  --project="$PROJECT_TARGET" \
  --engine="$ENGINE" \
  --location="$LOCATION" \
  --call-tool ge_list_connectors

echo ""
echo "=================================================================="
echo " Installation Complete!"
echo " Next Step: Reload your Antigravity window or restart AGY to load"
echo " the '$SERVER_NAME' MCP server."
echo "=================================================================="
