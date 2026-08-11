# Contributing

Thank you for helping improve Doop's method, evidence, tests, documentation, or
privacy controls. This repository is a reproducible engineering showcase, not
a supported consumer health product. Contributions should strengthen what a
reviewer can verify without widening the private-data or medical-safety
boundary.

## Good contribution areas

- synthetic protocol, parser, metric, and confidence-gate tests;
- clearer evidence links, limitations, provenance, or reproducibility steps;
- privacy-gate and repository-history checks;
- deterministic safety and unavailable-data behavior;
- accessibility and static-site improvements that introduce no telemetry;
- narrowly scoped bug fixes backed by data-free tests.

Large product features, production deployment support, new live messaging
writes, vendor-cloud integrations, and claims of proprietary-score parity are
outside the current showcase scope. Open an issue before investing in a change
that would materially expand that boundary.

## Synthetic-only publication rule

Public contributions must contain only synthetic or generated inputs. Do not
commit, paste, or attach:

- real health, sleep, workout, route, location, or demographic records;
- Apple Health or other exports, even short excerpts or exporter headers;
- databases, packet captures, device identifiers, chat identifiers, or
  screenshots from a personal runtime;
- credentials, configuration files, notification text, local paths, or
  owner-linked dates;
- personal profile, schedule, injury, or preference information;
- third-party media, code, or data without compatible redistribution terms.

Use clearly invented dates and values. If a parser test needs an export-shaped
document, construct the smallest synthetic document by hand and omit unrelated
identity fields. Generated BLE frames must be built by the repository's
synthetic helpers rather than copied from a real capture.

Public issue and pull-request text is part of the publication boundary too.
When reporting a privacy problem, never quote the private value. Follow
[SECURITY.md](SECURITY.md).

## Commit identity and history

Git records author and committer email addresses in public history. This
repository requires a GitHub-provided noreply address:

~~~sh
git config user.name "YOUR-GITHUB-USERNAME"
git config user.email "ID+USERNAME@users.noreply.github.com"
~~~

See GitHub's documentation for the noreply address assigned to your account.
Use a GitHub handle, not a personal display name, as the Git author/committer
name. Commit and annotated-tag messages, tagger identity, and file paths are
scanned under the same privacy boundary as file contents.
Check every commit in the proposed branch, not only the final working tree.
Never solve a personal-data leak with an ordinary deletion commit; notify the
maintainer privately so the complete history can be remediated.

## Development setup

Use Python 3.11 or newer and uv. Install from the locked dependency set:

~~~sh
uv sync --all-extras --locked
~~~

Run the complete public gate before requesting review:

~~~sh
python3 tools/privacy_check.py
uv run ruff check src tests tools
uv run ruff format --check src tests tools
uv run mypy src/whoop_local
uv run pytest --cov=whoop_local --cov-report=term-missing -q
uv run pip-audit --local --progress-spinner=off
node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js
~~~

If a command is genuinely irrelevant to a documentation-only change, explain
why in the pull request. The privacy check is always required.

Do not point tests or scripts at a live database, wearable, Telegram account,
OpenClaw runtime, or personal export. Hardware observations belong in a
separately reviewed, aggregate evidence record and must distinguish historical
private-hardware evidence from publicly reproducible checks.

## Pull requests

Keep each pull request focused and describe:

1. the problem and bounded change;
2. the claim, invariant, or evidence link affected;
3. tests and synthetic fixtures added or updated;
4. privacy, licensing, and medical-claim implications;
5. any limitation or check that remains.

Reviewers may ask for a clean synthetic reproduction or a rebuilt branch when
the publication boundary is unclear. By submitting a contribution, you agree
that it may be distributed under the repository's Apache-2.0 license and that
you have the right to submit every included contribution.
