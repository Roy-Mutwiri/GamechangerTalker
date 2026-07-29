"""Read a stream back and work out what to rewrite.

    python -m tools.review                 # the most recent run
    python -m tools.review --runs          # list runs
    python -m tools.review --run a1b2c3    # a specific one
    python -m tools.review --export out.md

This is the other half of the tuning loop. `--simulate` tells you what the
library *would* say; this tells you what it actually said on a live stream,
and which parts of it grated.

What it looks for, in order of how much it usually matters:

  filler share over time   bridges climbing means the real templates are
                           exhausting themselves
  repeats                  the same sentence twice inside a few minutes is
                           what viewers notice first
  clustering               four templates describing the same fact in a row
  never fired              templates the market never asked for
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from narrator.config import load_config, project_root


def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(
            f"no transcript log at {db}. Run the narrator first, or point "
            "app.log_db at the right file."
        )
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT r.run_id, r.started_at, r.symbol, r.mode, count(l.id) AS lines"
        " FROM runs r LEFT JOIN lines l ON l.run_id = r.run_id"
        " GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT 25"
    ).fetchall()
    print(f"{'run':<14}{'started':<22}{'mode':<18}{'symbol':<18}lines")
    print("-" * 78)
    for row in rows:
        started = row["started_at"][:19].replace("T", " ")
        print(
            f"{row['run_id']:<14}{started:<22}{row['mode'] or '':<18}"
            f"{row['symbol'] or '':<18}{row['lines']}"
        )


def bar(fraction: float, width: int = 34) -> str:
    return "#" * max(0, min(width, int(fraction * width)))


def review(conn: sqlite3.Connection, run_id: str, out: list[str]) -> None:
    meta = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    rows = conn.execute(
        "SELECT market_time, template_id, source, text, emote FROM lines"
        " WHERE run_id = ? ORDER BY market_time",
        (run_id,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"run {run_id} has no lines")

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    times = [dt.datetime.fromisoformat(r["market_time"]) for r in rows]
    span = (times[-1] - times[0]).total_seconds()
    emit(f"# Stream review: {run_id}")
    emit()
    if meta:
        emit(f"- started `{meta['started_at'][:19].replace('T', ' ')}`")
        emit(f"- symbol `{meta['symbol']}`, mode `{meta['mode']}`")
    emit(f"- {len(rows)} lines over {span / 3600:.1f} hours")
    emit(f"- one line every {span / max(1, len(rows)):.0f}s on average")

    # ---- filler share over time ------------------------------------------
    emit()
    emit("## Filler share by hour")
    emit()
    emit("Bridges climbing over a stream means the real templates are running")
    emit("out. Raise `max_per_session`, shorten cooldowns, or write more lines.")
    emit()
    per_hour: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for row, when in zip(rows, times, strict=True):
        hour = int((when - times[0]).total_seconds() // 3600)
        per_hour[hour][row["source"]] += 1
    emit("```")
    for hour in sorted(per_hour):
        counts = per_hour[hour]
        total = sum(counts.values())
        bridge = counts.get("bridge", 0)
        share = bridge / max(1, total)
        emit(
            f"  hour {hour:>2}  {total:>4} lines  {bridge:>3} filler "
            f"({share * 100:>3.0f}%) {bar(share)}"
        )
    emit("```")

    # ---- repeats ----------------------------------------------------------
    emit()
    emit("## Repeated sentences")
    emit()
    emit("The same words twice inside ten minutes is what viewers notice first.")
    emit()
    seen: dict[str, dt.datetime] = {}
    repeats: list[tuple[str, float, str]] = []
    for row, when in zip(rows, times, strict=True):
        text = row["text"]
        if text in seen:
            gap = (when - seen[text]).total_seconds()
            if gap < 600:
                repeats.append((row["template_id"], gap, text))
        seen[text] = when
    if repeats:
        emit("```")
        for template_id, gap, text in sorted(repeats, key=lambda r: r[1])[:15]:
            emit(f"  {gap:>5.0f}s apart  [{template_id}]  {text[:64]}")
        emit("```")
        emit(f"{len(repeats)} repeats inside ten minutes.")
    else:
        emit("None. Good.")

    # ---- clustering -------------------------------------------------------
    emit()
    emit("## Templates that cluster together")
    emit()
    emit("Pairs that keep firing back to back are usually describing the same")
    emit("fact twice. Gate one on the other, or lengthen a cooldown.")
    emit()
    pairs: Counter[tuple[str, str]] = Counter()
    for earlier, later in zip(rows, rows[1:], strict=False):
        if earlier["template_id"] != later["template_id"]:
            pairs[(earlier["template_id"], later["template_id"])] += 1
    common = [pair for pair in pairs.most_common(12) if pair[1] > 2]
    if common:
        emit("```")
        for (first, second), count in common:
            emit(f"  {count:>3}x  {first}  ->  {second}")
        emit("```")
    else:
        emit("Nothing stands out.")

    # ---- usage ------------------------------------------------------------
    spoken = Counter(row["template_id"] for row in rows)
    emit()
    emit("## Most used")
    emit()
    emit("```")
    for template_id, count in spoken.most_common(15):
        share = count / len(rows)
        emit(f"  {template_id:<36}{count:>4}  {share * 100:>4.1f}%  {bar(share, 20)}")
    emit("```")

    library_ids = _library_ids()
    if library_ids:
        never = sorted(library_ids - set(spoken))
        emit()
        emit(f"## Never fired ({len(never)} of {len(library_ids)})")
        emit()
        emit("Either the market never asked, or the condition cannot be met.")
        emit("Check the conditions before writing more lines.")
        emit()
        emit("```")
        for template_id in never:
            emit(f"  {template_id}")
        emit("```")

    emotes = Counter(row["emote"] for row in rows if row["emote"])
    if emotes:
        emit()
        emit("## Emotes")
        emit()
        emit("```")
        for name, count in emotes.most_common():
            emit(f"  {name:<14}{count}")
        emit("```")


def _library_ids() -> set[str]:
    try:
        cfg = load_config(project_root() / "config.toml")
        from narrator.script.library import TemplateLibrary

        library = TemplateLibrary(cfg.path(cfg.templates.dir), cfg)
        library.load()
    except Exception:
        return set()
    return {t.id for t in library.templates}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", help="run id (default: the most recent)")
    ap.add_argument("--runs", action="store_true", help="list runs and exit")
    ap.add_argument("--db", help="path to the transcript database")
    ap.add_argument("--export", help="also write the report to a markdown file")
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    db = Path(args.db) if args.db else cfg.path(cfg.app.log_db)
    conn = connect(db)

    if args.runs:
        list_runs(conn)
        return

    run_id = args.run
    if not run_id:
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit("no runs recorded yet")
        run_id = row["run_id"]

    out: list[str] = []
    review(conn, run_id, out)
    conn.close()

    if args.export:
        Path(args.export).write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwritten to {args.export}")


if __name__ == "__main__":
    main()
