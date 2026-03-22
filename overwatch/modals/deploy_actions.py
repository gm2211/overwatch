"""Deploy row action modal — choose what to open for the selected deploy."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option


class DeployActionResult:
    """Result from the deploy actions modal."""
    def __init__(self, action: str, url: str = ""):
        self.action = action  # "open_url" or "view_logs"
        self.url = url


class DeployActionsModal(ModalScreen[DeployActionResult | None]):
    """Modal offering URL choices for a deploy row."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DeployActionsModal {
        align: center middle;
    }

    DeployActionsModal #actions-dialog {
        width: 55;
        max-width: 80%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }

    DeployActionsModal #actions-title {
        text-align: center;
        text-style: bold;
        width: 100%;
    }

    DeployActionsModal #actions-info {
        width: 100%;
        height: auto;
        max-height: 4;
        color: $text-muted;
        margin-bottom: 1;
    }

    DeployActionsModal OptionList {
        height: auto;
        max-height: 6;
    }
    """

    def __init__(self, record: dict) -> None:
        super().__init__()
        self._record = record
        # Each option: (action, url)
        self._options: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        rec = self._record
        svc = rec.get("service_name", "")
        env = rec.get("environment", "")
        title = f"{svc} ({env})" if env else svc or "Deploy"

        info_parts = []
        branch = rec.get("branch", "")
        commit = rec.get("commit", "")
        message = rec.get("message", "")
        ref_parts = []
        if branch:
            ref_parts.append(branch)
        if commit:
            ref_parts.append(commit)
        if ref_parts:
            info_parts.append("[dim]Ref:[/dim] " + " @ ".join(ref_parts))
        if message:
            msg = message if len(message) <= 60 else message[:58] + ".."
            info_parts.append(f"[dim]Msg:[/dim] {msg}")
        info_lines = info_parts

        with Vertical(id="actions-dialog"):
            yield Label(title, id="actions-title")
            if info_lines:
                yield Static("\n".join(info_lines), id="actions-info")
            yield OptionList()

    def on_mount(self) -> None:
        option_list = self.query_one(OptionList)
        rec = self._record

        service_url = rec.get("service_url", "")
        deploy_url = rec.get("deploy_url", "")
        has_ids = rec.get("service_id") and rec.get("deploy_id")

        if service_url:
            self._options.append(("open_url", service_url))
            option_list.add_option(Option("Open website", id="website"))

        if deploy_url:
            self._options.append(("open_url", deploy_url))
            option_list.add_option(Option("Open deploy page", id="deploy"))

        if has_ids:
            self._options.append(("view_logs", ""))
            option_list.add_option(Option("View logs", id="logs"))

        if not self._options:
            self.dismiss(None)
            return

        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._options):
            action, url = self._options[idx]
            self.dismiss(DeployActionResult(action, url))

    def on_click(self, event) -> None:
        if self is event.widget:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
