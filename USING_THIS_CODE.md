# Using this code as a reference implementation

Doop is an experimental, local-first reference implementation for an
**owner-controlled WHOOP 4.0 (Gen4)**. It is useful for studying or adapting a
raw-first BLE pipeline, independent metric computation, local MCP access, and
confirmation-gated training records. It is not a production health product,
medical device, supported replacement for the WHOOP service, or a promise of
compatibility with future firmware.

The repository's planned `v1.0` tag identifies the completed portfolio
artifact. The installable Python package intentionally remains `0.1.0` to
signal that its runtime API and hardware compatibility are experimental.

## Supported boundary

The narrow path documented here is:

1. Python 3.11+ on macOS or Linux;
2. one locally owned WHOOP 4.0 and one active BLE client;
3. live HR/RR capture into a private local SQLite file;
4. independent daily metric computation;
5. local inspection and explicitly confirmed workout/proposal actions through
   the Streamlit dashboard, plus read-only queries through the stdio MCP server.

The data-free Python tests and synthetic seed work without a band. Physical BLE
behavior was accepted on one Gen4 device; that is evidence, not a compatibility
matrix.

Not supported here: WHOOP 5.0/MG, Windows, iOS/Android collection, multi-user
hosting, network-exposed MCP, remote dashboards, clinical interpretation,
automatic service installation, turnkey Raspberry Pi synchronization, or a
turnkey OpenClaw/Telegram deployment. Research probes, report ingestion, Pi
sync helpers, and agent acceptance tooling remain inspectable source, not part
of this minimal supported workflow.

## Install and verify

Install `uv`, clone the repository, and run:

```sh
uv sync --all-extras --locked
uv run pytest -q
```

The install exposes these local commands:

- `doop-init`
- `doop-scan`
- `doop-collect`
- `doop-compute`
- `doop-mcp`

Use them through `uv run`, or directly from the environment after activation.

## Keep configuration outside the repository

Doop reads an optional TOML file. Set its location before starting any command:

```sh
export DOOP_CONFIG="$HOME/.config/doop/config.toml"
```

Create that file with owner-only permissions. A representative shape is:

```toml
[runtime]
db_path = "private/whoop.db" # relative paths resolve beside this config file
host = "laptop"
timezone = "Etc/UTC"
device_name = "WHOOP"

[athlete]
age = 35
# Prefer age when an exact birth date is unnecessary. A local ISO TOML date is
# also supported through date_of_birth when historical age is required.

[metrics]
# An observed/reviewed HRmax overrides the age formula when supplied.
strain_hrmax_bpm = 185
# Optional advanced overrides:
# strain_hrmax_intercept = 208.0
# strain_hrmax_age_coefficient = 0.7
# strain_trimp_coefficient = 0.64
# strain_trimp_exponent = 1.92
```

All values above are illustrative. Use your own reviewed context locally and
never commit this file. Date of birth and age are mutually exclusive.

Environment variables override TOML:

| Setting | Environment variable |
|---|---|
| Config file | `DOOP_CONFIG` |
| SQLite path | `DOOP_DB_PATH` |
| Source host label | `DOOP_HOST` |
| IANA timezone | `DOOP_TIMEZONE` |
| Advertised-name filter | `DOOP_DEVICE_NAME` |
| Date of birth | `DOOP_ATHLETE_DOB` |
| Explicit age | `DOOP_ATHLETE_AGE` |
| Explicit HRmax | `DOOP_STRAIN_HRMAX_BPM` |
| HRmax intercept | `DOOP_STRAIN_HRMAX_INTERCEPT` |
| HRmax age coefficient | `DOOP_STRAIN_HRMAX_AGE_COEFFICIENT` |
| TRIMP coefficient | `DOOP_STRAIN_TRIMP_COEFFICIENT` |
| TRIMP exponent | `DOOP_STRAIN_TRIMP_EXPONENT` |

Invalid explicit configuration stops startup rather than silently using a
different path, timezone, or athlete. Without a config file, the database uses
the platform's private application-data directory, the host label is inferred,
and timezone is inferred with a UTC fallback. Without DOB, age, or explicit
HRmax, Doop still computes non-age-dependent metrics but **withholds strain**.

