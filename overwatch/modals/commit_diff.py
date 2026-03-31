"""Commit diff modal — shows git log between two environment commits."""

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog, Static


class CommitDiffModal(ModalScreen[None]):
    """Modal showing commits between two environment SHAs."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", priority=True),
        Binding("q", "dismiss_modal", "Close", priority=True),
        Binding("y", "copy_diff", "Copy", priority=True),
    ]

    DEFAULT_CSS = """
    CommitDiffModal {
        align: center middle;
    }

    CommitDiffModal #diff-dialog {
        width: 90%;
        height: 75%;
        background: $surface;
        border: tall $primary;
        padding: 0;
    }

    CommitDiffModal #diff-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }

    CommitDiffModal #diff-status {
        width: 100%;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        dock: bottom;
    }

    CommitDiffModal RichLog {
        height: 1fr;
        background: #1e1e2e;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        project_dir: str,
        from_env: str,
        to_env: str,
        from_sha: str,
        to_sha: str,
    ) -> None:
        super().__init__()
        self._project_dir = project_dir
        self._from_env = from_env
        self._to_env = to_env
        self._from_sha = from_sha
        self._to_sha = to_sha
        self._log_text = ""

    def compose(self) -> ComposeResult:
        title = f"Commit diff: {self._from_env} vs {self._to_env}  ({self._from_sha[:7]}..{self._to_sha[:7]})"
        with Vertical(id="diff-dialog"):
            yield Label(title, id="diff-title")
            yield RichLog(id="diff-log", highlight=True, markup=False, wrap=True)
            yield Static("[dim]Loading…[/dim]", id="diff-status")

    def on_mount(self) -> None:
        self._run_git_log()

    def _git_log_range(self, from_sha: str, to_sha: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "log", "--oneline", "--no-decorate", f"{from_sha}..{to_sha}"],
            capture_output=True, text=True, timeout=10,
            cwd=self._project_dir,
        )

    def _run_git_log(self) -> None:
        rich_log = self.query_one("#diff-log", RichLog)
        status = self.query_one("#diff-status", Static)

        try:
            result = self._git_log_range(self._from_sha, self._to_sha)
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "unknown revision" in stderr or "bad revision" in stderr:
                    subprocess.run(
                        ["git", "fetch", "--quiet"],
                        capture_output=True, timeout=30,
                        cwd=self._project_dir,
                    )
                    result = self._git_log_range(self._from_sha, self._to_sha)
                    if result.returncode != 0:
                        rich_log.write(f"git error: {result.stderr.strip()}")
                        status.update("[dim]Esc to close[/dim]")
                        return
                else:
                    rich_log.write(f"git error: {stderr}")
                    status.update("[dim]Esc to close[/dim]")
                    return

            total_lines = 0
            sections = []

            # Commits on to_env not on from_env
            fwd_output = result.stdout.strip()
            if fwd_output:
                sections.append((f"Commits on {self._to_env} not on {self._from_env}:", fwd_output))

            # Commits on from_env not on to_env (reverse direction)
            rev_result = self._git_log_range(self._to_sha, self._from_sha)
            rev_output = rev_result.stdout.strip() if rev_result.returncode == 0 else ""
            if rev_output:
                sections.append((f"Commits on {self._from_env} not on {self._to_env}:", rev_output))

            if not sections:
                rich_log.write(f"{self._from_env} and {self._to_env} are at the same point.")
                status.update("[dim]Esc to close[/dim]")
                return

            all_text = []
            for header, output in sections:
                rich_log.write(f"── {header}")
                rich_log.write("")
                all_text.append(header)
                for line in output.splitlines():
                    rich_log.write(line)
                    total_lines += 1
                all_text.append(output)
                rich_log.write("")

            self._log_text = "\n".join(all_text)
            status.update(
                f"[dim]{total_lines} commit{'s' if total_lines != 1 else ''} | y copy | Esc close[/dim]"
            )

        except subprocess.TimeoutExpired:
            rich_log.write("git log timed out")
            status.update("[dim]Esc to close[/dim]")
        except FileNotFoundError:
            rich_log.write("git not found — is it installed?")
            status.update("[dim]Esc to close[/dim]")

    def action_copy_diff(self) -> None:
        import platform
        import shutil

        if not self._log_text:
            self.app.notify("Nothing to copy", timeout=2)
            return

        if platform.system() == "Darwin" and shutil.which("pbcopy"):
            cmd = ["pbcopy"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            self.app.notify("No clipboard tool found", timeout=2)
            return

        try:
            subprocess.run(cmd, input=self._log_text.encode(), timeout=5)
            lines = len(self._log_text.splitlines())
            self.app.notify(f"Copied {lines} lines", timeout=2)
        except Exception:
            self.app.notify("Failed to copy", timeout=2)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        if self is event.widget:
            self.dismiss(None)
