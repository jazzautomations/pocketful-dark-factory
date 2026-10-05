# FACTORY.md — a three-seat headless factory on band-sdk

## 0. Verify in three commands

Use the three commands in [README.md](README.md#verify-in-three-commands), from the pinned harness checkout with its dependencies installed and Docker running. They include a clean review-branch clone and a fresh isolated output directory. The checker does not build stages; the exact all-stage CLI attempt and the separately reproduced 710 shipped checks have distinct receipts.

For a separate room audit, run `python tools/factory/usage.py room.json`. On this export it reports event counts/timing and unknown token usage, with exit status 0 for a successful structural audit. Its `usage.status` remains `unavailable`; process success does not mean tokens were measured. It cannot reproduce the operator-reported token table.

This document is enough to stand the factory up again on a plain Linux VM and point it at a different problem. Nothing in it is specific to the track we entered; the track lives entirely in the dispatch message pasted into the room.

## 1. Shape

Three seats, one room, one dispatch. See the [reviewed architecture and room timeline](docs/diagrams/README.md); the operator-supplied diagram exports are retained separately as unverified illustrations.

| Seat | Owns | Harness | Configured model |
|---|---|---|---|
| `coordinator` | scope, sequencing, complete handoffs, acceptance, reporting | Claude Code (headless, via `band-sdk` `ClaudeSDKAdapter`) | `claude-sonnet-5-5` |
| `implementer` | one scoped item at a time, builds and self-checks, commits, reports the hash | Claude Code (headless) | `claude-opus-5-5` |
| `reviewer` | independent reproduction on the exact revision, probes beyond the shipped checks, ACCEPT/REJECT with evidence | Claude Code (headless) | `claude-opus-5-5` |

The seats are Band **external agents**. Each one is a Python process running `seat.py`: the Band SDK keeps the WebSocket to the room, and every message addressed to the seat becomes one Claude Code turn with the seat's mandate as its system section. There is no Band Desktop, no GUI and no human in the loop after the dispatch. Seats only receive messages that mention them, so the coordinator is the only seat that ever sees the human dispatch, and every handoff must carry the whole task and the whole specification (the mandates enforce this).

Configured model IDs describe the mandates and adapter setup. They are not independent evidence of the effective model used by every runtime. The final room report leaves implementer/reviewer model identities unknown. Runtime records are needed to resolve that limit.

## 2. Setup (about 15 minutes)

Host reported by the operators: Oracle Cloud VM, x86-64, 4 vCPU, 5.8 GiB RAM, Ubuntu 24.04, Docker 29. A 37 GiB ARM host was prepared as a fallback and not needed.

```sh
# 1. Claude Code CLI, logged in (subscription)
curl -fsSL https://claude.ai/install.sh | bash && claude   # /login once

# 2. Band SDK with the Claude Code adapter, plus the Band MCP server for the human side
python3 -m venv ~/band-venv && ~/band-venv/bin/pip install "band-sdk[claude-sdk]" band-mcp "mcp<2"

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

```sh
bash tools/factory/start.sh <mandates directory or factory workspace> <absolute result repo>     # starts the three seats (nohup, one log each)
~/band-venv/bin/python tools/factory/dispatch.py <room id> <dispatch.txt>                     # one message, mentions only the coordinator
```

The dispatch is the only human input. Ours named the track, the absolute paths of the specifications and the result repository, the check command, the folder-per-stage rule and the acceptance flow, and told the coordinator to run all four stages in order. After that we read the room and did nothing else.

## 4. What the room shows (the run we submit)

The unchanged official full-session export contains 556 events: 555 agent events and one human dispatch. No later human message appears in the export.

| Recorded event | UTC timestamp |
|---|---|
| First setup event | 2026-10-04 20:51:31.554 |
| Human dispatch | 2026-10-04 20:53:18.700 |
| Coordinator final text report | 2026-10-04 22:07:03.747 |
| Last exported event | 2026-10-04 22:07:11.968 |

Dispatch to final text report: 73 minutes 45.047 seconds. Dispatch to last event: 73 minutes 53.268 seconds. The full room interval is 75 minutes 40.414 seconds (approximately 76 minutes), including setup. The reviewer accepted each stage on first review; there was no rejection episode to demonstrate a recovery loop.

The current room export contains no `metadata.band_usage` records. The coordinator's final report explicitly says token usage was unknown. The published original usage.py expects different field names and cannot recompute the token table from this export. The proposed replacement can audit event counts and timing; it intentionally does not infer token usage from context remaining or sum counters whose semantics are unknown.

The following figures were reported by the run operators, and are not independently supported by the current public room export:

| Seat | Reported turns | Reported output tokens | Reported cache writes | Reported cache reads |
|---|---|---|---|---|
| coordinator | 9 | 123,280 | 182,555 | 4,127,192 |
| implementer | 18 | 180,625 | 347,262 | 21,347,966 |
| reviewer | 18 | 101,925 | 261,334 | 13,553,339 |

Reported adapter estimates at list prices: coordinator USD 14.14, implementer USD 99.54, reviewer USD 62.52; total USD 176.20. The seats used subscription access. This estimate is not an invoice. Sanitized original per-seat logs, runtime/session identity and counter semantics must accompany any claim that these counters are independently measured.

Independent supplementary reproduction of the unchanged shipped suites passed all 710 required checks across the four frozen stage folders (147, 182, 188 and 193). No required-suite failure, error or skip; all three next-stage overshoot probes failed as expected. Each service used the official resource limits on an internal Docker network, with genuine preceding-stage services for upgrade checks. The subsequent exact official all-stage CLI independently passed from fresh public clones at review revision `b493eb4` on 5 October: four claimed stage folders, 710 cumulative required checks, exit 0. [Raw receipt](docs/validation/official-cli-run-20261005.json) and [archive](docs/validation/official-cli-run-20261005.zip) retain commands, reports, clean-clone provenance and the setup failure preceding the successful retry. Earlier dependency-download failures remain preserved. This application validation is post-run, not a second BAND run, and does not establish hidden-test coverage or factory eligibility.

## 5. Design choices and what they cost

- **Headless seats instead of Band Desktop.** We could not run Band Desktop on any of our machines. The SDK adapter gives the same room semantics (addressed messages, `@handle` mentions, tool-call and usage events) and runs on a server. Cost: we had to write `seat.py` and read the adapter source; no documentation covers the Claude Code adapter in detail.
- **Full specification in every handoff.** Seats cannot read the room, so the coordinator pastes the complete task and the complete spec of every stage so far into each handoff, split into numbered parts when needed (the stage 2 handoff was 4 parts). Cost: large prompts; the reported cache-write totals still require original usage records; benefit: the implementer and reviewer never work from a summary.
- **Reviewer reproduces, never trusts.** The reviewer exports the exact revision with `git archive`, builds from the Dockerfile, follows `RUN.md`, runs the official harness in isolated mode with a fresh output directory, and then probes the spec clauses the shipped checks never touch (concurrency races, rounding, error precedence, restarts, earlier stages still holding). Cost: independent review adds execution and model usage; the reported per-seat counters remain unverified; benefit: acceptance means something.
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

## 9. Submission evidence still required

Before final submission, the maintainers must confirm documentation authorship and review, retain the approved MIT license and third-party notices, provide reproducible usage records or explicitly retain the unknown status, correct equivalent timing/usage claims in the slides and video, finish required form attachments and confirm the final submission receipt when the human hold is released. Carlos and Felipe are now observed as team members in the Pocketful track; see the [draft UI observation](docs/validation/lablab-draft-observation-20261005.json). The video currently shows the BAND console; the event wording calls for BAND Desktop room footage. SDK cloud seats are allowed, but footage equivalence requires organiser clarification or compliant footage. Do not manufacture missing run evidence or insert counters into the original room export.


Independent reproduction reports, room timing audit and corrected-media provenance are in [docs/validation](docs/validation/README.md). Original media is retained alongside corrected review derivatives.

The launcher was repaired after the run to use the adjacent seat.py, the documented band-venv, quoted paths and preflight checks for all three mandates and identities. These repairs are not evidence of the exact launcher used during the scored run. Launcher tests cover preflight behavior without starting workers; a fresh end-to-end factory run has not been performed with this revision.

## 10. Docker and independent validation

Each frozen stage has its own Dockerfile and RUN.md. The base image tag is `python:3.12.7-slim-bookworm`; a tag is not a digest pin. The application uses the standard library and needs no runtime dependency download.

The independent supplementary reproduction used separate stage images, Docker internal runtime networking and the official 2 vCPU / 2 GiB service limits. It used genuine previous-stage services for populated exports and upgrades. Build-time dependency installation can require network access. Earlier official all-stage attempts failed during runner dependency/network setup before test collection and are preserved. The subsequent exact command completed successfully on 5 October; its official mode remains isolated. The outer build client used bridge networking for registry access, while the unchanged official driver creates internal test-service networks with the prescribed resource limits.

The room contains reviewer isolated-run reports and revision-based handoffs. Neither those reports nor public-suite counts establish complete clause coverage, hidden-test results or a judging score. Acceptance on first review is legitimate and is not a missing rejection quota.

Provider seats were reported as host processes using Claude Code AUTO permission mode. Service-container isolation does not establish agent sandboxing. The launcher repair and local preflight tests were performed after the run; they do not prove the original seats were started with this repaired launcher.
