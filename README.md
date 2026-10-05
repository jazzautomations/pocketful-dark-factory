# Dark Factory — pocketful, built by a headless three-seat band

Team: Felipe Salvego (Jazz Automations) and Carlos Henrique. Track: **pocketful**.

Everything under `stage-1/` … `stage-4/` was written by the three Band seats in the room recorded in `room.json`, from one human dispatch, with no human message afterwards. These documentation files and mandates were prepared separately from the generated stage source. This branch is a documentation and evidence review; maintainer review is pending.

## The factory at a glance

[Reviewed architecture, acceptance flow and recorded timeline](docs/diagrams/README.md) distinguish observed room facts from intended gates and unverified operator estimates. Original diagram exports are retained for provenance; their labels are not measurement receipts.

## How to read this repository

| Path | What it is |
|---|---|
| `FACTORY.md` | The factory: seats, setup, design choices, recorded timing, reported token estimates and evidence limits, how bad work is caught |
| `mandates/` | One generic mandate per seat (`coordinator.md`, `implementer.md`, `reviewer.md`), each starting with the configured harness and model |
| `room.json` | Full session download of the room, unchanged |
| `stage-N/` | The service as accepted at stage N: `Dockerfile`, `RUN.md`, source. Each folder builds and runs on its own |
| `tools/factory/` | `seat.py`, `start.sh`, `dispatch.py`, `usage.py`: seat orchestration and audit scripts; see FACTORY.md for usage-data limits |
| `docs/dispatch.txt` | The single human dispatch, verbatim |
| `docs/diagrams/` | Reviewed Markdown diagrams and retained operator-supplied draw.io/SVG/PNG originals |
| `docs/video-factory-run.mp4`, `docs/slides.pdf` | Operator-supplied original media; see Reviewed media for corrected review derivatives and eligibility limits |

## Run a stage

Each `stage-N/RUN.md` has the one command. For example:

```sh
cd stage-4 && docker build -t pocketful . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful
```

Check it the way judges do, from a fresh clone of this repository with the pinned organiser harness:

```sh
python -m harness check <clone> --track pocketful
python -m harness run --track pocketful --repo <clone> --all --mode isolated
```

## Result of the submitted run

| Stage folder | Required shipped-suite checks independently reproduced |
|---|---|
| 1 | 147/147 |
| 2 | 182/182 (147 + 35) |
| 3 | 188/188 (147 + 35 + 6) |
| 4 | 193/193 (147 + 35 + 6 + 5) |

A separate supplementary reproduction at result revision `1042011`, with unchanged organiser suites at `803560d2`, passed all 710 required checks with no failure, error or skip. The three next-stage overshoot probes failed as expected. Services ran in an internal Docker network with the official 2 CPU / 2 GiB limits, and upgrade tests used the genuine preceding stage service. A subsequent independent exact official `harness run --all --mode isolated` execution from fresh public clones at review revision `b493eb4` also passed all four folders and 710 cumulative required checks, exit 0; its [receipt and raw archive](docs/validation/official-cli-run-20261005.json) are separate from this supplementary reproduction. The logs and provenance are in [docs/validation](docs/validation/README.md). Shipped checks do not establish hidden-test coverage or a judging score.

Reviewer rejections: 0. Human messages after dispatch: 0. Recorded timing, reported usage estimates and their limits are in `FACTORY.md`. The [lablab draft observation](docs/validation/lablab-draft-observation-20261005.json) confirms Carlos and Felipe as visible team members in the Pocketful track. The form remains a draft with missing cover/slides; no final submission receipt is established.


## Reviewed media

The original video and slides remain in docs/. The corrected [video](docs/video-factory-run-corrected.mp4) replaces the original closing summary and retains the preceding room/app sequence with normal video re-encoding. [Evidence-review slides](docs/slides-evidence-review.pdf) contain updated counts and timing, with missing usage counters disclosed. The original recording remains intact. [Edit provenance](docs/validation/video-correction-receipt.json) records source/output hashes and validation scope. These are review artifacts, not proof of final submission or Desktop-footage eligibility.


## License

Project source is licensed under [MIT](LICENSE), with copyright 2026 Jazz Automations and acarloshenrique. Separately installed runtimes and provider tools retain their own licenses and terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).


## Verify in three commands

Prerequisites: Git, Python 3.12+ with the organiser harness dependencies installed in the active environment, a running Docker daemon, and the harness checkout pinned to `803560d2a678ace1414465c098eb0ab5380ffade`. Run these commands from that harness checkout. Installation and recording/provider setup are separate prerequisites, not hidden inside the command count.

For this review candidate (the documentation branch is still awaiting maintainer review):

```sh
git clone --branch codex/evidence-review-20261004 https://github.com/jazzautomations/pocketful-dark-factory.git result-review
python -m harness check result-review --track pocketful
python -m harness run --track pocketful --repo result-review --all --mode isolated --out runs/result-review-fresh
```

Use a clean clone and fresh output directory, record the checked Git revision, and read the required-suite reports and exit status. On Windows follow the pinned guide’s supported isolated/WSL setup and UTF-8 handling. The checker alone does not verify stage builds. The exact all-stage command passed independently on 5 October at review revision `b493eb4`, with 710 cumulative required shipped checks and exit 0. Its [receipt](docs/validation/official-cli-run-20261005.json) and [raw outputs](docs/validation/official-cli-run-20261005.zip) preserve the clean public-clone provenance. Earlier runner/network setup failures and the separate supplementary reproduction remain archived. This does not certify hidden tests.

Each stage is built from its own `Dockerfile` into a separate service image. The harness applies the official 2 vCPU / 2 GiB service limits and internal runtime network, and uses genuine preceding-stage services for populated-state upgrades. Build-time installation may require network access; runtime service isolation does not imply the provider agents themselves were sandboxed.
