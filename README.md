# Newspaper

[简体中文](README.zh-CN.md)

A self-hosted news publishing pipeline with configurable topics, daily/weekly/monthly schedules, evidence checks, static HTML, Markdown, JSON, RSS, email and Telegram. It runs on a Linux VPS without keeping a desktop or chat session open.

Topics, sources, prompts and provider credentials belong to each installation. This repository supplies a reusable implementation and fictional configuration examples.

## Install

Requires Python 3.11+, Node 26.13.0 (pinned in `.node-version`), and systemd/Caddy for the included VPS deployment.

The repository distributes application code, templates, static assets, prompts, tests and configuration examples. Installed dependencies are excluded from Git: `node_modules/` and `.venv/` are local installation directories and are not included in a clone or source download.

| Dependencies | Declaration | Locked installation versions |
| --- | --- | --- |
| Python: httpx, Jinja2, jsonschema, markdown-it-py | `pyproject.toml` | `requirements.lock` |
| Node.js: DeepSeek Harness, dsh-free-search, sharp, undici, proxy-agent and supporting packages | `package.json` | `package-lock.json` |

Install the Node version in `.node-version` before running `make install`; CI and `package.json` use the same version.

`make install` creates the Python virtual environment and downloads both sets of dependencies from the lock files. Keep the manifests and lock files in Git so installations are reproducible. A source-publication checkout does not need installed dependencies to commit or push code; install them there only when running local checks or the application.

```bash
git clone https://github.com/GuZhengSVT/Newspaper.git
cd Newspaper
make install
make check test
cp config.example.toml config.local.toml
cp project.example.toml project.local.toml
cp .env.example credentials.env
cp accounts.example.toml accounts.local.toml
```

Edit the configuration files before generating news. Select an OpenAI-compatible model endpoint and model ID in `config.local.toml`, and put its key in `credentials.env`. The endpoint must accept `thinking.type = "enabled"` and `reasoning_effort = "max"` / `"low"` for coordinators and workers. No provider or model subscription is included. The pinned DeepSeek Harness and `dsh-free-search` dependencies provide search/retrieval; the default bounded mode plans queries, then calls plugin engines directly. Search availability is recorded, rather than assumed.

```bash
export NEWSPAPER_CONFIG="$PWD/config.local.toml"
export NEWSPAPER_CREDENTIALS="$PWD/credentials.env"
.venv/bin/python -m newspaper.cli check-config
.venv/bin/python -m newspaper.cli settings
.venv/bin/python -m newspaper.cli schedule
.venv/bin/python -m newspaper.cli doctor
mkdir -p ./private
.venv/bin/python -m newspaper.cli preview --manual --output ./private/first-preview
.venv/bin/python -m http.server --bind localhost --directory ./private/first-preview/web/current
```

`check-config`, `settings`, `schedule` and `doctor` make no model, search or notification requests. `settings` shows the active config version, modes, budgets and readiness without endpoints, prompt text, private paths, credentials or delivery addresses. `schedule` shows the latest due reporting windows and the publications belonging to each delivery task.

Each preview requires a fresh output directory and makes real research and model requests using the configured credentials. It writes its own database and site under that directory. The preview publishes locally within that directory and suppresses notifications. The local web server serves only the generated `web/current` tree. `run --no-notify` publishes to the configured site, records the issue in the production history and suppresses notification for that run.

For an immediate end-to-end run, list all enabled delivery tasks with `schedule --manual`, then invoke `run --manual --task TASK_ID` one task at a time. Each command uses the existing retrieval, editing, verification, publication and notification pipeline. Manual issues have separate publication IDs and look back one configured period from the invocation time, without changing normal schedules or start dates. An explicit timezone-aware `--cutoff` can give multiple manual runs the same cutoff earlier that day.

## Configuration files

