#!/usr/bin/env python3
"""Render.com deploy provider for deploy-watch.

Auto-discovers all web services in the workspace and fetches recent
deploys for each, returning them as interleaved JSON-line records
sorted by time (most recent first).
"""

import json
import logging
import os
import ssl
import sys
import traceback
import urllib.request
import urllib.error
from datetime import datetime

# ---------------------------------------------------------------------------
# Debug logging — writes to /tmp/deploy-watch-render.log
# ---------------------------------------------------------------------------

_log = logging.getLogger("overwatch-render")
_log.setLevel(logging.DEBUG)
_log.propagate = False
if not _log.handlers:
    import tempfile
    _log_path = os.path.join(tempfile.gettempdir(), "deploy-watch-render.log")
    try:
        _fh = logging.FileHandler(_log_path)
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        _log.addHandler(_fh)
    except (PermissionError, OSError):
        _log.addHandler(logging.NullHandler())

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    _log.debug("SSL: using certifi (%s)", certifi.where())
except (ImportError, AttributeError):
    SSL_CONTEXT = None
    _log.debug("SSL: certifi not available, SSL_CONTEXT=None")

RENDER_API_BASE = "https://api.render.com/v1"
TIMEOUT = 10
DEPLOYS_PER_SERVICE = 5
MAX_TOTAL_RECORDS = 20


