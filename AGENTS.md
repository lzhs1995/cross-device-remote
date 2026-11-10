# Cross-device remote: contributor and agent entry

Read `SKILL.md`, then the recipe matching the user's required execution route.
English: `README.md`. 繁體中文：`README.zh-TW.md`.

- Mac controls transport; Windows owns execution, software licences and data.
- A request for RStudio/ClaudeR must use `xdev rstudio`; do not substitute
  `xdev rscript` or direct Mplus, even when they use the same inputs.
- Keep one job ID after timeout or interruption. Fetch its result; do not replay
  a submission just because the terminal or network disconnected.
- Authentication material, machine profiles, endpoints and research outputs stay
  outside Git. Run `python3 scripts/check_public.py` before publishing.
- Test offline with `python3 scripts/check.py`.
  Real Windows/Mplus tests require a separately authorized, free execution slot.
- Never modify a live installation, shared queue, cursor, or another agent's
  process while preparing a source change. State the source revision and actual
  acceptance scope separately from scientific success.
- Any CLI agent can call `scripts/xdev --help`. Skill-directory installation is
  convenience, not proof that every application has loaded its instructions.