| File | Purpose | Publish to Git? |
| --- | --- | --- |
| `project.local.toml` | Groups, sections, searches, prompts, filters, sources, calendar and homepage | No |
| `config.local.toml` | Model endpoint, paths, retrieval budgets, notification switches; references `project_file` | No |
| `credentials.env` | Model key, SMTP account/recipients, Telegram token/chat | No |
| `accounts.local.toml` | Private administrator-managed accounts, destinations, subscriptions and bilingual preferences | No |

The complete editable project template is [project.example.toml](project.example.toml). `project_file` is resolved relative to the runtime config; prompt file paths are resolved relative to the project file. Local configuration filenames are ignored by Git. Keep generated news and databases outside the checkout.

## Groups and sections

`[[groups]]` defines first-level categories with stable `id`, display `name`, and a list of child section IDs. `[[sections]]` defines each child with `id`, `name`, and `scope`. Every enabled section must belong to exactly one enabled group. Both levels support `enabled`, `notify` and `schedule`. Disabling a group disables its children.

Each section independently supports:

- `search.queries`: fixed queries, or an empty list for AI planning from scope, keywords and languages. Templates accept `{date}`, `{window_start}` and `{window_end}` as ISO dates. `search.query_count` controls planned queries (1–8).
- `prompts.plan/select/edit/review/repair/compress/collect`: extra instructions scoped to that section. Global `[prompts]` also supports text and `<stage>_file`; `editorial`/`editorial_file` apply to editing and evidence stages. Custom instructions do not disable evidence validation.
- `filter.importance`: schema values `重大`/`重要`; `filter.reliability`: `A`/`B`; `exclude_keywords`: literal substring exclusions; `require_primary`: require primary official evidence for core facts. Importance labels remain Chinese schema enums even with other topic names.
- `format`: character and paragraph limits, including separate limits for major stories. An overlong article receives bounded compression and review.
- `search.domains`: domains worth a targeted gap search. Passed to the planning model as a hint only; nothing is fetched or counted as read because it is listed.
- `sources`: independently fetched `rss` (RSS/Atom), `index` (HTML links, optionally narrowed by a `link_pattern` regex), or `page` (the URL itself); `enabled = false` temporarily skips one. Mark `required = true` to make failure to read a source fail that publication.
- `discovery`: `max_indexes_per_edition` (1–128), `max_links_per_index` (1–30), `max_previews_per_section` (1–60, applied round-robin across publishers so an early source cannot crowd out the rest) and `max_gap_queries` (0–40, the total top-up search budget for sections that already have source coverage).

Collection reads every enabled configured source **before** searching, then hands those results and their headlines to the planner so search only fills gaps. A section with no fixed queries must issue at least one search when no configured source produced an item inside the window; a section whose sources did produce items may skip search entirely. Coverage requires a qualifying link and an in-window publication timestamp with a timezone from the same publisher. Date-only links, update times and search snippets can still enter selection. A feed with no qualifying link leaves a search gap.

Planning receives explicit per-section limits and a feasible total budget. Invalid plans receive one correction attempt with structured constraint feedback; repeated violations stop the run and retain diagnostic counts, without storing response bodies. An empty preview selection triggers original-article checks from up to three distinct publishers within the existing fetch budget, followed by selection and empty-issue review. Failed original fetches still cannot produce a successful empty publication. When entity terms do not match a source language, or match only its header, evidence packing uses the available lead budget rather than cutting an otherwise complete abstract at the short lead boundary.

Unreadable candidate originals trigger a search for alternative evidence about the same event, bounded by remaining gap-query and fetch budgets. Alternative sources still require the normal timestamp, quotation and semantic checks. Final reviews cover only current draft articles; filtered candidates cannot reappear as review verdicts. A mismatched verdict list receives one contract-correction attempt, and empty drafts are reviewed against fetched source contexts.

Exceeding the source or discovery budget fails rather than silently omitting sources or sections. Discovered links and search results share the source fetch budget. All selected articles still need original evidence, timestamps within the reporting window and final review.

## Calendar and delivery tasks

Schedules inherit from `[schedule]` to `[groups.schedule]` to `[sections.schedule]`. Times use `project.timezone` (an IANA zone). Examples for a section:

