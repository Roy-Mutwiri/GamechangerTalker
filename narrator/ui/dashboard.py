"""The live terminal UI.

    +- trade fix narrator ---- XAUUSD ---- 02:14:31 live -------------+
    | facts                     | transcript                          |
    |   price      3341.20      | 14:32:07 [price.drift]  Gold's at   |
    |   change_day  -11.40      | 14:32:41 [levels.approach_pdl] ...  |
    |   ...                     |                                     |
    +----------------------------------------------------------------+
    | mt5 ok . warudo ok . kokoro cuda . cache 82% . density 11%      |
    | > operator types here                                           |
    +----------------------------------------------------------------+

Keystrokes are read directly rather than through input(), because input()
echoes into the same terminal rich is repainting and the two fight. Reading
raw keys means the buffer is ours to draw, so the input line sits inside the
layout and behaves.

Anything typed is spoken next at priority 5. Lines starting with / are
commands.
"""

from __future__ import annotations

import queue
import sys
import threading
from collections import deque
from datetime import datetime
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from narrator.config import Config

TRANSCRIPT_LINES = 200

SOURCE_STYLE = {
    "template": "cyan",
    "bridge": "bright_black",
    "override": "bold magenta",
}

HELP = "type to speak  ·  /mute  /unmute  /skip  /reload  /quiet 300  /help  /quit"


