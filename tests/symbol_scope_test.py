# /llm_harness/tests/symbol_scope_test.py
"""Definition-level and depth scopes for llm_harness: a session may own one class or method, leaving the rest of the file to others, or a folder to a set depth."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-symscope-")
RESULTS = []
STEP = [0]


def run(session, *lines):
    STEP[0] += 1
    head = ["@SCRIPT [C: step %d]" % STEP[0], "@SESSION " + session, "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def hunk(path, old, new):
    return ["@FILE " + path, "@HUNK TOP | edit", "--- [OLD CODE] ---", old, "+++ [NEW CODE] +++", new, "=== [END HUNK] ==="]


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
    write("m.py", "class K:\n    def f(self):\n        return 1\n\n    def g(self):\n        return 2\n")
    write("pkg/top.py", "x = 'top level file'\n")
    write("pkg/sub/deep.py", "y = 'nested level file'\n")
    out = run("alpha", "@SCOPE own f | m.py#K.f")
    check("a session can own one method", "[SCOPE] alpha scope set" in out, out)
    out = run("beta", *hunk("/m.py", "        return 2", "        return 20"))
    check("another session may edit a different method of the same file", "return 20" in read("m.py"), out)
    out = run("beta", *hunk("/m.py", "        return 1", "        return 10"))
    check("an edit inside the owned method is refused, naming the entry", "inside session alpha's scope (m.py#K.f)" in out and "return 10" not in read("m.py"), out)
    out = run("beta", "@SCOPE own g | m.py#K.g")
    check("a sibling method can be owned by another session", "[SCOPE] beta scope set" in out, out)
    out = run("gamma", "@SCOPE whole class | m.py#K")
    check("owning the enclosing class is refused as an overlap", "[SCOPE REFUSED]" in out, out)
    out = run("gamma", "@TEXT_REPLACE /m.py | expect 1 | inside f", "--- [OLD TEXT] ---", "def f(self):", "+++ [NEW TEXT] +++", "def f(self, x=0):", "=== [END TEXT] ===")
    check("a text replacement inside an owned method is refused", "inside session alpha's scope" in out and "x=0" not in read("m.py"), out)
    out = run("gamma", "@DELETE_FILE /m.py")
    check("deleting a file that holds an owned method is refused", os.path.exists(os.path.join(WS, "m.py")), out)
    out = run("delta", "@SCOPE top only | pkg depth 1")
    check("a folder scope can carry a depth", "[SCOPE] delta scope set" in out, out)
    out = run("epsilon", *hunk("/pkg/sub/deep.py", "y = 'nested level file'", "y = 'changed nested file'"))
    check("files below that depth stay free", "changed nested file" in read("pkg/sub/deep.py"), out)
    out = run("epsilon", *hunk("/pkg/top.py", "x = 'top level file'", "x = 'changed top file'"))
    check("files within that depth are owned", "inside session delta's scope" in out and "changed top file" not in read("pkg/top.py"), out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
