# /llm_harness/tests/repeat_test.py
"""Repeatable-script test for llm_harness: @REPEATABLE runs on every paste, ordinary scripts replay, and repeatable scripts cannot change files."""
import os, re, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-repeat-")
RESULTS = []


def run(*lines):
    payload = "\n".join(list(lines) + [""])
    p = subprocess.run([sys.executable, HARNESS], input=payload.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-600:].replace("\n", "\n    "))


def main():
    with open(os.path.join(WS, "a.txt"), "w") as fh:
        fh.write("x = 0\n")
    rep = ["@SCRIPT [R: try a value]", "@REPEATABLE", "@WORKSPACE " + WS, "@RUN python -c \"print('value', 7)\""]
    first, second = run(*rep), run(*rep)
    nums = re.findall(r"\[ASSIGNED\] R-(\d+)", first + second)
    check("an identical @REPEATABLE script runs every time", "value 7" in first and "value 7" in second and "[REPLAY]" not in second and len(set(nums)) == 2, second)
    edited = run(*(rep[:3] + ["@RUN python -c \"print('value', 8)\""]))
    check("an edited value runs too", "value 8" in edited, edited)
    plain = ["@SCRIPT [R: plain]", "@WORKSPACE " + WS, "@RUN python -c \"print('plain')\""]
    run(*plain)
    again = run(*plain)
    check("an ordinary identical script still replays", "[REPLAY]" in again, again)
    bad = run("@SCRIPT [C: change]", "@REPEATABLE", "@WORKSPACE " + WS, "@DELETE_FILE /a.txt")
    check("a @REPEATABLE script cannot change files", "[REFUSED] a @REPEATABLE script" in bad and os.path.exists(os.path.join(WS, "a.txt")), bad)
    numbered = run("@SCRIPT [R-999: numbered]", "@REPEATABLE", "@WORKSPACE " + WS, "@RUN python -c \"print(1)\"")
    check("a @REPEATABLE script cannot carry its own number", "[REFUSED] a @REPEATABLE script" in numbered, numbered)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
