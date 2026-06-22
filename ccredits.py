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

try:  # tomllib is stdlib on 3.11+; tomli is the backport for 3.9/3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    import tomli as tomllib

NANO_AIU_PER_CREDIT = 1_000_000_000  # CLI constant Ygs=1e9: credits = totalNanoAiu / 1e9
DEFAULT_COST_PER_CREDIT = 0.01  # GitHub bills overage AI credits at $0.01 each.
CONFIG_FILENAME = "config.toml"


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
        help="Project this month's total from average use per elapsed calendar "
        "day (weekends included).",
    )
    parser.add_argument(
        "--weekdays-only",
        action="store_true",
        help="With --projected, average over weekdays (Mon-Fri) only, excluding "
        "weekend usage. Default projects over every calendar day.",
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
        "--sessions",
        action="store_true",
        help="Add a per-session breakdown (each session's repo, model, and "
        "credits). Off by default; the summary, per-day and per-model views "
        "always show.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of tables.",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="TOML config file. Default: ./config.toml in the current directory, "
        "if present. May set 'budget' and 'cost_per_credit'.",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="CREDITS",
        help="Monthly AI-credit budget. Shows used/remaining and, with "
        "--projected, whether you're on track to exceed it. Overrides config.",
    )
    parser.add_argument(
        "--cost-per-credit",
        type=float,
        default=None,
        metavar="USD",
        help="Dollar cost per AI credit for cost estimates "
        f"(default {DEFAULT_COST_PER_CREDIT}). Overrides config.",
    )
    return parser.parse_args(argv)


def credits_from_nano(nano_aiu: float) -> float:
    return nano_aiu / NANO_AIU_PER_CREDIT


def format_credits(credits: float) -> str:
    """Mirror the CLI: values in (0, 0.01) show as '<0.01', else 2 decimals."""
    if 0 < credits < 0.01:
        return "<0.01"
    return f"{credits:.2f}"


def format_cost(credits: float, cost_per_credit: float) -> str:
    """Dollar value of `credits` at the configured per-credit cost."""
    return f"${credits * cost_per_credit:.2f}"


def pct_change(current: float, previous: float) -> float | None:
    """Percent change from previous to current; None when previous is 0."""
    if previous == 0:
        return None
    return (current - previous) / previous * 100


def load_config(config_path: Path | None) -> dict:
    """Load settings from a TOML config file.

    Looks at an explicit --config path when given, otherwise `config.toml` in
    the current working directory. Returns {} when no readable config is found,
    so a missing or malformed file never crashes the tool.
    """
    candidate = config_path if config_path is not None else Path.cwd() / CONFIG_FILENAME
    if not candidate.is_file():
        return {}
    try:
        with candidate.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


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


def count_days(start: date, end: date) -> int:
    days = (end - start).days + 1
    return days if days > 0 else 0


def count_weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    if days <= 0:
        return 0
    return sum(1 for i in range(days) if (start + timedelta(days=i)).weekday() < 5)


def project_month(report: dict, use_utc: bool, weekdays_only: bool = False) -> dict:
    """Project the month's total from average use per elapsed period.

    By default the period is every calendar day, so all usage feeds the average.
    With `weekdays_only`, weekend (Sat/Sun) usage is excluded and the average is
    taken over Mon-Fri only.
    """
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

    if weekdays_only:
        used = sum(
            s["credits"]
            for s in report["sessions"]
            if to_zone(s["timestamp"], use_utc).weekday() < 5
        )
        elapsed = count_weekdays(first, reference)
        total_periods = count_weekdays(first, last)
    else:
        used = report["total"]
        elapsed = count_days(first, reference)
        total_periods = count_days(first, last)

    avg = used / elapsed if elapsed else 0.0
    return {
        "weekdays_only": weekdays_only,
        "unit": "weekday" if weekdays_only else "day",
        "used_credits": used,
        "excluded_credits": report["total"] - used,
        "periods_elapsed": elapsed,
        "periods_total": total_periods,
        "avg_per_period": avg,
        "projected_total": avg * total_periods,
        "complete": today > last,
    }


