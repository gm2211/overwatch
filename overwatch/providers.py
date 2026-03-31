"""Provider discovery, configuration, and deploy fetching."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

from .config import config_read

_log = logging.getLogger("overwatch")


def default_providers_dir() -> str:
    """Find providers directory relative to this package.

    Layout: overwatch/overwatch/providers.py
    Providers: overwatch/providers/
    """
    overwatch_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(overwatch_dir, "providers")


def list_providers(providers_dir: str) -> list[str]:
    """List available provider scripts (executable files in providers dir)."""
    providers: list[str] = []
    if not os.path.isdir(providers_dir):
        return providers
    for fname in sorted(os.listdir(providers_dir)):
        fpath = os.path.join(providers_dir, fname)
        if not os.path.isfile(fpath) or not os.access(fpath, os.X_OK):
            continue
        if fname.startswith("README") or fname.endswith(".md"):
            continue
        providers.append(fname)
    return providers


def provider_display_name(provider: str, providers_dir: str) -> str:
    """Get the human-readable name of a provider."""
    script = os.path.join(providers_dir, provider)
    if not os.access(script, os.X_OK):
        return provider
    try:
        result = subprocess.run(
            [script, "name"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return provider


_config_fields_cache: dict[str, list[dict]] = {}


def provider_config_fields(provider: str, providers_dir: str) -> list[dict]:
    """Get config fields for a provider.

    Returns list of dicts with keys: key, label, required, default.
    """
    cache_key = f"{providers_dir}/{provider}"
    if cache_key in _config_fields_cache:
        return _config_fields_cache[cache_key]

    script = os.path.join(providers_dir, provider)
    if not os.access(script, os.X_OK):
        return []
    try:
        result = subprocess.run(
            [script, "config"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        fields = data.get("fields", [])
        for f in fields:
            f.setdefault("required", False)
            f.setdefault("default", "")
        _config_fields_cache[cache_key] = fields
        return fields
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []


class FetchError(Exception):
    """Raised when fetch_deploys encounters an error, with a user-visible message."""
    pass


def fetch_deploys(config_file: str, providers_dir: str) -> list[dict] | None:
    """Call the configured provider's list command and return parsed records.

    Returns list of records on success, None on error.
    Raises FetchError with a descriptive message on failure.
    """
    cfg = config_read(config_file)
    provider = cfg.get("provider")
    if not provider:
        _log.debug("fetch_deploys: no provider configured")
        return []

    script = os.path.join(providers_dir, provider)
    if not os.access(script, os.X_OK):
        _log.warning("fetch_deploys: provider script not executable: %s", script)
        raise FetchError(f"Provider script not executable: {script}")

    fields = provider_config_fields(provider, providers_dir)
    env = os.environ.copy()
    provider_cfg = cfg.get(provider, {})

    for field in fields:
        key = field["key"]
        val = provider_cfg.get(key, field.get("default", ""))
        env_key = f"DEPLOY_WATCH_{key.upper()}"
        env[env_key] = str(val)

    _log.debug("fetch_deploys: calling %s list", script)
    try:
        result = subprocess.run(
            [script, "list"], capture_output=True, text=True,
            timeout=30, env=env
        )
        if result.stderr.strip():
            _log.debug("fetch_deploys: stderr: %s", result.stderr.strip())
        if result.returncode != 0:
            stderr_msg = result.stderr.strip()[:200] if result.stderr.strip() else f"exit code {result.returncode}"
            _log.warning("fetch_deploys: provider exited %d: %s", result.returncode, stderr_msg)
            raise FetchError(f"Provider failed: {stderr_msg}")
        records: list[dict] = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                _log.warning("fetch_deploys: bad JSON line: %s", line[:120])
        _log.debug("fetch_deploys: parsed %d records", len(records))
        return records
    except subprocess.TimeoutExpired:
        _log.error("fetch_deploys: provider timed out after 30s")
        raise FetchError("Provider timed out (30s)")
    except FileNotFoundError:
        _log.error("fetch_deploys: provider script not found: %s", script)
        raise FetchError(f"Provider script not found: {script}")


def fetch_logs(
    config_file: str,
    providers_dir: str,
    service_id: str,
    deploy_id: str,
    log_type: str = "build",
) -> str:
    """Call the provider's logs command and return output text.

    log_type: "build", "deploy", or "app"
    Returns the log text, or raises FetchError on failure.
    """
    cfg = config_read(config_file)
    provider = cfg.get("provider")
    if not provider:
        raise FetchError("No provider configured")

    script = os.path.join(providers_dir, provider)
    if not os.access(script, os.X_OK):
        raise FetchError(f"Provider script not executable: {script}")

    fields = provider_config_fields(provider, providers_dir)
    env = os.environ.copy()
    provider_cfg = cfg.get(provider, {})
    for field in fields:
        key = field["key"]
        val = provider_cfg.get(key, field.get("default", ""))
        env[f"DEPLOY_WATCH_{key.upper()}"] = str(val)

    try:
        result = subprocess.run(
            [script, "logs", service_id, deploy_id, log_type],
            capture_output=True, text=True, timeout=30, env=env,
        )
        output = result.stdout
        if result.returncode != 0:
            stderr_msg = result.stderr.strip()[:200] if result.stderr.strip() else f"exit code {result.returncode}"
            if output:
                return output + f"\n[stderr: {stderr_msg}]"
            raise FetchError(f"Logs failed: {stderr_msg}")
        if not output.strip():
            return "(no logs available)"
        return output
    except subprocess.TimeoutExpired:
        raise FetchError("Log fetch timed out (30s)")
    except FileNotFoundError:
        raise FetchError(f"Provider script not found: {script}")


def cancel_deploy(
    config_file: str,
    providers_dir: str,
    service_id: str,
    deploy_id: str,
) -> str:
    """Call the provider's cancel command to cancel a deploy.

    Returns success message or raises FetchError on failure.
    """
    cfg = config_read(config_file)
    provider = cfg.get("provider")
    if not provider:
        raise FetchError("No provider configured")

    script = os.path.join(providers_dir, provider)
    if not os.access(script, os.X_OK):
        raise FetchError(f"Provider script not executable: {script}")

    fields = provider_config_fields(provider, providers_dir)
    env = os.environ.copy()
    provider_cfg = cfg.get(provider, {})
    for field in fields:
        key = field["key"]
        val = provider_cfg.get(key, field.get("default", ""))
        env[f"DEPLOY_WATCH_{key.upper()}"] = str(val)

    try:
        result = subprocess.run(
            [script, "cancel", service_id, deploy_id],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0:
            stderr_msg = result.stderr.strip()[:200] if result.stderr.strip() else f"exit code {result.returncode}"
            raise FetchError(f"Cancel failed: {stderr_msg}")
        return result.stdout.strip() or "Deploy cancelled"
    except subprocess.TimeoutExpired:
        raise FetchError("Cancel timed out (30s)")
    except FileNotFoundError:
        raise FetchError(f"Provider script not found: {script}")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def elapsed_since(start_epoch: int | str) -> str:
    """Format elapsed time from a unix epoch to now."""
    try:
        start = int(start_epoch)
    except (ValueError, TypeError):
        return str(start_epoch)
    diff = max(0, int(time.time()) - start)
    if diff < 60:
        return f"{diff}s"
    elif diff < 3600:
        return f"{diff // 60}m {diff % 60}s"
    else:
        return f"{diff // 3600}h {(diff % 3600) // 60}m"


def format_elapsed(record: dict) -> str:
    """Format the duration column for a deploy record."""
    build_started = record.get("build_started", "")
    deploy_finished = record.get("deploy_finished", "")

    if not build_started:
        return ""

    try:
        start = int(build_started)
    except (ValueError, TypeError):
        return str(build_started)

    # Completed deploy: show actual duration
    if deploy_finished:
        try:
            end = int(deploy_finished)
            dur = max(0, end - start)
            if dur < 60:
                return f"{dur}s"
            elif dur < 3600:
                return f"{dur // 60}m {dur % 60}s"
            else:
                return f"{dur // 3600}h {(dur % 3600) // 60}m"
        except (ValueError, TypeError):
            pass

    # In-progress: show running elapsed time
    build_status = record.get("build_status", "")
    deploy_status = record.get("deploy_status", "")
    if build_status in ("building", "pending") or deploy_status in ("deploying", "pending"):
        return elapsed_since(build_started)

    # Terminal state without deploy_finished (cancelled, failed)
    return "\u2014"


def format_age(record: dict) -> str:
    """Format how long ago the deploy was started."""
    build_started = record.get("build_started", "")
    if not build_started:
        return ""
    return f"{elapsed_since(build_started)} ago"
