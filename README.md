# Engine-Scoped Gemini Enterprise MCP Router for Antigravity (AGY)

A zero-dependency Python 3 Model Context Protocol (MCP) server (`ge_mcp_server.py`) that connects **Google Antigravity (AGY)** directly to all data connectors and search data stores bound to a specific **Gemini Enterprise (Agentspace / Discovery Engine)** instance.

---

## 1. Architecture Overview

Instead of registering a separate MCP server in `mcp_config.json` for every connector (or installing third-party `npm` packages that may be blocked by corporate proxies), Antigravity loads **one lightweight Python STDIO MCP server** (`gemini-enterprise`) bound to a single Gemini Enterprise Engine (`--engine=<ENGINE_ID>`).

```mermaid
flowchart LR
    User["User in Antigravity IDE / CLI"] --> AGY["Antigravity Agent"]
    AGY <-->|"STDIO JSON-RPC (4 Meta-Tools)"| MCP["ge_mcp_server.py (--project, --engine)"]
    MCP -->|"1. Check Bound Connectors"| Widget["Discovery Engine widgetConfigs (default_search_widget_config)"]
    MCP -->|"2. Discover & Execute Tools"| Invoke["Discovery Engine :invokeConnectorMcp"]
    MCP -->|"3. Grounded Enterprise Search"| Assist["Discovery Engine :streamAssist"]
    Invoke --> Jira["Jira Cloud Connector"]
    Invoke --> M365["Microsoft 365 (Outlook / Teams / SharePoint / OneDrive)"]
    Invoke --> GWorkspace["Google Drive / Gmail / Calendar"]
    Invoke --> BQ["BigQuery / Custom MCP Connectors"]
```

> [!IMPORTANT]
> **Strict Engine-Level Isolation:** `ge_mcp_server.py` inspects the target engine's `default_search_widget_config` (`collectionComponents`) and `dataStoreIds` before listing or invoking any tool. Even if a GCP project hosts dozens of connectors across other Gemini Enterprise instances, **users can only see and invoke connectors attached to their configured `--engine`**.

---

## 2. Repository Contents

| File | Description |
| :--- | :--- |
| [`ge_mcp_server.py`](./ge_mcp_server.py) | Zero-dependency Python 3 MCP server implementing the 4 engine-scoped meta-tools (`ge_list_connectors`, `ge_search_tools`, `ge_call_tool`, `ge_assist_query`). |
| [`install_ge_mcp.sh`](./install_ge_mcp.sh) | Automated installer script that detects `python3` and `gcloud`, discovers or validates `--engine`, installs `ge_mcp_server.py`, safely merges `mcp_config.json`, and runs a live smoke test. |
| [`mcp_config.example.json`](./mcp_config.example.json) | Example `mcp_config.json` snippet showing how Antigravity launches the server. |
| [`legacy_v1/ge_mcp_server.v1_backup.py`](./legacy_v1/ge_mcp_server.v1_backup.py) | Backup of the initial `v1` single-connector MCP server design. |

---

## 3. Prerequisites on the Target Machine

Before running the installer on another user's machine, verify:

1. **Python 3.9+** is installed (`python3 --version`).
2. **Google Cloud SDK (`gcloud`)** is installed and authenticated with the user's account:
   ```bash
   gcloud auth login
   ```
3. **IAM & Per-User Connector OAuth:**
   - The user has Discovery Engine access (`roles/discoveryengine.viewer` or `roles/discoveryengine.user`) on the GCP project.
   - For user-authenticated action connectors (such as **Jira**, **Microsoft Outlook / 365**, **Google Drive**, **Gmail**, **Google Calendar**), the user has clicked **Connect / Authorize** at least once under **Connected apps** in the Gemini Enterprise web app UI so their OAuth refresh token is stored (`authState: AUTHORIZED`).

---

## 4. Step-by-Step Deployment into Another User's AGY Environment

### Option A: Automated 1-Command Setup (Recommended)

Clone this repository and run [`install_ge_mcp.sh`](./install_ge_mcp.sh):

```bash
git clone https://github.com/caugusto/ge_mcp.git
cd ge_mcp

# If you know both your Project ID/Number and Engine ID:
bash install_ge_mcp.sh \
  --project <GCP_PROJECT_ID_OR_NUMBER> \
  --engine <GEMINI_ENTERPRISE_ENGINE_ID> \
  --location global
```

If you **do not know your Engine ID**, omit `--engine`:
```bash
bash install_ge_mcp.sh --project <GCP_PROJECT_ID_OR_NUMBER>
```
- If the project has **one** Gemini Enterprise engine, the installer selects it automatically.
- If the project has **multiple** engines, the installer lists every engine ID and display name in the project so you can pick the right one.

---

### Option B: Manual Setup

1. **Copy the server script to `~/.gemini/config/ge_mcp_server.py` (or reference it directly from your local repo clone):**
   ```bash
   mkdir -p ~/.gemini/config
   cp ge_mcp_server.py ~/.gemini/config/ge_mcp_server.py
   chmod +x ~/.gemini/config/ge_mcp_server.py
   ```

