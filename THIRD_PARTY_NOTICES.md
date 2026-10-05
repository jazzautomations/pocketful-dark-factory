# Third-party components and notices

The project authors license their contributed source under the MIT text in LICENSE. This does not replace the licenses or service terms of separately installed third-party components. No third-party JavaScript library or CDN is referenced by the generated browser interface.

## Generated service runtime

- Python 3.12.7 and its standard library are provided by the Docker base image python:3.12.7-slim-bookworm. Python uses the PSF License Version 2 and includes additional notices for incorporated components. See https://docs.python.org/3.12/license.html and the notices supplied with the Python distribution.
- Debian Bookworm packages in that base image retain their individual licenses. There is no single MIT license covering the base image. See https://www.debian.org/legal/licenses/ and the installed packages' /usr/share/doc/<package>/copyright files where supplied.
- The repository supplies Dockerfiles; it does not vendor Python or Debian source. Distributors of built images must retain the corresponding upstream notices and satisfy the applicable component licenses.

## Factory orchestration dependencies

These packages are installed separately for the factory and are not vendored in this repository:

| Component | Upstream information | Published license information |
|---|---|---|
| band-sdk | https://pypi.org/project/band-sdk/ | MIT |
| band-mcp | https://pypi.org/project/band-mcp/ | MIT |
| mcp Python SDK | https://pypi.org/project/mcp/ | MIT |
| Claude Agent SDK / Claude Code adapter | https://pypi.org/project/claude-agent-sdk/ | SDK package metadata identifies MIT; upstream also states applicable Anthropic service terms and component-specific exceptions |
| Claude Code CLI and provider access | https://www.anthropic.com/legal | Separate provider terms; no CLI or account entitlement is redistributed here |

These entries describe current upstream publications checked on 4 October 2026. They do not establish which exact versions were installed during the scored run. The original environment lock/freeze and per-component license files remain required for a full run-environment inventory. New installations should preserve their installed license files and record their dependency versions without exporting credentials.

## Validation and presentation tooling

The organiser harness and its test runner are external validation tools. This repository retains report output, not vendored copies of those tools or their dependencies. The corrected video retains the original room/app sequence with normal encoding and an explicitly changed summary card; no FFmpeg binary is distributed in this repository. Review slide/video provenance is documented in docs/validation/.

This notice is an inventory and attribution aid, not a claim that every external dependency is MIT or that provider terms have been independently cleared for every possible redistribution.
