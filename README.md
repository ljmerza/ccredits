# ccredits

A small Python CLI that tells you how many **GitHub Copilot AI credits** you've used in
a given month — the new token-based ("AI credits" / AIU) system, *not* premium requests.

It needs **no billing API** (that's unavailable on organization accounts). Instead it
reads the GitHub Copilot CLI's own local session logs under `~/.copilot/session-state/`.

## Features

- Monthly AI-credit total, plus breakdowns **per day**, **per model**, and **per session**.
- **Projection** of the full month from your average weekday pace (`--projected`).
- **History** of the last N months (`--history`).
- Pick any month (`--month`), local or UTC boundary (`--utc`), and JSON output (`--json`).

## How it works

Every time a `copilot` CLI session exits, it writes a `session.shutdown` event
containing `totalNanoAiu` — the exact number behind the `Session: X credits` footer the
CLI shows you. Credits are derived the same way the CLI does (constant verified from the
CLI bundle, `Ygs = 1e9`):

```
credits = totalNanoAiu / 1_000_000_000
```

The tool sums that across all completed sessions in the chosen month.

## Requirements

- Python 3.9+
- [`rich`](https://pypi.org/project/rich/) (installed automatically with the package)
- The GitHub Copilot CLI, run at least once (so `~/.copilot/session-state/` exists)

## Setup

Install it as an isolated, globally-available `ccredits` command:

```bash
uv tool install .
```

Alternatives: `pipx install .` (same isolated-global result), or
`uv pip install -e .` inside a venv for an editable dev install.

## Usage

```bash
# Current month
ccredits

# A specific month
ccredits --month 2026-05

# Project this month's total from average weekday use
ccredits --projected

# Summary of the last 3 months (or --history 6 for six)
ccredits --history

# UTC month boundary (matches GitHub billing reset); default is local time
ccredits --utc

# Machine-readable output
ccredits --json

# Point at a non-default session dir
ccredits --session-dir /path/to/.copilot/session-state
```

### Options

| Flag | Description |
|------|-------------|
| `--month YYYY-MM` | Month to report. Default: current month. |
| `--projected` | Add a projected month total based on average **weekday** use. |
| `--history [N]` | Add a summary of the last `N` months (default 3). |
| `--utc` | Use the UTC calendar month as the boundary (default: local time). |
| `--json` | Emit JSON instead of tables. |
| `--session-dir PATH` | Copilot session-state dir. Default: `~/.copilot/session-state`. |

### Example output

```
Copilot AI credits — 2026-06 (local time)
0.75 credits used across 1 session(s)

Per day                          Per model
┏━━━━━━━━━━━━┳━━━━━━━━━┓          ┏━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Date       ┃ Credits ┃          ┃ Model      ┃ Credits ┃
┡━━━━━━━━━━━━╇━━━━━━━━━┩          ┡━━━━━━━━━━━━╇━━━━━━━━━┩
│ 2026-06-13 │    0.75 │          │ gpt-5-mini │    0.75 │
└────────────┴─────────┘          └────────────┴─────────┘
```

## Projection (`--projected`)

Estimates the full month's total assuming you keep using Copilot at your current
**weekday** pace:

```
avg_per_weekday = weekday_credits_so_far / weekdays_elapsed_so_far
projected_total = avg_per_weekday × total_weekdays_in_month
```

Weekend (Sat/Sun) usage is excluded from the average, and the average is taken over
*every* elapsed weekday — including weekdays with no usage — so a quiet Tuesday pulls the
projection down. For a past month it just reports the final actual total.

## History (`--history [N]`)

Shows a per-month total + session count for the last `N` months (default 3) ending with
the selected month, plus a combined total.

## Caveats

- **Only completed sessions count.** `totalNanoAiu` is written when a session exits, so a
  session you're currently in isn't counted until you close it. A crashed session can't be
  reconstructed (per-message events store token counts, not AIU). The tool reports how
  many running/incomplete sessions it skipped.
- **Local disk only.** It counts whatever sessions exist under `~/.copilot/session-state`
  on this machine. If the CLI prunes old sessions, an older month may read low.
- **Your usage, not the org pool.** Numbers reflect this machine's sessions, not your
  organization's shared credit pool.
- The default month boundary uses **local time** to match the footer you see; GitHub
  billing itself resets on the **UTC** calendar month — use `--utc` for that.
```
