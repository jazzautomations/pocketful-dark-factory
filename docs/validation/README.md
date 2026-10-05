# Independent evidence review

The reviewed generated source is revision 10420119704d374f39452573694d3b1c579e54d0. Official organiser harness revision: 803560d2a678ace1414465c098eb0ab5380ffade. No stage source, mandate or room export changed in this branch.

The supplementary reproduction builds all four stage folders and runs every required preceding suite against each folder, with genuine preceding-stage services for upgrade tests. It uses the unchanged shipped tests, the existing df-harness-runner image, an internal Docker network, and official 2 CPU / 2 GiB service limits. All 10 required suite executions pass: 147 + 182 + 188 + 193 = 710 checks, with no failure, error, skip or deselection. All three next-stage overshoot probes fail as expected.

supplementary-reports.zip contains every command log and counts file. verification.json contains aggregate counts, runner identity and reviewed revisions. This reproduction is not a receipt for the exact official harness run --all command. The separate exact CLI attempt ended in runner dependency download/build errors before test collection; its reports are retained in official-command-failure-reports.zip. Shipped suites do not establish hidden-test coverage, judging score or factory eligibility.

room-audit.json records event counts, source hash and time intervals from the official full-session export. The corrected usage.py validates both exported field spellings and timezone-bearing timestamps. It intentionally reports unavailable usage when counter semantics or evidence are absent. It does not sum unknown cumulative counters or substitute context remaining for tokens.

The original video is retained in docs/video-factory-run.mp4. The corrected derivative replaces only the closing summary from 218.56 seconds, with normal H264 re-encoding of the preceding sequence. Full decode succeeds; a frame comparison at 180 seconds checks sampled content preservation, not full pixel identity. video-correction-receipt.json provides hashes and the validation scope.

Pending before final submission: maintaining the approved MIT license and third-party notices, reproducible original usage records or explicit unknown status, effective runtime/model identities, maintainer review, teammate acceptance, media uploads and actual submission receipt. Event wording requires BAND Desktop room footage; web-console equivalence remains unresolved. No missing evidence was manufactured or inserted into room.json.

The project owners approved MIT licensing with copyright 2026 Jazz Automations and acarloshenrique on 4 October 2026. LICENSE and THIRD_PARTY_NOTICES.md implement that approval. The factory setup now documents the legacy MCP client API compatibility bound; this is a post-run reproduction correction, not an assertion of the original installed dependency versions.


## Additional run-1 comparison baseline — 5 October 2026

The existing supplementary spec-attack suite passed 35/35 tests against the frozen stage-4 application, with zero failures, errors or skips. This is a post-run independent observation, not a seat review or a new event in the original room. The cached service image was used only after source verification and then reverified by immutable image ID: app.py and all three static files match the frozen run-1 sources. The service used 2 vCPU / 2 GiB on an internal Docker network with no host port, separate disposable state, and was removed along with its network afterward.

[spec-attack-run1-20261005.zip](spec-attack-run1-20261005.zip) retains the exact tested probe source and its SHA-256, raw pytest output, JUnit counts, execution receipt, image identity and source verification. The test credentials are disposable fixture values, not provider credentials. These probes are partial, principally stage 1 and a few stage-4 boundaries; they are not a complete stage-3/4 clause matrix, full upgrade verification, official harness receipt or hidden-test certification. Do not add the 35 to the 710 and describe the total as official checks. Use the same frozen probes for candidate comparison.
