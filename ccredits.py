#!/usr/bin/env python3
"""Read how many GitHub Copilot AI credits you've used in a given month.

Reads the GitHub Copilot CLI's local session logs (no billing API needed, which
is unavailable on org accounts). Each completed session writes a `session.shutdown`
event containing `totalNanoAiu` -- the same number the CLI shows as its
`Session: X credits` footer. Credits are derived exactly as the CLI does:

    credits = totalNanoAiu / 1_000_000_000

Caveats:
  * Only *completed* sessions are counted -- a still-running session isn't
    recorded until it exits, and a crashed session can't be reconstructed
    (per-message events store token counts, not AIU).
  * Counts whatever session dirs exist under ~/.copilot/session-state on this
    machine. If the CLI prunes old sessions, an older month may be incomplete.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

NANO_AIU_PER_CREDIT = 1_000_000_000  # CLI constant Ygs=1e9: credits = totalNanoAiu / 1e9


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    now = datetime.now()
    parser = argparse.ArgumentParser(
        description="Show GitHub Copilot AI credits used in a given month, "
        "read from the Copilot CLI's local session logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--month",
        default=now.strftime("%Y-%m"),
        metavar="YYYY-MM",
        help="Month to report (default: current month, %(default)s).",
    )
    parser.add_argument(
        "--session-dir",
        default=str(Path.home() / ".copilot" / "session-state"),
        metavar="PATH",
        help="Copilot CLI session-state directory (default: %(default)s).",
    )
    parser.add_argument(
        "--utc",
        action="store_true",
        help="Use UTC for the month boundary. GitHub billing resets on the UTC "
        "calendar month; the default uses local time to match the CLI footer.",
    )
    parser.add_argument(
        "--projected",
        action="store_true",
        help="Project this month's total from average daily use, counting only "
        "weekdays (Mon-Fri). Weekend usage is excluded from the average.",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        type=int,
        const=3,
        default=None,
        metavar="N",
        help="Show a summary of the last N months ending with the selected month "
        "(default 3 when the flag is given).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of tables.",
    )
    return parser.parse_args(argv)


def credits_from_nano(nano_aiu: float) -> float:
    return nano_aiu / NANO_AIU_PER_CREDIT


def format_credits(credits: float) -> str:
    """Mirror the CLI: values in (0, 0.01) show as '<0.01', else 2 decimals."""
    if 0 < credits < 0.01:
        return "<0.01"
    return f"{credits:.2f}"


def parse_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (e.g. '2026-06-13T23:02:10.650Z')."""
    if not raw:
        return None
    try:
        # fromisoformat handles offsets but not a trailing 'Z' before 3.11.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_events(events_path: Path):
    """Yield parsed JSON objects from an events.jsonl file, skipping bad lines."""
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def load_session(events_path: Path) -> dict | None:
    """Reduce one session's events to the fields we report on.

    Returns a dict for every readable session. `completed` is False when no
    `session.shutdown` with totalNanoAiu was found (still running / crashed).
    """
    session_id = events_path.parent.name
    start_ctx: dict = {}
    shutdown: dict | None = None

    for event in iter_events(events_path):
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "session.start":
            start_ctx = data
        elif etype == "session.shutdown":
            # Take the last shutdown if a session somehow logged more than one.
            shutdown = {"data": data, "timestamp": event.get("timestamp")}

    context = (start_ctx.get("context") or {}) if start_ctx else {}
    repo = context.get("repository") or context.get("cwd") or "-"

    if not shutdown or shutdown["data"].get("totalNanoAiu") is None:
        return {"session_id": session_id, "repo": repo, "completed": False}

    sdata = shutdown["data"]
    ts = parse_timestamp(shutdown["timestamp"])
    if ts is None:
        start_ms = sdata.get("sessionStartTime")
        if isinstance(start_ms, (int, float)):
            ts = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)

    model_metrics = sdata.get("modelMetrics") or {}
    per_model = {
        model: credits_from_nano(m.get("totalNanoAiu") or 0)
        for model, m in model_metrics.items()
    }

    return {
        "session_id": session_id,
        "repo": repo,
        "completed": True,
        "timestamp": ts,
        "credits": credits_from_nano(sdata.get("totalNanoAiu") or 0),
        "models": per_model,
        "current_model": sdata.get("currentModel"),
    }


def to_zone(ts: datetime, use_utc: bool) -> datetime:
    return ts.astimezone(timezone.utc) if use_utc else ts.astimezone()


def session_month(ts: datetime | None, use_utc: bool) -> str | None:
    if ts is None:
        return None
    return to_zone(ts, use_utc).strftime("%Y-%m")


