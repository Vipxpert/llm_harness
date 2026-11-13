# /llm_harness/tests/campaign_test.py
"""Campaign test for llm_harness: a sweep's legend tags verdicts, its rule shows only while it runs, Tree probes overlay verdicts, and ending it removes them."""
import os, subprocess, sys, tempfile

HARNESS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), ".llm_harness", "harness.py")
WS = tempfile.mkdtemp(prefix="harness-campaign-")
RESULTS = []
STEP = [0]


def run(*lines, side=False, footer=False):
    STEP[0] += 1
    head = ["@SCRIPT [P: step %d]" % STEP[0]] + (["@SIDE"] if side else []) + ["@SESSION alpha", "@WORKSPACE " + WS]
    p = subprocess.run([sys.executable, HARNESS], input="\n".join(head + list(lines) + [""]).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    return out if footer else out.split("=== L1 CONTRACT")[0]


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
    write("pkg/a.py", "def one():\n    return 1\n")
    write("lib/b.py", "def two():\n    return 2\n")
    proj = os.path.basename(WS)
    write("docs/trees/tree_master.txt", "# /%s/docs/trees/tree_master.txt\n/%s (Root)\n\u251c\u2500\u2500 /pkg (Workers)\n    \u2514\u2500\u2500 a.py (One)\n\u2514\u2500\u2500 /lib (Library)\n" % (proj, proj))
    out = run("@GREP one", footer=True)
    check("no campaign: no campaign line and no campaign rule", "=== CAMPAIGN" not in out and "[L1-Campaign]" not in out, out)
    out = run("@CAMPAIGN go | done = ported to Go; keep = stays Python", footer=True)
    check("a running campaign shows its legend and its rule", "=== CAMPAIGN go (0 area(s) with a verdict): done = ported to Go; keep = stays Python ===" in out and "[L1-Campaign]" in out, out)
    out = run("@VERDICT /pkg | done | ported", "@VERDICT /lib | clean | nothing here", footer=True)
    check("a legend word joins the campaign, another word is flagged", "=== CAMPAIGN go (1 area(s) with a verdict)" in out and "'clean' is not a word of any active campaign's legend" in out, out)
    out = run("@PROBE /docs/trees/tree_master.txt | Tree | TOP | 3")
    check("Tree probes overlay verdicts on their nodes", "/pkg (Workers)  <<verdict: done (campaign go)>>" in out and "/lib (Library)  <<verdict: clean>>" in out, out)
    write("pkg/new.py", "x = 1\n")
    out = run("@PROBE /docs/trees/tree_master.txt | Tree | TOP | 3")
    check("the overlay marks a stale verdict", "<<verdict: done (campaign go), STALE>>" in out, out)
    out = run("@CAMPAIGN go | done = x", side=True)
    check("a side script cannot start a campaign", "[REFUSED]" in out, out)
    out = run("@CAMPAIGN_END go", "@VERDICTS all", footer=True)
    check("ending the campaign removes its verdicts, its line and its rule", "[CAMPAIGN ENDED] go: 1 verdict(s) removed" in out and "/pkg/ [done]" not in out and "=== CAMPAIGN go" not in out and "[L1-Campaign]" not in out, out)
    check("verdicts outside the campaign survive", "/lib/ [clean]" in out, out)
    print("test workspace: " + WS)
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