def build_history(all_sessions: list[dict], month: str, use_utc: bool, n: int) -> list[dict]:
    """Totals for the n months ending with `month` (oldest first).

    Each row carries the month-over-month delta vs the previous row: `delta`
    (credit change) and `delta_pct` (None for the first row or when the prior
    month was zero).
    """
    months = [shift_month(month, -k) for k in range(n - 1, -1, -1)]
    rows = []
    prev_total: float | None = None
    for m in months:
        agg = aggregate(all_sessions, m, use_utc)
        total = agg["total"]
        delta = None if prev_total is None else total - prev_total
        delta_pct = None if prev_total is None else pct_change(total, prev_total)
        rows.append(
            {
                "month": m,
                "total": total,
                "counted": agg["counted"],
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
        prev_total = total
    return rows


def month_over_month(all_sessions: list[dict], month: str, use_utc: bool) -> dict:
    """Current month vs the immediately preceding month."""
    prev_month = shift_month(month, -1)
    current = aggregate(all_sessions, month, use_utc)["total"]
    previous = aggregate(all_sessions, prev_month, use_utc)["total"]
    return {
        "prev_month": prev_month,
        "prev_total": previous,
        "delta": current - previous,
        "delta_pct": pct_change(current, previous),
    }


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


def _delta_markup(delta: float, delta_pct: float | None) -> str:
    """Rich-markup string for a credit delta: red when up, green when down."""
    arrow = "▲" if delta > 0 else "▼" if delta < 0 else "→"
    color = "red" if delta > 0 else "green" if delta < 0 else "dim"
    pct = f"{delta_pct:+.0f}%" if delta_pct is not None else "n/a"
    return f"[{color}]{arrow} {delta:+.2f} credits ({pct})[/{color}]"


def render_rich(report: dict) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    tz_label = "UTC" if report["use_utc"] else "local time"

    cost_per_credit = report["cost_per_credit"]

    console.print()
    console.print(
        f"[bold]Copilot AI credits — {report['month']}[/bold] "
        f"([dim]{tz_label}[/dim])"
    )
    console.print(
        f"[bold green]{format_credits(report['total'])} credits[/bold green] "
        f"([green]{format_cost(report['total'], cost_per_credit)}[/green]) "
        f"used across {report['counted']} session(s)"
    )
    mom = report.get("mom")
    if mom is not None:
        console.print(
            f"  [dim]vs {mom['prev_month']} "
            f"({format_credits(mom['prev_total'])} credits):[/dim] "
            f"{_delta_markup(mom['delta'], mom['delta_pct'])}"
        )
    console.print()

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

    if report.get("show_sessions") and report["sessions"]:
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
        unit = projection["unit"]
        if projection["periods_elapsed"] == 0:
            console.print(
                f"[bold]Projection:[/bold] no elapsed {unit}s yet — nothing to project from.\n"
            )
        else:
            label = "Final (month complete)" if projection["complete"] else "Projected month total"
            console.print(
                f"[bold]{label}:[/bold] "
                f"[bold magenta]{format_credits(projection['projected_total'])} credits[/bold magenta] "
                f"([magenta]{format_cost(projection['projected_total'], cost_per_credit)}[/magenta])"
            )
            console.print(
                f"  [dim]{format_credits(projection['avg_per_period'])}/{unit} avg × "
                f"{projection['periods_total']} {unit}s "
                f"({format_credits(projection['used_credits'])} over "
                f"{projection['periods_elapsed']} elapsed {unit}(s)).[/dim]"
            )
            if projection["excluded_credits"] > 0:
                console.print(
                    f"  [dim]Excludes {format_credits(projection['excluded_credits'])} "
                    "weekend credits from the average.[/dim]"
                )
            console.print()

    budget = report.get("budget")
    if budget:
        used = report["total"]
        remaining = budget - used
        used_pct = (used / budget * 100) if budget else 0.0
        over = used > budget
        used_color = "red" if over else "yellow" if used_pct >= 80 else "green"
        console.print(
            f"[bold]Budget:[/bold] [{used_color}]{format_credits(used)} / "
            f"{format_credits(budget)} credits ({used_pct:.0f}%)[/{used_color}] — "
            f"[dim]{format_credits(abs(remaining))} "
            f"{'over' if over else 'remaining'} "
            f"({format_cost(abs(remaining), cost_per_credit)})[/dim]"
        )
        projection = report.get("projection")
        if projection and projection["periods_elapsed"] > 0:
            projected = projection["projected_total"]
            proj_over = projected > budget
            proj_color = "red" if proj_over else "green"
            verb = "exceed" if proj_over else "stay under"
            diff = abs(projected - budget)
            console.print(
                f"  [{proj_color}]Projected to {verb} budget by "
                f"{format_credits(diff)} credits "
                f"({format_cost(diff, cost_per_credit)}).[/{proj_color}]"
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
        hist_table.add_column("Cost", justify="right")
        hist_table.add_column("Δ vs prev", justify="right")
        for row in history:
            if row["delta"] is None:
                delta_cell = "[dim]—[/dim]"
            else:
                delta_cell = _delta_markup(row["delta"], row["delta_pct"])
            hist_table.add_row(
                row["month"],
                str(row["counted"]),
                format_credits(row["total"]),
                format_cost(row["total"], cost_per_credit),
                delta_cell,
            )
        hist_table.add_section()
        hist_total = sum(r["total"] for r in history)
        hist_table.add_row(
            "[bold]Total[/bold]",
            str(sum(r["counted"] for r in history)),
            f"[bold]{format_credits(hist_total)}[/bold]",
            f"[bold]{format_cost(hist_total, cost_per_credit)}[/bold]",
            "",
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
    cost_per_credit = report["cost_per_credit"]
    out = {
        "month": report["month"],
        "timezone": "utc" if report["use_utc"] else "local",
        "total_credits": round(report["total"], 6),
        "cost_per_credit": cost_per_credit,
        "total_cost": round(report["total"] * cost_per_credit, 6),
        "counted_sessions": report["counted"],
        "incomplete_sessions": report["incomplete"],
        "unreadable_sessions": report["unreadable"],
        "per_day": {d: round(c, 6) for d, c in report["per_day"].items()},
        "per_model": {m: round(c, 6) for m, c in report["per_model"].items()},
    }
    if report.get("show_sessions"):
        out["sessions"] = [
            {
                "session_id": s["session_id"],
                "started": s["timestamp"].isoformat() if s["timestamp"] else None,
                "repo": s["repo"],
                "model": s.get("current_model"),
                "credits": round(s["credits"], 6),
            }
            for s in report["sessions"]
        ]
    mom = report.get("mom")
    if mom is not None:
        out["month_over_month"] = {
            "prev_month": mom["prev_month"],
            "prev_total_credits": round(mom["prev_total"], 6),
            "delta_credits": round(mom["delta"], 6),
            "delta_pct": round(mom["delta_pct"], 2) if mom["delta_pct"] is not None else None,
        }
    budget = report.get("budget")
    if budget:
        used = report["total"]
        out["budget"] = {
            "budget_credits": budget,
            "used_credits": round(used, 6),
            "remaining_credits": round(budget - used, 6),
            "used_pct": round(used / budget * 100, 2) if budget else None,
            "over_budget": used > budget,
            "budget_cost": round(budget * cost_per_credit, 6),
        }
    projection = report.get("projection")
    if projection:
        out["projection"] = {
            "basis": "weekdays" if projection["weekdays_only"] else "calendar_days",
            "projected_total_credits": round(projection["projected_total"], 6),
            "projected_total_cost": round(projection["projected_total"] * cost_per_credit, 6),
            "avg_per_period": round(projection["avg_per_period"], 6),
            "used_credits": round(projection["used_credits"], 6),
            "excluded_credits": round(projection["excluded_credits"], 6),
            "periods_elapsed": projection["periods_elapsed"],
            "periods_total": projection["periods_total"],
            "month_complete": projection["complete"],
        }
    history = report.get("history")
    if history is not None:
        out["history"] = [
            {
                "month": r["month"],
                "credits": round(r["total"], 6),
                "cost": round(r["total"] * cost_per_credit, 6),
                "sessions": r["counted"],
                "delta_credits": round(r["delta"], 6) if r["delta"] is not None else None,
                "delta_pct": round(r["delta_pct"], 2) if r["delta_pct"] is not None else None,
            }
            for r in history
        ]
    print(json.dumps(out, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config).expanduser() if args.config else None)

    # Precedence: CLI flag > config file > built-in default.
    budget = args.budget if args.budget is not None else config.get("budget")
    cost_per_credit = (
        args.cost_per_credit
        if args.cost_per_credit is not None
        else config.get("cost_per_credit", DEFAULT_COST_PER_CREDIT)
    )

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
    report["cost_per_credit"] = cost_per_credit
    report["budget"] = budget
    report["mom"] = month_over_month(report["_all_sessions"], args.month, args.utc)
    report["show_sessions"] = args.sessions

    if args.projected:
        report["projection"] = project_month(report, args.utc, args.weekdays_only)
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