def load_all_completed(session_dir: Path) -> tuple[list[dict], int, int]:
    """Parse every session once. Returns (completed_sessions, incomplete, unreadable)."""
    sessions = []
    incomplete = 0
    unreadable = 0
    for path in sorted(session_dir.glob("*/events.jsonl")):
        session = load_session(path)
        if session is None:
            unreadable += 1
        elif not session["completed"]:
            incomplete += 1
        else:
            sessions.append(session)
    sessions.sort(key=lambda s: s.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc))
    return sessions, incomplete, unreadable


def aggregate(all_sessions: list[dict], month: str, use_utc: bool) -> dict:
    sessions = [s for s in all_sessions if session_month(s.get("timestamp"), use_utc) == month]

    per_day: dict[str, float] = defaultdict(float)
    per_model: dict[str, float] = defaultdict(float)
    for s in sessions:
        local_ts = to_zone(s["timestamp"], use_utc)
        per_day[local_ts.strftime("%Y-%m-%d")] += s["credits"]
        for model, credits in s["models"].items():
            per_model[model] += credits

    return {
        "month": month,
        "total": sum(s["credits"] for s in sessions),
        "sessions": sessions,
        "per_day": dict(sorted(per_day.items())),
        "per_model": dict(sorted(per_model.items(), key=lambda kv: -kv[1])),
        "counted": len(sessions),
        "use_utc": use_utc,
    }


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(x) for x in month.split("-"))
    first = date(year, mon, 1)
    next_first = date(year + (mon == 12), (mon % 12) + 1, 1)
    return first, next_first - timedelta(days=1)


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    idx = year * 12 + (mon - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def count_weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    if days <= 0:
        return 0
    return sum(1 for i in range(days) if (start + timedelta(days=i)).weekday() < 5)


def project_month(report: dict, use_utc: bool) -> dict:
    """Project the month's total from average use per *elapsed weekday*."""
    month = report["month"]
    first, last = month_bounds(month)
    today = datetime.now(timezone.utc).date() if use_utc else datetime.now().date()

    # How far through the month we are: today if it's the current month, else the
    # whole month for a past month (future months haven't started).
    if today < first:
        reference = first - timedelta(days=1)  # no elapsed days
    elif today > last:
        reference = last
    else:
        reference = today

    weekday_credits = sum(
        s["credits"] for s in report["sessions"] if to_zone(s["timestamp"], use_utc).weekday() < 5
    )
    weekdays_elapsed = count_weekdays(first, reference)
    weekdays_total = count_weekdays(first, last)

    avg = weekday_credits / weekdays_elapsed if weekdays_elapsed else 0.0
    return {
        "weekday_credits": weekday_credits,
        "weekend_credits": report["total"] - weekday_credits,
        "weekdays_elapsed": weekdays_elapsed,
        "weekdays_total": weekdays_total,
        "avg_per_weekday": avg,
        "projected_total": avg * weekdays_total,
        "complete": today > last,
    }


def build_history(all_sessions: list[dict], month: str, use_utc: bool, n: int) -> list[dict]:
    """Totals for the n months ending with `month` (oldest first)."""
    months = [shift_month(month, -k) for k in range(n - 1, -1, -1)]
    rows = []
    for m in months:
        agg = aggregate(all_sessions, m, use_utc)
        rows.append({"month": m, "total": agg["total"], "counted": agg["counted"]})
    return rows


def collect(session_dir: Path, month: str, use_utc: bool) -> dict:
    all_sessions, incomplete, unreadable = load_all_completed(session_dir)
    report = aggregate(all_sessions, month, use_utc)
    report["incomplete"] = incomplete
    report["unreadable"] = unreadable
    report["_all_sessions"] = all_sessions
    return report


def local_str(ts: datetime | None, use_utc: bool) -> str:
    if ts is None:
        return "-"
    shown = ts.astimezone(timezone.utc) if use_utc else ts.astimezone()
    return shown.strftime("%Y-%m-%d %H:%M")


def render_rich(report: dict) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    tz_label = "UTC" if report["use_utc"] else "local time"

    console.print()
    console.print(
        f"[bold]Copilot AI credits — {report['month']}[/bold] "
        f"([dim]{tz_label}[/dim])"
    )
    console.print(
        f"[bold green]{format_credits(report['total'])} credits[/bold green] "
        f"used across {report['counted']} session(s)\n"
    )

    if report["per_day"]:
        day_table = Table(title="Per day", title_justify="left", header_style="bold")
        day_table.add_column("Date")
        day_table.add_column("Credits", justify="right")
        for day, credits in report["per_day"].items():
            day_table.add_row(day, format_credits(credits))
        console.print(day_table)

    if report["per_model"]:
        model_table = Table(title="Per model", title_justify="left", header_style="bold")
        model_table.add_column("Model")
        model_table.add_column("Credits", justify="right")
        for model, credits in report["per_model"].items():
            model_table.add_row(model, format_credits(credits))
        console.print(model_table)

    if report["sessions"]:
        sess_table = Table(title="Per session", title_justify="left", header_style="bold")
        sess_table.add_column("Started")
        sess_table.add_column("Repo")
        sess_table.add_column("Model")
        sess_table.add_column("Session", style="dim")
        sess_table.add_column("Credits", justify="right")
        for s in report["sessions"]:
            sess_table.add_row(
                local_str(s["timestamp"], report["use_utc"]),
                str(s["repo"]),
                s.get("current_model") or ", ".join(s["models"]) or "-",
                s["session_id"][:8],
                format_credits(s["credits"]),
            )
        console.print(sess_table)

    projection = report.get("projection")
    if projection:
        if projection["weekdays_elapsed"] == 0:
            console.print(
                "[bold]Projection:[/bold] no elapsed weekdays yet — nothing to project from.\n"
            )
        else:
            label = "Final (month complete)" if projection["complete"] else "Projected month total"
            console.print(
                f"[bold]{label}:[/bold] "
                f"[bold magenta]{format_credits(projection['projected_total'])} credits[/bold magenta]"
            )
            console.print(
                f"  [dim]{format_credits(projection['avg_per_weekday'])}/weekday avg × "
                f"{projection['weekdays_total']} weekdays "
                f"({format_credits(projection['weekday_credits'])} over "
                f"{projection['weekdays_elapsed']} elapsed weekday(s)).[/dim]"
            )
            if projection["weekend_credits"] > 0:
                console.print(
                    f"  [dim]Excludes {format_credits(projection['weekend_credits'])} "
                    "weekend credits from the average.[/dim]"
                )
            console.print()

    history = report.get("history")
    if history:
        hist_table = Table(
            title=f"Last {len(history)} months", title_justify="left", header_style="bold"
        )
        hist_table.add_column("Month")
        hist_table.add_column("Sessions", justify="right")
        hist_table.add_column("Credits", justify="right")
        for row in history:
            hist_table.add_row(
                row["month"], str(row["counted"]), format_credits(row["total"])
            )
        hist_table.add_section()
        hist_table.add_row(
            "[bold]Total[/bold]",
            str(sum(r["counted"] for r in history)),
            f"[bold]{format_credits(sum(r['total'] for r in history))}[/bold]",
        )
        console.print(hist_table)

    notes = []
    if report["incomplete"]:
        notes.append(
            f"{report['incomplete']} running/incomplete session(s) skipped "
            "(no credits recorded until a session exits)"
        )
    if report["unreadable"]:
        notes.append(f"{report['unreadable']} unreadable session(s) skipped")
    if not report["sessions"]:
        notes.append("No completed sessions found for this month on this machine.")
    for note in notes:
        console.print(f"[yellow]note:[/yellow] {note}")
    console.print()


def render_json(report: dict) -> None:
    out = {
        "month": report["month"],
        "timezone": "utc" if report["use_utc"] else "local",
        "total_credits": round(report["total"], 6),
        "counted_sessions": report["counted"],
        "incomplete_sessions": report["incomplete"],
        "unreadable_sessions": report["unreadable"],
        "per_day": {d: round(c, 6) for d, c in report["per_day"].items()},
        "per_model": {m: round(c, 6) for m, c in report["per_model"].items()},
        "sessions": [
            {
                "session_id": s["session_id"],
                "started": s["timestamp"].isoformat() if s["timestamp"] else None,
                "repo": s["repo"],
                "model": s.get("current_model"),
                "credits": round(s["credits"], 6),
            }
            for s in report["sessions"]
        ],
    }
    projection = report.get("projection")
    if projection:
        out["projection"] = {
            "projected_total_credits": round(projection["projected_total"], 6),
            "avg_per_weekday": round(projection["avg_per_weekday"], 6),
            "weekday_credits": round(projection["weekday_credits"], 6),
            "weekend_credits": round(projection["weekend_credits"], 6),
            "weekdays_elapsed": projection["weekdays_elapsed"],
            "weekdays_total": projection["weekdays_total"],
            "month_complete": projection["complete"],
        }
    history = report.get("history")
    if history is not None:
        out["history"] = [
            {"month": r["month"], "credits": round(r["total"], 6), "sessions": r["counted"]}
            for r in history
        ]
    print(json.dumps(out, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session_dir = Path(args.session_dir).expanduser()

    if not session_dir.is_dir():
        msg = f"session directory not found: {session_dir}"
        if args.json:
            print(json.dumps({"error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
            print(
                "Is the GitHub Copilot CLI installed and have you run it at least once?",
                file=sys.stderr,
            )
        return 1

    report = collect(session_dir, args.month, args.utc)

    if args.projected:
        report["projection"] = project_month(report, args.utc)
    if args.history is not None:
        n = max(1, args.history)
        report["history"] = build_history(report["_all_sessions"], args.month, args.utc, n)

    if args.json:
        render_json(report)
    else:
        render_rich(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
