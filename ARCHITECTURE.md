# ARCHITECTURE.md

> How the system fits together. Read before changing how layers or sync stations connect.

## The product layers

```
        [ WHOOP 4.0 band ]  — records continuously, buffers up to 14 days onboard
                │  BLE (bleak)
                ▼
   1. CAPTURE   connect, subscribe to characteristics, receive live
                notifications AND download the onboard buffer on reconnect;
                frame + parse packets
                │   writes
                ▼
   2. STORAGE   SQLite — raw_packets (verbatim) + typed reading tables
                + sessions + metrics_daily  (rows tagged by source_host)
                │   reads
                ▼
   3. COMPUTE   HRV (RMSSD/SDNN), resting HR, sleep windows,
                recovery + strain proxies, rolling baselines
                │   reads
                ▼
   4. ACCESS    FastMCP server — query tools over the DB so an agent
                can answer "how's my recovery?"

   5. PRESENT  Streamlit dashboard reading the same DB
                │
                ├── local detailed review + confirmed actions
                │
                └── 6. CONVERSE  OpenClaw Gateway/agent ──outbound──► Telegram
                              Doop MCP + persona + confirmation UI
```

**Design principle:** layers are decoupled through the database and narrow service contracts, not prompt logic. Capture only writes; compute reads + writes derived rows; MCP access only reads. The dashboard may invoke confirmed local services; OpenClaw receives only explicitly allowed Doop tools and never arbitrary database access. Each layer is independently testable and replaceable.

## Telegram coaching surface (Phase 9)

OpenClaw is the self-hosted agent runtime and Telegram gateway. Doop is an OpenClaw integration through its existing local stdio MCP server plus versioned workspace/skill instructions. Telegram is not a remote MCP endpoint or the source of coaching truth.

```
[ Optional user chat ]
          │ Telegram cloud (minimum useful conversational content)
          ▼
[ OpenClaw Gateway + agent ] ──stdio MCP──► [ Doop read tools ] ──► SQLite
          │
          ├──► [ SOUL.md + Doop skill ]  facts first, tone second
          ├──► [ local model primary ]
          │         optional future fallback: Groq (not accepted)
          │
          └──► [ future narrow confirmation service ]  exact pending object only
```

- MVP runs OpenClaw Gateway on the Mac using its Telegram long-polling channel; no inbound public web server or network-exposed MCP server.
- OpenClaw owns Telegram transport, session routing, retries, inline-button rendering, and DM access control. Doop does not implement a second bot client or agent loop.
- Bot token and numeric owner allowlist live in OpenClaw's secret/config boundary, never in Doop or content returned to the agent.
- OpenClaw registers Doop's current stdio MCP server directly and filters it to the necessary read tools. It is validated with `openclaw mcp doctor --probe`.
- The accepted route uses a local Mac model endpoint. A Groq fallback harness
  exists but no provider credential/route was configured or accepted, so cloud
  fallback is not operational and is not claimed. If evaluated later, only the
  specific request and concise structured Doop result may cross that boundary.
  Raw sensor/history data and unrelated memory remain excluded.
- If the Mac-hosted Gateway sleeps, the bot is offline. True continuous
  availability requires a later always-on Gateway plus a least-privilege
  synchronized Doop read surface.
- The Doop OpenClaw integration provides versioned `SOUL.md` persona material and operating/skill instructions; those may change wording, energy, and humor but cannot change structured recommendations, confidence, planned loads, safety flags, or confirmation state.
- OpenClaw sends concise results, plan prescriptions, reviewed drafts, proposal diffs, and configured reminders. Raw packets, sensor time series, database/export files, routes, and diagnostic dumps stay local.
- Future workout/proposal writes terminate in D47/D48 services through short-lived, single-use, owner-bound references rendered as OpenClaw inline buttons. The language model never receives a general write capability.
- Telegram failure is isolated: capture, compute, MCP, local dashboard, and scheduled local work continue without it.

## Sync model — Option B (dual sync stations, no phone)

The topology below is retained as historical design evidence. It is not a
turnkey deployment shipped by this repository; the supported reference path in
`USING_THIS_CODE.md` is one local macOS or Linux host.

The band is its own buffer. It keeps recording when out of range and **catches up on reconnect** (WHOOP currently documents up to 14 days for WHOOP 4.0). This is retention capacity, not a write guarantee: a dead battery or invalid RTC can leave no record to recover.

```
             [ WHOOP 4.0 ]  ── buffers up to 14 days ──┐
              reconnects when in range                  │
        ┌───────────────────────────┬──────────────────┘
        ▼                           ▼
[ Optional always-on node ]  [ Canonical workstation ]
 continuous local capture     development + canonical data
        │                           │
        └──────────► same SQLite dataset ◄──────────┘
                          │
                    MCP → Claude
```

