"""Deploys tab — DataTable showing deployment status from configured provider."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static
from textual import work
from rich.text import Text

from ..config import config_read, config_write, config_get_provider, config_remove, config_get_tab, config_set_tab
from ..providers import (
    list_providers,
    provider_display_name,
    fetch_deploys,
    FetchError,
    format_elapsed,
    default_providers_dir,
)

_log = logging.getLogger("overwatch")


# Textual DataTable hover events can render badly under some terminals/multiplexers
# (ghost header rows on mouse movement). Suppress hover rendering but allow scroll.
class _KeyboardOnlyDataTable(DataTable):
    def _on_mouse_move(self, event) -> None:  # type: ignore[override]
        event.stop()
        event.prevent_default()

    def watch_hover_coordinate(self, old, value) -> None:
        pass


# ---------------------------------------------------------------------------
# Status styling helpers
# ---------------------------------------------------------------------------

_ENV_STYLES: dict[str, str] = {
    "prod": "bold #f38ba8",
    "production": "bold #f38ba8",
    "staging": "#f9e2af",
    "stg": "#f9e2af",
    "dev": "#a6e3a1",
    "development": "#a6e3a1",
    "preview": "#a6e3a1",
}

_ENV_ABBREV: dict[str, str] = {
    "production": "prod",
    "development": "dev",
    "staging": "staging",
    "stg": "stg",
    "preview": "preview",
}


def _env_text(env: str) -> Text:
    key = env.lower().strip()
    style = _ENV_STYLES.get(key, "dim")
    label = _ENV_ABBREV.get(key, key) if key else ""
    return Text(label, style=style)


_STATUS_STYLES: dict[str, str] = {
    "live": "bold #a6e3a1",
    "success": "bold #a6e3a1",
    "building": "bold #f9e2af",
    "deploying": "bold #f9e2af",
    "pending": "bold #f9e2af",
    "failed": "bold #f38ba8",
    "cancelled": "dim",
}


def _status_text(status: str) -> Text:
    style = _STATUS_STYLES.get(status, "")
    return Text(status, style=style)


# ---------------------------------------------------------------------------
# DeploysTab widget
# ---------------------------------------------------------------------------


class DeploysTab(Vertical):
    """Content widget for the Deploys tab."""

    def __init__(
        self,
        project_dir: str,
        providers_dir: str | None = None,
        dash_id: str = "",
    ) -> None:
        super().__init__()
        self._project_dir = project_dir
        self._providers_dir = providers_dir or default_providers_dir()
        self._dash_id = dash_id
        self._config_file = os.path.join(project_dir, ".deploy-watch.json")
        self._cache_file = os.path.join(project_dir, ".deploy-watch.cache.json")
        self._cached_records: list[dict] = []
        self._urls: list[str] = []
        self._row_records: list[dict] = []
        self._fetch_error = ""
        self._last_fetch_time = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="deploy-service-url")
        yield Static("", id="deploy-env-summary")
        yield _KeyboardOnlyDataTable(
            id="deploy-table",
            cursor_type="row",
            zebra_stripes=True,
            show_row_labels=False,
            header_height=1,
        )
        yield Static("", id="deploy-status")

    def _load_cache(self) -> list[dict]:
        """Load cached records from disk."""
        try:
            with open(self._cache_file, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return []

    def _save_cache(self, records: list[dict]) -> None:
        """Persist records to disk for instant next startup."""
        try:
            with open(self._cache_file, "w") as f:
                json.dump(records, f)
        except OSError:
            pass

    def on_mount(self) -> None:
        table = self.query_one("#deploy-table", DataTable)
        table.mouse_hover = False
        # Show cached data immediately so the screen isn't empty
        cached = self._load_cache()
        if cached:
            self._cached_records = cached
            self._populate_table()
        # Then refresh from the provider in the background
        self._refresh_data()

    def get_selected_url(self) -> str:
        """Return the URL for the currently selected row, or empty string."""
        table = self.query_one("#deploy-table", DataTable)
        if table.row_count == 0:
            return ""
        try:
            row_idx = table.cursor_coordinate.row
            if 0 <= row_idx < len(self._urls):
                return self._urls[row_idx]
        except Exception:
            pass
        return ""

    def get_selected_record(self) -> dict | None:
        """Return the full record for the currently selected row."""
        table = self.query_one("#deploy-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_idx = table.cursor_coordinate.row
            if 0 <= row_idx < len(self._row_records):
                return self._row_records[row_idx]
        except Exception:
            pass
        return None

    def _refresh_data(self, notify: bool = False) -> None:
        """Start a background refresh. Set notify=True for user-initiated refreshes."""
        # Update status bar immediately on the main thread (before worker starts)
        try:
            self.query_one("#deploy-status", Static).update(
                "[bold #f9e2af]Refreshing…[/bold #f9e2af]"
            )
        except Exception:
            pass
        self._do_refresh(notify)

    @work(exclusive=True, thread=True)
    def _do_refresh(self, notify: bool = False) -> None:
        """Fetch deploy data from provider in a background thread."""
        provider = config_get_provider(self._config_file)
        if not provider:
            self.app.call_from_thread(self._show_unconfigured)
            return

        fetch_error = ""
        try:
            records = fetch_deploys(self._config_file, self._providers_dir)
        except FetchError as exc:
            _log.error("fetch_deploys: %s", exc)
            records = None
            fetch_error = str(exc)
        except Exception as exc:
            _log.error("fetch_deploys raised: %s", exc)
            records = None
            fetch_error = f"Unexpected error: {exc}"

        if not fetch_error:
            if records is None:
                fetch_error = "Provider error"
            elif not records:
                fetch_error = "No records"

        fetch_time = int(time.time())
        record_count = len(records) if records else 0
        should_notify = notify

        def _apply() -> None:
            self._fetch_error = fetch_error
            if records is not None:
                self._cached_records = records
                self._save_cache(records)
            self._last_fetch_time = fetch_time
            self._populate_table()
            if should_notify:
                if fetch_error:
                    self.app.notify(f"Refresh: {fetch_error}", timeout=3)
                else:
                    self.app.notify(f"Refreshed: {record_count} records", timeout=2)

        self.app.call_from_thread(_apply)

    def _show_unconfigured(self) -> None:
        """Show unconfigured state."""
        table = self.query_one("#deploy-table", DataTable)
        table.clear()
        self._urls = []
        self._row_records = []
        providers = list_providers(self._providers_dir)
        if providers:
            names = ", ".join(providers)
            msg = f"Not configured. Press [bold]p[/bold] to select a provider.\nAvailable: {names}"
        else:
            msg = f"Not configured. No providers found in {self._providers_dir}"

        self.query_one("#deploy-status", Static).update(msg)
        self.query_one("#deploy-service-url", Static).update("")
        self.query_one("#deploy-env-summary", Static).update("")

    @staticmethod
    def _normalize_env_name(name: str) -> str:
        env = name.strip().lower()
        if env in ("production",):
            return "prod"
        if env in ("stg",):
            return "staging"
        return env

    @staticmethod
    def _revision_for_record(record: dict) -> str:
        revision = str(record.get("commit", "") or "").strip()
        if not revision:
            revision = str(record.get("tag", "") or record.get("version", "") or "").strip()
        if (
            len(revision) > 7
            and all(ch in "0123456789abcdefABCDEF" for ch in revision)
        ):
            return revision[:7]
        return revision

    def _environment_summary(self, records: list[dict]) -> Text:
        latest_by_env: dict[str, str] = {}
        for rec in records:
            env = self._normalize_env_name(str(rec.get("environment", "") or ""))
            if not env:
                continue
            revision = self._revision_for_record(rec)
            if not revision:
                continue
            if env not in latest_by_env:
                latest_by_env[env] = revision

        if not latest_by_env:
            return Text("")

        ordered_envs = ["prod", "staging"]
        extras = sorted([e for e in latest_by_env if e not in ordered_envs])
        envs = [e for e in ordered_envs if e in latest_by_env] + extras

        parts: list[Text] = [Text("Env commits: ", style="dim")]
        for idx, env in enumerate(envs):
            style = _ENV_STYLES.get(env, "dim")
            parts.append(Text(f"{env}", style=style))
            parts.append(Text(f" {latest_by_env[env]}", style="bold #89b4fa"))
            if idx < len(envs) - 1:
                parts.append(Text(" | ", style="dim"))
        return Text.assemble(*parts)

    # Approximate column widths (header + padding) for non-message columns.
    _COL_WIDTH = {
        "Commit": 9,
        "Version": 12,
        "Service": 16,
        "Env": 9,
        "Build": 11,
        "Deploy": 11,
        "Elapsed": 12,
    }

    # Default columns (Service and Version hidden by default).
    _DEFAULT_COLUMNS = ["Commit", "Env", "Build", "Deploy", "Elapsed", "Message"]

    def _get_columns(self) -> list[str]:
        """Return the configured column list, or defaults."""
        tab_cfg = config_get_tab(self._config_file, "deploys")
        cols = tab_cfg.get("columns")
        if isinstance(cols, list) and cols:
            return cols
        return list(self._DEFAULT_COLUMNS)

    def _message_budget(self, columns: list[str]) -> int:
        """Return max chars for the Message column, or 0 to hide it."""
        if "Message" not in columns:
            return 0
        try:
            width = self.app.size.width
        except Exception:
            width = 120
        fixed_total = sum(self._COL_WIDTH.get(c, 10) for c in columns if c != "Message")
        remaining = width - fixed_total
        if remaining < 8:
            return 0
        return min(remaining, 60)

    def _cell_for_column(self, col: str, rec: dict, msg_budget: int) -> Text:
        """Build the Rich Text cell for a given column name."""
        if col == "Commit":
            return Text(rec.get("commit", "")[:7], style="bold")
        elif col == "Version":
            v = rec.get("tag", "") or rec.get("version", "")
            return Text(v, style="#cba6f7") if v else Text("")
        elif col == "Service":
            s = rec.get("service_name", "")
            return Text(s, style="bold") if s else Text("")
        elif col == "Env":
            return _env_text(rec.get("environment", ""))
        elif col == "Build":
            return _status_text(rec.get("build_status", ""))
        elif col == "Deploy":
            return _status_text(rec.get("deploy_status", ""))
        elif col == "Elapsed":
            return Text(format_elapsed(rec), style="dim")
        elif col == "Message":
            msg = rec.get("message", "")
            if msg_budget > 0 and len(msg) > msg_budget:
                msg = msg[: msg_budget - 2] + ".."
            return Text(msg)
        return Text("")

    def _populate_table(self) -> None:
        """Populate the DataTable with cached records."""
        table = self.query_one("#deploy-table", DataTable)
        table.clear(columns=True)

        columns = self._get_columns()
        msg_budget = self._message_budget(columns)

        # If message doesn't fit, drop it
        if "Message" in columns and msg_budget <= 0:
            columns = [c for c in columns if c != "Message"]

        table.add_columns(*columns)
        self._urls = []
        self._row_records = []

        records = self._cached_records
        if not records and not self._fetch_error:
            provider = config_get_provider(self._config_file)
            if not provider:
                self._show_unconfigured()
                return

        self.query_one("#deploy-service-url", Static).update("")
        self.query_one("#deploy-env-summary", Static).update(self._environment_summary(records))

        for rec in records:
            row = [self._cell_for_column(c, rec, msg_budget) for c in columns]
            table.add_row(*row)
            self._urls.append(rec.get("deploy_url", "") or rec.get("service_url", ""))
            self._row_records.append(rec)

        # Status line
        ts = time.strftime("%H:%M:%S")
        provider = config_get_provider(self._config_file)
        name = provider_display_name(provider, self._providers_dir) if provider else "—"
        count = len(records) if records else 0
        if self._fetch_error:
            status = f"[bold red]{self._fetch_error}[/bold red] | {name} | {ts}"
        else:
            status = f"{name} | {count} deploys | Updated {ts}"
        self.query_one("#deploy-status", Static).update(status)

    def on_resize(self) -> None:
        """Re-populate table when terminal width changes to adjust message column."""
        if self._cached_records:
            self._populate_table()

    def refresh_data(self, notify: bool = False) -> None:
        """Public trigger for refresh."""
        self._refresh_data(notify=notify)

    def configure_columns(self) -> None:
        """Open the column configuration modal."""
        from ..modals.column_config import ColumnConfigModal

        current = self._get_columns()

        def _on_config(result: list[str] | None) -> None:
            if result is None:
                return
            config_set_tab(self._config_file, "deploys", {"columns": result})
            self._populate_table()
            self.app.notify("Column config saved.")

        self.app.push_screen(ColumnConfigModal(current), callback=_on_config)

    def configure_provider(self) -> None:
        """Launch provider picker → config flow."""
        from ..modals.provider_picker import ProviderPicker

        def _on_pick(provider: str | None) -> None:
            if provider is None:
                return
            cfg = config_read(self._config_file)
            existing = cfg.get(provider, {})
            self._configure_provider_fields(
                provider,
                initial_values=existing if isinstance(existing, dict) else {},
            )

        self.app.push_screen(ProviderPicker(self._providers_dir), callback=_on_pick)

    def _configure_provider_fields(
        self,
        provider: str,
        initial_values: dict[str, str] | None = None,
    ) -> None:
        """Show config fields modal for the selected provider."""
        from ..modals.provider_config import ProviderConfigModal

        def _on_config(values: dict | None) -> None:
            if values is None:
                return
            cfg = {
                "provider": provider,
                provider: values,
            }
            config_write(self._config_file, cfg)
            self.app.notify(f"Provider configured: {provider}")
            self._refresh_data()

        self.app.push_screen(
            ProviderConfigModal(
                provider,
                self._providers_dir,
                initial_values=initial_values,
            ),
            callback=_on_config,
        )

    def manage_provider(self) -> None:
        """Show provider management options (change/remove)."""
        provider = config_get_provider(self._config_file)
        if not provider:
            self.configure_provider()
            return
        # Provider is configured — offer change or remove
        from ..modals.provider_manage import ProviderManageModal

        name = provider_display_name(provider, self._providers_dir)
        current_cfg = config_read(self._config_file)
        existing_vals = current_cfg.get(provider, {})

        def _on_manage(action: str | None) -> None:
            if action == "configure":
                self._configure_provider_fields(
                    provider,
                    initial_values=existing_vals if isinstance(existing_vals, dict) else {},
                )
            elif action == "change":
                self.configure_provider()
            elif action == "remove":
                config_remove(self._config_file)
                self._cached_records = []
                self._urls = []
                self.app.notify("Provider removed.")
                self._show_unconfigured()

        self.app.push_screen(ProviderManageModal(name), callback=_on_manage)

    def disable_dashboard_pane(self) -> None:
        """Disable the dashboard pane for this project via settings.local.json."""
        import json

        settings_path = os.path.join(
            self._project_dir, ".claude", "settings.local.json"
        )
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)

        settings: dict = {}
        try:
            with open(settings_path, "r") as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        panes = settings.setdefault("panes", {})
        panes["dashboard"] = False
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

        self.app.notify(
            'Dashboard pane disabled. Set "panes": {"dashboard": true} in '
            ".claude/settings.local.json to re-enable."
        )
        self.app.set_timer(2.0, self.app.exit)
