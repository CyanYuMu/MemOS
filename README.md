# MemOS

Memory service for AI agents. This repository builds on [MemTensor/MemOS](https://github.com/MemTensor/MemOS), licensed under Apache-2.0.

The Python package and data-plane API are under `src/memos`; tests are under `tests`. See `CONTRIBUTING.md` and `Makefile` for development and verification commands. Configure storage and model providers for your environment before starting the service.

The [control-plane backend](https://github.com/hugegraph/MemOS/tree/be) and [dashboard frontend](https://github.com/hugegraph/MemOS/tree/fe) are maintained on separate branches.

The Issue #6 Skill prototype is documented in the [delivery guide](docs/skill-prototype-handoff.md), including startup, a walkthrough, module responsibilities, validation evidence, and known gaps. The candidate management workbench is available; actual Skill publication and the post-release P5 recovery demonstration remain unqualified.