```toml
[sections.schedule]
period = "weekly"
weekday = "mon"
time = "09:00"
# start_date = "2027-01-04"
```

| Period | Additional fields | Reporting window |
| --- | --- | --- |
| `daily` | `time` | Previous local day to this slot |
| `weekly` | `weekday = "mon"` … `"sun"`, `time` | Previous week to this slot |
| `monthly` | `month_day = 1` … `31`, `time` | Previous month to this slot |
| `interval` | `every_days = 1` … `30`, required quoted `start_date`, `time` | Previous N days to this slot |

A Monday 09:00 weekly report starts generation at that time and covers the preceding Monday 09:00 through this Monday 09:00. Delivery follows successful generation. `start_date` gates the first allowed publication date; the first report still has a complete lookback. Monthly days missing in a short month clamp to its last day. Late starts catch up only the latest due slot, without generating all missed issues.

**Each top-level group works and sends independently, in configuration order.** `[model] coordination = "configured"` delegates directly from project settings. With `coordination = "model"`, the coordinator uses `max` reasoning to delegate the group's tasks and approve a short result summary. Search planning, selection, editing, fact review, illustration and translation use isolated `low` requests. The coordinator receives section briefs and compact article metadata; source bodies and full drafts stay with the workers. Model usage records include group, role, reasoning effort and the provider's raw token/cache counters. Workers run serially, and delivery for the current group is attempted before starting the next group. Search caches are isolated between groups.

Set the optional `[model] max_tokens_per_edition` to pause new model requests once the edition reaches its recorded token allowance. Each edition combines the providers' reported `usage.total_tokens` across coordination, editing, retries and deferred translation. `status` shows `total_tokens`, `unreported_calls`, the configured `limit`, and whether that limit blocks further requests. A failed HTTP request, transport error or response with missing or invalid usage adds to `unreported_calls` and contributes zero recorded tokens. The request that crosses the threshold can exceed it. Usage inside the legacy DSH collection subprocess is unavailable to this counter. Provider backoff persists across processes and increases after each failed probe. With failure alerts enabled, three consecutive model calls that end in a retryable failure for an endpoint raise one incident until the endpoint responds successfully.

Delivery tasks are scoped to both group and schedule. Different groups never share a message, even when their schedules match. Daily, weekly and monthly publications within a group remain separate. Related events can link to a retained edition across schedules. A weekly edition can cite previously verified evidence. A follow-up in the same reporting task must identify a new claim and new evidence. Retained history is migrated to these identities without reannouncing published issues.

A persistent batch records the expected publications for one group and schedule before publishing, and recovers after partial failure or restart. Other groups do not delay its delivery. Successful channels are never resent. Known delivery failures retry only that channel, up to five attempts with backoff. If the provider may have accepted a message but acknowledgement is lost, the outbox becomes `uncertain` and is not automatically retried; inspect `status` and the provider before taking manual action. Telegram cannot offer an absolute exactly-once guarantee. Issues published with `run --no-notify` or imported from legacy files are not automatically announced.

