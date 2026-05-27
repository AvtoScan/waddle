#!/usr/bin/env python3
"""
Waddle monitor.py
Reads targets.yaml, checks each target, writes status.json, sends alerts.
"""

import asyncio
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import aiohttp
import yaml

# ── paths ────────────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent
TARGETS_FILE    = ROOT / "targets.yaml"
STATUS_FILE     = ROOT / "status.json"
BADGE_FILE      = ROOT / "badge.svg"
INCIDENTS_DIR   = ROOT / "incidents"
INCIDENTS_INDEX = INCIDENTS_DIR / "index.json"

# ── helpers ──────────────────────────────────────────────────────────────────

def resolve_env(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )

def resolve_channel(channel: dict) -> dict:
    """Resolve all env placeholders in a channel config."""
    return {k: resolve_env(v) for k, v in channel.items()}

def load_config() -> dict:
    if not TARGETS_FILE.exists():
        print(f"[waddle] ERROR: {TARGETS_FILE} not found", file=sys.stderr)
        sys.exit(1)
    with open(TARGETS_FILE) as f:
        return yaml.safe_load(f)

def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"updated_at": None, "targets": []}

def save_status(data: dict) -> None:
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def in_maintenance(target: dict) -> bool:
    m = target.get("maintenance", {})
    if not m or not m.get("enabled"):
        return False
    try:
        start = datetime.fromisoformat(m["start"].replace("Z", "+00:00"))
        end   = datetime.fromisoformat(m["end"].replace("Z",   "+00:00"))
        return start <= datetime.now(timezone.utc) <= end
    except (KeyError, ValueError):
        return False

def trim_history(history: list, keep: int) -> list:
    return history[-keep:] if len(history) > keep else history

def previous_status(old_targets: list, name: str) -> str | None:
    for t in old_targets:
        if t.get("name") == name:
            return t.get("status")
    return None

# ── badge ────────────────────────────────────────────────────────────────────

def generate_badge(targets: list) -> None:
    has_down = any(t.get("status") == "down" and not t.get("maintenance") for t in targets)
    has_slow = any(t.get("status") in ("slow", "warn") and not t.get("maintenance") for t in targets)

    if has_down:
        label, color = "partial outage", "#c0392b"
    elif has_slow:
        label, color = "degraded", "#d68910"
    else:
        label, color = "operational", "#1e8449"

    lw = 54
    rw = len(label) * 7 + 14
    tw = lw + rw
    lx, rx = lw // 2, lw + rw // 2
    font = "DejaVu Sans,Verdana,Geneva,sans-serif"

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{tw}" height="20">'
        f'<clipPath id="r"><rect width="{tw}" height="20" rx="3"/></clipPath>'
        f'<g clip-path="url(#r)">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
        f'</g>'
        f'<g fill="#fff" text-anchor="middle" font-family="{font}" font-size="11">'
        f'<text x="{lx}" y="15" fill="#010101" fill-opacity=".25">waddle</text>'
        f'<text x="{lx}" y="14">waddle</text>'
        f'<text x="{rx}" y="15" fill="#010101" fill-opacity=".25">{label}</text>'
        f'<text x="{rx}" y="14">{label}</text>'
        f'</g>'
        f'</svg>'
    )
    BADGE_FILE.write_text(svg, encoding="utf-8")

# ── incidents ─────────────────────────────────────────────────────────────────

def parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    parts = content[3:].split("---", 1)
    if len(parts) < 2:
        return {}, content
    try:
        meta = yaml.safe_load(parts[0]) or {}
        return meta, parts[1].strip()
    except yaml.YAMLError:
        return {}, content.strip()

def scan_incidents() -> list[dict]:
    if not INCIDENTS_DIR.exists():
        return []
    incidents = []
    for path in sorted(INCIDENTS_DIR.glob("*.md"), reverse=True):
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            date = meta.get("date", "")
            if hasattr(date, "isoformat"):
                date = date.isoformat()
            incidents.append({
                "file":   path.name,
                "title":  meta.get("title", path.stem),
                "date":   str(date),
                "status": meta.get("status", "resolved"),
                "body":   body,
            })
        except OSError:
            pass
    return incidents

def generate_incident_index(incidents: list[dict]) -> None:
    INCIDENTS_DIR.mkdir(exist_ok=True)
    with open(INCIDENTS_INDEX, "w", encoding="utf-8") as f:
        json.dump(incidents, f, indent=2, ensure_ascii=False)

# ── checks ───────────────────────────────────────────────────────────────────

