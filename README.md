# Newspaper

[简体中文说明](README.zh-CN.md)

Self-hosted daily news publishing: collect evidence with DeepSeek Harness, verify and deduplicate it, publish Markdown and a responsive static website, then notify readers by email, Telegram and RSS.

Newspaper runs independently on a Linux VPS. A computer, browser tab or Codex session does not need to stay open. The default model is **d1api / deepseek-v4.1-flash**. Search and page retrieval use **dsh-free-search**. DSH runs only during collection and exits afterward; the only permanent web process is Caddy.

## Pipeline

```text
12:00 Asia/Shanghai
  → AI query plan for each section
  → bounded searches through DSH plugin engines
  → shortlist, then fetch selected originals through the same DSH plugin
  → structured editing, historical deduplication and evidence quotation checks
  → separate model evidence review; at most one repair / supplementary research round
  → report.json → Markdown + static HTML + RSS
  → atomic website release → email / Telegram outbox
```

Empty editorial configuration deliberately pauses generation. An optional reusable two-edition editorial example is provided; it is not enabled by installation. The website and retention service continue working while categories are being chosen.

## Requirements

- Linux with systemd, Python 3.11+ and Node 22.13+.
- An API key for the configured model provider.
- Caddy for the included deployment, and authenticated SMTP / a Telegram Bot for optional notifications.
- Approximately 600 MB of disk for the pinned DSH dependencies. A batch job is limited to 512 MiB in the included systemd unit; this limit is not a guarantee of actual consumption or task success. The job may need tuning for a different machine or workload.

## Quick start

```bash
git clone https://github.com/GuZhengSVT/Newspaper.git
cd Newspaper
make install
make check test
```

Python dependencies are pinned in `requirements.lock`, JavaScript dependencies in `package-lock.json`. DSH and the search plugin are also explicitly versioned. `.npmrc` documents the plugin's prerelease peer-range mismatch; offline tests and profile composition must pass before upgrading.

For a local test, put configuration and data outside the checkout:

```bash
cp config.example.toml /tmp/newspaper-config.toml
cp .env.example /tmp/newspaper-credentials.env
# Edit these files. Fill model credentials, and set desired categories.
export NEWSPAPER_CONFIG=/tmp/newspaper-config.toml
export NEWSPAPER_CREDENTIALS=/tmp/newspaper-credentials.env
export NEWSPAPER_DATA=/tmp/newspaper-data
export NEWSPAPER_WEB=/tmp/newspaper-web
.venv/bin/python -m newspaper.cli doctor
.venv/bin/python -m newspaper.cli build
```

`doctor` performs only local checks; it makes no model, search or notification requests. Use `run` after 12:00 to run a real edition. There is no simulated-success mode and no automatic publication when all research fails.

## VPS installation

The included units use the following layout:

| Directory | Contents | In Git? |
| --- | --- | --- |
| `/opt/Newspaper` | Source, prompts, schemas, templates and dependency locks | Yes, excluding installed dependencies |
| `/etc/newspaper` | Local configuration and `credentials.env` | No |
| `/var/lib/newspaper` | SQLite, dated JSON / Markdown / evidence, temporary DSH state | No |
| `/srv/newspaper` | One active static website release | No |

Install the checkout at `/opt/Newspaper`, install Python and Node dependencies with `make install`, then run `sudo bash scripts/install-vps.sh`. It creates a dedicated unprivileged user and installs systemd units and the Caddy configuration. Configure `/etc/newspaper/config.toml` and `/etc/newspaper/credentials.env` before enabling the generation timer:

```bash
sudo systemctl enable --now newspaper-clean.timer newspaper-notify.timer
sudo systemctl enable --now newspaper.timer
sudo systemctl list-timers 'newspaper*'
```

Edit `base_url` to the public IP URL or domain. The supplied Caddyfile serves HTTP on port 80; it exposes only the generated website, not configuration, evidence or administrative APIs. IP-based access is public read-only browsing. If a private site is needed, configure authentication with HTTPS. A domain may be used with Caddy's automatic HTTPS. Verify the VPS firewall and cloud security group allow the selected port.