Enable email/Telegram in runtime configuration. Credential variable names are in [.env.example](.env.example). SMTP uses an existing provider with STARTTLS or implicit TLS. Notifications contain the complete published news text and source references, with webpage links at the end. Email includes responsive HTML and plain-text alternatives. The HTML follows the website’s paper background, serif typography and fine rules in a mobile-friendly single column, with paired English and Chinese paragraphs. Telegram starts each article in a separate message and keeps sources with the text before optional photos; photo captions include attribution links. It uses escaped HTML, keeps a bilingual paragraph pair together when it fits, splits long text at paragraph boundaries within its [message limits](https://core.telegram.org/bots/api#sendmessage), and checkpoints acknowledged parts so a definite failure resumes without repeating them. Photos are separate, optional messages; rejected image URLs do not prevent text delivery. `notify = false` excludes a group or section from delivery while retaining web publication.

Each article may include one externally hosted image in HTML, Markdown, email and Telegram. Candidates must be embedded in a cited source page with an informative publisher description. A separate editorial pass selects only images depicting the same subject and event; uncertain matches are omitted. The verifier checks public HTTPS redirects, download limits, successful decoding, dimensions, aspect ratio and image detail. Logos, advertisements, thumbnails and blank or strongly blurred placeholders are excluded. Captions preserve illustration/archive qualifiers and link to the source. Optional image failures leave verified text intact; binary images are not retained. The `batch` section field only groups topics in the alternative collection path; bounded retrieval and notification grouping do not use it.

## Homepage and feeds

With `[translation] enabled = true`, each approved Chinese article is translated in one model call, without a model reviewer or feedback-driven rewriting. Original Chinese sentences are stored unchanged alongside English in `article.bilingual`. Local checks validate structure, sentence IDs, completeness and explicit calendar dates. Failures are recorded. With `publish = "together"`, an issue waits for all translations. With `publish = "chinese_first"`, the approved Chinese issue goes live while the translation job completes individual articles. A bilingual destination waits for its subscribed translations and the published site. The prompt requests faithful, idiomatic English, including accurate attribution, qualifications, numbers and terminology.

Sentence splitting preserves punctuation inside titles, quotations and brackets. A private checkpoint saves the approved Chinese, cited evidence and each completed article translation, so a failed attempt resumes without new research or retranslating completed articles. Checkpoints are excluded from public pages and notifications, removed after publication, and expire with their dated issue. Existing archives retain support for previously stored sentence boundaries.

A single bilingual switch in the navigation menu saves the reading preference across the homepage and issue pages. Article sections have no separate switches; stories without a translation remain in Chinese. Reading uses **one English paragraph followed by its Chinese paragraph**, with sentences aligned in order within each pair and the original paragraph boundaries preserved. English is generated during news production, never on a reader's request. Chinese-only reading remains the default. The `translate` CLI command backfills retained reports and rebuilds the site without changing their dates or facts, or sending notifications; already translated articles are skipped.

### Private notification accounts

Set the runtime `accounts_file` to a private TOML file (relative to the runtime config) and `[notifications] mode = "accounts"`. Copy [accounts.example.toml](accounts.example.toml); the administrator edits this file, with no public registration, login or subscription API. Keep it outside Git, readable only by the administrator and service account (`0640` in the VPS installer).

An account has a unique `id`, optional `name/enabled`, an `emails` array and at most one optional numeric-string `telegram_chat_id`. The Telegram user must first start the configured bot. `subscriptions = [{ section = "research", bilingual = true }, { section = "culture" }]` subscribes only to those group or section IDs. Specific section rules override groups, which override `*`. `[notifications.defaults] subscriptions = ["*"]` and `bilingual = false` supply defaults: omitted subscriptions inherit the default scope, `[]` subscribes to nothing, and omitted bilingual flags inherit the default. Publication/delivery times retain the existing global→group→section schedule inheritance; disabled sections and `notify = false` remain excluded.

Every bound destination receives only that account's selected sections; selected bilingual sections use the same paired paragraphs as the website. A browser's reading switch does not change the administrator's account preferences. Missing destinations never fall back to another account or global recipient. SMTP credentials and Bot Token remain shared transport secrets, and channel enable flags still apply.

Delivery status and Telegram progress are isolated per account and destination. Successful destinations are not resent; ambiguous acceptance stops automatic retry. Recipients and preferences are frozen when a completed task becomes ready. Revoking an account, address or subscription, or changing a bilingual preference, cancels affected delayed retries rather than sending stale content. New settings apply to future tasks without backfilling previous notifications. Account mode stops legacy global-queue delivery; `mode = "legacy"` explicitly restores the old behavior. `accounts` prints effective subscriptions and destination counts without addresses; `check-config` validates files, and `status` reports account delivery states.

Optionally set `[notifications.failure_alerts] account_id = "ExampleAdmin"` in runtime configuration. Operations alerts go only to that account's bound email/Telegram destinations through enabled channels, independently of news subscriptions. Alerts trigger after three generation failures within six hours, five failed news delivery attempts, or three consecutive command failures. Generation and notification jobs check for incidents and include the task, stage and recorded reasons. Alerts are deduplicated per incident/destination; resolved incidents are not backfilled. Alert delivery retries up to five times with backoff and stops on ambiguous acceptance; inspect `status` for results. Removing the configuration, disabling the account or revoking a binding stops the corresponding alerts. `run --no-notify` sends no alerts. Configuration-load and database-open failures cannot use this application-level alert channel.

`[homepage] mode = "groups"` directly shows each selected first-level group's articles. `groups = ["research", "culture"]` controls inclusion and order. `mode = "sections"` with a `sections` list shows selected children instead; `mode = "index"` shows publication links. The homepage combines the latest retained issue for each section, so weekly/monthly content remains available between updates. Each section links to its issue date and full report; empty latest issues do not revive old articles.

Homepage layout is independent of content mode: `layout = "tabs"` (default) shows one group at a time with buttons; `layout = "columns"` shows groups side by side on desktop and stacks them on mobile. `layout_switch = true` (default) adds a reader layout selector and saves their choice in the browser; set it to `false` to enforce the configured layout. Tabs support arrow keys, Home/End, and direct links from the outline. Without JavaScript, all groups remain readable.

[examples/politics.toml](examples/politics.toml) is an optional politics preset with China, United States, Europe, Russia–Ukraine war, Middle East, South America, Africa and other international affairs. Merge its group and sections into a private project file and add `politics` to `homepage.groups`, or use it as a standalone project. It pairs official releases and UN reporting with BBC, The Guardian, Agência Brasil and CNA. Reuters/AP and relevant official sites are gap-search hints; they are not counted as sources read. Conflict topics take precedence over geography, and contested claims require attribution and corroboration. Source access can change; optional failures remain coverage gaps for search.

The reading interface includes a contents outline, responsive layout, light/dark themes, archive search and downloads. UI text and schema labels are currently Chinese. Combined RSS is `/feed.xml`; group RSS is `/feed-<group-id>.xml`. Publications use `/daily/YYYY-MM-DD/<publication-id>/`; downloads use `/downloads/YYYY-MM-DD/<publication-id>.md` or `.json`. IDs for split schedules are shown by `schedule`.

## VPS operations

### Build and install a source release

`make release REF=<tag-or-commit>` exports the selected committed tree, embeds `release.toml` with its application version and full commit, and writes an archive and a SHA-256 checksum in `dist/`. To package version tags in CI, install `deploy/github/ci.yml` as `.github/workflows/ci.yml`. Packaging uses committed source and pinned dependency manifests.

1. Verify the downloaded archive with `sha256sum -c <archive>.sha256` and extract it under `/opt`. The versioned directory is the service working directory. Keep it writable only by trusted deployment administrators.
2. From the extracted directory, run `make install` with the pinned Node version and Python 3.11+. Make the pinned Node executable available on the service's PATH (`/usr/local/bin:/usr/bin:/bin`).
3. Review `bash scripts/install-vps.sh init --dry-run --caddy` for a fresh installation or replace `init` with `upgrade` for an existing VPS. With `--caddy`, add the top-level line `import /etc/caddy/newspaper.caddy` to the existing `/etc/caddy/Caddyfile`. For a legacy installation, move its Newspaper site block to a separate source file and remove that block from the main config before adding the import. Pass `--caddy-site /path/to/site.caddy` to use that site block or set the domain before installation. Leave Caddy running on its loaded configuration during this edit.
4. Run the same installer as root without `--dry-run`. On upgrade, stop all Newspaper timers and wait for active Newspaper services to finish first. The installer updates the service units to the new release directory and uses the existing private settings and state. With Caddy management enabled, it validates the combined Caddy config before replacing its independent site fragment. Reload Caddy yourself after installation. Omit the Caddy option when another service already provides the static origin.

| Path | Contents |
| --- | --- |
| Extracted release under `/opt` | Committed application with locked local dependencies |
| `/etc/newspaper/config.toml` | Runtime configuration |
| `/etc/newspaper/project.local.toml` | Private project preferences |
| `/etc/newspaper/credentials.env` | Private credentials |
| `/var/lib/newspaper` | SQLite, reports, evidence and temporary work |
| `/srv/newspaper/current` | Atomic static website release |

The supplied systemd units grant write access to the default state directories. If your deployment uses other paths, configure the corresponding `ReadWritePaths` in systemd drop-ins and point the static server at the chosen `web_dir/current`.

From the extracted release directory, configure the files in `/etc/newspaper` and run these commands before enabling production scheduling:

```bash
sudo -u newspaper .venv/bin/python -m newspaper.cli --version
sudo -u newspaper .venv/bin/python -m newspaper.cli check-config
sudo -u newspaper .venv/bin/python -m newspaper.cli settings
sudo -u newspaper .venv/bin/python -m newspaper.cli schedule
sudo -u newspaper .venv/bin/python -m newspaper.cli build
sudo systemctl enable --now newspaper.timer newspaper-clean.timer newspaper-notify.timer
sudo -u newspaper .venv/bin/python -m newspaper.cli status
```

Existing configurations may omit `config_version` and the new mode fields. They default to version 1, local publication, model coordination and combined bilingual release. The fresh-install example uses configured coordination and Chinese-first release. For Pages mode, use an HTTPS website root for top-level `base_url` and enable `newspaper-publish.timer` once the Pages workflow is configured. Enable `newspaper-translate.timer` when `translation.enabled = true` and `translation.publish = "chinese_first"`. Disable an optional timer when its mode is retired. `status` shows the application release, site versions and the latest measured source-to-publication counts for each section. See [publication setup and verification](docs/publication.zh-CN.md).

The generation timer checks schedules every minute. Published slots are skipped. Failed publications wait at least five minutes and permit at most three failed attempts per six hours. A run has a 50-minute systemd timeout. Notification retries and retention checks have separate timers. Remote confirmation runs independently of generation. Set top-level `base_url` to the public HTTPS site origin for RSS and delivery links and run `build` after changing it. The supplied Caddy fragment defaults to HTTP port 80. Configure its hostname and HTTPS for the deployment.

## Evidence and retention

The pipeline plans searches, shortlists provisional leads, fetches originals, edits structured facts, verifies quotations/dates/history, and runs separate model review. Search snippets and model memory cannot establish facts. Each fact needs matching source quotations; claimed corroboration requires distinct source hosts and semantic independence review. Bounded repair can fix evidence or wording, but only individually approved stories are published. Retrieval failures cannot masquerade as a successful no-news edition. Automated review still cannot guarantee truth.

Only selected evidence metadata, quotations and adjacent context are retained. Unselected pages, intermediate drafts and isolated DSH sessions/caches are deleted after the run; cleanup removes crash leftovers. Run metrics record counts, outcomes and usage without response bodies or credentials.

Retention is 30 calendar dates in the configured project timezone, including the publication date. Rebuilds/retries do not extend it. Cleanup removes expired reports, evidence, web routes, downloads, RSS/search entries, events, notification batches, run records and superseded releases. SQLite uses secure deletion and vacuuming. Direct hosting serves the files on the VPS. The optional Pages workflow uploads a verified site artifact with a one-day retention setting. Both modes can publish without committing articles to Git. Reader downloads and third-party provider copies are outside server deletion control.

## Development

`make check test` runs offline with synthetic data and mocked delivery. It covers calendar windows, task grouping, retries/restart recovery, evidence checks, filtering, custom homepages, privacy boundaries, rendering and retention. It never sends real email/Telegram or generates live news. Keep useful regression tests; remove only disposable probes and generated caches. Dependencies are pinned in `requirements.lock` and `package-lock.json`; update profile checks with dependency upgrades.

MIT licensed. DeepSeek Harness and `dsh-free-search` retain their own licenses.
