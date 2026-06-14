# ccredits

A small Python CLI that tells you how many **GitHub Copilot AI credits** you've used in
a given month — the new token-based ("AI credits" / AIU) system, *not* premium requests.

It needs **no billing API** (that's unavailable on organization accounts). Instead it
reads the GitHub Copilot CLI's own local session logs under `~/.copilot/session-state/`.

## Features

- Monthly AI-credit total, plus breakdowns **per day**, **per model**, and **per session**.
- **Projection** of the full month from your average daily pace (`--projected`), or weekdays only (`--weekdays-only`).
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

Install it as an isolated, globally-available `ccredits` command (needs
[`uv`](https://docs.astral.sh/uv/getting-started/installation/)):

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

# Project this month's total from average daily use (weekends included)
ccredits --projected

# ...or project from weekday (Mon-Fri) pace only
ccredits --projected --weekdays-only

# Summary of the last N months, with month-over-month deltas (default 3)
ccredits --history

# Track against a monthly budget (or set it in config.toml — see below)
ccredits --budget 300 --projected

# Override the per-credit cost used for $ estimates (default $0.01)
ccredits --cost-per-credit 0.01

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
| `--projected` | Add a projected month total based on average use per **calendar day** (weekends included). |
| `--weekdays-only` | With `--projected`, average over **weekdays (Mon–Fri)** only, excluding weekend usage. |
| `--history [N]` | Add a summary of the last `N` months (default 3), with month-over-month deltas. |
| `--budget CREDITS` | Monthly AI-credit budget; shows used/remaining and (with `--projected`) whether you're on track to exceed it. Overrides config. |
| `--cost-per-credit USD` | Dollar cost per AI credit for cost estimates (default `0.01`). Overrides config. |
| `--config PATH` | TOML config file. Default: `./config.toml` if present. |
| `--utc` | Use the UTC calendar month as the boundary (default: local time). |
| `--json` | Emit JSON instead of tables. |
| `--session-dir PATH` | Copilot session-state dir. Default: `~/.copilot/session-state`. |

### Config file

Budget and per-credit cost can live in a TOML config so you don't retype them.
`ccredits` reads `config.toml` from the current directory (or a path given with
`--config`). Copy the tracked example and edit it — your `config.toml` is gitignored,
so it never gets committed:

```bash
cp config.example.toml config.toml
```

```toml
# config.toml
budget = 300            # monthly AI-credit budget (omit to disable budget tracking)
cost_per_credit = 0.01  # GitHub bills overage AI credits at $0.01 each
```

Precedence is **CLI flag > config file > built-in default**. Cost estimates appear
everywhere credits do (summary, history, projection); the **month-over-month delta**
(current vs previous month) shows in the summary, and per-row deltas show in `--history`.

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

Estimates the full month's total assuming you keep using Copilot at your current pace.
By default the average is taken over **every elapsed calendar day**:

```
avg_per_day = credits_so_far / days_elapsed_so_far
projected_total = avg_per_day × total_days_in_month
```

The average covers *every* elapsed day — including days with no usage — so a quiet day
pulls the projection down. For a past month it just reports the final actual total.

Add `--weekdays-only` to base the projection on **weekdays (Mon–Fri)** instead: weekend
usage is then excluded from the average and the projection spans only the month's
weekdays. Note that in this mode the projection can read *lower* than what you've already
spent if much of your usage falls on weekends.

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
