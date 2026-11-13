# /llm_harness/tests/concurrency_test.py
"""Concurrency test for llm_harness: many harness processes started at once must each get a unique script number."""
import json, os, re, subprocess, sys, tempfile


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    harness = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    ws = tempfile.mkdtemp(prefix="harness-conc-")
    for i in range(1, n + 1):
        with open(os.path.join(ws, "f%d.py" % i), "w") as f:
            f.write("v = 0\n")
    pause = "Start-Sleep -Milliseconds 300" if os.name == "nt" else "sleep 0.3"
    procs = []
    for i in range(1, n + 1):
        payload = "\n".join(["@SCRIPT [C: change f%d]" % i, "@SESSION %s" % ("etl" if i % 2 else "docs"), "@WORKSPACE " + ws,
                             "@FILE /f%d.py" % i, "@HUNK TOP | bump", "--- [OLD CODE] ---", "v = 0", "+++ [NEW CODE] +++",
                             "v = %d" % i, "=== [END HUNK] ===", "@RUN " + pause, ""])
        p = subprocess.Popen([sys.executable, harness], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        p.stdin.write(payload.encode("utf-8"))
        p.stdin.close()
        procs.append((i, p))
    numbers, problems = [], []
    for i, p in procs:
        out = p.stdout.read().decode("utf-8", "replace")
        p.wait()
        m = re.search(r"\[ASSIGNED\] C-(\d+)", out)
        if m:
            numbers.append(int(m.group(1)))
        else:
            problems.append("script %d got no number; last line: %s" % (i, (out.strip().splitlines() or ["(no output)"])[-1]))
    state_dir = os.path.join(ws, ".llm_harness")
    state, sessions = read_json(os.path.join(state_dir, "state.json")), read_json(os.path.join(state_dir, "sessions.json"))
    changed = 0
    for i in range(1, n + 1):
        with open(os.path.join(ws, "f%d.py" % i), encoding="utf-8") as f:
            changed += ("v = %d" % i) in f.read()
    checks = [("unique numbers 1..%d" % n, sorted(numbers) == list(range(1, n + 1))),
              ("state ends at %d" % n, state.get("last_num") == n),
              ("both sessions registered", sorted(sessions) == ["docs", "etl"]),
              ("all %d files changed" % n, changed == n)]
    for name, ok in checks:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
    for pr in problems:
        print("  " + pr)
    print("test workspace: " + ws)
    sys.exit(0 if all(ok for _, ok in checks) else 1)


if __name__ == "__main__":
    main()
