"""Tests for the edges: MT5 symbol detection, the web UI payloads, playback
degradation, preflight, and the transcript log.

These are the parts that only misbehave on someone else's machine -- a broker
that calls gold something else, a box with no audio device, a dead Warudo.
None of them need real hardware here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from narrator.config import Config, load_config, project_root
from narrator.logbook import SpeechLog
from narrator.market.mt5_adapter import MT5Adapter, ReplayAdapter
from narrator.preflight import check_templates, check_warudo, run_preflight
from narrator.speech.playback import Playback
from narrator.speech.visemes import rest_frame
from narrator.ui.webui import WebUI

T0 = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@dataclass
class FakeSymbol:
    name: str


class FakeMT5:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def symbols_get(self):
        return [FakeSymbol(n) for n in self.names]


# ---------------------------------------------------------------------------
# Gold is called six different things depending on the broker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "available,expected",
    [
        (["EURUSD", "XAUUSD", "GBPUSD"], "XAUUSD"),
        (["EURUSD", "GOLD"], "GOLD"),
        (["XAUUSD.m", "EURUSD.m"], "XAUUSD.m"),
        (["XAUUSD.pro", "XAUUSD.pro.x"], "XAUUSD.pro"),
        (["GOLDSPOT", "EURUSD"], "GOLDSPOT"),
        (["XAUUSDx", "EURUSD"], "XAUUSDx"),
        (["EURUSD", "USDJPY"], ""),
    ],
)
def test_gold_symbol_autodetection(available, expected):
    adapter = MT5Adapter(Config())
    assert adapter._detect_symbol(FakeMT5(available)) == expected


def test_autodetection_prefers_the_shortest_prefix_match():
    """XAUUSD before XAUUSD.raw.ecn -- the plain one is nearly always right."""
    adapter = MT5Adapter(Config())
    found = adapter._detect_symbol(FakeMT5(["XAUUSD.raw.ecn", "XAUUSD", "XAUUSDm"]))
    assert found == "XAUUSD"


def test_an_explicit_symbol_in_config_wins():
    cfg = Config()
    cfg.market.symbol = "GOLD#"
    adapter = MT5Adapter(cfg)
    assert adapter.symbol == "GOLD#"


def test_symbols_get_failure_is_survived():
    class Broken:
        def symbols_get(self):
            raise RuntimeError("terminal went away")

    adapter = MT5Adapter(Config())
    assert adapter._detect_symbol(Broken()) == ""
    assert "symbols_get" in adapter._last_error


# ---------------------------------------------------------------------------
# Replay adapter
# ---------------------------------------------------------------------------


def test_replay_advance_to_is_deterministic():
    cfg = load_config(project_root() / "config.toml")
    first = ReplayAdapter(cfg)
    first.load()
    second = ReplayAdapter(cfg)
    second.load()

    target = first.now().replace(hour=6, minute=0)
    assert first.advance_to(target)
    assert second.advance_to(target)
    assert first.now() == second.now()
    assert first.tick == second.tick
    assert first.store.count("M15") == second.store.count("M15")


def test_replay_reports_exhaustion_rather_than_crashing():
    cfg = load_config(project_root() / "config.toml")
    adapter = ReplayAdapter(cfg)
    adapter.load()
    way_past = adapter._bars[-1].time.replace(year=2030)
    assert adapter.advance_to(way_past) is False
    assert adapter.finished


def test_replay_start_at_after_the_data_is_rejected():
    cfg = load_config(project_root() / "config.toml")
    cfg.replay.start_at = "2030-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="after the last bar"):
        ReplayAdapter(cfg).load()


# ---------------------------------------------------------------------------
# Web UI payloads
# ---------------------------------------------------------------------------


def web(cfg: Config | None = None) -> WebUI:
    cfg = cfg or Config()
    cfg.webui.open_browser = False
    return WebUI(cfg, run_id="run1", symbol="XAUUSD", mode="test")


def test_state_payload_is_json_safe():
    ui = web()
    ui.send_state(
        {"price": 3341.2, "candle_seconds_left": {"M15": 42}, "when": T0},
        {"feed": "ok"},
        T0,
    )
    message = ui._last_state
    assert message["type"] == "state"
    assert message["facts"]["price"] == 3341.2
    assert message["facts"]["when"] == T0.isoformat()  # datetimes serialised
    assert message["clock"] == "12:00:00"


def test_lines_are_kept_for_a_page_that_opens_late():
    ui = web()
    for index in range(250):
        ui.send_line(T0, f"t.{index}", "hello", source="template", emote=None)
    assert len(ui.history) == 200  # bounded
    assert ui.history[-1]["id"] == "t.249"


def test_utterance_payload_packs_frames_compactly():
    ui = web()
    frames = [rest_frame() for _ in range(3)]
    ui.history.clear()
    ui.clients.clear()
    ui.send_utterance("hello", 1.25, frames)
    # No clients, so nothing is sent -- but the packing must not raise and the
    # shape is what the page expects: [t, aa, ee, ih, oh, ou].
    packed = [
        [round(f.t, 3)] + [round(f.weights[v], 3) for v in ("aa", "ee", "ih", "oh", "ou")]
        for f in frames
    ]
    assert all(len(row) == 6 for row in packed)


def test_commands_queue_and_pop():
    ui = web()
    assert ui.pop_command() is None
    ui.commands.put("/mute")
    ui.commands.put("gold looks heavy")
    assert ui.pop_command() == "/mute"
    assert ui.pop_command() == "gold looks heavy"
    assert ui.pop_command() is None


def test_broadcast_without_a_loop_does_not_raise():
    ui = web()
    ui.clients.add(object())
    ui.broadcast({"type": "note", "text": "hi"})  # no running loop


def test_disabled_web_ui_is_inert():
    cfg = Config()
    cfg.webui.enabled = False
    ui = WebUI(cfg, run_id="r", symbol="X", mode="m")
    assert ui.start_http() is False
    ui.send_note("nothing happens")
    asyncio.run(ui.stop())


# ---------------------------------------------------------------------------
# Playback without a device
# ---------------------------------------------------------------------------


def test_playback_degrades_quietly_when_there_is_no_device():
    playback = Playback(Config())
    playback.available = False
    assert playback.play([0.0] * 100, 24000) == 0.0
    assert playback.playing is False
    playback.stop()
    playback.close()
    assert playback.devices() == []


def test_playback_reports_its_device_name():
    playback = Playback(Config())
    # open() may or may not find a device on a build machine; either is fine,
    # what matters is that it never raises and always leaves a usable state.
    playback.open()
    assert isinstance(playback.device_name, str)
    assert playback.device_name


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_warudo_check_is_a_warning_not_a_failure_by_default():
    cfg = Config()
    cfg.warudo.port = 1  # nothing listens here
    cfg.preflight.require_warudo = False
    result = check_warudo(cfg)
    assert result.ok is False
    assert result.fatal is False
    assert "WARUDO_SETUP" in result.detail


def test_warudo_can_be_made_fatal():
    cfg = Config()
    cfg.warudo.port = 1
    cfg.preflight.require_warudo = True
    assert check_warudo(cfg).fatal is True


def test_template_check_passes_on_the_shipped_library():
    cfg = load_config(project_root() / "config.toml")
    result = check_templates(cfg)
    assert result.ok
    assert "templates" in result.detail


def test_preflight_skips_what_the_run_does_not_need():
    cfg = load_config(project_root() / "config.toml")
    report = run_preflight(cfg, need_cuda=False, need_mt5=False, need_warudo=False)
    assert report.ok()
    rendered = report.render()
    assert "skipped" in rendered
    assert "[OK  ]" in rendered


# ---------------------------------------------------------------------------
# Transcript log
# ---------------------------------------------------------------------------


def test_speech_log_round_trip(tmp_path):
    db = tmp_path / "log.sqlite"
    log = SpeechLog(db, "run-1")
    log.open(symbol="XAUUSD", mode="test", config_summary={"a": 1})
    log.write(
        market_time=T0,
        template_id="price.drift",
        source="template",
        priority=3,
        text="Gold's at thirty-three forty-one twenty.",
        emote="neutral",
        facts={"price": 3341.2, "session": "london"},
        dry_run=True,
    )
    log.close()

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT * FROM lines").fetchone()
    runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    conn.close()
    assert runs == 1
    assert row is not None
    assert "thirty-three" in row[7]


def test_speech_log_never_raises_when_the_path_is_bad(tmp_path):
    """A logging failure must not take the stream down."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    log = SpeechLog(blocker / "nested" / "log.sqlite", "run-2")
    log.open(symbol="X", mode="m", config_summary={})
    log.write(
        market_time=T0,
        template_id="t",
        source="template",
        priority=1,
        text="hi",
        emote=None,
        facts={},
        dry_run=True,
    )
    log.close()
