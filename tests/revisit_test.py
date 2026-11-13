# /llm_harness/tests/revisit_test.py
"""[REVISIT] test for llm_harness: rereading an unchanged definition is reported with who read it and when; a changed one is not."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-revisit-")
RESULTS = []
STEP = [0]


def run(*lines, session=None, side=False):
    STEP[0] += 1
    head = ["@SCRIPT [P: step %d]" % STEP[0]] + (["@SIDE"] if side else []) + (["@SESSION " + session] if session else []) + ["@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def main():
    with open(os.path.join(WS, "m.py"), "w") as f:
        f.write("# /m/m.py\nclass Box:\n    def first(self):\n        a = 1\n        return a\n\n    def second(self):\n        b = 2\n        return b\n")
    out = run("@PROBE /m.py | Lines | 3-5", session="alpha")
    check("a first read is silent", "[REVISIT]" not in out, out)
    out = run("@PROBE /m.py | Lines | 3-6", session="alpha")
    check("an overlapping range over the same unchanged method is reported", "[REVISIT] /m.py Box.first() was read" in out and "by session alpha" in out, out)
    out = run("@PROBE /m.py | Scope | Box.first", session="beta")
    check("another session sees who read it before", "Box.first() was read" in out, out)
    run("@FILE /m.py", "@HUNK Box.first | change", "--- [OLD CODE] ---", "        a = 1", "+++ [NEW CODE] +++", "        a = 10", "=== [END HUNK] ===", session="alpha")
    out = run("@PROBE /m.py | Lines | 3-5", session="alpha")
    check("a changed method is not reported", "Box.first() was read" not in out, out)
    out = run("@PROBE /m.py | Scope | Box.second", "@PROBE /m.py | Lines | 7-9", session="alpha")
    check("two reads in the same script are not reported against each other", "Box.second() was read" not in out, out)
    out = run("@PROBE /m.py | Lines | 7-9", side=True, session="gamma")
    out2 = run("@PROBE /m.py | Lines | 7-8", side=True, session="gamma")
    check("side scripts are told too", "Box.second() was read" in out and "Box.second() was read" in out2, out2)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