## Categories

Add any number of `[[sections]]` entries to the local TOML file:

```toml
[[sections]]
id = "your-stable-id"
name = "Displayed section name"
scope = "Which facts belong here; what should be excluded"
keywords = ["search terms", "检索词"]
enabled = true
```

Section IDs must be unique. All prompts are in `prompts/`; there are separate research, editing and review prompts. The structure is defined by `schemas/report.schema.json`. Rendering code supplies dates, the reporting window and source URLs rather than allowing the model to invent them. Facts require quotations matching the independently fetched source text. Model review still needs to assess interpretation, publication timestamps and source independence; these checks cannot guarantee factual correctness.

## Notifications

Enable `notifications.email.enabled` and/or `notifications.telegram.enabled` in local configuration. SMTP and Telegram credentials belong only in `/etc/newspaper/credentials.env`; all variable names are in `.env.example`. SMTP supports STARTTLS or implicit TLS. It uses an existing SMTP service, not a new mail server. Set `SMTP_PORT=465` with `SMTP_SECURITY=ssl`, or the corresponding settings for your provider.

Notifications contain the edition date and reading/download links, not full articles or attachments. A persistent outbox avoids resending confirmed deliveries. Failed deliveries use bounded backoff, up to five attempts, without regenerating the edition. Remote request timeouts can leave delivery ambiguous, so duplicates cannot be eliminated in all failure cases. Missing credentials do not consume attempts. Imported legacy editions are not pushed automatically.

RSS is at `/feed.xml`; dated pages at `/daily/YYYY-MM-DD/`; downloads at `/downloads/YYYY-MM-DD.md` and `.json`. RSS readers control their own polling frequency.

## Retention: no news backups

An edition expires at 00:00 Asia/Shanghai on its date + 30 days: the current date and previous 29 dates are retained. Rebuilding or retrying does not extend that deadline. The midnight timer performs normal expiration, and a five-minute timer recovers missed or interrupted cleanup. Cleanup also runs before generation.

Cleanup deletes:

- Selected evidence excerpts, structured reports and Markdown, including orphaned dated directories.
- Published HTML, downloadable files, RSS items, search entries and superseded website releases.
- Event / notification rows through cascading database deletion; SQLite uses `secure_delete` and `VACUUM`, without WAL archives.
- Temporary DSH profiles, sessions, search caches and diagnostic output. Each task has an isolated DSH home and cache directory, deleted after the task; crash leftovers are removed by cleanup.
- Expired run records. Journal output does not contain article bodies or model responses.

News is **never committed to Git, uploaded as CI artifacts, or backed up by this application**. Do not include the data and website directories in system snapshots or external backup jobs if the same policy must apply there. Files already downloaded by readers, emails, Telegram messages and third-party provider logs are outside VPS deletion control. HTTP responses use `Cache-Control: no-store` to discourage retained browser/proxy copies; this is not remote deletion of reader devices.

## Operations

```bash
# Run as the newspaper service user, with the configured credentials file:
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli doctor
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli status
sudo systemctl start newspaper.service          # real generation, if categories exist
sudo systemctl start newspaper-clean.service    # cleanup and rebuild
sudo systemctl start newspaper-notify.service   # retry pending deliveries
sudo systemctl disable --now newspaper.timer    # stop future generation
```

Run these commands from the checkout, or install the Python project into its virtual environment (`.venv/bin/pip install --no-deps -e .`). Tasks share a process lock. The daily generator waits up to 90 seconds for a short maintenance job, so the noon cleanup cannot silently consume the day’s generation. Other competing jobs exit with code 75 and retry on their next timer tick. Cleanup and notification retries are offset by 30 seconds. The site stays available during generation. Failed builds preserve the last complete release; expiration cleanup removes stale dated routes even if rebuilding fails.

Import an existing archive with `newspaper import-legacy /path/to/News`. Only unexpired editions are imported, original Markdown bytes are preserved, and the website labels them as legacy format. Legacy documents are not asserted to satisfy the new JSON schema.

## Tests and contributing

