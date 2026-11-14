# /llm_harness/tests/parser_test.py
"""Parser and formatter test for llm_harness: a @GIT_COMMIT path list closes at the next directive when @END_GIT_COMMIT is missing, and Go files are formatted by gofmt (when installed) instead of being rolled back over spaces."""
import os, shutil, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-parser-")
RESULTS = []
STEP = [0]


def run(*lines):
    STEP[0] += 1
    head = ["@SCRIPT [C: step %d]" % STEP[0], "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def git(*args):
    subprocess.run(["git"] + list(args), cwd=WS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def main():
    with open(os.path.join(WS, "a.txt"), "w") as f:
        f.write("a\n")
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    with open(os.path.join(WS, "a.txt"), "w") as f:
        f.write("b\n")
    out = run("@GIT_COMMIT chore: change a", "/a.txt", "@GIT_STATUS")
    check("a commit without @END_GIT_COMMIT closes at the next directive and still commits", "[COMMITTED]" in out and "had no @END_GIT_COMMIT" in out and "closed at the next directive" in out, out)
    out = run("@GIT_COMMIT chore: nothing", "@GIT_STATUS")
    check("a commit with no paths is ignored with a warning", "lists no paths; ignored" in out and "[COMMITTED]" not in out, out)
    with open(os.path.join(WS, "CHANGELOG.md"), "w") as f:
        f.write("# Changelog\n\n## Unreleased\n")
    git("add", "CHANGELOG.md")
    git("commit", "-qm", "changelog")
    with open(os.path.join(WS, "a.txt"), "w") as f:
        f.write("c\n")
    out = run("@GIT_COMMIT feat: change a again", "/a.txt", "@END_GIT_COMMIT")
    check("a feat commit without CHANGELOG.md gets a reminder", "CHANGELOG.md was not in this feat commit" in out, out)
    with open(os.path.join(WS, "a.txt"), "w") as f:
        f.write("d\n")
    with open(os.path.join(WS, "CHANGELOG.md"), "a") as f:
        f.write("- changed a\n")
    out = run("@GIT_COMMIT fix: change a with its changelog line", "/a.txt", "/CHANGELOG.md", "@END_GIT_COMMIT")
    check("a fix commit that includes CHANGELOG.md gets no reminder", "[COMMITTED]" in out and "CHANGELOG.md was not" not in out, out)
    if shutil.which("gofmt"):
        with open(os.path.join(WS, "m.go"), "w", newline="\n") as f:
            f.write("package main\n\nfunc main() {\n\tx := 1\n\t_ = x\n}\n")
        out = run("@FILE /m.go", "@HUNK TOP | add with spaces", "--- [OLD CODE] ---", "    _ = x", "+++ [NEW CODE] +++", "    _ = x", "    y := 2", "    _ = y", "=== [END HUNK] ===")
        text = open(os.path.join(WS, "m.go"), encoding="utf-8").read()
        check("a Go hunk written with spaces is formatted by gofmt, not rolled back", "\ty := 2" in text and "ROLLED BACK" not in out, out)
        out = run("@FILE /m.go", "@HUNK TOP | break", "--- [OLD CODE] ---", "package main", "+++ [NEW CODE] +++", "package main", "this is not go", "=== [END HUNK] ===")
        check("a Go syntax error still rolls back", "ROLLED BACK" in out and "this is not go" not in open(os.path.join(WS, "m.go"), encoding="utf-8").read(), out)
    else:
        print("SKIP gofmt checks: gofmt is not installed on this machine")
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
