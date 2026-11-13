# /llm_harness/tests/scopes_test.py
"""Scope test for llm_harness: two sessions declare write scopes, see each other's changes once, and cannot write into each other's files."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-scope-")
RESULTS = []


def run(session, *lines):
    payload = "\n".join(["@SCRIPT [C: step]", "@SESSION " + session, "@WORKSPACE " + WS] + list(lines) + [""])
    p = subprocess.run([sys.executable, HARNESS], input=payload.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace")


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-600:].replace("\n", "\n    "))


def hunk(path, old, new):
    return ["@FILE " + path, "@HUNK TOP | edit", "--- [OLD CODE] ---", old, "+++ [NEW CODE] +++", new, "=== [END HUNK] ==="]


def main():
    for d in ("src/etl", "src/api", "docs"):
        os.makedirs(os.path.join(WS, d), exist_ok=True)
    for f in ("src/etl/a.py", "src/etl/own.py", "src/api/b.py", "docs/c.txt"):
        with open(os.path.join(WS, f), "w") as fh:
            fh.write("x = 0\n")
    out = run("etl", "@SCOPE remove dead ETL code | src/etl/**")
    check("A declares a scope", "[SCOPE] etl scope set" in out, out)
    out = run("etl", *hunk("/src/etl/own.py", "x = 0", "x = 1"))
    check("A edits inside its own scope", "[OK] /src/etl/own.py" in out, out)
    out = run("api", "@SCOPE api refactor | src/api/**")
    check("B sees A's scope on its first look", "OTHER SESSIONS' SCOPES (first look)" in out and "etl: remove dead ETL code" in out, out)
    out = run("api", "@SCOPE wider | src/**")
    check("B's overlapping scope is refused", "[SCOPE REFUSED]" in out, out)
    out = run("api", *hunk("/src/etl/a.py", "x = 0", "x = 1"))
    check("B's edit inside A's scope is refused", "inside session etl's scope" in out and "x = 0" in open(os.path.join(WS, "src/etl/a.py")).read(), out)
    out = run("api", *hunk("/src/api/b.py", "x = 0", "x = 2"))
    check("B's edit inside its own scope works", "[OK] /src/api/b.py" in out, out)
    cmd = "python -c \"open('src/etl/a.py','a').write('# touched\\n')\""
    out = run("api", "@RUN " + cmd)
    check("B's @RUN change in A's scope is reported", "[SCOPE ALERT]" in out and "src/etl/a.py changed (session etl)" in out, out)
    out = run("etl", "@SCOPE remove dead ETL code | src/etl/**, docs/**")
    out = run("api", "@GREP x")
    check("B is told once about A's scope change", "SCOPE CHANGES FROM OTHER SESSIONS (shown once)" in out and "docs/**" in out, out)
    out = run("api", "@GREP x = 2")
    check("...and not again", "SCOPE CHANGES FROM OTHER SESSIONS" not in out, out)
    out = run("etl", *hunk("/src/api/b.py", "x = 2", "x = 3"))
    check("A cannot write into B's scope either", "inside session api's scope" in out, out)
    out = run("api", "@RUN python \"" + HARNESS + "\" --workspace . --grant api src/etl/a.py")
    check("a script cannot run the grant command", "[REFUSED] --grant" in out, out)
    g = subprocess.run([sys.executable, HARNESS, "--workspace", WS, "--grant", "api", "src/etl/a.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = run("api", *hunk("/src/etl/a.py", "x = 0", "x = 5"))
    check("after the user's grant, B may edit that one file", "GRANTED" in g.stdout.decode() and "[OK] /src/etl/a.py" in out, out)
    out = run("api", "@UNDO 1..999")
    check("B's undo skips A's scripts", "[UNDO SKIPPED]" in out and "belongs to session etl" in out, out)
    out = run("etl", "@SCOPE_END")
    out = run("api", *hunk("/docs/c.txt", "x = 0", "x = 9"))
    check("after A releases its scope, B may edit there", "[OK] /docs/c.txt" in out and "etl scope released" in out, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