`make check test` runs offline: evidence citation, date window, deduplication, publication review, rendering/escaping, atomic release failure, retention across database/files/site/feed, notifications, credentials parsing and process locking. Test data is synthetic and never published. CI has no live credentials and produces no news artifacts.

Keep installation paths configurable, place machine-specific settings outside the repository, and update lockfiles plus profile checks when upgrading DSH. Python 3.13 and Node 22 are the current CI targets.

MIT licensed. DeepSeek Harness and dsh-free-search retain their respective licenses; they are installed dependencies, not vendored source.

## Separate editions and bounded evidence (schema v2)

`editorial.example.toml` defines two independent editions, Technology & Politics and ACGN, with 15 sections. Append it to the local TOML configuration, or define your own `[[editions]]` (`id`, `name`, `sections`) and `[[sections]]` (`id`, `name`, `scope`, `batch`). Each enabled section belongs to exactly one edition. The default install remains unconfigured.

The shared editorial policy is `prompts/editorial.md`. Importance is major/important only; reliability is A/B, with unsubstantiated C rumors excluded. Empty sections are hidden in Markdown and HTML. The default `search.mode="bounded"` lets the AI plan two queries per section and executes the pinned plugin's keyless Bing/DDG/Exa engines directly. No growing agent transcript or extra search-model calls are involved. A shortlist uses search previews only for discovery; at most 24 selected originals per edition are fetched for actual evidence. Canonical URL fetches are cached within the run and identical bodies deduplicated. Drafting reads selected contexts; review reads cited passages with adjacent context. Content repairs replace only affected articles; schema failures may retry the draft once. `mode="headless"` remains an optional DSH CLI path with related sections sharing a research batch. The SQLite entity index retrieves only relevant, compact event history. Exact historical event keys are also checked locally across editions. Entity aliases and semantic novelty still depend on model judgment.

Each fact has its own verification mode: primary official material may directly support a fact with one source, while a fact marked corroborated requires citations from distinct source hosts; the review must additionally reject syndicated copies and unsupported interpretations. An official claim is not an independently verified result. This is automated evidence checking, not a guarantee of truth or human review.

Published evidence files contain only source metadata, cited quotations and short adjacent contexts. Unselected pages, intermediate drafts and DSH sessions are removed when the run exits; the regular cleaner removes crash remnants. All editions, evidence, event/entity indexes and metrics expire within the same 30-day policy. No permanent historical fingerprint store is kept.

Daily routes are `/daily/YYYY-MM-DD/technology-politics/` and `/daily/YYYY-MM-DD/acgn/`; downloads use `/downloads/YYYY-MM-DD/<edition>.md` or `.json`. Existing single-edition archives remain accessible. Subscribe to the combined `/feed.xml`, or `/feed-technology-politics.xml` and `/feed-acgn.xml` independently.

Run `newspaper run --no-notify` for a published web preview without enqueueing or sending email/Telegram notifications. A published date/edition is immutable and skipped on reruns; preview notifications are not later enqueued automatically. Failure of one edition does not block another and returns an overall nonzero status. Run metrics retain direct model API usage and duration/input-character counts without request bodies. Bounded mode has no additional DSH model calls; all planning/editing API calls are measured. Optional headless-mode DSH aggregate token usage is explicitly unknown (`null`) when unavailable, so its direct API totals do not represent the full bill.

Transient model HTTP 429/502/503/504 and transport failures retry at most three requests, waiting 15 then 60 seconds. The systemd daily job retries after five minutes, capped at three starts in six hours (each run has a 50-minute timeout). Completed editions are skipped on retries; notifications retry through their own outbox without regenerating news. Permanent API errors are not retried within a request.

Individual articles with invalid quotations or other evidence violations are excluded before review; global schema/configuration failures still fail the edition. Final reviews must explicitly cover every article. After the bounded repair round, only articles individually approved by the final review can be published; unsupported candidates are omitted and summaries rebuilt from retained content. Search-index timestamps are unverified hints, not authoritative source publication times. A reviewed `ready` edition whose web build failed resumes from its saved evidence without new model calls. Public coverage notes contain only reader-relevant limitations, not discarded candidate lists or tool identifiers.
