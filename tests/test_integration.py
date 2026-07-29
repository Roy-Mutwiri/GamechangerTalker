"""End-to-end tests: the whole app, wired as it runs live.

Everything below drives narrator.main the way the operator does, against the
replay adapter and the silent engine, and asserts on what actually came out.
Unit tests keep the parts honest; these keep the wiring honest -- which is
where the last three real bugs came from.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from narrator.config import load_config, project_root
from narrator.main import main, parse_args

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def project() -> Path:
    return project_root()


def run_app(tmp_path: Path, *extra: str) -> int:
    """Run a short headless session and return the exit code.

    `--allow-delayed` is not incidental: recorded bars are not real-time
    prices, and the narrator refuses to start on them without being told so
    explicitly. A test that runs on replay has to say it, same as an operator.
    """
    return main(
        [
            "--dry-run",
            "--replay",
            "--allow-delayed",
            "--plain",
            "--no-web",
            "--no-avatar",
            "--speed",
            "60",
            "--minutes",
            "20",
            "--seed",
            "7",
            *extra,
        ]
    )


# ---------------------------------------------------------------------------
# The informational modes
# ---------------------------------------------------------------------------


def test_validate_only_passes_on_the_shipped_config():
    assert (
        main(["--validate-only", "--dry-run", "--replay", "--allow-delayed", "--no-web"])
        == 0
    )


def test_list_facts(capsys):
    assert main(["--list-facts"]) == 0
    out = capsys.readouterr().out
    assert "price" in out
    assert "minutes_since_move" in out
    assert "facts available to templates" in out


def test_list_templates(capsys):
    assert main(["--list-templates"]) == 0
    out = capsys.readouterr().out
    assert "price.drift" in out
    assert "templates total" in out


def test_argument_parsing_defaults():
    args = parse_args([])
    assert args.dry_run is False
    assert args.replay is False
    args = parse_args(["--replay"])
    assert args.replay is True
    args = parse_args(["--replay", "some.csv"])
    assert args.replay == "some.csv"


# ---------------------------------------------------------------------------
# A whole session
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_full_dry_run_speaks_and_logs(tmp_path, capsys):
    assert run_app(tmp_path) == 0
    out = capsys.readouterr().out

    # It said things, and each line is stamped with the template that fired.
    assert "[price." in out or "[levels." in out or "[session." in out
    assert "lines spoken" in out
    assert "speech density" in out

    # Numbers came out spoken, not printed. No stray digits in the transcript
    # body -- that is the whole point of the normalizer.
    spoken_lines = [
        line for line in out.splitlines() if line[:2].isdigit() and "] " in line
    ]
    assert spoken_lines, "no transcript lines at all"
    for line in spoken_lines[:40]:
        body = line.split("] ", 1)[1]
        assert not any(ch.isdigit() for ch in body), f"unspoken digits in: {body}"


@pytest.mark.slow
def test_the_transcript_reaches_sqlite_with_its_facts():
    cfg = load_config(project_root() / "config.toml")
    db = cfg.path(cfg.app.log_db)
    before = 0
    if db.exists():
        conn = sqlite3.connect(db)
        before = conn.execute("SELECT count(*) FROM lines").fetchone()[0]
        conn.close()

    assert (
        main(
            [
                "--dry-run",
                "--replay",
                "--allow-delayed",
                "--plain",
                "--no-web",
                "--no-avatar",
                "--speed",
                "60",
                "--minutes",
                "15",
                "--seed",
                "3",
            ]
        )
        == 0
    )

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT template_id, text, facts FROM lines ORDER BY id DESC LIMIT 20"
    ).fetchall()
    after = conn.execute("SELECT count(*) FROM lines").fetchone()[0]
    conn.close()

    assert after > before, "nothing was written to the transcript log"
    for template_id, text, facts_json in rows:
        assert template_id and text
        facts = json.loads(facts_json)
        # The snapshot is what makes a line reviewable afterwards.
        assert "price" in facts
        assert "session" in facts


# ---------------------------------------------------------------------------
# Deterministic simulation
# ---------------------------------------------------------------------------


def test_pacing_holds_the_minimum_gap():
    """No two lines closer together than min_gap_seconds."""
    from narrator.simulate import simulate

    cfg = load_config(project_root() / "config.toml")
    result, _ = simulate(cfg, minutes=180, seed=11)

    assert len(result.lines) > 20
    stamps = [when for when, _ in result.lines]
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]
    assert min(gaps) >= cfg.scheduler.min_gap_seconds, (
        f"lines came {min(gaps)}s apart, floor is {cfg.scheduler.min_gap_seconds}s"
    )


def test_a_simulation_is_reproducible_from_its_seed():
    """Same fixture, same seed, same transcript -- which is what makes an A/B
    of a template change readable."""
    from narrator.simulate import simulate

    cfg = load_config(project_root() / "config.toml")
    first, _ = simulate(cfg, minutes=120, seed=99)
    second, _ = simulate(cfg, minutes=120, seed=99)
    assert first.transcript() == second.transcript()
    assert first.transcript(), "the simulation said nothing at all"


def test_a_different_seed_gives_a_different_transcript():
    from narrator.simulate import simulate

    cfg = load_config(project_root() / "config.toml")
    first, _ = simulate(cfg, minutes=120, seed=1)
    second, _ = simulate(cfg, minutes=120, seed=2)
    assert first.transcript() != second.transcript()


def test_simulation_never_speaks_while_the_mouth_is_busy():
    """One line at a time: the next cannot start before the last finished."""
    from narrator.simulate import simulate

    cfg = load_config(project_root() / "config.toml")
    result, _ = simulate(cfg, minutes=240, seed=5)
    for (t_a, u_a), (t_b, _) in zip(result.lines, result.lines[1:], strict=False):
        gap = (t_b - t_a).total_seconds()
        assert gap >= u_a.estimated_seconds, (
            f"{u_a.template_id} was still speaking when the next line started"
        )


def test_simulation_respects_per_session_caps():
    from narrator.simulate import simulate

    cfg = load_config(project_root() / "config.toml")
    result, library = simulate(cfg, minutes=720, seed=3)
    # Counters reset per trading session, so the run total may exceed the cap;
    # what must hold is that nothing ran away entirely.
    for template_id, count in result.spoken.items():
        cap = library.by_id[template_id].max_per_session
        assert count <= cap * 8, f"{template_id} fired {count} times, cap is {cap}"


def test_the_cli_simulate_flag_runs(capsys):
    assert main(["--simulate", "--minutes", "60", "--seed", "4"]) == 0
    out = capsys.readouterr().out
    assert "lines spoken" in out
    assert "never fired" in out or "templates used" in out


# ---------------------------------------------------------------------------
# Failure modes the stream has to survive
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The prices are the market's, or there are no prices
# ---------------------------------------------------------------------------


def test_replay_will_not_start_without_being_told_it_is_not_live(capsys):
    """The failure this exists to prevent: recorded July bars narrated as the
    market, at prices seven hundred dollars from where gold actually was."""
    assert main(["--dry-run", "--replay", "--plain", "--no-web", "--no-avatar"]) == 1
    out = capsys.readouterr().out
    assert "prices" in out and "not a real-time feed" in out


def test_the_delayed_feed_is_refused_on_the_same_rule(capsys):
    """Delayed is not a milder problem than recorded. Yahoo's gold is exactly
    ten minutes behind, and ten minutes is several moves in this market."""
    assert main(["--dry-run", "--web-feed", "--plain", "--no-web", "--no-avatar"]) == 1
    assert "not a real-time feed" in capsys.readouterr().out


def test_allowing_delayed_prices_says_so_for_the_whole_run(capsys):
    """It starts, but it never reads as a healthy feed -- a WARN at boot and
    NOT LIVE in the status bar, not a line buried at startup."""
    assert run_app(Path(), "--minutes", "5") == 0
    out = capsys.readouterr().out
    assert "NOT REAL TIME" in out
    assert "NOT LIVE" in out


def test_a_missing_replay_file_fails_loudly(capsys):
    with pytest.raises(FileNotFoundError, match="replay csv not found"):
        main(
            [
                "--dry-run",
                "--replay",
                "does_not_exist.csv",
                "--allow-delayed",
                "--plain",
                "--no-web",
                "--no-avatar",
                "--minutes",
                "1",
            ]
        )


def test_a_broken_template_library_refuses_to_start(tmp_path, capsys):
    (tmp_path / "broken.json").write_text(
        '[{"id": "x.y", "when": "no_such_fact > 1", "variants": ["hi"]}]',
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'[templates]\ndir = "{tmp_path.as_posix()}"\n'
        f"[preflight]\nrequire_cuda = false\nrequire_mt5 = false\n",
        encoding="utf-8",
    )
    code = main(["--dry-run", "--replay", "--no-web", "--config", str(config)])
    assert code == 1
    out = capsys.readouterr().out
    assert "no_such_fact" in out
    assert "FAIL" in out
