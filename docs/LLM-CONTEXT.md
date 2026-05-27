# Waddle 🐧 — LLM Context

This file is an instruction for AI tools (Claude, ChatGPT, Cursor, etc.).
Read it before making any changes to the Waddle project.

---

## What is Waddle

An independent external uptime monitor. Pings sites and services from outside,
writes results to `status.json`, serves the status page as static files.
Runs as a GitHub Actions cron, GitLab CI scheduled pipeline, or Kubernetes CronJob.
No server, no npm, no frameworks.

**Core idea:** if your server goes down and all your bots go silent — Waddle lives separately and speaks up.

---

## Repo structure

```
waddle/
├── index.html              ← status page (SPA, zero npm)
├── style.css               ← all styles (linked from index.html)
├── config.json             ← site name, timezone
├── targets.yaml            ← WHAT to monitor and WHERE to alert — edit this
├── status.json             ← check results (auto-generated, do not edit)
├── monitor.py              ← monitoring script (asyncio, aiohttp, pyyaml)
├── requirements.txt        ← aiohttp>=3.9, pyyaml>=6.0
│
├── .github/workflows/
│   ├── monitor.yml         ← cron (disabled by default), writes status.json, commits
│   └── deploy.yml          ← deploys GitHub Pages on push or after monitor run
│
├── .gitlab-ci.yml          ← monitor (scheduled) + pages (on push)
│
├── k8s/
│   ├── cronjob.yaml        ← CronJob */5 * * * *, same monitor.py
│   ├── deployment.yaml     ← nginx Pod + Service serving index.html
│   ├── configmap.yaml      ← targets.yaml, config.json, nginx.conf, PVC
│   ├── ingress.yaml        ← Ingress with optional TLS
│   └── secret.yaml.example ← Secret template (do not commit filled-in version)
│
└── docs/
    ├── LLM-CONTEXT.md      ← this file
    └── architecture.md     ← architecture overview
```

---

## config.json

```json
{
  "site_name": "My Status Page",
  "tagline": "status page",
  "timezone": "UTC"
}
```

All fields are optional. `index.html` reads this file on load.

---

## targets.yaml — full structure

Two sections: `notifications.channels` and `targets`.
Channels are declared once with a unique `id`.
Targets reference channels via `notify: ["id1", "id2"]`.

```yaml
settings:
  interval_minutes: 15        # recommended for GitHub free plan
  timeout_seconds: 10
  slow_threshold_ms: 2000
  history_keep: 200           # how many recent checks to keep per target

notifications:
  channels:
    - id: "tg"
      type: telegram
      token: "${TELEGRAM_BOT_TOKEN}"
      chat_id: "${TELEGRAM_CHAT_ID}"

    - id: "discord"
      type: discord
      webhook_url: "${DISCORD_WEBHOOK_URL}"

    - id: "email"
      type: email
      smtp_host: "${SMTP_HOST}"
      smtp_port: 587
      smtp_user: "${SMTP_USER}"
      smtp_pass: "${SMTP_PASS}"
      to: "you@example.com"

    - id: "hook"
      type: webhook
      url: "${WEBHOOK_URL}"
      method: POST            # GET or POST

targets:
  - name: "My site"
    url: "https://example.com"
    type: http                # http | tcp | ssl
    group: "production"       # used for grouping on the status page
    expected_status: 200      # default 200
    check_text: "Welcome"     # optional: text that must appear in response body
    notify: ["tg", "email"]

  - name: "Mail server"
    url: "mail.example.com:25"
    type: tcp
    group: "infrastructure"
    notify: ["tg"]

  - name: "SSL cert"
    url: "https://example.com"
    type: ssl
    ssl_warn_days: 14         # default 14
    group: "production"
    notify: ["email"]

  - name: "Staging"
    url: "https://staging.example.com"
    notify: ["discord"]
    maintenance:
      enabled: true
      start: "2026-06-01T02:00:00Z"
      end:   "2026-06-01T04:00:00Z"
```

---

## Channel types

| type | Required fields | Optional |
|---|---|---|
| `telegram` | `token`, `chat_id` | — |
| `discord` | `webhook_url` | — |
| `email` | `smtp_host`, `smtp_user`, `smtp_pass`, `to` | `smtp_port` (default 587) |
| `webhook` | `url` | `method` (GET/POST, default POST) |

Secrets are always `"${VAR_NAME}"` — substituted from environment when monitor.py runs.

---

## Check types

| type | What it checks | Specific fields |
|---|---|---|
| `http` | status code, response time, optional body text | `expected_status`, `check_text`, `slow_threshold_ms` |
| `tcp` | TCP connect to host:port | url in `"host:port"` format |
| `ssl` | days until certificate expires | `ssl_warn_days` |

**Limitation:** `type: tcp` only works reliably in Kubernetes mode.
GitHub/GitLab Actions may block non-standard ports.

