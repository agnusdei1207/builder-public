<div align="center">
  <h1>Autonomous Penetration-Testing Agent</h1>
  <p>Local-first, lightweight, fast.</p>
  <p>
    <a href="https://www.npmjs.com/package/pentesting"><img src="https://img.shields.io/npm/v/pentesting.svg" alt="npm"></a>
    <a href="https://www.npmjs.com/package/pentesting"><img src="https://img.shields.io/npm/dm/pentesting.svg" alt="npm downloads"></a>
    <a href="https://hub.docker.com/r/agnusdei1207/pentesting"><img src="https://img.shields.io/docker/pulls/agnusdei1207/pentesting.svg" alt="Docker pulls"></a>
    <a href="https://www.rust-lang.org/"><img src="https://img.shields.io/badge/built%20with-Rust-000000?logo=rust" alt="Built with Rust"></a>
    <a href="https://agnusdei1207.github.io/pentesting-public/explorer.html"><img src="https://img.shields.io/badge/3D%20Architecture-Explorer-3b82f6.svg" alt="3D Architecture Explorer"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  </p>
</div>

---

## 🚀 Quick Start

```bash
npm install -g pentesting && pentesting   # install and run
npx pentesting                            # or run once, no install
pnpm dlx pentesting
yarn dlx pentesting
```

## 🐳 Run with Docker

```bash
export PENTESTING_IMAGE="pentesting:latest"  # published reference or local tag
export OPENAI_API_KEY="sk-or-..."              # any OpenAI-compatible provider key (e.g. OpenRouter)
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="minimax/minimax-m3:free"  # the model used in this benchmark
export OPENAI_MAX_TOKENS="16384"
export BRAVE_SEARCH_API_KEY="bsk_..."          # optional: Brave Search API key

docker run -it --rm \
  -v "$(pwd):/workspace" \
  -v pentesting-config:/root/.pentesting \
  -w /workspace \
  -e OPENAI_API_KEY \
  -e OPENAI_BASE_URL \
  -e OPENAI_MODEL \
  -e OPENAI_MAX_TOKENS \
  -e BRAVE_SEARCH_API_KEY \
  "$PENTESTING_IMAGE"
```

## 🎯 Your first mission

```bash
/model                                   # pick a provider / model (or preset OPENAI_* env)
/goal 10.10.11.20:8080 get root flag     # record the mission
/auto                                    # explicitly start or stop autonomous continuation
```

# Benchmark — XBOW-104

*An evidence-first benchmark of the agent's capability: given only a target URL,
it reconnoiters the application, forms and tests hypotheses, and attempts to
capture a hidden flag — with no human in the loop.*

## Abstract

Pentesting is an autonomous agent that performs black-box web exploitation:
given only a target URL, it reconnoiters the application, forms and tests
hypotheses, and attempts to capture a hidden flag — with no human in the loop.
This page reports its measured capability on the **XBOW-104** suite (104
single-flag web-exploitation CTFs) and treats the benchmark not as a score but
as **data-driven development research**: every run is analysed to locate where
the system, not the model, is the limiting factor.

## Results

On XBOW-104, run on a **free-tier `minimax/minimax-m3:free` backbone at $0 total cost** (MiniMax-M3, 1M context, via OpenRouter):

- **Solve rate: 67 / 81 fair attempts (82.7%).**
- 104 tasks scored; **23 excluded** as *browser-pending* because browser tooling
  was unavailable during this run. They received no fair attempt and remain
  pending remeasurement with the current browser-verification capability.
- A flag counts as solved **only when it is captured from the live target** —
  flags recoverable from harness state are rejected.

![Outcome mix across 104 tasks](https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/benchmarks/xbow104/graphs/outcome_mix.png)

*Figure 1. Outcome mix across the 104 tasks.*

![Solve rate by difficulty level](https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/benchmarks/xbow104/graphs/solve_rate_by_level.png)

*Figure 2. Solve rate by difficulty level.*

![Solve rate by vulnerability class](https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/benchmarks/xbow104/graphs/solve_rate_by_tag.png)

*Figure 3. Solve rate by vulnerability class (tag).*

![Token usage per task](https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/benchmarks/xbow104/graphs/token_usage.png)

*Figure 4. Token usage per task — a few looping runs dominate the cost.*

![Solve count across runs](https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/benchmarks/xbow104/graphs/history.png)

*Figure 5. Solve count by benchmark run. This is Run 1 — the first pass over the suite; the series extends only as the suite is re-measured after adopting improvements (keyed by run, not by date).*

Full breakdown, per-vulnerability-class rates, and token usage are in the
[live report](https://agnusdei1207.github.io/pentesting-public/) and the
[benchmark data](https://github.com/agnusdei1207/pentesting-public/blob/main/benchmarks/xbow104/README.md). *(Figures regenerate from the
raw run evidence; numbers reflect the latest published run.)*

## Methodology

The benchmark is one process in a larger data-driven research loop:

**run → per-task transcript analysis → root-cause classification → aggregation → adoption filtering → next iteration.**

Each success and failure is classified by *why* it happened —
**model limitation**, **system-design limitation** (our runtime: orchestration,
prompting, tooling, loop control, context management), **pure difficulty**, or
**benchmark-infrastructure fault** — so we can separate "needs a stronger model"
from "needs a better system." The system-design findings become a prioritised
improvement backlog that drives the next iteration and is re-measured on the
same suite.

**Model-limitation evidence (companion experiment).** The ceiling here is the
model, not the system. In a separate controlled experiment on the same backbone,
even when a *decisive, unambiguous clue* was surfaced and injected directly into
the model's context, `minimax/minimax-m3:free` failed to act on that clue
**≈42% of the time** — i.e. it ignored a solution it had already been handed.
That is a clear model-level ceiling, independent of orchestration or tooling: a
stronger backbone would lift those cases with no system change. It also frames
the XBOW-104 non-solves above — when a task is missed, the evidence points at
the model's reasoning, not the runtime.

## Reproducibility

- **Backbone:** a single fixed model per run, recorded in every run's evidence.
- **Environment:** each target runs in its own pinned container; flags are
  deterministic per task; runs are headless with no interactive input.
- **Evidence:** every attempt retains its full transcript, tool telemetry, and a
  content-integrity manifest. Metrics published here are clean aggregates only —
  no transcripts, prompts, flag values, or internal paths.

## Links

- **Live report:** <https://agnusdei1207.github.io/pentesting-public/>
- **3D architecture explorer:** <https://agnusdei1207.github.io/pentesting-public/explorer.html>
- **Source (mirror):** this repository · **Package:** `npm i -g pentesting`

<sub>Closed-core agent distributed as a native binary; this public mirror and its installers are MIT-licensed. Built in Rust · benchmark backbone: minimax/minimax-m3:free (MiniMax-M3, OpenRouter free tier).</sub>
