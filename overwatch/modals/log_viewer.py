"""Log viewer screen — tabbed view for build, deploy, and service logs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog, Static, TabbedContent, TabPane
from textual import on, work


class LogViewerModal(ModalScreen[None]):
    """Modal with tabs for Build, Deploy, and Service logs."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", priority=True),
        Binding("q", "dismiss_modal", "Close", priority=True),
    ]

    DEFAULT_CSS = """
    LogViewerModal {
        align: center middle;
    }

    LogViewerModal #log-dialog {
        width: 90%;
        height: 85%;
        background: $surface;
        border: tall $primary;
        padding: 0;
    }

    LogViewerModal #log-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }

    LogViewerModal #log-status {
        width: 100%;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        dock: bottom;
    }

    LogViewerModal TabbedContent {
        height: 1fr;
    }

    LogViewerModal TabPane {
        padding: 0;
    }

    LogViewerModal RichLog {
        height: 1fr;
        background: #1e1e2e;
        padding: 0 1;
    }
    """

    # Track which tabs have been loaded
    _loaded: set[str]

    def __init__(
        self,
        config_file: str,
        providers_dir: str,
        record: dict,
    ) -> None:
        super().__init__()
        self._config_file = config_file
        self._providers_dir = providers_dir
        self._record = record
        self._loaded = set()

    def compose(self) -> ComposeResult:
        rec = self._record
        svc = rec.get("service_name", "")
        env = rec.get("environment", "")
        commit = rec.get("commit", "")
        title_parts = [svc]
        if env:
            title_parts.append(f"({env})")
        if commit:
            title_parts.append(f"@ {commit}")
        title = " ".join(title_parts)

        with Vertical(id="log-dialog"):
            yield Label(title, id="log-title")
            with TabbedContent(id="log-tabs"):
                with TabPane("Build", id="build-pane"):
                    yield RichLog(id="build-log", highlight=True, markup=False, wrap=True)
                with TabPane("Deploy", id="deploy-pane"):
                    yield RichLog(id="deploy-log", highlight=True, markup=False, wrap=True)
                with TabPane("Service", id="app-pane"):
                    yield RichLog(id="app-log", highlight=True, markup=False, wrap=True)
            yield Static("[dim]Loading…[/dim]", id="log-status")

    def on_mount(self) -> None:
        # Load the initially active tab
        self._load_tab("build-pane")

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = str(event.pane.id) if event.pane.id else ""
        self._load_tab(pane_id)

    def _load_tab(self, pane_id: str) -> None:
        if pane_id in self._loaded:
            return
        self._loaded.add(pane_id)

        type_map = {
            "build-pane": ("build", "build-log"),
            "deploy-pane": ("deploy", "deploy-log"),
            "app-pane": ("app", "app-log"),
        }
        if pane_id not in type_map:
            return

        log_type, log_widget_id = type_map[pane_id]
        self._fetch_logs(log_type, log_widget_id)

    @work(thread=True)
    def _fetch_logs(self, log_type: str, log_widget_id: str) -> None:
        from ..providers import fetch_logs, FetchError

        rec = self._record
        service_id = rec.get("service_id", "")
        deploy_id = rec.get("deploy_id", "")

        if not service_id or not deploy_id:
            def _no_ids():
                self.query_one(f"#{log_widget_id}", RichLog).write(
                    "No service/deploy ID available."
                )
                self.query_one("#log-status", Static).update("[dim]Esc to close[/dim]")
            self.app.call_from_thread(_no_ids)
            return

        def _show_loading():
            self.query_one("#log-status", Static).update(
                f"[dim]Loading {log_type} logs…[/dim]"
            )
        self.app.call_from_thread(_show_loading)

        try:
            log_text = fetch_logs(
                self._config_file,
                self._providers_dir,
                service_id,
                deploy_id,
                log_type=log_type,
            )
        except FetchError as exc:
            def _show_error(msg=str(exc)):
                self.query_one(f"#{log_widget_id}", RichLog).write(f"Error: {msg}")
                self.query_one("#log-status", Static).update("[dim]Esc to close[/dim]")
            self.app.call_from_thread(_show_error)
            return
        except Exception as exc:
            def _show_unexpected(msg=str(exc)):
                self.query_one(f"#{log_widget_id}", RichLog).write(
                    f"Unexpected error: {msg}"
                )
                self.query_one("#log-status", Static).update("[dim]Esc to close[/dim]")
            self.app.call_from_thread(_show_unexpected)
            return

        wid = log_widget_id
        lt = log_type

        def _show_logs():
            rich_log = self.query_one(f"#{wid}", RichLog)
            lines = log_text.splitlines()
            for line in lines:
                rich_log.write(line)
            self.query_one("#log-status", Static).update(
                f"[dim]{len(lines)} lines ({lt}) | Esc to close[/dim]"
            )

        self.app.call_from_thread(_show_logs)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        if self is event.widget:
            self.dismiss(None)
