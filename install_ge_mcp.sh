#!/usr/bin/env bash
# ==============================================================================
# install_ge_mcp.sh
#
# Automated installer for the Engine-Scoped Gemini Enterprise MCP Server
# in Google Antigravity (AGY).
#
# Usage:
#   bash install_ge_mcp.sh \
#     --project <PROJECT_ID_OR_NUMBER> \
#     [--engine <ENGINE_ID>] \
#     [--location <ENGINE_LOCATION>]
# ==============================================================================

set -euo pipefail

PROJECT="${GE_MCP_PROJECT:-}"
ENGINE="${GE_MCP_ENGINE:-}"
LOCATION="${GE_MCP_LOCATION:-}"
SERVER_NAME="gemini-enterprise"

export CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.antigravity"

usage() {
  cat <<EOF
Usage:
  bash $(basename "$0") --project <GCP_PROJECT_ID_OR_NUMBER> [--engine <ENGINE_ID>] [--location <ENGINE_LOCATION>]

Options:
  --project <PROJECT>     (Required) GCP Project ID or Project Number hosting Gemini Enterprise
                          (or set GE_MCP_PROJECT).
  --engine <ENGINE_ID>    (Optional) Gemini Enterprise Engine / App ID (or set GE_MCP_ENGINE).
                          If omitted, the installer scans Discovery Engine locations (global, us, eu)
                          and auto-selects if 1 engine exists, or lists all engines so you can choose one.
  --location <LOCATION>   (Optional) Location of the Gemini Enterprise instance (e.g. global, us, eu)
                          (or set GE_MCP_LOCATION). Must match the location where the engine lives.
                          If omitted, the installer auto-detects the location from the engine.
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
      LOCATION="${2:-}"
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
  echo "ERROR: --project <GCP_PROJECT_ID_OR_NUMBER> (or GE_MCP_PROJECT) is required." >&2
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

# 4. Auto-discover or validate --engine and its --location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PY="$SCRIPT_DIR/ge_mcp_server.py"
if [[ ! -f "$SOURCE_PY" ]]; then
  echo "ERROR: Could not find ge_mcp_server.py in $SCRIPT_DIR" >&2
  exit 1
fi

echo "[4/6] Resolving Gemini Enterprise Engine & Location in project $PROJECT_TARGET..."
RESOLVED_PAIR="$("$PYTHON_BIN" "$SOURCE_PY" --project="$PROJECT_TARGET" --engine="$ENGINE" --location="$LOCATION" --resolve-engine)" || {
  echo "" >&2
  echo "Please re-run with explicit --engine <ENGINE_ID> and --location <LOCATION>." >&2
  exit 1
}

IFS='|' read -r ENGINE LOCATION ENGINE_DISPLAY_NAME <<< "$RESOLVED_PAIR"
echo "      Resolved Engine:   $ENGINE ($ENGINE_DISPLAY_NAME)"
echo "      Engine Location:   $LOCATION"

# 5. Copy ge_mcp_server.py to $HOME/.gemini/config/ge_mcp_server.py and write dynamic mcp_config.json
TARGET_DIR="$HOME/.gemini/config"
TARGET_PY="$TARGET_DIR/ge_mcp_server.py"
mkdir -p "$TARGET_DIR"
mkdir -p "$HOME/.gemini/antigravity"

cp "$SOURCE_PY" "$TARGET_PY"
chmod +x "$TARGET_PY"
echo "[5/6] Installed MCP server script to: \$HOME/.gemini/config/ge_mcp_server.py"

"$PYTHON_BIN" "$TARGET_PY" \
  --project="$PROJECT_TARGET" \
  --engine="$ENGINE" \
  --location="$LOCATION" \
  --write-mcp-config

# 6. Run live smoke test
echo "[6/6] Running smoke test (ge_list_connectors) against engine '$ENGINE' (location '$LOCATION')..."
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