class KeyboardReader:
    """Raw keystrokes off the console, on a background thread.

    Windows uses msvcrt; anywhere else falls back to line-buffered stdin,
    which is good enough for development on another machine.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.lines: queue.Queue[str] = queue.Queue()
        self.interrupted = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = False

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        try:
            import msvcrt  # noqa: F401

            target = self._windows_loop
        except ImportError:
            target = self._posix_loop
        self.available = True
        self._thread = threading.Thread(target=target, daemon=True, name="keys")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- loops --------------------------------------------------------------

    def _windows_loop(self) -> None:
        import msvcrt

        while not self._stop.is_set():
            if not msvcrt.kbhit():
                self._stop.wait(0.03)
                continue
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):  # function / arrow key: swallow both halves
                msvcrt.getwch()
                continue
            if char == "\x03":  # ctrl-c
                self.interrupted.set()
                return
            if char in ("\r", "\n"):
                if self.buffer.strip():
                    self.lines.put(self.buffer.strip())
                self.buffer = ""
            elif char == "\x08":  # backspace
                self.buffer = self.buffer[:-1]
            elif char == "\x1b":  # escape clears the line
                self.buffer = ""
            elif char.isprintable():
                self.buffer += char

    def _posix_loop(self) -> None:  # pragma: no cover - not the target platform
        for line in sys.stdin:
            if self._stop.is_set():
                return
            if line.strip():
                self.lines.put(line.strip())

    def pop(self) -> str | None:
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None


class Dashboard:
    def __init__(self, cfg: Config, *, run_id: str, symbol: str, mode: str) -> None:
        self.cfg = cfg
        self.run_id = run_id
        self.symbol = symbol
        self.mode = mode
        self.facts: dict[str, Any] = {}
        self.transcript: deque[tuple[datetime, str, str, str, str | None]] = deque(
            maxlen=TRANSCRIPT_LINES
        )
        self.status: dict[str, str] = {}
        self.notes: deque[str] = deque(maxlen=3)
        self.keys = KeyboardReader()
        self.speaking: str = ""
        self.clock: datetime | None = None
        self._live: Live | None = None
        self.console = Console(highlight=False)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.keys.start()
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=8,
            screen=True,
            transient=False,
        )
        self._live.start()

    def refresh(self) -> None:
        if self._live is not None:
            self._live.update(self.render())

    def stop(self) -> None:
        self.keys.stop()
        if self._live is not None:
            self._live.stop()
            self._live = None

    # -- data in ------------------------------------------------------------

    def update_facts(self, facts: dict[str, Any], clock: datetime) -> None:
        self.facts = facts
        self.clock = clock

    def add_line(
        self,
        when: datetime,
        template_id: str,
        text: str,
        *,
        source: str = "template",
        emote: str | None = None,
    ) -> None:
        self.transcript.append((when, template_id, text, source, emote))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def set_status(self, **values: str) -> None:
        self.status.update(values)

    # -- rendering ----------------------------------------------------------

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header(), name="header", size=3),
            Layout(name="body"),
            Layout(self._footer(), name="footer", size=7),
        )
        layout["body"].split_row(
            Layout(self._facts_panel(), name="facts", size=37),
            Layout(self._transcript_panel(), name="transcript"),
        )
        return layout

    def _header(self) -> Panel:
        clock = self.clock.strftime("%Y-%m-%d %H:%M:%S") if self.clock else "--"
        session = str(self.facts.get("session") or "--")
        price = self.facts.get("price")
        price_text = f"{price:,.2f}" if isinstance(price, (int, float)) else "--"
        change = self.facts.get("change_day")
        if isinstance(change, (int, float)):
            colour = "green" if change > 0 else "red" if change < 0 else "white"
            change_text = Text(f"{change:+.2f}", style=colour)
        else:
            change_text = Text("--")

        line = Text()
        line.append(" trade fix narrator ", style="bold white on blue")
        line.append(f"  {self.symbol}  ", style="bold")
        line.append(price_text, style="bold yellow")
        line.append("  ")
        line.append_text(change_text)
        line.append(f"   {session}", style="magenta")
        line.append(f"   {clock} UTC", style="dim")
        line.append(f"   {self.mode}", style="dim")
        return Panel(Align.left(line), border_style="blue", padding=(0, 1))

    def _facts_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=21, no_wrap=True)
        table.add_column(justify="right", width=11, no_wrap=True)
        for key in self.cfg.ui.fact_panel_keys:
            table.add_row(key, _format_value(self.facts.get(key)))

        table.add_row("", "")
        for key in ("pdh", "pdl", "asian_high", "asian_low", "day_open"):
            value = self.facts.get(key)
            if value is not None:
                table.add_row(key, _format_value(value))

        table.add_row("", "")
        for key in ("bars_in_range", "consecutive_bars", "range_state", "spread"):
            table.add_row(key, _format_value(self.facts.get(key)))
        return Panel(table, title="facts", border_style="grey37", padding=(0, 1))

    def _transcript_panel(self) -> Panel:
        height = max(6, (self.console.size.height or 30) - 12)
        rows = list(self.transcript)[-height:]
        body = Text()
        for when, template_id, text, source, emote in rows:
            style = SOURCE_STYLE.get(source, "cyan")
            body.append(when.strftime("%H:%M:%S "), style="dim")
            body.append(f"[{template_id}] ", style=style)
            body.append(text)
            if emote:
                body.append(f"  ({emote})", style="dim yellow")
            body.append("\n")
        if self.speaking:
            body.append("\n> ", style="bold green")
            body.append(self.speaking, style="green")
        return Panel(body, title="transcript", border_style="grey37", padding=(0, 1))

    def _footer(self) -> Panel:
        status = Text()
        for key in (
            "feed",
            "engine",
            "audio",
            "warudo",
            "cache",
            "density",
            "lines",
            "state",
        ):
            if key not in self.status:
                continue
            value = self.status[key]
            style = "green"
            lowered = value.lower()
            if any(word in lowered for word in ("down", "fail", "off", "muted", "stale")):
                style = "red"
            elif any(word in lowered for word in ("silent", "quiet", "none")):
                style = "yellow"
            status.append(f"{key} ", style="dim")
            status.append(value, style=style)
            status.append("   ")

        notes = Text("\n".join(self.notes), style="yellow") if self.notes else Text("")
        prompt = Text("> ", style="bold green")
        prompt.append(self.keys.buffer, style="white")
        prompt.append("_", style="blink white" if self.keys.available else "dim")
        if not self.keys.available:
            prompt = Text("> (no tty: override input unavailable)", style="dim")

        return Panel(
            Group(status, notes, prompt, Text(HELP, style="dim")),
            border_style="grey37",
            padding=(0, 1),
        )


def _format_value(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value)
    return text if len(text) <= 11 else text[:11]
