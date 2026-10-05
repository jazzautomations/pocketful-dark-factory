# FACTORY.md — a three-seat headless factory on band-sdk

## 0. Verify it in three commands

From a fresh clone, with the organiser's pinned harness (`803560d2`) in `../dark-factory-wearedevs`:

```sh
python -m harness check . --track pocketful                               # gates 1, 2 and 4: mandates, room.json, @handle exchange
python -m harness run --track pocketful --repo . --all --mode isolated    # every stage folder, built and tested like a judge does
python tools/factory/usage.py room.json                                   # minutes per stage, tokens per seat, rejections, from the room itself
```

Expected: `ok` from the check; `claims stage 1` … `claims stage 4` from the run; the usage table reproduced in section 4.

This document is enough to stand the factory up again on a plain Linux VM and point it at a different problem. Nothing in it is specific to the track we entered; the track lives entirely in the dispatch message pasted into the room.

## 1. Shape

Three seats, one room, one dispatch.

![Architecture](docs/diagrams/architecture.svg)

| Seat | Owns | Harness | Model |
|---|---|---|---|
| `coordinator` | scope, sequencing, complete handoffs, acceptance, reporting | Claude Code (headless, via `band-sdk` `ClaudeSDKAdapter`) | `claude-sonnet-5-5` |
| `implementer` | one scoped item at a time, builds and self-checks, commits, reports the hash | Claude Code (headless) | `claude-opus-5-5` |
| `reviewer` | independent reproduction on the exact revision, probes beyond the shipped checks, ACCEPT/REJECT with evidence | Claude Code (headless) | `claude-opus-5-5` |

The seats are Band **external agents**. Each one is a Python process running `seat.py`: the Band SDK keeps the WebSocket to the room, and every message addressed to the seat becomes one Claude Code turn with the seat's mandate as its system section. There is no Band Desktop, no GUI and no human in the loop after the dispatch. Seats only receive messages that mention them, so the coordinator is the only seat that ever sees the human dispatch, and every handoff must carry the whole task and the whole specification (the mandates enforce this).

## 2. Setup (about 15 minutes)

Host used: Oracle Cloud VM, x86-64, 4 vCPU, 5.8 GiB RAM, Ubuntu 24.04, Docker 29. A 37 GiB ARM host was prepared as a fallback and not needed.

```sh
# 1. Claude Code CLI, logged in (subscription)
curl -fsSL https://claude.ai/install.sh | bash && claude   # /login once

# 2. Band SDK with the Claude Code adapter, plus the Band MCP server for the human side
python3 -m venv ~/band-venv && ~/band-venv/bin/pip install "band-sdk[claude-sdk]" band-mcp mcp

# 3. Three external agents on app.band.ai (Agents -> New Agent -> External). Keep the keys outside git:
#    ~/.config/band/env        BAND_BASE_URL, BAND_USER_KEY (human key, used only to create the room and send the dispatch)
#    ~/.config/band/seats.json {seat: {"data": {"agent": {"id": ...}, "credentials": {"api_key": ...}}}}

# 4. Official harness, pinned, next to the workspace
git clone https://github.com/band-ai/dark-factory-wearedevs ~/pf-harness && (cd ~/pf-harness && git checkout 803560d2 && python3 -m venv .venv && .venv/bin/pip install -r harness/requirements.txt)

# 5. Empty result repository
mkdir -p ~/band-work/result && git -C ~/band-work/result init -b main
```

`seat.py` (in `tools/factory/`) builds the adapter like this:

```python
ClaudeSDKAdapterConfig(
    model=<from the mandate's Model: line>,
    custom_section=<the seat's mandate file>,
    permission_mode=ClaudePermissionMode.AUTO,   # no bypass: Claude Code's own permission policy decides each tool call
    cwd=<result repository>,
    setting_sources=(),                           # no host skills/settings leak into the seat
    turn_timeout_s=5400,
    cli=ClaudeCLIOptions(env={GIT_AUTHOR_NAME: <seat>, ...}),
)
```

Each seat commits under its own git identity, so the history shows who did what without any post-processing.

## 3. Running a job

![One stage through the factory](docs/diagrams/stage-flow.svg)

```sh
tools/factory/start.sh <dir with mandates/> <absolute result repo>     # starts the three seats (nohup, one log each)
tools/factory/dispatch.py <room id> <dispatch.txt>                     # one message, mentions only the coordinator
```

The dispatch is the only human input. Ours named the track, the absolute paths of the specifications and the result repository, the check command, the folder-per-stage rule and the acceptance flow, and told the coordinator to run all four stages in order. After that we read the room and did nothing else.

## 4. What the room shows (the run we submit)

![Timeline of the submitted run](docs/diagrams/timeline.svg)

Measured from `room.json` (every number below is in the file; `tools/factory/usage.py` recomputes them):

