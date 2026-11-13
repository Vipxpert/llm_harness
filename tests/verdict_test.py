# /llm_harness/tests/verdict_test.py
"""Verdict test for llm_harness: recorded conclusions about folders, files and definitions, listed with tree descriptions, shown on reads, and marked stale by later changes."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-verdict-")
RESULTS = []
STEP = [0]


def run(*lines, session="alpha", side=False):
    STEP[0] += 1
    head = ["@SCRIPT [P: step %d]" % STEP[0]] + (["@SIDE"] if side else []) + ["@SESSION " + session, "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def write(rel, text):
    os.makedirs(os.path.dirname(os.path.join(WS, rel)) or WS, exist_ok=True)
    with open(os.path.join(WS, rel), "w", encoding="utf-8") as f:
        f.write(text)


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def main():
    write("pkg/a.py", "def one():\n    return 1\n")
    write("pkg/sub/b.py", "def two():\n    return 2\n")
    write("pkg/sub/c.py", "def three():\n    return 3\n")
    write("docs/trees/tree_master.txt", "# /ws/docs/trees/tree_master.txt\n/ws (Root)\n\\-- /pkg (The package that holds the workers)\n    \\-- /sub (Helpers)\n")
    out = run("@VERDICT /pkg | done | nothing left to port", "@VERDICT /pkg/sub | done | same", "@VERDICT /pkg/sub/c.py | keep | must stay Python")
    check("verdicts are recorded and fingerprinted", "[VERDICT] /pkg: done" in out and os.path.isfile(os.path.join(WS, ".llm_harness", "verdicts.json")), out)
    out = run("@VERDICTS")
    check("the listing shows path, verdict and the tree's description", "/pkg/ [done] nothing left to port" in out and "tree: The package that holds the workers" in out, out)
    check("a child that says the same as its parent is hidden", "/pkg/sub/ [done]" not in out, out)
    check("a child with a different verdict is shown", "/pkg/sub/c.py [keep]" in out, out)
    out = run("@PROBE /pkg/a.py | Lines | 1-2", session="beta")
    check("reading code under a verdict shows it", "[VERDICT] /pkg: done - nothing left to port (session alpha)" in out, out)
    write("pkg/new.py", "def four():\n    return 4\n")
    out = run("@VERDICTS all", session="beta")
    check("a new file under a folder verdict marks it stale", "[STALE VERDICT] 1 new" in out and "/pkg/new.py" in out, out)
    out = run("@VERDICTS stale", session="beta")
    check("the stale filter lists only stale verdicts", "/pkg/ [done]" in out and "/pkg/sub/c.py" not in out, out)
    out = run("@VERDICT /pkg/a.py | done", side=True)
    check("a side script cannot record verdicts", "[REFUSED]" in out, out)
    out = run("@VERDICT /pkg/nothing.py | done")
    check("a verdict on a missing path is refused", "[VERDICT REFUSED] /pkg/nothing.py" in out, out)
    out = run("@VERDICT /pkg | clear", "@VERDICTS all")
    check("a verdict can be cleared", "[VERDICT CLEARED] /pkg" in out and "/pkg/ [done]" not in out, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