def get_config_from_env():
    """Read provider config from DEPLOY_WATCH_* environment variables."""
    owner_id = os.environ.get("DEPLOY_WATCH_OWNERID", "")
    _log.debug("DEPLOY_WATCH_OWNERID %s", owner_id or "NOT SET (will list all)")

    api_key_env = os.environ.get("DEPLOY_WATCH_APIKEYENV", "RENDER_DOT_COM_TOK")
    api_key = os.environ.get(api_key_env, "")
    _log.debug("API key env var: %s (%s)", api_key_env, "set" if api_key else "NOT SET")
    if not api_key:
        print(
            f"Error: environment variable {api_key_env} is not set. "
            f"Set it to your Render API key, or specify a different env var "
            f"name via apiKeyEnv in .deploy-watch.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    return owner_id, api_key


class ApiError(Exception):
    """Non-fatal API error with details."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def api_get(path, api_key, fatal=True):
    """Make an authenticated GET request to the Render API.

    If fatal=True (default), exits on error. If fatal=False, raises ApiError.
    """
    url = f"{RENDER_API_BASE}{path}"
    _log.debug("API GET %s", url)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as resp:
            _log.debug("API response %s %s", resp.status, url)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        _log.error("API HTTP %s for %s: %s", e.code, url, body[:200])
        if fatal:
            print(f"Error: Render API returned {e.code}: {body}", file=sys.stderr)
            sys.exit(1)
        raise ApiError(f"HTTP {e.code}: {body[:200]}", status_code=e.code)
    except Exception as e:
        _log.error("API error for %s: %s", url, e)
        if fatal:
            print(f"Error: could not reach Render API: {e}", file=sys.stderr)
            sys.exit(1)
        raise ApiError(f"{type(e).__name__}: {e}")


def fetch_services(api_key, owner_id=""):
    """Discover all web services, optionally filtered by owner/workspace."""
    params = "?type=web_service&limit=100"
    if owner_id:
        params += f"&ownerId={owner_id}"
    data = api_get(f"/services{params}", api_key)

    services = []
    if isinstance(data, list):
        for item in data:
            # API may return {service: {...}} wrappers or flat objects
            svc = item.get("service", item) if isinstance(item, dict) else item
            if svc.get("type") == "web_service":
                services.append(svc)

    _log.debug("Discovered %d web services", len(services))
    return services


STATUS_MAP = {
    "created": "pending",
    "build_in_progress": "building",
    "update_in_progress": "deploying",
    "live": "live",
    "build_failed": "failed",
    "update_failed": "failed",
    "canceled": "cancelled",
    "deactivated": "cancelled",
}

_BRANCH_ENV_MAP = {
    "main": "prod",
    "master": "prod",
    "production": "prod",
    "staging": "staging",
    "stg": "staging",
    "dev": "dev",
    "develop": "dev",
}

# Keywords that map to environments when found anywhere in the branch name
_ENV_KEYWORDS = [
    ("prod", "prod"),
    ("main", "prod"),
    ("master", "prod"),
    ("staging", "staging"),
    ("stg", "staging"),
    ("dev", "dev"),
]


def _infer_environment(branch: str) -> str:
    """Infer environment from the service's configured branch."""
    key = branch.lower().strip()
    # Exact match first
    env = _BRANCH_ENV_MAP.get(key)
    if env:
        return env
    # Keyword match (e.g. deploy/staging, release/prod)
    for keyword, env_name in _ENV_KEYWORDS:
        if keyword in key:
            return env_name
    return ""


def iso_to_epoch(iso_str):
    """Convert an ISO 8601 timestamp to unix epoch string."""
    if not iso_str:
        return ""
    s = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return str(int(dt.timestamp()))
    except (ValueError, TypeError):
        return ""


def _unwrap_deploys(deploys_raw):
    """Unwrap deploy objects from API response."""
    deploys = []
    if isinstance(deploys_raw, list):
        for item in deploys_raw:
            if isinstance(item, dict) and "deploy" in item:
                deploys.append(item["deploy"])
            else:
                deploys.append(item)
    return deploys


def _build_record(deploy, svc_id, svc_name, svc_url, environment, branch=""):
    """Build a single deploy record dict."""
    status_raw = deploy.get("status", "")
    mapped_status = STATUS_MAP.get(status_raw, status_raw)

    commit_obj = deploy.get("commit", {}) or {}
    created_at = iso_to_epoch(deploy.get("createdAt"))
    finished_at = iso_to_epoch(deploy.get("finishedAt"))

    commit_id = commit_obj.get("id", "")
    commit_msg = (commit_obj.get("message", "") or "").split("\n")[0]

    deploy_id = deploy.get("id", "")

    record = {
        "commit": commit_id[:7] if commit_id else "",
        "tag": "",
        "version": "",
        "message": commit_msg,
        "author": deploy.get("creator", {}).get("name", "")
                  or deploy.get("creator", {}).get("email", ""),
        "environment": environment,
        "service_name": svc_name,
        "branch": branch,
    }

    # Map build/deploy status
    if mapped_status in ("pending", "building"):
        record["build_status"] = mapped_status
        record["deploy_status"] = "pending"
    elif mapped_status == "deploying":
        record["build_status"] = "success"
        record["deploy_status"] = "deploying"
    elif mapped_status == "live":
        record["build_status"] = "success"
        record["deploy_status"] = "live"
    elif mapped_status == "failed":
        if status_raw == "build_failed":
            record["build_status"] = "failed"
            record["deploy_status"] = ""
        else:
            record["build_status"] = "success"
            record["deploy_status"] = "failed"
    elif mapped_status == "cancelled":
        record["build_status"] = "cancelled"
        record["deploy_status"] = "cancelled"
    else:
        record["build_status"] = mapped_status
        record["deploy_status"] = mapped_status

    # Timestamps
    record["build_started"] = created_at
    if mapped_status == "live" and finished_at:
        record["deploy_finished"] = finished_at
    else:
        record["deploy_finished"] = ""

    if svc_url:
        record["service_url"] = svc_url

    # IDs for log fetching
    record["service_id"] = svc_id
    record["deploy_id"] = deploy_id

    # Render deploy dashboard URL
    if deploy_id:
        record["deploy_url"] = (
            f"https://dashboard.render.com/web/{svc_id}/deploys/{deploy_id}"
        )

    return record


def cmd_name():
    print("Render")


def cmd_config():
    config = {
        "fields": [
            {
                "key": "ownerId",
                "label": "Owner/Team ID (tea-xxx, optional)",
                "required": False,
                "default": "",
            },
            {
                "key": "apiKeyEnv",
                "label": "API Key env var",
                "required": False,
                "default": "RENDER_DOT_COM_TOK",
            },
        ]
    }
    print(json.dumps(config))


def _fetch_service_deploys(svc, api_key):
    """Fetch deploys for a single service. Returns list of (sort_key, record)."""
    svc_id = svc["id"]
    svc_name = svc.get("name", "")
    svc_url = (
        svc.get("serviceDetails", {}).get("url", "")
        or svc.get("url", "")
    )
    branch = svc.get("branch", "")
    environment = _infer_environment(branch)

    results = []
    try:
        deploys_raw = api_get(
            f"/services/{svc_id}/deploys?limit={DEPLOYS_PER_SERVICE}",
            api_key,
        )
    except (SystemExit, ApiError):
        _log.warning("Failed to fetch deploys for %s (non-fatal)", svc_id)
        return results

    for deploy in _unwrap_deploys(deploys_raw):
        record = _build_record(deploy, svc_id, svc_name, svc_url, environment, branch)
        sort_key = deploy.get("createdAt", "")
        results.append((sort_key, record))
    return results


def cmd_list():
    from concurrent.futures import ThreadPoolExecutor

    owner_id, api_key = get_config_from_env()

    services = fetch_services(api_key, owner_id)
    if not services:
        _log.debug("No web services found")
        return

    # Fetch deploys from all services in parallel
    all_deploys = []
    with ThreadPoolExecutor(max_workers=min(len(services), 8)) as pool:
        futures = [
            pool.submit(_fetch_service_deploys, svc, api_key)
            for svc in services
        ]
        for future in futures:
            try:
                all_deploys.extend(future.result())
            except Exception as e:
                _log.warning("Error fetching deploys: %s", e)

    # Sort by time descending, truncate
    all_deploys.sort(key=lambda x: x[0], reverse=True)
    for _, record in all_deploys[:MAX_TOTAL_RECORDS]:
        print(json.dumps(record))

    _log.debug("Emitted %d records from %d services",
               min(len(all_deploys), MAX_TOTAL_RECORDS), len(services))


import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\(B")


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text).strip()


def _format_timestamp(ts):
    """Extract a short time string from an ISO timestamp."""
    if not ts:
        return ""
    # "2026-03-22T18:23:24.36052Z" → "18:23:24"
    if "T" in ts:
        time_part = ts.split("T")[-1]
        # Strip timezone and fractional seconds
        for sep in ("+", "Z"):
            time_part = time_part.split(sep)[0]
        # Strip fractional seconds
        if "." in time_part:
            time_part = time_part.split(".")[0]
        return time_part
    return ts


def _extract_message(entry):
    """Extract the log message from a log entry dict, trying common field names."""
    for key in ("message", "msg", "text", "line"):
        val = entry.get(key, "")
        if val:
            return str(val)
    # If entry has a nested "log" object, try that
    log = entry.get("log")
    if isinstance(log, dict):
        return _extract_message(log)
    if isinstance(log, str):
        return log
    return None


def _print_log_entries(data):
    """Print log entries from various API response formats."""
    printed = 0

    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                print(_strip_ansi(entry))
                printed += 1
            elif isinstance(entry, dict):
                ts = entry.get("timestamp", "") or entry.get("time", "") or entry.get("t", "")
                msg = _extract_message(entry)
                if msg is None or msg.strip() == "":
                    # Skip entries with no actual message content
                    continue
                msg = _strip_ansi(msg)
                # Add blank line before build step headers for readability
                if msg.startswith("==>") and printed > 0:
                    print()
                if ts:
                    print(f"[{_format_timestamp(ts)}] {msg}")
                else:
                    print(msg)
                printed += 1
            else:
                print(str(entry))
                printed += 1
    elif isinstance(data, dict):
        # Try nested log arrays
        for key in ("logs", "log", "entries", "lines", "items", "data"):
            items = data.get(key, [])
            if isinstance(items, list) and items:
                _print_log_entries(items)
                return
        # Single entry — try to extract message
        msg = _extract_message(data)
        if msg:
            ts = data.get("timestamp", "") or data.get("time", "")
            if ts:
                print(f"[{_format_timestamp(ts)}] {msg}")
            else:
                print(msg)
        else:
            print(json.dumps(data, indent=2))
        printed += 1

    return printed


def cmd_logs(service_id, deploy_id, log_type="build"):
    """Fetch and print deploy logs for a given service + deploy.

    log_type: "build", "deploy", or "app"
    """
    owner_id, api_key = get_config_from_env()

    # Get deploy details for context and time range
    deploy_data = None
    try:
        deploy_data = api_get(
            f"/services/{service_id}/deploys/{deploy_id}", api_key, fatal=False
        )
    except ApiError as e:
        print(f"(could not fetch deploy details: {e})")

    start_time = ""
    end_time = ""
    if deploy_data:
        created = deploy_data.get("createdAt", "")
        finished = deploy_data.get("finishedAt", "")
        start_time = created or ""
        end_time = finished or ""

    # If no ownerId configured, try to get it from the service details
    if not owner_id:
        try:
            svc_data = api_get(f"/services/{service_id}", api_key, fatal=False)
            if svc_data:
                owner_id = svc_data.get("ownerId", "")
        except ApiError:
            pass

    if not owner_id:
        print("(ownerId is required for logs — configure it in provider settings)")
        return

    # Render API: GET /v1/logs
    #   resource: service ID
    #   ownerId: workspace/owner ID
    #   type: "build" | "app" | "request"
    #   startTime/endTime: epoch/unix timestamps (not ISO)
    #   direction: "forward" | "backward"
    from urllib.parse import urlencode

    # Map log_type to Render API type param
    # "build" → build logs, "deploy" → app logs during deploy window, "app" → app logs
    api_type = log_type if log_type in ("build", "app", "request") else "build"
    if log_type == "deploy":
        api_type = "app"  # deploy logs are app logs scoped to the deploy window

    params = {
        "resource": service_id,
        "ownerId": owner_id,
        "type": api_type,
        "limit": "100",
    }

    if log_type == "app":
        # Service logs: show recent, most recent first
        params["direction"] = "backward"
    else:
        # Build/deploy logs: scoped to deploy window, oldest first
        params["direction"] = "forward"
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

    # Fetch logs with pagination
    total_printed = 0
    max_pages = 10  # safety limit

    for _page in range(max_pages):
        try:
            data = api_get(f"/logs?{urlencode(params)}", api_key, fatal=False)
        except ApiError as e:
            if total_printed == 0:
                print(f"Logs API failed: {e}")
            return

        if data is None:
            if total_printed == 0:
                print("(no response from logs API)")
            return

        # Extract log entries from response
        entries = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = data.get("logs", [])

        if entries:
            total_printed += _print_log_entries(entries)

        # Check for more pages
        has_more = False
        if isinstance(data, dict):
            has_more = data.get("hasMore", False)
            if has_more:
                next_start = data.get("nextStartTime")
                next_end = data.get("nextEndTime")
                if next_start:
                    params["startTime"] = str(next_start)
                if next_end:
                    params["endTime"] = str(next_end)

        if not has_more:
            break

    if total_printed == 0:
        print("(no log entries in this time window)")


def main():
    _log.debug("--- invoked: %s", " ".join(sys.argv))

    if len(sys.argv) < 2:
        _log.error("No command given")
        print("Usage: renderdotcom.py <name|config|list|logs>", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "name":
            cmd_name()
        elif cmd == "config":
            cmd_config()
        elif cmd == "list":
            cmd_list()
        elif cmd == "logs":
            if len(sys.argv) < 4:
                print("Usage: renderdotcom.py logs <serviceId> <deployId> [build|deploy|app]", file=sys.stderr)
                sys.exit(1)
            lt = sys.argv[4] if len(sys.argv) > 4 else "build"
            cmd_logs(sys.argv[2], sys.argv[3], lt)
        else:
            _log.error("Unknown command: %s", cmd)
            print(f"Error: unknown command '{cmd}'", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        _log.error("Unhandled exception:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