| | |
|---|---|
| Dispatch | 2026-10-04 20:51 UTC |
| Stage 1 accepted | +24 min |
| Stage 2 accepted | +45 min |
| Stage 3 accepted | +62 min |
| Stage 4 accepted | +76 min (final report 22:07 UTC) |
| Rejections by the reviewer | 0 (every stage accepted on first review) |
| Human messages after the dispatch | 0 |

Token usage reported by the seats into the room (`band_usage` events, cumulative over the run):

| Seat | Turns | Output tokens | Cache writes | Cache reads |
|---|---|---|---|---|
| coordinator | 9 | 123,280 | 182,555 | 4,127,192 |
| implementer | 18 | 180,625 | 347,262 | 21,347,966 |
| reviewer | 18 | 101,925 | 261,334 | 13,553,339 |

Cost: the seats ran on a Claude subscription, so there is no per-token invoice; the table above is the measured spend. The adapter's own per-turn estimate at list prices (logged as `Complete - <ms>, $<cost>` in each seat's log) sums to coordinator $14.14, implementer $99.54, reviewer $62.52: about **$176 for the whole run**, 76 minutes of wall clock.

## 5. Design choices and what they cost

- **Headless seats instead of Band Desktop.** We could not run Band Desktop on any of our machines. The SDK adapter gives the same room semantics (addressed messages, `@handle` mentions, tool-call and usage events) and runs on a server. Cost: we had to write `seat.py` and read the adapter source; no documentation covers the Claude Code adapter in detail.
- **Full specification in every handoff.** Seats cannot read the room, so the coordinator pastes the complete task and the complete spec of every stage so far into each handoff, split into numbered parts when needed (the stage 2 handoff was 4 parts). Cost: large prompts, visible in the cache-write numbers; benefit: the implementer and reviewer never work from a summary.
- **Reviewer reproduces, never trusts.** The reviewer exports the exact revision with `git archive`, builds from the Dockerfile, follows `RUN.md`, runs the official harness in isolated mode with a fresh output directory, and then probes the spec clauses the shipped checks never touch (concurrency races, rounding, error precedence, restarts, earlier stages still holding). Cost: the reviewer spends as many tokens as the implementer; benefit: acceptance means something.
- **Opus for the two seats that touch code, Sonnet for coordination.** The coordinator's job is sequencing and faithful copying; the implementer and reviewer need the stronger model to read a 25 KB specification and find what the sample checks do not ask.
- **Permission mode `auto`, not bypass.** Claude Code's own permission policy evaluates each tool call. It never blocked a build, a test or a commit during the run.

## 6. How the factory catches bad work

- The reviewer's ACCEPT must name the revision hash, the exact harness command and the counts, so a claimed pass without evidence is a REJECT by the mandate.
- A REJECT carries command, expected and observed output and the spec clause; the implementer fixes exactly that and reports a new hash; the reviewer checks the new hash, not the working tree.
- Stage folders are frozen after acceptance: the reviewer checks that earlier folders are byte-identical and that no nested `.git`, submodule, symlink or credential entered the repository.
- The coordinator posts the stage outcome to the room owner with remaining uncertainty listed, so weak spots are on record even when the stage is accepted (see the stage 1 note about `limit=4%0A`, carried as a fix into the stage 2 copy).

## 7. What we tried that failed

- Band Desktop on Linux without a GPU and on a 3.7 GiB laptop: not viable for three seats plus Docker builds.
- Playwright-launched Chromium for recording the console: Google sign-in refuses automation-controlled browsers; we recorded the console in a normal browser instead.
- Passing `allowedTools` to the adapter: it owns that flag; permission mode is the right knob.
- Playwright's own recorder needs its bundled ffmpeg; we linked the system ffmpeg.

## 8. Pointing it at something else

Replace the dispatch text (paths, check command, folder rule). The mandates say nothing about wallets, reservations or counters; they say how a coordinator, an implementer and a reviewer work. We rehearsed on the organiser's `toy` track first: one dispatch, stage 1 accepted in two minutes, `harness check` gates 1, 2 and 4 green, before touching the real track.

## 9. Docker, where it sits

- Every stage folder is one Docker image built from its own `Dockerfile` (pinned multi-arch `python:3.12.7-slim-bookworm`, standard library only, nothing fetched at run time). `RUN.md` is one `docker build && docker run` line.
- The reviewer's acceptance run is the harness in **isolated mode**: Docker internal network, no outbound access, 2 vCPU, 2 GiB, Chromium inside the runner container. That is the judges' environment, so an accepted stage was already graded the way it will be graded.
- The implementer builds and exercises its own image before reporting, and the reviewer rebuilds from a `git archive` of the exact revision, so an image that only works from a dirty working tree cannot pass.
- Seats themselves run on the host under Claude Code's `auto` permission mode; Docker Sandbox for seats (`sbx`) is optional in the guide and we did not use it, which is the one Docker-related gap we would close next.
