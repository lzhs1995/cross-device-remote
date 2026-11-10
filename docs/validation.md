# Validation and evidence boundaries

[繁體中文](validation.zh-TW.md) · [Project](../README.md)

## Current claim

The operational route from a Mac agent through Windows RStudio/ClaudeR, R, MplusAutomation and Mplus has been used for real research batches in the originating deployment. The reusable lessons here come from that use. Private research outputs, machine identities and session records are not distributed with this repository.

That history does **not** validate every fresh installation of this public package. Check the repository's CI and release-specific validation records for the exact revision you use. Do not infer a fresh-machine Windows pass, an overnight reliability guarantee, automatic loading by every agent, or scientific acceptance from documentation alone.

## Acceptance ladder

| Level | Required evidence |
| --- | --- |
| Source and installation | Exact revision, local offline checks, installer result, expected links and file set |
| Network | Trusted route to the intended server; correct interface and allowlist |
| Command channel | Fresh signed Windows poll and a new harmless command result |
| RStudio route | Discovery, PID/birth/parent/listener agreement, real response in the intended session |
| Long work | Original submission intent and job ID, progress, durable terminal state, runtime budget |
| Result transport | Explicit export file set, sizes, SHA checks, local free-space check |
| Scientific result | Unchanged design, software/package versions from the actual route, convergence and domain-specific independent checks |
| Agent integration | The named agent discovers the installed skill and successfully invokes its CLI in that environment |

A link installed for Codex, Claude Code, Gemini or Antigravity is filesystem integration, not proof that a particular version loads it automatically. Agents with another skill convention can read `SKILL.md` explicitly and invoke the same CLI.

## Tests

Read each test before running it. Offline parser, identity-state, and file-safety tests do not contact Windows. `scripts/selftest.sh` is a connected check: it can create remote synthetic files and tasks and should run only in an approved test window.

Do not run a model merely to make an installation claim. When an engine test is authorized, use synthetic input and a finite budget. Record the actual R process and `packageVersion("MplusAutomation")`; an Rscript library and an interactive RStudio library can differ.

Test success must name its scope. Examples: “offline CLI test passed”, “Windows command receipt verified”, “HTTP fallback RStudio test passed”, or “synthetic model checked”. HTTP fallback success is not native MCP success. An outer R job marked complete is not proof that all batch members passed.

## Results and privacy

`pull-dir`, `rscript`, and `mplus` collect an entire selected work directory. They do not automatically filter individual-level data or input copies. For restricted data, execute against existing Windows-local input, create a separate result-only export, inspect its exact manifest, then collect it and compare the received file set and hashes. Configure project-specific transfer caps and disk floors in the domain workflow.

Checksums prove byte identity, not correct modeling. A normal Mplus termination message is a necessary check in many workflows, not a sufficient scientific acceptance criterion. Negative variances, slow convergence, precision checks and information-matrix warnings require separate treatment.

## Performance

Measure time from grant to submission, Windows engine start/end, result export, and collection separately. Report both engineering time and scientific engine time. Network ping is not end-to-end task latency, and a high engine-time fraction in one batch is not a controlled speedup against a Windows-local agent.

Remote development is useful when one submission produces a substantial batch while the Mac handles editing and review. Repeated complete transfers for tiny jobs can dominate runtime. Keep the original model and accuracy requirements when improving scheduling.
