# /llm_harness/tests/line_edits_test.py
"""[LINE EDITS] test for llm_harness: a hunk whose OLD CODE matched may change its lines by their number inside that block."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-lines-")
RESULTS = []
STEP = [0]
BODY = "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n"


def run(*lines):
    STEP[0] += 1
    head = ["@SCRIPT [C: step %d]" % STEP[0], "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def edit(old, edits):
    return ["@FILE /m.py", "@HUNK f | edit", "--- [OLD CODE] ---"] + old + ["+++ [LINE EDITS] +++"] + edits + ["=== [END HUNK] ==="]


def body():
    with open(os.path.join(WS, "m.py"), encoding="utf-8") as f:
        return f.read()


def reset():
    with open(os.path.join(WS, "m.py"), "w", newline="\n") as f:
        f.write(BODY)


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def main():
    block = ["    a = 1", "    b = 2", "    c = 3"]
    reset()
    out = run(*edit(block, ["2:     b = 20"]))
    check("'N: text' replaces line N of the matched block", "    b = 20\n" in body() and "    a = 1\n" in body() and "    c = 3\n" in body(), out)
    reset()
    out = run(*edit(block, ["3-", "1+     d = 4", "0+     z = 0"]))
    b = body()
    check("'N-' deletes and 'N+' inserts after line N (0 = before line 1)", "    c = 3" not in b and "    z = 0\n    a = 1\n    d = 4\n    b = 2\n" in b, out)
    reset()
    out = run(*edit(block, ["4: nope"]))
    check("a line number outside the block is refused", "outside the 3 recited OLD CODE lines" in out and body() == BODY, out)
    out = run(*edit(block, ["2: x", "2-"]))
    check("editing one line twice is refused", "line 2 is edited twice" in out and body() == BODY, out)
    out = run(*edit(block, ["2- extra"]))
    check("'N-' with text is refused", "deletes the line and takes no text" in out, out)
    out = run(*edit(["    a = 1", "    b = 999"], ["1: x = 1"]))
    check("a block that does not match changes nothing", "MISMATCH" in out and body() == BODY, out)
    reset()
    run(*edit(block, ["1:     a = 100"]))
    hist = sorted(os.listdir(os.path.join(WS, ".llm_harness", "history")))
    out = run("@UNDO " + hist[-1].split("_")[0].lstrip("0"))
    check("undo restores a line-edit hunk", body().replace("# /" + os.path.basename(WS) + "/m.py\n", "") == BODY, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
