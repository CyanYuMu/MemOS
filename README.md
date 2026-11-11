# MemOS

Memory service for AI agents. This repository builds on [MemTensor/MemOS](https://github.com/MemTensor/MemOS), licensed under Apache-2.0.

The Python package and data-plane API are under `src/memos`; tests are under `tests`. See `CONTRIBUTING.md` and `Makefile` for development and verification commands. Configure storage and model providers for your environment before starting the service.

The [control-plane backend](https://github.com/hugegraph/MemOS/tree/be) and [dashboard frontend](https://github.com/hugegraph/MemOS/tree/fe) are maintained on separate branches.

The Issue #6 Skill prototype is documented in the [delivery guide](docs/skill-prototype-handoff.md), including startup, module responsibilities, a Claude Code example, and known limitations. A narrowly scoped Dream UPDATE repair Skill has passed independent qualification, been formally published, and completed the minimal tool invalidation/recovery loop with successful Claude Code normal use and feedback. See the [P5 results](docs/skill-prototype-p5-results.md) for evidence and the limits of the experiment.

To generate your own source evidence and unpublished candidate without the author's private artifacts, follow the [public reproduction guide](docs/skill-prototype-public-reproduction.md). It includes offline checks, fresh Claude collection, actual Memory readback, graph import, and the review gates before publication.
