# Changelog

Notable changes to llm_harness, newest first. Each `feat`, `fix` or `perf` commit adds a line under **Unreleased**; a version bump moves those lines under the new version's heading.

## Unreleased

- `gofmt -w` formats Go files after a hunk, so tabs pasted as spaces no longer roll the file back; only real syntax errors do.
- A `@GIT_COMMIT` path list without `@END_GIT_COMMIT` closes at the next directive instead of dropping the commit.
- The cross-process state lock treats Windows' "delete pending" error as busy, so concurrent sessions no longer crash on it.
- `CHANGELOG.md`, and a reminder when a `feat`, `fix` or `perf` commit leaves it out.
- README: step-by-step install for Windows and Linux, session start for both, and moving to another computer.
- `+++ [LINE EDITS] +++` in hunks: change, delete or insert lines by their number inside the matched OLD CODE, without reciting the block twice.
- `| depth N` on `@FIND`, `@GREP`, `@LIST_RECURSIVE` and `@TEXT_REPLACE`, and one glob rule for all of them: with a `/` a glob starts at the root and `*` stays in one folder; without one it matches file names at any depth.
- Scopes may own a single definition (`file#Class.method`), leaving the rest of the file to other sessions, or a folder to a set depth (`pkg depth 1`).

## 3.4

- `@TEXT_REPLACE`: count-locked text edits inside a line, a definition (`file#Class.method`) or a whole folder.
- Inspection log and `[REVISIT]`: rereads of unchanged definitions say who read them and when.
- `@VERDICT` / `@VERDICTS`: conclusions about folders, files and definitions that travel with git, go stale when their area changes, and show on reads.
- `@CAMPAIGN` / `@CAMPAIGN_END`: a sweep's verdict legend, footer line and rule while it runs, tree overlay, one-step cleanup.
- `@REPEATABLE`: scripts the user reruns by hand run on every paste.
- Sessions: each chat's own aim, uncommitted work owned per session (release and commit guards, hand-over), `@MESSAGE`, `@SCOPE_REQUEST`, `@BRAKE` / `@BRAKE_ACK`, and the user's `--release` / `--unbrake`.
- The user's notes show in every footer.
- Rule: files are edited by content only, never by line numbers or `@RUN` rewrites.

## 3.3

- Harness-assigned script numbers under a cross-process lock, and `@SESSION`.
- `@SCOPE` write scopes: overlaps refused, edits into another session's scope refused, `@RUN` changes there reported, scope changes announced once, the user's `--grant`.
- `@HELP` and the full command list in every footer.
- A nudge when a script only did git work, and a hint when a one-line OLD CODE is only part of a line.

## 3.2 and earlier

- Content-matched hunks with syntax rollback, an undo journal, a git gate, and replay of lost output.
- Code graph through codebase-memory-mcp (callers, callees, impact, coverage warnings), `| Unused`, `| Refs` and call-site lines.
- Tree drift check, `@SIDE`, `@GIT_INIT`, user-owned notes, timings, and the README.
