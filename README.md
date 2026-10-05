# Dark Factory — pocketful, built by a headless three-seat band

Team: Felipe Salvego (Jazz Automations) and Carlos Henrique. Track: **pocketful**.

Everything under `stage-1/` … `stage-4/` was written by the three Band seats in the room recorded in `room.json`, from one human dispatch, with no human message afterwards. The humans wrote this file, `FACTORY.md` and the mandates.

## The factory at a glance

![Architecture: one dispatch, three headless seats, Docker + harness, what gets submitted](docs/diagrams/architecture.svg)

![One stage through the factory: handoff, build, independent review, accept or reject](docs/diagrams/stage-flow.svg)

![The submitted run minute by minute, tokens per seat, room evidence](docs/diagrams/timeline.svg)

## How to read this repository

| Path | What it is |
|---|---|
| `FACTORY.md` | The factory: seats, setup, design choices, measured time and token spend, how bad work is caught |
| `mandates/` | One generic mandate per seat (`coordinator.md`, `implementer.md`, `reviewer.md`), each starting with the harness and model it ran |
| `room.json` | Full session download of the room, unchanged |
| `stage-N/` | The service as accepted at stage N: `Dockerfile`, `RUN.md`, source. Each folder builds and runs on its own |
| `tools/factory/` | `seat.py`, `start.sh`, `dispatch.py`, `usage.py`: the scripts that ran the seats and measured the run |
| `docs/dispatch.txt` | The single human dispatch, verbatim |
| `docs/diagrams/` | Architecture, stage flow and run timeline (SVG + PNG) |
| `docs/video-factory-run.mp4`, `docs/slides.pdf` | Submission video and slides |

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

| Stage | Accepted at | Shipped checks (isolated) |
|---|---|---|
| 1 | +24 min | 147/147 |
| 2 | +45 min | 147/147, 35/35 |
| 3 | +62 min | 147/147, 35/35, 6/6 |
| 4 | +76 min | 147/147, 35/35, 6/6, 5/5 |

Reviewer rejections: 0. Human messages after dispatch: 0. Details and token usage per seat in `FACTORY.md`.
