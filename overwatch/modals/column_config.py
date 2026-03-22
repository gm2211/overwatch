"""Column configuration modal — pick which columns to show and their order."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option


ALL_COLUMNS = ["Commit", "Version", "Service", "Env", "Build", "Deploy", "Elapsed", "Message"]


class ColumnConfigModal(ModalScreen[list[str] | None]):
    """Modal for selecting and reordering deploy table columns."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", priority=True),
        Binding("space", "toggle", "Toggle", show=False, priority=True),
        Binding("J", "move_down", "Move Down", key_display="Shift+J", priority=True),
        Binding("K", "move_up", "Move Up", key_display="Shift+K", priority=True),
    ]

    DEFAULT_CSS = """
    ColumnConfigModal {
        align: center middle;
    }

    ColumnConfigModal #colcfg-dialog {
        width: 45;
        max-width: 80%;
        height: auto;
        max-height: 70%;
        background: $surface;
        border: tall $primary;
        padding: 1 2;
    }

    ColumnConfigModal #colcfg-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }

    ColumnConfigModal #colcfg-help {
        width: 100%;
        color: $text-muted;
        margin-top: 1;
    }

    ColumnConfigModal OptionList {
        height: auto;
        max-height: 12;
    }
    """

    def __init__(self, current_columns: list[str]) -> None:
        super().__init__()
        self._items: list[tuple[str, bool]] = []
        seen = set()
        for col in current_columns:
            if col in ALL_COLUMNS:
                self._items.append((col, True))
                seen.add(col)
        for col in ALL_COLUMNS:
            if col not in seen:
                self._items.append((col, False))

    def compose(self) -> ComposeResult:
        with Vertical(id="colcfg-dialog"):
            yield Label("Configure Columns", id="colcfg-title")
            yield OptionList()
            yield Static(
                "[dim]Space[/dim] toggle  [dim]Shift+J/K[/dim] reorder  [dim]Enter[/dim] save  [dim]Esc[/dim] cancel",
                id="colcfg-help",
            )

    def on_mount(self) -> None:
        self._rebuild_options()
        self.query_one(OptionList).focus()

    def _rebuild_options(self) -> None:
        ol = self.query_one(OptionList)
        try:
            highlighted = ol.highlighted
        except Exception:
            highlighted = 0
        ol.clear_options()
        for col, enabled in self._items:
            icon = "[bold green]✓[/bold green]" if enabled else "[dim]✗[/dim]"
            label = f"  {icon}  {col}"
            ol.add_option(Option(label))
        if highlighted is not None and 0 <= highlighted < len(self._items):
            ol.highlighted = highlighted

    def action_toggle(self) -> None:
        ol = self.query_one(OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self._items):
            col, enabled = self._items[idx]
            self._items[idx] = (col, not enabled)
            self._rebuild_options()

    def action_move_up(self) -> None:
        ol = self.query_one(OptionList)
        idx = ol.highlighted
        if idx is not None and idx > 0:
            self._items[idx - 1], self._items[idx] = self._items[idx], self._items[idx - 1]
            self._rebuild_options()
            ol.highlighted = idx - 1

    def action_move_down(self) -> None:
        ol = self.query_one(OptionList)
        idx = ol.highlighted
        if idx is not None and idx < len(self._items) - 1:
            self._items[idx], self._items[idx + 1] = self._items[idx + 1], self._items[idx]
            self._rebuild_options()
            ol.highlighted = idx + 1

    def action_save(self) -> None:
        result = [col for col, enabled in self._items if enabled]
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        if self is event.widget:
            self.dismiss(None)
