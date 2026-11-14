# /llm_harness/tests/depth_test.py
"""Depth and glob test for llm_harness: '| depth N' on @FIND, @GREP, @LIST_RECURSIVE and @TEXT_REPLACE, and one glob rule for all of them."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-depth-")
RESULTS = []
STEP = [0]
FILES = ["a.py", "pkg/b.py", "pkg/sub/c.py", "pkg/sub/deep/d.py"]


def run(*lines):
    STEP[0] += 1
    head = ["@SCRIPT [P: step %d]" % STEP[0], "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def found(out, rel):
    return ("  /" + rel + "\n") in out.replace("\r", "")


def main():
    for rel in FILES:
        os.makedirs(os.path.dirname(os.path.join(WS, rel)) or WS, exist_ok=True)
        with open(os.path.join(WS, rel), "w", encoding="utf-8") as f:
            f.write("value = 'marker_text_here'\n")
    out = run("@FIND *.py")
    check("a glob without a folder matches file names at any depth", all(found(out, r) for r in FILES), out)
    out = run("@FIND pkg/*.py")
    check("a glob with a folder keeps * inside that one folder", found(out, "pkg/b.py") and not found(out, "pkg/sub/c.py"), out)
    out = run("@FIND pkg/** | depth 2")
    check("@FIND honours depth", found(out, "pkg/b.py") and found(out, "pkg/sub/c.py") and not found(out, "pkg/sub/deep/d.py"), out)
    out = run("@GREP marker_text_here | in pkg/** | depth 1")
    check("@GREP honours depth", "/pkg/b.py:" in out and "/pkg/sub/c.py:" not in out, out)
    out = run("@GREP marker_text_here")
    check("@GREP without depth still searches everything", all(("/" + r + ":") in out for r in FILES), out)
    out = run("@LIST_RECURSIVE /pkg | depth 1")
    check("@LIST_RECURSIVE honours depth", "pkg/b.py" in out and "pkg/sub/c.py" not in out, out)
    out = run("@TEXT_REPLACE pkg/** | expect 2 | depth 2 | two levels", "--- [OLD TEXT] ---", "'marker_text_here'", "+++ [NEW TEXT] +++", "'changed_text_here'", "=== [END TEXT] ===")
    check("@TEXT_REPLACE honours depth", "changed_text_here" in read("pkg/b.py") and "changed_text_here" in read("pkg/sub/c.py") and "marker_text_here" in read("pkg/sub/deep/d.py"), out)
    out = run("@TEXT_REPLACE *.py | expect 2 | by name", "--- [OLD TEXT] ---", "'marker_text_here'", "+++ [NEW TEXT] +++", "'final_text_here!'", "=== [END TEXT] ===")
    check("@TEXT_REPLACE uses the same name rule as @FIND", "final_text_here!" in read("a.py") and "final_text_here!" in read("pkg/sub/deep/d.py"), out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
