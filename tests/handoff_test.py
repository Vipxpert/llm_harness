# /llm_harness/tests/handoff_test.py
"""Hand-off test for llm_harness: session aims, uncommitted-work ownership, messages, the brake and the user's session commands, in a git workspace."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-handoff-")
RESULTS = []
STEP = [0]


def run(session, *lines):
    STEP[0] += 1
    payload = "\n".join(["@SCRIPT [C: step %d]" % STEP[0], "@SESSION " + session, "@WORKSPACE " + WS] + list(lines) + [""])
    p = subprocess.run([sys.executable, HARNESS], input=payload.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace").split("=== L1 CONTRACT")[0]


def user(*args):
    p = subprocess.run([sys.executable, HARNESS, "--workspace", WS] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace")


def check(name, ok, out=""):
    RESULTS.append(ok)
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok and out:
        print("    ..." + out[-700:].replace("\n", "\n    "))


def hunk(path, old, new):
    return ["@FILE " + path, "@HUNK TOP | edit", "--- [OLD CODE] ---", old, "+++ [NEW CODE] +++", new, "=== [END HUNK] ==="]


def commit(msg, *paths):
    return ["@GIT_COMMIT " + msg] + ["/" + p for p in paths] + ["@END_GIT_COMMIT"]


def git(*args):
    subprocess.run(["git"] + list(args), cwd=WS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main():
    for d in ("src/a", "src/c"):
        os.makedirs(os.path.join(WS, d), exist_ok=True)
    for f in ("src/a/f.py", "src/shared.py", "src/c.py", "src/c/other.py"):
        with open(os.path.join(WS, f), "w") as fh:
            fh.write("x = 0\n")
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    a, b = "alpha | fix the alpha feature", "beta | fix the beta feature"
    out = run(a, "@SCOPE alpha work | src/a/**, src/shared.py", *hunk("/src/shared.py", "x = 0", "x = 1"))
    check("a session shows its own aim under THIS SESSION", "=== THIS SESSION: alpha | aim: fix the alpha feature" in out, out)
    out = run(a, "@SCOPE_END")
    check("release is refused while its changes are uncommitted", "[RELEASE REFUSED]" in out and "/src/shared.py" in out, out)
    out = run(a, "@SCOPE_END handover")
    check("an explicit hand-over releases and is announced", "uncommitted work handed over: src/shared.py" in out, out)
    out = run(b, *hunk("/src/shared.py", "x = 1", "x = 2"))
    check("no shared-file warning for handed-over work", "[SHARED FILE]" not in out and "[OK] /src/shared.py" in out, out)
    out = run(b, *commit("feat: beta", "src/shared.py"))
    check("handed-over work may be committed by the next session", "[COMMITTED]" in out, out)
    c, d = "gamma | gamma work", "delta | delta work"
    run(c, "@SCOPE gamma | src/c.py", *hunk("/src/c.py", "x = 0", "x = 3"))
    run(c, "@SCOPE gamma narrowed | src/c/**")
    out = run(d, *hunk("/src/c.py", "x = 3", "x = 4"))
    check("editing a file with another session's uncommitted work warns", "[SHARED FILE] /src/c.py holds uncommitted changes from session gamma" in out, out)
    out = run(d, *commit("feat: delta", "src/c.py"))
    check("committing another active session's uncommitted work is refused", "uncommitted changes from other active sessions" in out and "[COMMITTED]" not in out, out)
    out = run(d, "@RUN python \"" + HARNESS + "\" --workspace . --release gamma")
    check("a script cannot run --release", "[REFUSED] --grant, --revoke, --release and --unbrake" in out, out)
    rel = user("--release", "gamma")
    out = run(d, *commit("feat: delta", "src/c.py"))
    check("after the user releases that session, the commit goes through", "SESSION RELEASED BY THE USER" in rel and "[COMMITTED]" in out, out)
    out = run(b, "@MESSAGE alpha | src/shared.py now holds x = 2")
    out1 = run(a, "@GREP x")
    out2 = run(a, "@GREP x")
    check("a message reaches its target once, labelled as from an AI", "message from beta: src/shared.py now holds x = 2" in out1 and "not from the user" in out1 and "message from beta" not in out2, out1)
    run(b, "@SCOPE_REQUEST alpha | src/a/f.py | need it for the beta fix")
    out = run(a, "@GREP x")
    check("a scope request is delivered", "scope request from beta: asks for write access to src/a/f.py | need it for the beta fix" in out, out)
    run(b, "@BRAKE alpha | stop: src/a/f.py breaks beta")
    out = run(a, *hunk("/src/a/f.py", "x = 0", "x = 5"), "@RUN python -c \"print('ran')\"")
    check("a braked session's changes and commands are refused", "[BRAKED] session beta" in out and "braked by session beta" in out and "ran" not in out.split("BRAKED")[-1].split("SESSION")[0], out)
    out = run(a, "@BRAKE_ACK", *hunk("/src/a/f.py", "x = 0", "x = 5"))
    check("@BRAKE_ACK lifts the brake and the change applies", "[BRAKE ACKNOWLEDGED]" in out and "[OK] /src/a/f.py" in out, out)
    out = run(b, "@GREP x")
    check("the braking session hears the acknowledgement", "brake acknowledged from alpha" in out, out)
    run(b, "@BRAKE alpha | again")
    ub = user("--unbrake", "alpha")
    out = run(a, *hunk("/src/a/f.py", "x = 5", "x = 6"))
    check("the user can lift a brake", "BRAKE LIFTED BY THE USER" in ub and "[OK] /src/a/f.py" in out, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