async def check_http(target: dict, timeout: int, slow_ms: int) -> dict:
    url           = target["url"]
    expected_code = target.get("expected_status", 200)
    check_text    = target.get("check_text")

    result = {
        "status": "unknown",
        "status_code": None,
        "response_ms": None,
        "error": None,
    }

    try:
        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                ssl=False,
            ) as resp:
                body = await resp.text(errors="replace")
                ms   = int((time.monotonic() - t0) * 1000)

                result["status_code"] = resp.status
                result["response_ms"] = ms

                if resp.status != expected_code:
                    result["status"] = "down"
                    result["error"]  = f"HTTP {resp.status} (expected {expected_code})"
                elif check_text and check_text not in body:
                    result["status"] = "down"
                    result["error"]  = f"text not found: {check_text!r}"
                elif ms > slow_ms:
                    result["status"] = "slow"
                else:
                    result["status"] = "up"

    except asyncio.TimeoutError:
        result["status"] = "down"
        result["error"]  = f"timeout (>{timeout}s)"
    except aiohttp.ClientError as e:
        result["status"] = "down"
        result["error"]  = str(e)

    return result

def check_tcp(target: dict, timeout: int) -> dict:
    """TCP connect check. URL format: host:port"""
    url = target["url"]
    result = {"status": "unknown", "response_ms": None, "error": None}
    try:
        host, port_str = url.rsplit(":", 1)
        port = int(port_str)
    except ValueError:
        result["status"] = "down"
        result["error"]  = f"invalid tcp url: {url!r} — expected host:port"
        return result

    try:
        t0 = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            ms = int((time.monotonic() - t0) * 1000)
        result["status"]      = "up"
        result["response_ms"] = ms
    except (socket.timeout, TimeoutError):
        result["status"] = "down"
        result["error"]  = f"timeout (>{timeout}s)"
    except OSError as e:
        result["status"] = "down"
        result["error"]  = str(e)

    return result

def check_ssl(target: dict, warn_days: int) -> dict:
    """Check SSL cert expiry. Returns 'up', 'warn', or 'down'."""
    url = target["url"]
    result = {"status": "unknown", "ssl_days_left": None, "error": None}

    # strip scheme
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    ctx  = ssl.create_default_context()

    try:
        conn = ctx.wrap_socket(
            socket.create_connection((host, 443), timeout=10),
            server_hostname=host,
        )
        cert   = conn.getpeercert()
        conn.close()

        expire_str = cert["notAfter"]
        expire     = datetime.strptime(expire_str, "%b %d %H:%M:%S %Y %Z")
        expire     = expire.replace(tzinfo=timezone.utc)
        days_left  = (expire - datetime.now(timezone.utc)).days

        result["ssl_days_left"] = days_left

        if days_left <= 0:
            result["status"] = "down"
            result["error"]  = "certificate expired"
        elif days_left <= warn_days:
            result["status"] = "warn"
            result["error"]  = f"expires in {days_left} days"
        else:
            result["status"] = "up"

    except ssl.SSLError as e:
        result["status"] = "down"
        result["error"]  = f"SSL error: {e}"
    except (socket.timeout, TimeoutError):
        result["status"] = "down"
        result["error"]  = "timeout"
    except OSError as e:
        result["status"] = "down"
        result["error"]  = str(e)

    return result

# ── notifications ─────────────────────────────────────────────────────────────

async def send_telegram(channel: dict, text: str) -> None:
    token   = channel.get("token", "")
    chat_id = channel.get("chat_id", "")
    if not token or not chat_id:
        print(f"[waddle] telegram: missing token or chat_id", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": chat_id, "text": text}, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        print(f"[waddle] telegram error: {e}", file=sys.stderr)

async def send_discord(channel: dict, text: str) -> None:
    url = channel.get("webhook_url", "")
    if not url:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"content": text}, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        print(f"[waddle] discord error: {e}", file=sys.stderr)

def send_email(channel: dict, text: str, subject: str) -> None:
    host = channel.get("smtp_host", "")
    user = channel.get("smtp_user", "")
    pwd  = channel.get("smtp_pass", "")
    to   = channel.get("to", "")
    port = int(channel.get("smtp_port", 587))
    if not all([host, user, pwd, to]):
        print(f"[waddle] email: missing smtp config", file=sys.stderr)
        return
    try:
        msg              = MIMEText(text)
        msg["Subject"]   = subject
        msg["From"]      = user
        msg["To"]        = to
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, pwd)
            smtp.send_message(msg)
    except Exception as e:
        print(f"[waddle] email error: {e}", file=sys.stderr)

async def send_webhook(channel: dict, text: str) -> None:
    url    = channel.get("url", "")
    method = channel.get("method", "POST").upper()
    if not url:
        return
    payload = {"text": text, "timestamp": now_iso()}
    try:
        async with aiohttp.ClientSession() as s:
            if method == "GET":
                await s.get(url, params=payload, timeout=aiohttp.ClientTimeout(total=10))
            else:
                await s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        print(f"[waddle] webhook error: {e}", file=sys.stderr)

async def dispatch_alert(channel: dict, message: str, subject: str) -> None:
    ch_type = channel.get("type", "")
    if ch_type == "telegram":
        await send_telegram(channel, message)
    elif ch_type == "discord":
        await send_discord(channel, message)
    elif ch_type == "email":
        send_email(channel, message, subject)
    elif ch_type == "webhook":
        await send_webhook(channel, message)
    else:
        print(f"[waddle] unknown channel type: {ch_type!r}", file=sys.stderr)