## Minimal local WHOOP 4.0 workflow

Initialize the configured database:

```sh
uv run doop-init
```

On macOS, first grant the terminal or IDE Bluetooth access in System Settings.
On Linux, ensure BlueZ and the current user's Bluetooth permissions are working.
Then wake/charge the band, stop competing BLE clients, and save the matching
device for this host:

```sh
uv run doop-scan --save
```

If multiple nearby names match, set a narrower local `device_name` or pass
`--name`. The scan output contains a device identifier; treat terminal output
and copied diagnostics as private.

Start live capture:

```sh
uv run doop-collect
```

The collector sends the Gen4 realtime-stream toggle and reconnects after gaps.
Leave it running only while you intend to collect. A WHOOP band accepts one
live BLE client, so the official app or another collector may interfere.

In another local shell, compute the latest completed noon-to-noon windows:

```sh
uv run doop-compute --catch-up-days 3
```

Recovery, sleep, and strain are independent proxies, not WHOOP scores or
medical outputs. Low-quality values are withheld from coaching, but that does
not make the remaining values clinically validated.

Start the dashboard on loopback only:

```sh
uv run streamlit run src/whoop_local/dashboard/app.py \
  --server.address 127.0.0.1
```

The dashboard initializes and reads the configured database. Its reviewed
confirmation flows can also write workout completions and accepted proposals;
opening a session or viewing a recommendation does not write. It has no remote
authentication layer, so do not bind it to a LAN or public interface. The MCP
server is the enforced read-only access boundary.

For a local MCP client, register the installed stdio command and keep the
client's working directory/config private:

```json
{
  "command": "uv",
  "args": ["run", "doop-mcp"]
}
```

The MCP transport is stdio and intentionally has no network authentication.

## No-hardware smoke path

The existing minimal seed is synthetic and refuses to overwrite a database:

```sh
uv run python -m tools.create_film_seed \
  --output tmp/reference-demo.db --film-date 2037-07-06
DOOP_DB_PATH=tmp/reference-demo.db uv run streamlit run \
  src/whoop_local/dashboard/app.py --server.address 127.0.0.1
```

This demonstrates code paths; it does not emulate BLE, validate a device, or
turn the repository into a production demo.

## Buffered history is a separate opt-in risk

Live collection does not require the standalone history offloader. The history
path sends proprietary commands, sets/verifies the strap clock, and acknowledges
downloaded chunks. Firmware changes could invalidate assumptions.

Before opting in:

- use only an owned Gen4 device;
- stop the normal collector and every competing BLE client;
- back up the SQLite database;
- read `CASE_STUDY.md`, `ARCHITECTURE.md`, and the module's `--help` output;
- understand that `--execute` is an acknowledgement of device state changes.

Then, and only then:

```sh
uv run python -m whoop_local.capture.history_sync --execute
```

Do not run the diagnostic probe modules as an installation step.

## Privacy and adaptation rules

- The SQLite database contains sensitive biometric and workout history. Doop
  restricts local file modes but does not encrypt the database; use full-disk
  encryption and an owner-only backup location.
- Apple Health, workout exports, raw packet logs, database backups, device
  identifiers, configuration, and agent trajectories must stay outside Git.
- `.gitignore` is a convenience, not a privacy guarantee. Review staged files,
  Git history, release assets, screenshots, and videos before publishing.
- Debug/probe logs may include raw payloads, timestamps, paths, and identifiers.
  Do not paste them into a public issue without deliberate redaction.
- Telegram/OpenClaw is an optional cloud messaging boundary, not part of the
  local core. Do not enable it merely to complete this workflow.
- The included training plan and seed are synthetic examples. Plan authoring is
  still code-level in this reference release; adapt it locally and preserve the
  explicit prepare/review/confirm boundary for workout writes.

If you publish an adaptation, describe exactly which devices, firmware,
platforms, and paths you tested. Call it experimental unless you have your own
repeatable hardware acceptance and maintenance plan.