2. **Add the `"gemini-enterprise"` entry to `~/.gemini/config/mcp_config.json` (and `~/.gemini/antigravity/mcp_config.json`):**
   ```json
   {
     "mcpServers": {
       "gemini-enterprise": {
         "command": "/usr/bin/python3",
         "args": [
           "/Users/<TARGET_USERNAME>/.gemini/config/ge_mcp_server.py",
           "--project=<GCP_PROJECT_NUMBER_OR_ID>",
           "--engine=<GEMINI_ENTERPRISE_ENGINE_ID>",
           "--location=global"
         ],
         "env": {
           "PATH": "/Users/<TARGET_USERNAME>/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "PYTHONUNBUFFERED": "1"
         }
       }
     }
   }
   ```

3. **Test the server from the terminal before reloading Antigravity:**
   ```bash
   python3 ~/.gemini/config/ge_mcp_server.py \
     --project=<GCP_PROJECT_NUMBER_OR_ID> \
     --engine=<GEMINI_ENTERPRISE_ENGINE_ID> \
     --location=global \
     --call-tool ge_list_connectors
   ```

4. **Reload or restart Antigravity** so it spawns the `gemini-enterprise` MCP server.

---

## 5. What Needs to Be Updated for Another Environment?

When moving this configuration to another user or another Gemini Enterprise instance, update these **6 values** in `mcp_config.json` (all of which [`install_ge_mcp.sh`](./install_ge_mcp.sh) configures automatically):

| Parameter in `mcp_config.json` | Example Value | What Another User Should Set It To | How to Find It |
| :--- | :--- | :--- | :--- |
| `command` | `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` | Full path to their `python3` binary | Run `which python3` |
| `args[0]` | `/Users/caugusto/Documents/antigravity/ge_mcp/ge_mcp_server.py` | Full path to their `ge_mcp_server.py` | Path where `ge_mcp_server.py` is installed |
| `--project=...` | `--project=16933400417` | Their GCP Project Number (preferred) or Project ID | Run `gcloud projects describe <PROJECT_ID> --format="value(projectNumber)"` |
| `--engine=...` | `--engine=test1-agentspace_1741135345115` | Their Gemini Enterprise App / Engine ID | Found in **Google Cloud Console -> Agent Builder -> Apps**, or by running `bash install_ge_mcp.sh --project <PROJECT>` |
| `--location=...` | `--location=global` | Multi-region of their Gemini Enterprise app (`global`, `us`, or `eu`) | Check the Location column in **Agent Builder -> Apps** (usually `global`) |
| `env.PATH` | `/Users/caugusto/google-cloud-sdk/bin:...` | Must include the directory containing their `gcloud` binary | Run `dirname "$(which gcloud)"` |

> [!TIP]
> **Environment Variable Alternative:** Instead of passing `--project`, `--engine`, and `--location` in `args`, you can also set `GE_MCP_PROJECT`, `GE_MCP_ENGINE`, and `GE_MCP_LOCATION` in the `"env"` block of `mcp_config.json`.

---

## 6. How to Use the MCP Server & How the 4 Meta-Tools Work

You do **not** need to name the meta-tools in your chat prompts—just ask natural-language questions (e.g., *"Create a Jira ticket..."* or *"Check my Outlook inbox..."*). Antigravity automatically orchestrates the 4 meta-tools behind the scenes:

1. **`ge_list_connectors`**
   - **Purpose:** Lists all action connectors and search data stores bound to the configured `--engine`, along with each connector's `authState` (`AUTHORIZED` vs `UNKNOWN`) and its enabled actions/entities.
   - **When used:** Called first when you ask what integrations are available or when the agent needs to identify which connector handles a domain (e.g. Jira vs Outlook vs Drive).

2. **`ge_search_tools`**
   - **Purpose:** Queries `:invokeConnectorMcp` (`method: "tools/list"`) across all bound connectors (caching schemas in memory for fast subsequent lookups) and searches tool names, descriptions, and input parameter schemas by `query` keyword or `connector_id`.
   - **When used:** Called automatically when the agent knows the domain (e.g., `query="issue"` or `query="mail"` or `query="calendar"`) and needs the exact tool name and required JSON arguments.

3. **`ge_call_tool`**
   - **Purpose:** Executes a specific tool (`method: "tools/call"`) on a bound connector (`connector_id`, `tool_name`, `arguments`).
   - **Security Guardrail:** Verifies that `connector_id` is in the engine's allowlist before invoking `:invokeConnectorMcp`. Any attempt to call an unlinked project connector is rejected.

4. **`ge_assist_query`**
   - **Purpose:** Sends a natural-language query to the engine's `:streamAssist` endpoint to search unstructured knowledge across all bound data stores (SharePoint sites, OneDrive documents, Google Drive files, Confluence pages, GCS buckets).
   - **When used:** Called when you ask knowledge/retrieval questions rather than transactional action calls.

