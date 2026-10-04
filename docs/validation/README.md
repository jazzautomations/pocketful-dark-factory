# Independent evidence review

The reviewed generated source is revision 10420119704d374f39452573694d3b1c579e54d0. Official organiser harness revision: 803560d2a678ace1414465c098eb0ab5380ffade. No stage source, mandate or room export changed in this branch.

The supplementary reproduction builds all four stage folders and runs every required preceding suite against each folder, with genuine preceding-stage services for upgrade tests. It uses the unchanged shipped tests, the existing df-harness-runner image, an internal Docker network, and official 2 CPU / 2 GiB service limits. All 10 required suite executions pass: 147 + 182 + 188 + 193 = 710 checks, with no failure, error, skip or deselection. All three next-stage overshoot probes fail as expected.

supplementary-reports.zip contains every command log and counts file. verification.json contains aggregate counts, runner identity and reviewed revisions. This reproduction is not a receipt for the exact official harness run --all command. The separate exact CLI attempt ended in runner dependency download/build errors before test collection; its reports are retained in official-command-failure-reports.zip. Shipped suites do not establish hidden-test coverage, judging score or factory eligibility.

room-audit.json records event counts, source hash and time intervals from the official full-session export. The corrected usage.py validates both exported field spellings and timezone-bearing timestamps. It intentionally reports unavailable usage when counter semantics or evidence are absent. It does not sum unknown cumulative counters or substitute context remaining for tokens.

The original video is retained in docs/video-factory-run.mp4. The corrected derivative replaces only the closing summary from 218.56 seconds, with normal H264 re-encoding of the preceding sequence. Full decode succeeds; a frame comparison at 180 seconds checks sampled content preservation, not full pixel identity. video-correction-receipt.json provides hashes and the validation scope.

Pending before final submission: rights-holder-approved MIT license, reproducible original usage records or explicit unknown status, effective runtime/model identities, maintainer review, teammate acceptance, media uploads and actual submission receipt. Event wording requires BAND Desktop room footage; web-console equivalence remains unresolved. No missing evidence was manufactured or inserted into room.json.
