# llm_harness

A workflow harness for doing real coding work with an AI **chat interface** (ChatGPT, Claude, Gemini in a browser) instead of an agent app.

## Why a chat-interface harness

Agent apps run commands on your machine by themselves. In a chat window the AI cannot touch your machine at all: it writes, you paste. This harness turns that loop into a dependable protocol:

- The AI answers each turn with one script. You paste it into a terminal and paste the output back.
- `harness.py` executes the script safely: exact-match patches, syntax rollback, an undo journal, a git gate.
- Every run ends with a footer: the next script number, the project's pinned goal and the standing rules (L1). The rules arrive at the end of every paste, so long chats do not drift away from them.

No API key, no MCP server, no agent permissions. Python 3 standard library only.

## Cost model

The harness runs locally and makes no model API calls of its own, so it needs no separately metered LLM API. You can drive it from a monthly AI chat subscription, using that plan's included usage rather than paying per API request. That can be more predictable or more economical than an API-metered agent workflow. Some coding agents are also offered through subscriptions, and every plan has usage limits, so the actual cost depends on the provider, the plan, the workload and how much context each session consumes.

## The loop

1. You describe the task.
2. The AI replies with a short manifest (Talk, Current aim, what it will change, inspect and run, After), one script fence, and a test checklist.
3. You paste the fence into PowerShell (or bash).
4. You paste the full output back.

Lost an output, or pasted twice? Paste the same script again. An identical re-paste replays the saved output and runs nothing; an old read-only script with no saved output simply runs again.

## Several chats on one project

Several chats can work in the same project at once, each on its own goal:

- Each chat names itself with `@SESSION <name>` on every script and leaves the script number out; the harness assigns numbers under a lock, so two chats never collide.
- Before changing files, a chat declares `@SCOPE <aim> | <paths or globs>`. A scope that overlaps another session's is refused when it is declared.
- Every other session is told about a scope change once, the next time it runs. The harness cannot push a message into another chat window; it informs each chat when that chat runs.
- File changes made through the harness inside another session's scope are refused. Changes made by `@RUN` commands are detected and reported, not prevented, because the harness cannot know in advance what a command will write.
- `@UNDO` only reverses the session's own scripts, and `@GIT_FOLD` is refused while other sessions hold scopes.
- Each chat states its own aim with `@SESSION <name> | <aim>`; the project goal stays shared, and the footer shows this chat's session first, then the others.
- Uncommitted work has an owner: a session cannot release its scope until its changes are committed (or explicitly handed over), cannot commit a file holding another active session's uncommitted changes, and is warned when it edits such a file.
- Chats can leave each other notes with `@MESSAGE` and `@SCOPE_REQUEST`, shown once and labelled as coming from an AI, not from you. `@BRAKE` pauses another chat's changes until it acknowledges with `@BRAKE_ACK`; you can lift a brake with `--unbrake <session>`, and end a closed chat's session with `--release <session>`.
- Only you can override a scope: `python harness.py --workspace <folder> --grant <session> <path or glob>` (or `--revoke`), typed in your own terminal. A script that tries to run it is refused.

## Install (once per machine)

The harness is two files, `harness.py` and `workflow_commands.txt`, installed into `.llm_harness` in your home folder. The setup scripts copy them there from the folder they sit in, keep a backup of the previous version, offer to install or update the optional code graph (CBM, which needs internet), and pause at the end so you can read the result.

**Windows (PowerShell)**

1. Check that Python 3.8 or newer and git are installed:

        python --version; git --version

2. Clone the harness into a folder of your choice, for example `E:\Work\Me`:

        Set-Location E:\Work\Me; git clone https://github.com/Vipxpert/llm_harness.git

3. Run the setup from the clone (or double-click `setup.cmd` in Explorer):

        Set-Location E:\Work\Me\llm_harness; .\setup.cmd

4. To update later, pull and run the setup again:

        Set-Location E:\Work\Me\llm_harness; git pull; .\setup.cmd

**Linux / macOS (terminal)**

1. Check that Python 3.8 or newer and git are installed:

        python3 --version; git --version

2. Clone the harness, for example into your home folder:

        git clone https://github.com/Vipxpert/llm_harness.git ~/llm_harness

3. Run the setup (it uses curl or wget for the optional CBM; `NO_PAUSE=1` skips the final pause):

        sh ~/llm_harness/setup.sh

4. To update later, pull and run the setup again:

        cd ~/llm_harness && git pull && sh setup.sh

## Start a session

Run the handshake for the project folder you want to work on.

Windows (PowerShell):

    python "$HOME\.llm_harness\harness.py" --workspace "D:\path\to\project"

Linux / macOS:

    python3 ~/.llm_harness/harness.py --workspace ~/path/to/project

On Windows with Git Bash or WSL, add `--shell bash` so the syntax card shows the bash wrapper.

Then open a new chat and paste three things: `workflow_commands.txt`, the handshake output, and your task. The handshake prints the syntax card (every directive and the exact fence wrapper for your shell), the project's goal and its notes. Projects with a `.git` folder run in git mode; any other folder runs in no-git mode, which keeps undo but has no git directives until `@GIT_INIT [Name <email>]` turns it into a repository, carrying its numbering and undo history over.

## Moving to another computer