def build_alert_text(target_name: str, url: str, new_status: str, error: str | None, downtime_min: int | None) -> tuple[str, str]:
    """Returns (message_body, email_subject)."""
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    icons = {"down": "🔴", "slow": "🟡", "warn": "⚠️", "up": "🟢"}
    labels = {"down": "DOWN", "slow": "SLOW", "warn": "WARN", "up": "UP"}
    icon  = icons.get(new_status, "❓")
    label = labels.get(new_status, new_status.upper())

    lines = [f"{icon} {label} — {target_name}", url]
    if error:
        lines.append(f"Причина: {error}")
    if new_status == "up" and downtime_min:
        lines.append(f"Недоступность: {downtime_min} мин")
    lines.append(f"⏱ {ts}")

    body    = "\n".join(lines)
    subject = f"[Waddle] {label}: {target_name}"
    return body, subject

# ── main loop ─────────────────────────────────────────────────────────────────

async def run_checks(config: dict, old_status: dict) -> list[dict]:
    settings  = config.get("settings", {})
    timeout   = settings.get("timeout_seconds", 10)
    slow_ms   = settings.get("slow_threshold_ms", 2000)
    keep      = settings.get("history_keep", 200)
    warn_days = settings.get("ssl_warn_days", 14)

    channels_cfg = {
        c["id"]: resolve_channel(c)
        for c in config.get("notifications", {}).get("channels", [])
    }

    old_targets = old_status.get("targets", [])
    results     = []

    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        name     = target["name"]
        url      = target["url"]
        t_type   = target.get("type", "http")
        notify   = target.get("notify", [])
        maint    = in_maintenance(target)

        # find previous record
        prev_record = next((t for t in old_targets if t.get("name") == name), {})
        prev_status = prev_record.get("status")
        history     = list(prev_record.get("history", []))
        last_down   = prev_record.get("last_down")

        print(f"[waddle] checking {name} ({t_type}) ...", end=" ", flush=True)

        # run check
        if t_type == "http":
            check = await check_http(target, timeout, slow_ms)
        elif t_type == "tcp":
            check = check_tcp(target, timeout)
        elif t_type == "ssl":
            check = check_ssl(target, target.get("ssl_warn_days", warn_days))
        else:
            check = {"status": "unknown", "error": f"unknown type: {t_type!r}"}

        new_status = check["status"]
        print(new_status)

        # track downtime start
        if new_status in ("down", "slow") and prev_status not in ("down", "slow"):
            last_down = now_iso()
        if new_status == "up" and prev_status in ("down", "slow"):
            last_down = None

        # history
        history.append(new_status)
        history = trim_history(history, keep)

        # build record
        record = {
            "name":        name,
            "url":         url,
            "type":        t_type,
            "group":       target.get("group", ""),
            "status":      new_status,
            "status_code": check.get("status_code"),
            "response_ms": check.get("response_ms"),
            "ssl_days_left": check.get("ssl_days_left"),
            "error":       check.get("error"),
            "last_down":   last_down,
            "maintenance": maint,
            "history":     history,
        }
        results.append(record)

        # alert only on status change, skip during maintenance
        status_changed = new_status != prev_status
        if status_changed and not maint and notify:
            downtime_min = None
            if new_status == "up" and last_down:
                try:
                    ld = datetime.fromisoformat(last_down.replace("Z", "+00:00"))
                    downtime_min = int((datetime.now(timezone.utc) - ld).total_seconds() / 60)
                except ValueError:
                    pass

            msg, subj = build_alert_text(name, url, new_status, check.get("error"), downtime_min)

            alert_tasks = [
                dispatch_alert(channels_cfg[ch_id], msg, subj)
                for ch_id in notify
                if ch_id in channels_cfg
            ]
            if alert_tasks:
                await asyncio.gather(*alert_tasks)

    return results


async def main() -> None:
    print("[waddle] starting")

    config     = load_config()
    old_status = load_status()

    # validate write permissions early (common GitHub Actions mistake)
    try:
        STATUS_FILE.touch(exist_ok=True)
    except OSError as e:
        print(f"[waddle] ERROR: cannot write {STATUS_FILE}: {e}", file=sys.stderr)
        print("[waddle] GitHub: Settings → Actions → General → Workflow permissions → Read and write", file=sys.stderr)
        sys.exit(1)

    results = await run_checks(config, old_status)

    new_status = {
        "updated_at": now_iso(),
        "targets":    results,
    }
    save_status(new_status)

    try:
        generate_badge(results)
    except OSError as e:
        print(f"[waddle] badge: {e}", file=sys.stderr)

    try:
        generate_incident_index(scan_incidents())
    except OSError as e:
        print(f"[waddle] incidents: {e}", file=sys.stderr)

    print(f"[waddle] done — {len(results)} targets checked, status.json updated")


if __name__ == "__main__":
    asyncio.run(main())