- **Phases 1–4:** one workstation collector owns the canonical database.
- **Phase 5 (design only, not active work):** an optional always-on Linux node
  could write a local capture database; a consistent snapshot would be pulled
  and capture-only rows imported into the workstation-canonical dataset.
  Snapshot/import code and tests remain inspectable, but this repository ships
  no service deployment and makes no turnkey support claim.
- **No phone relay:** iOS restricts background BLE (would need a native app, not a Python script); Android is more permissive but not worth the complexity. The onboard buffer removes the need.

### History Sync (the crux of Option B)
Live streaming only captures data while connected. The Phase 4 offloader repairs and verifies the RTC, sends the exact padded Gen4 initialization, reassembles type-47 history records between type-49 START/END/COMPLETE markers, and commits raw + decoded data before acknowledging each exact eight-byte cursor. Replays are idempotent in typed tables, while raw transport remains append-only. Physical acceptance passed in D71 and automatic bounded catch-up is enabled on the Mac collector (D72/D73).

### Multi-host database reconciliation
Two collectors can pull overlapping data (esp. overlapping buffer downloads). Rules:
- Every row is tagged with a validated `source_host` label. Platform defaults
  are `mac`, `linux`, or `pi`, and a deployment may configure a distinct label.
- Historical records carry a device-side timestamp; **dedup on ingest** by (metric, device timestamp) so overlapping downloads don't double-count.
- The **workstation remains canonical**. The optional node atomically refreshes
  a snapshot with SQLite's online backup API; the workstation transfers that
  file over SSH and imports only raw/capture tables.
- Stable per-database identity plus the canonical `sync_ingest_rows` ledger makes repeated/overlapping snapshots idempotent. Incomplete gaps wait; derived metrics, plans, workouts, and external imports never replicate from Pi.
- SQLite is never opened over a network share. In this optional design, the
  always-on node is the primary collector because the band permits only one
  live BLE connection.

## Deployment & platform notes

| Stage | Host | Notes |
|-------|------|-------|
| Phases 1–4 (dev) | MacBook | `bleak` on CoreBluetooth. |
| Phase 5 (design only) | Raspberry Pi class host | Headless collection was designed and tested at the software boundary; no service unit or hardware deployment is shipped. |

**macOS BLE quirks**
- Connect by Apple's **system-assigned peripheral UUID** (from a scan); it's stable per-Mac but differs across machines, so the saved identifier is host-specific.
- Terminal/IDE needs **Bluetooth permission**: System Settings → Privacy & Security → Bluetooth. Without it, scans silently return nothing.

**Raspberry Pi design notes (unsupported)**
- Linux BlueZ exposes the real MAC, so the Pi's saved identifier differs from the Mac's — keep device identity config per-host.
- Any real deployment would need an independently reviewed service manager,
  watchdog, permissions, snapshot schedule, and hardware acceptance. Those
  deployment artifacts are intentionally not included.

## Tech stack & rationale

| Concern | Choice | Why |
|---------|--------|-----|
| Language | Python 3.11+ | Mature BLE, data, SQLite, and MCP ecosystem |
| BLE | `bleak` | Cross-platform (macOS/Linux), async, maintained |
| Storage | SQLite | Local, file-based, zero-ops, portable to Pi |
| Math | numpy / pandas | Standard for time-series + HRV |
| Access | Python MCP SDK / FastMCP | One language end-to-end; stdio for local agents |
| Tests | pytest | — |
| Dashboard (opt) | Streamlit | Local inspection surface over the same configured DB |
| Daily conversation (optional evidence path) | OpenClaw Gateway + Telegram channel + embedded agent | Demonstrates bounded relays and pre-model safety; not part of the supported local reference workflow |

## Suggested repo structure

```
whoop-local/
  README.md  USING_THIS_CODE.md  CASE_STUDY.md  ARCHITECTURE.md
  SCHEMA.md  METRICS.md  MCP_SPEC.md  SECURITY.md
  EVIDENCE_MANIFEST.md  evidence/
  pyproject.toml
  src/whoop_local/
    capture/      # BLE collector: live stream + history download
    db/           # schema, migrations, ingest/dedup helpers
    compute/      # HRV, recovery, strain, sleep
    mcp_server/   # FastMCP server
    dashboard/    # optional
    integrations/ # OpenClaw acceptance, relay, and setup-validation modules
  integrations/openclaw/ # optional workspace/skill/plugin templates
  tests/
  runtime data    # private application-data directory outside the checkout
```
