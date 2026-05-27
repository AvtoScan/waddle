# Waddle 🐧

**Independent external uptime monitor. No server. No cost. Fork and go.**

```
External world → GitHub Actions / Kubernetes CronJob
              → monitor.py pings your services
              → writes status.json
              → sends alerts to configured channels
              → index.html renders the status page
```

Key idea: Waddle lives **outside** your infrastructure. If your server goes down and all your bots go silent — Waddle sees it and speaks up.

Difference from Upptime: GitHub + GitLab out of the box, zero npm, single index.html, multi-channel alerts with per-target routing.

---

## Deployment modes

### GitHub Pages (zero infra)

```
GitHub Actions cron (every 15 min)
  → monitor.py
  → status.json (commit to repo)
  → workflow_run triggers deploy.yml
  → GitHub Pages serves index.html + status.json
```

### GitLab Pages

```
GitLab CI scheduled pipeline (every 15 min)
  → monitor.py (same script)
  → status.json (commit to repo)
  → GitLab Pages serves index.html
```

### Kubernetes

```
CronJob (every 5 min)
  → monitor.py (same script)
  → status.json → PVC
  → nginx Pod serves index.html behind Ingress
```

One script, three modes. No separate code paths.

---

## Repo structure

```
waddle/
├── index.html                  ← status page (SPA, zero npm)
├── style.css
├── config.json                 ← site name, timezone
├── targets.yaml                ← what to monitor and where to alert
├── status.json                 ← results (auto-generated)
├── monitor.py                  ← the only script
├── requirements.txt
│
├── .github/
│   └── workflows/
│       ├── monitor.yml         ← GitHub Actions cron (disabled by default)
│       └── deploy.yml          ← GitHub Pages deployment
│
├── .gitlab-ci.yml              ← GitLab CI scheduled pipeline
│
├── k8s/
│   ├── cronjob.yaml
│   ├── deployment.yaml
│   ├── configmap.yaml
│   ├── ingress.yaml
│   └── secret.yaml.example
│
└── docs/
    ├── LLM-CONTEXT.md          ← AI assistant context
    └── architecture.md         ← this file
```

---

## targets.yaml — full structure

```yaml
settings:
  interval_minutes: 15          # recommended for GitHub free plan
  timeout_seconds: 10
  slow_threshold_ms: 2000
  history_keep: 200             # how many recent checks to keep per target

notifications:
  channels:
    - id: "team-tg"
      type: telegram
      token: "${TELEGRAM_BOT_TOKEN}"
      chat_id: "${TELEGRAM_CHAT_ID}"

    - id: "client-email"
      type: email
      smtp_host: "${SMTP_HOST}"
      smtp_port: 587
      smtp_user: "${SMTP_USER}"
      smtp_pass: "${SMTP_PASS}"
      to: "client@example.com"

    - id: "dev-discord"
      type: discord
      webhook_url: "${DISCORD_WEBHOOK_URL}"

    - id: "custom-hook"
      type: webhook
      url: "${WEBHOOK_URL}"
      method: POST              # GET or POST

targets:
  - name: "Main site"
    url: "https://example.com"
    type: http                  # http | tcp | ssl
    group: "production"
    check_text: "Welcome"       # optional
    expected_status: 200        # optional, default 200
    notify: ["team-tg", "client-email"]

  - name: "API health"
    url: "https://api.example.com/health"
    type: http
    group: "production"
    notify: ["team-tg", "dev-discord"]

  - name: "Mail server"
    url: "mail.example.com:25"
    type: tcp
    group: "infrastructure"
    notify: ["team-tg"]

  - name: "SSL cert"
    url: "https://example.com"
    type: ssl
    ssl_warn_days: 14           # alert N days before expiry
    group: "production"
    notify: ["client-email"]

  - name: "Paused service"
    url: "https://paused.example.com"
    enabled: false              # skip entirely — no check, no alerts, not shown on page
    notify: ["team-tg"]

  # Maintenance window: no alerts during this period
  - name: "Staging"
    url: "https://staging.example.com"
    group: "staging"
    notify: ["dev-discord"]
    maintenance:
      enabled: false
      start: "2026-06-01T02:00:00Z"
      end: "2026-06-01T04:00:00Z"
```

---

## status.json — structure

```json
{
  "updated_at": "2026-05-27T14:32:00Z",
  "targets": [
    {
      "name": "Main site",
      "url": "https://example.com",
      "type": "http",
      "group": "production",
      "status": "up",
      "status_code": 200,
      "response_ms": 312,
      "ssl_days_left": 47,
      "last_down": null,
      "history": ["up","up","up","down","up"]
    }
  ]
}
```

`history` — last `history_keep` checks, rendered as bars on the status page.

---

## What monitor.py checks

| Type | What it does | When it alerts |
|---|---|---|
| `http` | GET request, status code | not `expected_status` |
| `http` + `check_text` | searches text in body | text not found |
| `http` + slow | response time | > `slow_threshold_ms` |
| `http` + timeout | waits `timeout_seconds` | no response |
| `tcp` | TCP connect to host:port | connection refused |
| `ssl` | checks certificate | < `ssl_warn_days` days left |

Alert fires only on **status change**. No spam while a service stays down.

---

## Alert format

```
🔴 DOWN — API health
https://api.example.com/health
Reason: timeout (>10s)
⏱ 14:32 UTC

🟡 SLOW — Main site
Reason: response time 3200ms > 2000ms
⏱ 14:37 UTC

🟢 UP — API health
Downtime: 8 min
⏱ 14:40 UTC

⚠️ WARN — SSL cert
Reason: expires in 12 days
⏱ 09:00 UTC
```

---

## Known limitations

- Only publicly reachable endpoints (GitHub/GitLab Actions run outside your network)
- TCP checks only recommended in Kubernetes mode — Actions may block non-standard ports
- GitHub free plan: 2 000 min/month shared across all repos. At 15 min interval: ~2 880 min/month
- status.json is trimmed to `history_keep` entries per target — file does not grow indefinitely