---

## Statuses

| Status | Meaning | Alert |
|---|---|---|
| `up` | all good | only if previously `down` or `slow` |
| `down` | unreachable / wrong status code / timeout | yes |
| `slow` | response_ms > slow_threshold_ms | yes |
| `warn` | SSL expires in < ssl_warn_days days | yes |
| `maint` | maintenance window is active | no |

Alerts fire only on **status change**. No spam while a service stays down.

---

## How monitor.py handles secrets

All `"${VAR_NAME}"` values in `targets.yaml` are replaced with environment variable values at runtime.
Don't change the format — just add the variable to the appropriate place
(GitHub Secrets / GitLab Variables / k8s Secret).

---

## Where secrets live

**GitHub:** Settings → Secrets and variables → Actions

**GitLab:** Settings → CI/CD → Variables

**Kubernetes:**
```bash
kubectl create secret generic waddle-secrets \
  --from-literal=TELEGRAM_BOT_TOKEN=xxx \
  --from-literal=TELEGRAM_CHAT_ID=xxx \
  --from-literal=DISCORD_WEBHOOK_URL=xxx \
  --from-literal=SMTP_HOST=xxx \
  --from-literal=SMTP_USER=xxx \
  --from-literal=SMTP_PASS=xxx \
  --from-literal=WEBHOOK_URL=xxx
```

---

## Common tasks

### Add a site to monitor

```yaml
targets:
  - name: "Google"
    url: "https://google.com"
    group: "external"
    notify: ["tg"]
```

### Add a channel and route alerts to it

```yaml
notifications:
  channels:
    - id: "dev-discord"
      type: discord
      webhook_url: "${DISCORD_WEBHOOK_URL}"

targets:
  - name: "API"
    url: "https://api.example.com/health"
    notify: ["tg", "dev-discord"]   # was tg only
```

### Silence alerts without removing the target

```yaml
- name: "Dev"
  url: "https://dev.example.com"
  notify: []
```

### Pause monitoring entirely

```yaml
- name: "Dev"
  url: "https://dev.example.com"
  enabled: false
  notify: ["tg"]
```

`enabled: false` — target is skipped completely: not checked, not written to `status.json`, not shown on the page. Remove the field or set `true` to resume.

### Route different services to different channels

Each target has its own `notify` list. One target can notify multiple channels;
different targets can notify different channels — all independent.

---

## Incidents

Create a file in `incidents/` — monitor.py will pick it up on the next run and update `incidents/index.json`. The page renders the section automatically.

**Filename:** `YYYY-MM-DD-short-description.md` — sorted by filename on the page (newest first).

**Template:**

```markdown
---
title: Brief incident title
date: 2026-05-27T14:32:00Z
status: ongoing
---
What happened and which service is affected.

Add updates here as the investigation progresses.
```

When the incident is resolved, change `status: ongoing` to `status: resolved`.

**Frontmatter fields:**

| Field | Required | Values |
|---|---|---|
| `title` | yes | any string |
| `date` | yes | ISO 8601 (`2026-05-27T14:32:00Z`) |
| `status` | yes | `ongoing` or `resolved` |

**Body** — plain markdown: paragraphs, `**bold**`, `` `code` ``.

---

## Do not

- Edit `status.json` manually — overwritten on every run
- Remove `${VAR_NAME}` from values — these are env references, not literal strings
- Add two channels with the same `id` — only the first one is used
- Use `type: tcp` in GitHub/GitLab mode
- Commit a filled-in `k8s/secret.yaml` to the repo

---

## GitHub Actions — important details

`monitor.yml`:
- Schedule is **commented out by default** — uncomment to enable automatic monitoring
- `concurrency: cancel-in-progress: false` — does not cancel the current run
- `[skip ci]` in commit message — prevents recursive pipeline triggers
- `git pull --rebase` before push — avoids conflicts
- `permissions: contents: write` — required, otherwise push returns 403
- At 15 min interval: ~2 880 min/month — fits the 2 000 min free plan with headroom
- The free plan pool is **shared across all repos** — check actual usage at Settings → Billing → Actions usage

`deploy.yml` — triggers on changes to `index.html`, `style.css`, `config.json`,
and after every "Waddle Monitor" workflow completion (via `workflow_run`).
This is how status.json reaches the deployment: commits via `GITHUB_TOKEN` do not
trigger other workflows directly, but `workflow_run` fires after the monitor finishes.

## GitLab CI — important details

`.gitlab-ci.yml`:
- `monitor` stage — scheduled pipelines only (`$CI_PIPELINE_SOURCE == "schedule"`)
- `pages` stage — on every push to main
- Schedule must be added manually: CI/CD → Schedules → `*/15 * * * *`
- Requires `GITLAB_TOKEN` variable with `api` + `write_repository` scope
