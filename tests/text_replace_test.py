# /llm_harness/tests/text_replace_test.py
"""@TEXT_REPLACE test for llm_harness: exact, count-locked, single-line replacement in a file, a scope or a whole folder."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-text-")
RESULTS = []
STEP = [0]


def run(*lines, session=None):
    STEP[0] += 1
    head = ["@SCRIPT [C: step %d]" % STEP[0]] + (["@SESSION " + session] if session else []) + ["@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def tr(target, old, new, expect=None):
    return ["@TEXT_REPLACE %s%s | edit" % (target, " | expect %d" % expect if expect is not None else ""), "--- [OLD TEXT] ---", old, "+++ [NEW TEXT] +++", new, "=== [END TEXT] ==="]


def read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


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
    write("notes.txt", "Entry point (state, numbering, sessions, footer and main)\nan obsolete second line\n")
    write("pkg/a.py", "from old.module.path import x\nfrom old.module.path import y\n")
    write("pkg/b.py", "import old.module.path as p\n")
    write("pkg/c.py", "class K:\n    def f(self):\n        return 'same text here'\n\n    def g(self):\n        return 'same text here'\n")
    out = run(*tr("/notes.txt", "footer and main)", "footer, notes and main)"))
    check("a small edit inside a long line, default expect 1", "footer, notes and main)" in read("notes.txt") and "line 1 now:" in out, out)
    out = run(*tr("/notes.txt", "Entry point (state", "Entry point (machine state"), *tr("/notes.txt", "an obsolete second line", ""))
    check("insert and trim in one script", "Entry point (machine state" in read("notes.txt") and "obsolete second line" not in read("notes.txt"), out)
    out = run(*tr("pkg/**", "old.module.path", "new.module.path", 3))
    check("the same change across a folder with the right count", "old.module.path" not in read("pkg/a.py") + read("pkg/b.py") and out.count("| Scope: TEXT | edit") == 2 and "/pkg/a.py" in out and "/pkg/b.py" in out, out)
    write("pkg/b.py", "import other.module.path as p\nimport other.module.path as q\n")
    out = run(*tr("pkg/**", "other.module.path", "next.module.path", 1))
    check("a wrong count is refused and nothing changes", "found 2 occurrence(s), expected 1" in out and "next.module" not in read("pkg/b.py"), out)
    out = run(*tr("/pkg/c.py", "same text here", "changed text"))
    check("an ambiguous single edit is refused, listing both lines", "found 2 occurrence(s), expected 1" in out and "/pkg/c.py:3" in out and "/pkg/c.py:6" in out, out)
    out = run(*tr("/pkg/c.py#K.g", "same text here", "changed text"))
    check("a #Class.method scope narrows the match", read("pkg/c.py").count("same text here") == 1 and "changed text" in read("pkg/c.py"), out)
    out = run(*tr("/notes.txt", "short", "x"))
    check("OLD TEXT shorter than 12 characters is refused", "[MALFORMED" in out and "at least 12 characters" in out, out)
    out = run("@TEXT_REPLACE /notes.txt | two lines", "--- [OLD TEXT] ---", "Entry point (machine", "state, numbering", "+++ [NEW TEXT] +++", "x", "=== [END TEXT] ===")
    check("multi-line OLD TEXT is refused", "one line each" in out, out)
    before = read("notes.txt")
    run(*tr("/notes.txt", "Entry point (machine state", "Entry point (MACHINE state"))
    hist = sorted(os.listdir(os.path.join(WS, ".llm_harness", "history")))
    last = hist[-1].split("_")[0].lstrip("0")
    out = run("@UNDO " + last)
    check("undo restores a text replacement", read("notes.txt") == before, out)
    run("@SCOPE notes | notes.txt", session="owner")
    out = run(*tr("/notes.txt", "Entry point (machine state", "Entry point (taken"), session="intruder")
    check("another session's scope is respected", "inside session owner's scope" in out and "taken" not in read("notes.txt"), out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
