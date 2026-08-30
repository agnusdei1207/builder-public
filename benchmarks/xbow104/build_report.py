#!/usr/bin/env python3
"""
build_report.py -- XBOW-104 benchmark -> clean, public-safe tables & graphs.

Reads the benchmark artifacts (read-only) and regenerates:
  data/results.json   flat, public-safe per-task array (newest COMPLETE run per task)
  data/summary.json   headline totals, fault-class counts, per-level / per-tag solve rates
  data/history.json   run-over-time series (upserted per snapshot, extends each build)
  graphs/*.png/.svg   outcome mix, solve-rate by level, solve-rate by tag,
                      per-task token usage, and a version/run-over-time curve

Design goals:
  * Idempotent & re-runnable -- safe to run again after the live re-run finishes.
  * Public-safe -- ONLY clean result metrics leave the artifacts dir. Transcripts,
    telemetry, prompts, flag values, hosts, internal paths, image ids, etc. are
    NEVER read into the output. See SAFE_* / the record builder below.
  * Robust to a concurrent live re-run -- a task's newest run dir may not have a
    finished (or valid) evidence.json yet; we transparently fall back to its most
    recent COMPLETE evidence.json and never touch in-progress files.

Stdlib + matplotlib only. No network.

Usage:
    python build_report.py [--artifacts DIR] [--out DIR] [--label LABEL]

Defaults resolve relative to the repository so it "just works" from anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths (all resolved relative to this file so the script is location-robust)
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
# public/benchmarks/xbow104/ -> repo root is three levels up
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DEFAULT_ARTIFACTS = os.path.join(REPO_ROOT, "benchmarks", "xbow104", "artifacts")
DEFAULT_OUT = HERE

STAMP_RE = re.compile(r"^(XBEN-\d+-\d+)-(\d{4}-\d{2}-\d{2}T[0-9-]+Z)$")

# ---------------------------------------------------------------------------
# Public-safe field policy
# ---------------------------------------------------------------------------
# Only these leave the artifacts dir. Everything else (flags_seen,
# expected_flag_sha256, host/hostUrl/internalUrl, service, agent_image[_id],
# suite_revision, backbone, error text, exit_code, prompts, transcripts,
# telemetry, ...) is deliberately dropped.
SAFE_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "cost_usd",
    "events",
)

# outcome -> (fault_class, fault_subtype)
FAULT_MAP = {
    "solved": ("solved", None),
    "unsolved": ("unsolved", None),
    "timeout": ("timeout", None),
    "benchmark_build_fault": ("target_broken", "benchmark_build_fault"),
    "benchmark_start_fault": ("target_broken", "benchmark_start_fault"),
    "provider_fault": ("provider_fault", None),
    "runtime_fault": ("runtime_fault", None),
}
# Tasks whose success requires a working headless browser: the target ships a
# PhantomJS-based XSS checker (an admin "bot" that renders submitted input). Our
# runtime has no headless-browser capability yet (in progress), so the agent gets
# no fair attempt on these — they are excluded pending that tooling and re-tested
# once it lands. Detected statically: phantomjs installed in the task Dockerfile.
BROWSER_PENDING = {
    "XBEN-004-24", "XBEN-008-24", "XBEN-010-24", "XBEN-011-24", "XBEN-012-24",
    "XBEN-013-24", "XBEN-015-24", "XBEN-016-24", "XBEN-017-24", "XBEN-018-24",
    "XBEN-046-24", "XBEN-047-24", "XBEN-048-24", "XBEN-049-24", "XBEN-050-24",
    "XBEN-051-24", "XBEN-059-24", "XBEN-062-24", "XBEN-064-24", "XBEN-065-24",
    "XBEN-070-24", "XBEN-074-24", "XBEN-087-24",
}

# fault classes excluded from the scoring denominator: the agent never got a fair
# attempt (broken target infrastructure, or a capability we don't have yet).
NON_SCORING_CLASSES = {"target_broken", "browser_pending"}


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------
def discover_runs(runs_dir: str) -> dict[str, list[tuple[str, str]]]:
    """Return {task: [(stamp, evidence_path), ...]} sorted oldest->newest."""
    by_task: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if not os.path.isdir(runs_dir):
        return by_task
    for name in os.listdir(runs_dir):
        m = STAMP_RE.match(name)
        if not m:
            continue
        task, stamp = m.group(1), m.group(2)
        ev = os.path.join(runs_dir, name, "evidence.json")
        by_task[task].append((stamp, ev))
    for runs in by_task.values():
        runs.sort(key=lambda t: t[0])
    return by_task


def load_evidence(path: str) -> dict | None:
    """Load an evidence.json, returning None if missing, half-written, or malformed.

    Guards against the concurrent live re-run: a run dir may exist with no
    evidence.json yet, or a partially flushed one. We require valid JSON plus the
    minimal fields we rely on.
    """
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "outcome" not in data or "task" not in data:
        return None
    return data


def newest_complete(runs: list[tuple[str, str]]) -> tuple[str, dict] | None:
    """Newest run whose evidence.json is present, valid and complete.

    Falls back to older runs when the newest dir is still in progress.
    Returns (stamp, evidence) or None if the task has no complete run yet.
    """
    for stamp, path in reversed(runs):
        ev = load_evidence(path)
        if ev is not None:
            return stamp, ev
    return None


# ---------------------------------------------------------------------------
# Record building (the public-safe projection)
# ---------------------------------------------------------------------------
def norm_level(val) -> str:
    if val is None or val == "":
        return "unknown"
    return str(val).strip()


def safe_usage(usage) -> dict:
    out = {k: 0 for k in SAFE_USAGE_FIELDS}
    if isinstance(usage, dict):
        for k in SAFE_USAGE_FIELDS:
            v = usage.get(k, 0)
            if isinstance(v, (int, float)):
                out[k] = v
    return out


def build_record(task: str, stamp: str, ev: dict) -> dict:
    outcome = ev.get("outcome", "runtime_fault")
    fault_class, fault_subtype = FAULT_MAP.get(outcome, ("runtime_fault", None))
    # A browser-dependent task is excluded regardless of its recorded outcome —
    # this also neutralises any historical false-positive "solved" that came from
    # reading harness state rather than exploiting the (unreachable) checker.
    if task in BROWSER_PENDING:
        fault_class, fault_subtype = "browser_pending", "needs_headless_browser"
    tags = ev.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    rec = {
        "task": task,
        "name": ev.get("name", task),
        "level": norm_level(ev.get("level")),
        "tags": [str(t) for t in tags],
        "outcome": outcome,
        "fault_class": fault_class,
        "fault_subtype": fault_subtype,
        "solved": fault_class == "solved",
        "valid_for_score": fault_class not in NON_SCORING_CLASSES,
        "duration_s": ev.get("duration_s"),
        "model": ev.get("model"),
        "provider": ev.get("provider"),
        "usage": safe_usage(ev.get("usage")),
        "run_date": stamp[:10],  # date only, provenance; not sensitive
    }
    return rec


# ---------------------------------------------------------------------------
# Snapshot computation
# ---------------------------------------------------------------------------
def compute_snapshot(by_task: dict[str, list[tuple[str, str]]], cutoff_date: str | None = None):
    """Build the per-task record list for a snapshot.

    cutoff_date (YYYY-MM-DD) restricts to runs on or before that date, used to
    reconstruct the historical baseline. Returns (records, in_progress_tasks).
    """
    records = []
    in_progress = []
    for task in sorted(by_task):
        runs = by_task[task]
        if cutoff_date is not None:
            runs = [r for r in runs if r[0][:10] <= cutoff_date]
        if not runs:
            continue
        picked = newest_complete(runs)
        if picked is None:
            in_progress.append(task)
            continue
        stamp, ev = picked
        # If the absolute-newest dir differs from the one we picked, this task
        # currently has an in-progress re-run we deliberately skipped.
        if stamp != runs[-1][0]:
            in_progress.append(task)
        records.append(build_record(task, stamp, ev))
    return records, in_progress


def _rate(solved: int, denom: int) -> float:
    return round(100.0 * solved / denom, 1) if denom else 0.0


def summarize(records: list[dict], in_progress: list[str], latest_data_date: str | None) -> dict:
    total = len(records)
    fault_counts = Counter(r["fault_class"] for r in records)
    solved = fault_counts.get("solved", 0)
    target_broken = fault_counts.get("target_broken", 0)
    browser_pending = fault_counts.get("browser_pending", 0)
    non_scoring = sum(fault_counts.get(c, 0) for c in NON_SCORING_CLASSES)
    fair_denom = total - non_scoring

    tokens_total = sum(r["usage"]["total_tokens"] for r in records)
    prompt_total = sum(r["usage"]["prompt_tokens"] for r in records)
    completion_total = sum(r["usage"]["completion_tokens"] for r in records)
    cached_total = sum(r["usage"]["cached_tokens"] for r in records)
    cost_total = round(sum(r["usage"]["cost_usd"] for r in records), 6)

    # per-level
    per_level: dict[str, dict] = {}
    lvl_groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        lvl_groups[r["level"]].append(r)
    for lvl in sorted(lvl_groups):
        rs = lvl_groups[lvl]
        s = sum(1 for r in rs if r["solved"])
        tb = sum(1 for r in rs if r["fault_class"] == "target_broken")
        denom = sum(1 for r in rs if r["fault_class"] not in NON_SCORING_CLASSES)
        per_level[lvl] = {
            "total": len(rs),
            "solved": s,
            "target_broken": tb,
            "fair_denominator": denom,
            "solve_rate_pct": _rate(s, denom),
        }

    # per-tag (a task may carry several tags -> counted under each)
    tag_groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        for t in r["tags"]:
            tag_groups[t].append(r)
    per_tag: dict[str, dict] = {}
    for tag in sorted(tag_groups):
        rs = tag_groups[tag]
        s = sum(1 for r in rs if r["solved"])
        tb = sum(1 for r in rs if r["fault_class"] == "target_broken")
        denom = sum(1 for r in rs if r["fault_class"] not in NON_SCORING_CLASSES)
        per_tag[tag] = {
            "total": len(rs),
            "solved": s,
            "target_broken": tb,
            "fair_denominator": denom,
            "solve_rate_pct": _rate(s, denom),
        }

    target_broken_tasks = sorted(
        ((r["task"], r["fault_subtype"]) for r in records if r["fault_class"] == "target_broken"),
        key=lambda t: t[0],
    )
    browser_pending_tasks = sorted(
        r["task"] for r in records if r["fault_class"] == "browser_pending"
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_data_date": latest_data_date,
        "tasks_scored": total,
        "solved": solved,
        "fair_denominator": fair_denom,
        "solve_rate_pct": _rate(solved, fair_denom),
        "target_broken_excluded": target_broken,
        "browser_pending_excluded": browser_pending,
        "browser_pending_tasks": browser_pending_tasks,
        "tasks_in_progress_rerun": len(in_progress),
        "in_progress_tasks": sorted(in_progress),
        "tokens": {
            "total": tokens_total,
            "prompt": prompt_total,
            "completion": completion_total,
            "cached": cached_total,
        },
        "cost_usd_total": cost_total,
        "fault_class_counts": dict(sorted(fault_counts.items())),
        "target_broken_tasks": [{"task": t, "subtype": st} for t, st in target_broken_tasks],
        "per_level": per_level,
        "per_tag": per_tag,
    }


# ---------------------------------------------------------------------------
# History (run-over-time) -- upsert per snapshot label, so the curve extends
# ---------------------------------------------------------------------------
def history_point(label: str, date: str, records: list[dict]) -> dict:
    total = len(records)
    solved = sum(1 for r in records if r["solved"])
    tb = sum(1 for r in records if r["fault_class"] == "target_broken")
    denom = sum(1 for r in records if r["fault_class"] not in NON_SCORING_CLASSES)
    return {
        "label": label,
        "date": date,
        "tasks_scored": total,
        "solved": solved,
        "fair_denominator": denom,
        "solve_rate_pct": _rate(solved, denom),
        "tokens_total": sum(r["usage"]["total_tokens"] for r in records),
        "cost_usd_total": round(sum(r["usage"]["cost_usd"] for r in records), 6),
    }


def upsert_history(hist_path: str, current: dict, seed_points: list[dict]) -> list[dict]:
    """Load history, seed baseline points on first creation, upsert current by label."""
    history: list[dict] = []
    if os.path.isfile(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                history = loaded
        except (OSError, ValueError):
            history = []

    by_label = {p.get("label"): p for p in history if isinstance(p, dict)}

    # First creation: seed real historical baseline points if not present.
    if not history:
        for sp in seed_points:
            by_label.setdefault(sp["label"], sp)

    by_label[current["label"]] = current  # upsert (idempotent per label)

    merged = list(by_label.values())
    merged.sort(key=lambda p: (p.get("date", ""), p.get("label", "")))
    return merged


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------
# neutral, muted palette
C_SOLVED = "#3f8f5b"
C_UNSOLVED = "#c0603a"
C_TIMEOUT = "#d6a13d"
C_PROVIDER = "#7d7f86"
C_TARGET = "#b0b3ba"
C_RUNTIME = "#5a5c63"
C_BAR = "#3f6f9f"
C_ACCENT = "#c0603a"
C_GRID = "#dddddd"
C_TEXT = "#222222"

FAULT_COLORS = {
    "solved": C_SOLVED,
    "unsolved": C_UNSOLVED,
    "timeout": C_TIMEOUT,
    "provider_fault": C_PROVIDER,
    "target_broken": C_TARGET,
    "runtime_fault": C_RUNTIME,
    "browser_pending": "#8b7fd6",  # excluded pending headless-browser tooling
}
FAULT_ORDER = ["solved", "unsolved", "timeout", "provider_fault", "runtime_fault", "target_broken", "browser_pending"]


def _style():
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "figure.dpi": 120,
            "savefig.dpi": 120,
            "font.size": 11,
            "font.family": "DejaVu Sans",
            "axes.edgecolor": "#888888",
            "axes.labelcolor": C_TEXT,
            "axes.titlecolor": C_TEXT,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "text.color": C_TEXT,
            "xtick.color": C_TEXT,
            "ytick.color": C_TEXT,
            "axes.grid": True,
            "grid.color": C_GRID,
            "grid.linewidth": 0.8,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def _save(fig, out_dir: str, name: str):
    png = os.path.join(out_dir, name + ".png")
    svg = os.path.join(out_dir, name + ".svg")
    fig.savefig(png, bbox_inches="tight", facecolor="white")
    try:
        fig.savefig(svg, bbox_inches="tight", facecolor="white")
    except Exception:
        pass
    import matplotlib.pyplot as plt

    plt.close(fig)
    return name


def graph_outcome_mix(records, summary, out_dir):
    import matplotlib.pyplot as plt

    counts = summary["fault_class_counts"]
    labels, sizes, colors = [], [], []
    for fc in FAULT_ORDER:
        if counts.get(fc):
            labels.append(f"{fc.replace('_', ' ')} ({counts[fc]})")
            sizes.append(counts[fc])
            colors.append(FAULT_COLORS[fc])

    fig, ax = plt.subplots(figsize=(6.6, 5.2))
    wedges, _ = ax.pie(
        sizes,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=1.5),
    )
    ax.set(aspect="equal")
    ax.grid(False)
    center = f"{summary['solved']}/{summary['fair_denominator']}\nsolved"
    ax.text(0, 0, center, ha="center", va="center", fontsize=15, fontweight="bold")
    ax.legend(
        wedges,
        labels,
        loc="center left",
        bbox_to_anchor=(1.0, 0.5),
        frameon=False,
        fontsize=10,
    )
    ax.set_title("Outcome mix (newest complete run per task)", pad=14)
    fig.text(
        0.5,
        -0.02,
        f"solve-rate {summary['solve_rate_pct']}%  ·  excluded from scoring: "
        f"{summary.get('browser_pending_excluded', 0)} browser-pending, "
        f"{summary['target_broken_excluded']} target-broken",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    return _save(fig, out_dir, "outcome_mix")


def graph_solve_by_level(summary, out_dir):
    import matplotlib.pyplot as plt

    per = summary["per_level"]

    def keyf(k):
        return (0, int(k)) if k.isdigit() else (1, k)

    levels = sorted(per, key=keyf)
    rates = [per[l]["solve_rate_pct"] for l in levels]
    solved = [per[l]["solved"] for l in levels]
    denom = [per[l]["fair_denominator"] for l in levels]

    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    x = range(len(levels))
    bars = ax.bar(x, rates, color=C_BAR, width=0.62)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"L{l}" for l in levels])
    ax.set_ylabel("solve-rate (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Solve-rate by level")
    ax.grid(axis="x", visible=False)
    for b, s, d in zip(bars, solved, denom):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + 2,
            f"{s}/{d}",
            ha="center",
            va="bottom",
            fontsize=9,
            color="#444444",
        )
    return _save(fig, out_dir, "solve_rate_by_level")


def graph_solve_by_tag(summary, out_dir):
    import matplotlib.pyplot as plt

    per = summary["per_tag"]
    # sort by fair_denominator desc then rate, keep tags with >=1 fair attempt
    tags = [t for t in per if per[t]["fair_denominator"] > 0]
    tags.sort(key=lambda t: (per[t]["fair_denominator"], per[t]["solve_rate_pct"]))
    rates = [per[t]["solve_rate_pct"] for t in tags]
    labels = [f"{t}  ({per[t]['solved']}/{per[t]['fair_denominator']})" for t in tags]

    fig, ax = plt.subplots(figsize=(7.6, max(4.0, 0.42 * len(tags) + 1.2)))
    y = range(len(tags))
    ax.barh(list(y), rates, color=C_BAR, height=0.66)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("solve-rate (%)")
    ax.set_xlim(0, 105)
    ax.set_title("Solve-rate by vulnerability tag")
    ax.grid(axis="y", visible=False)
    return _save(fig, out_dir, "solve_rate_by_tag")


def graph_token_usage(records, out_dir):
    import matplotlib.pyplot as plt

    rs = [r for r in records if r["usage"]["total_tokens"] > 0]
    rs.sort(key=lambda r: r["usage"]["total_tokens"])
    vals = [r["usage"]["total_tokens"] / 1e6 for r in rs]  # millions
    if not vals:
        return None
    # highlight heavy outliers (>= p90)
    srt = sorted(vals)
    p90 = srt[int(0.9 * (len(srt) - 1))]
    colors = [C_ACCENT if v >= p90 else C_BAR for v in vals]

    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    x = range(len(vals))
    ax.bar(x, vals, color=colors, width=1.0)
    ax.set_ylabel("total tokens (millions)")
    ax.set_xlabel("tasks (sorted by token usage, ascending)")
    ax.set_title("Per-task token usage  ·  heavy outliers highlighted")
    ax.grid(axis="x", visible=False)
    ax.margins(x=0.005)
    # label the 3 heaviest tasks; stagger horizontally to avoid overlap
    heavy = [r for r in rs if r["usage"]["total_tokens"] / 1e6 >= p90]
    for i, r in enumerate(reversed(heavy[-3:])):
        idx = rs.index(r)
        tid = r["task"].replace("XBEN-", "").lstrip("0").replace("-24", "") or r["task"]
        ax.annotate(
            tid,
            (idx, vals[idx]),
            textcoords="offset points",
            xytext=(-6, 6 + i * 12),
            ha="right",
            fontsize=8,
            color=C_ACCENT,
            arrowprops=dict(arrowstyle="-", color=C_ACCENT, lw=0.6),
        )
    med = srt[len(srt) // 2]
    ax.axhline(med, color="#999999", linewidth=0.9, linestyle="--")
    ax.text(0, med, f" median {med:.2f}M", va="bottom", ha="left", fontsize=8, color="#777777")
    return _save(fig, out_dir, "token_usage")


def graph_history(history, out_dir):
    import matplotlib.pyplot as plt

    if not history:
        return None
    labels = [p["label"] for p in history]
    solved = [p["solved"] for p in history]
    denom = [p.get("fair_denominator", 0) for p in history]

    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    x = range(len(history))
    ax.plot(x, solved, marker="o", color=C_SOLVED, linewidth=2.2, markersize=7, label="solved")
    ax.plot(x, denom, marker="s", color=C_TARGET, linewidth=1.4, linestyle="--", markersize=5, label="fair denominator")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
    ax.set_ylabel("tasks")
    ax.set_title("Solved count by benchmark run")
    ax.grid(axis="x", visible=False)
    ymax = max(denom + solved) if (denom or solved) else 1
    ax.set_ylim(0, ymax * 1.15 + 1)
    for xi, s in zip(x, solved):
        ax.annotate(str(s), (xi, s), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9, color=C_SOLVED, fontweight="bold")
    ax.legend(frameon=False, loc="lower right", fontsize=9)
    return _save(fig, out_dir, "history")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def render_readme(summary: dict, history: list[dict], out_dir: str) -> None:
    s = summary
    tb = s["target_broken_tasks"]

    def keyf(k):
        return (0, int(k)) if k.isdigit() else (1, k)

    lines = []
    A = lines.append
    A("# XBOW-104 Benchmark Results")
    A("")
    A(f"_Generated {s['generated_at']} · latest data {s['latest_data_date']}_")
    A("")
    A("Autonomous web-exploitation benchmark (104 tasks). Numbers below are the")
    A("**newest completed run per task**; a live re-run may still be in progress")
    A("for some tasks, in which case the most recent completed run is used.")
    A("")
    bp = s.get("browser_pending_excluded", 0)
    if bp:
        A(f"> **Scope note.** {bp} tasks require a working headless browser (the target")
        A("> ships a PhantomJS XSS-checker bot). The agent has no browser capability")
        A("> yet — that tooling is in progress — so these get no fair attempt and are")
        A("> excluded from the solve-rate, to be re-tested once it lands. The rate below")
        A("> is therefore over the tasks the agent can currently attempt.")
        A("")
    A("## Headline")
    A("")
    A("| Metric | Value |")
    A("| --- | --- |")
    A(f"| Solved | **{s['solved']} / {s['fair_denominator']}** (fair attempts) |")
    A(f"| Solve-rate | **{s['solve_rate_pct']}%** |")
    A(f"| Tasks scored | {s['tasks_scored']} |")
    A(f"| Target-broken (excluded from scoring) | {s['target_broken_excluded']} |")
    A(f"| Browser-pending (excluded; needs headless browser) | {s.get('browser_pending_excluded', 0)} |")
    A(f"| Re-runs in progress | {s['tasks_in_progress_rerun']} |")
    A(f"| Total tokens | {s['tokens']['total']:,} |")
    A(f"| Total cost (USD) | ${s['cost_usd_total']:.2f} |")
    A("")
    A("## Outcome breakdown")
    A("")
    A("| Fault class | Count |")
    A("| --- | --- |")
    for k, v in s["fault_class_counts"].items():
        A(f"| {k.replace('_', ' ')} | {v} |")
    A("")
    A("![Outcome mix](graphs/outcome_mix.png)")
    A("")
    A("## Solve-rate by level")
    A("")
    A("| Level | Solved | Fair attempts | Solve-rate |")
    A("| --- | --- | --- | --- |")
    for lvl in sorted(s["per_level"], key=keyf):
        d = s["per_level"][lvl]
        A(f"| L{lvl} | {d['solved']} | {d['fair_denominator']} | {d['solve_rate_pct']}% |")
    A("")
    A("![Solve-rate by level](graphs/solve_rate_by_level.png)")
    A("")
    A("## Solve-rate by vulnerability tag")
    A("")
    A("| Tag | Solved | Fair attempts | Solve-rate |")
    A("| --- | --- | --- | --- |")
    scored_tags = [t for t in s["per_tag"] if s["per_tag"][t]["fair_denominator"] > 0]
    for tag in sorted(scored_tags, key=lambda t: (-s["per_tag"][t]["fair_denominator"], t)):
        d = s["per_tag"][tag]
        A(f"| {tag} | {d['solved']} | {d['fair_denominator']} | {d['solve_rate_pct']}% |")
    A("")
    A("![Solve-rate by tag](graphs/solve_rate_by_tag.png)")
    A("")
    A("## Token usage")
    A("")
    A("![Per-task token usage](graphs/token_usage.png)")
    A("")
    A("## Benchmark runs")
    A("")
    A("_This is the **first pass** over the suite (Run 1). It happens to span two")
    A("calendar days, but that is a single run, not two. The series is keyed by run")
    A("number and extends only when the suite is deliberately re-measured after")
    A("adopting improvements — it is not a per-day timeline._")
    A("")
    A("![Solved by benchmark run](graphs/history.png)")
    A("")
    if history:
        A("| Run | Solved | Fair denom | Solve-rate | Tokens |")
        A("| --- | --- | --- | --- | --- |")
        for p in history:
            A(
                f"| {p['label']} | {p['solved']} | {p['fair_denominator']} | "
                f"{p['solve_rate_pct']}% | {p['tokens_total']:,} |"
            )
        A("")
    A("## Tasks excluded from scoring (target-broken)")
    A("")
    if tb:
        A("These tasks are excluded from the denominator: the challenge target failed")
        A("to build or start, so the agent never got a fair attempt.")
        A("")
        A("| Task | Reason |")
        A("| --- | --- |")
        for t in tb:
            A(f"| {t['task']} | {t['subtype']} |")
    else:
        A("_None._")
    A("")
    A("---")
    A("")
    A("_Only clean result metrics are published here. Transcripts, prompts, flag")
    A("values, telemetry, hosts and internal paths are never included._")
    A("")

    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build public-safe XBOW-104 report.")
    ap.add_argument("--artifacts", default=DEFAULT_ARTIFACTS, help="artifacts dir (read-only)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output dir (public/benchmarks/xbow104)")
    ap.add_argument("--label", default=None, help="override snapshot label for history")
    args = ap.parse_args(argv)

    runs_dir = os.path.join(args.artifacts, "runs")
    data_dir = os.path.join(args.out, "data")
    graphs_dir = os.path.join(args.out, "graphs")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(graphs_dir, exist_ok=True)

    by_task = discover_runs(runs_dir)
    if not by_task:
        print(f"[!] no run dirs found under {runs_dir}", file=sys.stderr)
        return 2

    records, in_progress = compute_snapshot(by_task)
    all_dates = [s for runs in by_task.values() for s, _ in runs]
    latest_data_date = max(all_dates)[:10] if all_dates else None

    summary = summarize(records, in_progress, latest_data_date)

    # ---- history: one point per *benchmark run*, labelled by run number ----
    # This is a single first pass over the suite; it happens to span two calendar
    # days, but that is NOT two runs. We therefore label by run number ("Run 1")
    # rather than by date, so the curve reflects intentional re-measurements
    # (after adopting improvements) and not the wall-clock the batch took.
    # Pass --label "Run N" on a later iteration to extend the curve.
    current_label = args.label or "Run 1"
    current_pt = history_point(current_label, latest_data_date or "", records)
    hist_path = os.path.join(data_dir, "history.json")
    history = upsert_history(hist_path, current_pt, [])

    # ---- write data ----
    write_json(os.path.join(data_dir, "results.json"), records)
    write_json(os.path.join(data_dir, "summary.json"), summary)
    write_json(hist_path, history)

    # ---- graphs ----
    _style()
    made = []
    for fn in (
        lambda: graph_outcome_mix(records, summary, graphs_dir),
        lambda: graph_solve_by_level(summary, graphs_dir),
        lambda: graph_solve_by_tag(summary, graphs_dir),
        lambda: graph_token_usage(records, graphs_dir),
        lambda: graph_history(history, graphs_dir),
    ):
        name = fn()
        if name:
            made.append(name)

    # ---- readme ----
    render_readme(summary, history, args.out)

    # ---- console summary ----
    print("XBOW-104 report built")
    print(f"  artifacts : {args.artifacts}")
    print(f"  output    : {args.out}")
    print(f"  scored    : {summary['tasks_scored']} tasks  "
          f"(in-progress re-runs skipped: {summary['tasks_in_progress_rerun']})")
    print(f"  solved    : {summary['solved']}/{summary['fair_denominator']} "
          f"= {summary['solve_rate_pct']}%  "
          f"(target-broken excluded: {summary['target_broken_excluded']})")
    print(f"  tokens    : {summary['tokens']['total']:,}   cost ${summary['cost_usd_total']:.2f}")
    print(f"  fault mix : {summary['fault_class_counts']}")
    print(f"  graphs    : {', '.join(made)}")
    print(f"  history   : {len(history)} point(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