---

## 7. Sample Questions & Prompts by Connector

Below are sample prompts you and your teammates can ask directly in Antigravity once `gemini-enterprise` is loaded.

### A. Connector Discovery & Health Checks
- *"List all connectors available in my Gemini Enterprise instance and tell me which ones are authorized."*
- *"What tools are available on my Jira and Microsoft 365 connectors?"*
- *"Search my Gemini Enterprise tools for anything related to calendar events or email."*

### B. Jira Cloud Prompts
- **Browse Projects & Issues:**
  - *"List my Jira projects and show all open issues across them."*
  - *"Find all open High or Highest priority issues in the Team Toro Loko (KAN) project."*
  - *"Search Jira using JQL for issues assigned to me that are still in 'To Do' or 'In Progress'."*
- **Create Issues / Tasks / Bugs:**
  - *"Create a new Task in the Team Toro Loko (KAN) Jira project titled 'Set up CI/CD deployment pipeline' with Highest priority."*
  - *"Log a Bug in project KAN titled 'OAuth token refresh timeout on mobile' with High priority and description 'Users see a 401 error after 60 minutes of inactivity'."*
- **Update, Transition & Comment:**
  - *"Add a comment to Jira issue KAN-5 saying 'Initial GitHub Actions workflow has been merged to main'."*
  - *"Update the priority of KAN-5 to High and transition its status to 'In Progress'."*
  - *"Log 2 hours of work on issue KAN-5 with the note 'Implemented staging deployment pipeline'."*

### C. Microsoft 365 — Outlook Mail Prompts
> *(Available when the Microsoft Outlook / Microsoft 365 connector is bound to your Gemini Enterprise Engine)*
- **Read & Triage Inbox:**
  - *"List my 10 most recent unread Outlook emails and summarize any urgent action items."*
  - *"Search my Outlook inbox for emails from `manager@company.com` about 'Q3 roadmap' from the last 14 days."*
  - *"Find the latest Outlook email thread with subject 'Customer Escalation - Acme Corp' and summarize the root cause discussed."*
- **Draft, Send & Reply:**
  - *"Send an Outlook email to `teammate@company.com` with subject 'Sprint 14 Status Update' and a bulleted list of all open issues in Jira project KAN."*
  - *"Create a draft reply in Outlook to the latest email about 'Security Audit' confirming that all high-priority items are tracked in Jira."*
  - *"Forward the latest email about 'Invoice #4821' to `finance@company.com` with a note asking for approval."*

### D. Microsoft 365 — Outlook Calendar & Teams Prompts
- **Calendar & Scheduling:**
  - *"What meetings are on my Outlook calendar for today and tomorrow?"*
  - *"Find a free 30-minute window on my Outlook calendar tomorrow afternoon between 1:00 PM and 5:00 PM."*
  - *"Schedule a 30-minute meeting on my Outlook calendar tomorrow at 2:00 PM EST titled 'CI/CD Pipeline Review' and invite `devops@company.com`."*
- **Microsoft Teams:**
  - *"List the Microsoft Teams and channels I belong to."*
  - *"Read the last 15 messages in the 'Engineering General' Teams channel and summarize what the team is working on."*
  - *"Post a message to our Teams deployment channel announcing that KAN-5 (CI/CD deployment pipeline) has been created and is in progress."*

### E. Microsoft 365 — SharePoint & OneDrive Prompts
- *"Search SharePoint and OneDrive for documents mentioning 'Cloud Migration Architecture' and summarize the key milestones."*
- *"Find the latest 'Quarterly Business Review' presentation in OneDrive or SharePoint."*
- *"What does our internal SharePoint policy say about production change management approvals?"* *(uses `ge_assist_query` for grounded enterprise search)*

### F. Google Workspace (Google Drive, Gmail, Google Calendar) Prompts
- *"Search my Google Drive for documents or spreadsheets containing 'Release Checklist'."*
- *"Check my Gmail for recent messages about 'deployment freeze' and summarize them."*
- *"List my upcoming Google Calendar events for the rest of this week."*

### G. Cross-Connector Multi-Step Workflows
Because all bound connectors sit behind the single `gemini-enterprise` MCP server, Antigravity can chain multiple connectors in a single prompt:
- **Outlook Email -> Jira Ticket -> Email Reply:**
  - *"Check my unread Outlook emails for any bug reports or escalations, create a corresponding Jira issue in project KAN for each one, and reply to the sender with the new Jira ticket key."*
- **SharePoint / Drive Spec -> Jira Backlog:**
  - *"Search our enterprise documents for the 'Q4 Platform Reliability Spec', extract the action items, and create Jira tasks for them in project KAN."*
- **BigQuery Metrics -> Jira / Outlook Report:**
  - *"Run a query on our BigQuery connector to check yesterday's failed job count, and if there are failures, create a High-priority Jira bug in KAN and email the summary via Outlook."*
