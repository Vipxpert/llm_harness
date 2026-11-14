# /llm_harness/tests/matcher_test.py
"""Matcher test for llm_harness: OLD CODE is found anywhere before a hunk counts as already applied, whitespace inside lines is tolerated, malformed hints fit the problem, and # Stage changes only trigger the tree reminder where the trees track them."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-matcher-")
RESULTS = []
STEP = [0]


def run(*lines):
    STEP[0] += 1
    head = ["@SCRIPT [C: step %d]" % STEP[0], "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0].replace("\r", "")


def hunk(path, scope, old, new):
    return ["@FILE " + path, "@HUNK %s | edit" % scope, "--- [OLD CODE] ---"] + old + ["+++ [NEW CODE] +++"] + new + ["=== [END HUNK] ==="]


def write(rel, text):
    os.makedirs(os.path.dirname(os.path.join(WS, rel)) or WS, exist_ok=True)
    with open(os.path.join(WS, rel), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def main():
    write("m.py", "def f():\n    a = 1\n    b = 2\n    return a\n\n\ndef g():\n    b = 2\n    return 0\n")
    out = run(*hunk("/m.py", "g", ["    a = 1", "    b = 2"], ["    b = 2"]))
    check("OLD CODE elsewhere in the file wins over NEW CODE found in the named scope", "a = 1" not in read("m.py") and "ALREADY APPLIED" not in out, out)
    write("c.txt", "value\t= 1    # the note\n")
    out = run(*hunk("/c.txt", "TOP", ["value = 1 # the note"], ["value = 2 # the note"]))
    check("whitespace differences inside lines are tolerated and reported", "value = 2" in read("c.txt") and "ignoring whitespace inside lines" in out, out)
    out = run("@TEXT_REPLACE /c.txt | expect 1 | too short", "--- [OLD TEXT] ---", "value", "+++ [NEW TEXT] +++", "x", "=== [END TEXT] ===")
    check("a malformed directive gets a hint that fits it", "fix what the parentheses above say" in out and "a marker line is missing" not in out, out)
    write("docs/trees/tree_master.txt", "# /m/docs/trees/tree_master.txt\n/m (Root)\n\\-- u.py (Uses # Stage: Tracked Stage)\n")
    write("t.py", "x = 1\n# Stage: Untracked Thing\ny = 2\n")
    write("u.py", "x = 1\n# Stage: Tracked Stage\ny = 2\n# Stage: Other Part\nz = 3\n")
    out = run(*hunk("/t.py", "TOP", ["# Stage: Untracked Thing"], ["# Stage: Renamed Thing"]))
    check("a # Stage change in a file the trees do not track gives no reminder", "TREE SYNC REMINDER" not in out, out)
    out = run(*hunk("/u.py", "TOP", ["# Stage: Other Part"], ["# Stage: Renamed Part"]))
    check("a # Stage change in a file whose stages the trees track gives a reminder", "TREE SYNC REMINDER" in out and "Renamed Part" in out, out)
    out = run(*hunk("/t.py", "TOP", ["y = 2"], ["y = 2", "", "def added():", "    return 1"]))
    check("a new definition still gives a reminder", "TREE SYNC REMINDER" in out and "+added" in out, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