1. Install the harness on the new computer (the steps above), then clone or pull your project there.
2. Run the handshake for the project. Your goal, notes, verdicts and campaigns come with the project, because they are committed in `.llm_harness/`.
3. If the project uses its own commit identity, set it again: repository-local git settings do not travel with a clone.

        git config user.name "Your Name"; git config user.email "you@example.com"

Script numbering, undo history, saved run outputs, the session list and the reread log stay on each machine and start fresh on the new one; that is by design and harmless.

## What the AI can do

| Purpose | Directives |
|---|---|
| Change files | `@FILE` + `@HUNK`, `@NEW_FILE`, `@DELETE_FILE`, `@RENAME`, `@TEXT_REPLACE` (count-locked text edits inside a line, a scope or a whole folder) |
| Read code | `@PROBE <file> \| Scope / Lines / TOC`, `@PROBE <file> \| Refs \| <scope>` (what a scope uses and where each piece is defined) |
| Search | `@FIND` (files by name), `@GREP` (text or regex), `@XREF <name>` (references), `@XREF <name> \| callers / callees / impact` (code graph) |
| Architecture tree | `@PROBE <tree> \| Tree \| <node> \| <depth>`, `@PROBE <tree> \| Check` (drift check) |
| Dead code | `@PROBE <file> \| Unused` (candidates with call-graph and text evidence) |
| Run anything else | `@RUN [--timeout <s>] <command>` (tests, builds, installs, CLI tools) |
| Project memory | `@GOAL`, `@NOTE_ADD / EDIT / REMOVE / LIST`, `@INIT_DEBUG` |
| History | `@UNDO`, `@GIT_INIT`, `@GIT_STATUS`, `@GIT_COMMIT`, `@GIT_FOLD` |

The syntax card printed by the handshake is the authoritative reference.

## Safety model

- A hunk applies only where its old code matches exactly one place; a mismatch prints the real on-disk lines to rebuild from.
- A Python or Go syntax error after patching restores the file.
- Every change is journaled; `@UNDO` restores the exact previous files.
- Commits happen only through git directives, after you confirm the change works. The harness aborts during a merge or rebase, never pushes, and refuses `@RUN git commit` and similar commands.
- Script numbers only go up: an old or reused number is refused, apart from the replay and re-run cases above. A second chat can work alongside with `@SIDE` scripts, which skip the numbering and may only read, search and run commands. A script marked `@REPEATABLE` runs on every paste, even an identical one, so you can rerun a test or tweak its values by hand; it may only read, search and run commands.

## Project memory and machine state

- `.llm_harness/goal.md` and `.llm_harness/notes.md` are project memory: they travel with the project, so commit them. Notes are yours: the AI writes, edits or removes one only when you ask, and may suggest one in its reply otherwise. `.llm_harness/verdicts.json` keeps the AIs' recorded conclusions about folders, files and definitions (`@VERDICT`), so they travel too; a later change in a verdict's area marks it stale. For a sweep that needs a verdict on every area, you can start a `@CAMPAIGN` with its own verdict words: its rule and progress show in every footer while it runs, Tree probes mark finished nodes, and `@CAMPAIGN_END` removes its verdicts.
- Script numbering, the undo journal, saved run outputs and the graph stamp are machine-local: in `.git/llm_harness/` for git projects, in `.llm_harness/` otherwise, and ignored by git.
- `docs/trees/tree_master.txt` is the curated architecture tree. It is read level by level, updated in the same script as the code it describes, and `| Check` reports nodes that no longer match the code.

## Optional: the code graph (CBM)

Install codebase-memory-mcp (github.com/DeusData/codebase-memory-mcp) to enable `callers / callees / impact`, the graph half of `| Unused`, and coverage warnings. The harness calls CBM's command-line mode, so no MCP connection is needed. It keeps CBM on its default per-user cache, re-indexes automatically when source files change, and warns when CBM parsed files only partly. Known limits: the graph misses Python method calls made through an object (the text checks compensate), and on some Windows setups CBM refuses a cache folder inside the project tree. Treat every graph answer as static evidence to confirm in source.

## Rules (L1)

`workflow_commands.txt` holds every rule exactly once. Its `L1 Rule` lines are printed in the footer after every run; rules tagged `(git)` print only for git projects. Edit a rule there and the next run everywhere uses the new wording.

## Repository layout

- `harness.py`: the harness.
- `workflow_commands.txt`: the workflow and rules the AI follows.
- `setup.cmd`, `setup.sh`: installers.
- `docs/trees/tree_master.txt`: this project's architecture tree.
- `.llm_harness/goal.md`, `.llm_harness/notes.md`: this project's memory.

## Developing the harness

Work on this repository through the harness itself, with this folder as the workspace, then copy `harness.py` and `workflow_commands.txt` into `~/.llm_harness` (or rerun setup). Keep the tree in sync in the same script as each change. After changing the harness, run `python tests/scopes_test.py` and `python tests/concurrency_test.py` through `@RUN`; each prints PASS or FAIL per check and tests the installed copy.

## Limitations

- You are in the loop for every step: one script per turn, pasted by hand.
- Day-to-day use is on Windows PowerShell; the bash path is exercised less.
- Graph searches pay CBM's start-up cost, a few seconds per run.
