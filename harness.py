#!/usr/bin/env python3
# /llm_harness/harness.py
# llm_harness: declarative patch / probe / undo / git harness for LLM-driven edits.
# Stdlib only, OS-agnostic, pure-ASCII source. Reads one payload from stdin per run.
# Install: save next to workflow_commands.txt in one folder (e.g. ~/.llm_harness), then run:
#   python harness.py --workspace "<project folder>"
import os, sys, re, json, glob, time, zlib, base64, fnmatch, hashlib, shutil, contextlib, subprocess, warnings

VERSION = "3.4"
WORKSPACE_NOTES_DIR = ".llm_harness"
MODE = "git"
HARNESS_DIR = os.path.join(".git", "llm_harness")
HISTORY_DIR = os.path.join(HARNESS_DIR, "history")
STATE_PATH = os.path.join(HARNESS_DIR, "state.json")
CONFIG_PATH = os.path.join(HARNESS_DIR, "config.json")
GRAPH_PATH = os.path.join(HARNESS_DIR, "graph.json")
WORKSPACE_NOTES_PATH = os.path.join(WORKSPACE_NOTES_DIR, "notes.md")
GOAL_PATH = os.path.join(WORKSPACE_NOTES_DIR, "goal.md")
SELF_PATH = os.path.abspath(__file__)
SELF_DIR = os.path.dirname(SELF_PATH)
RULES_PATH = os.path.join(SELF_DIR, "workflow_commands.txt")
TREE_MASTER = "docs/trees/tree_master.txt"
MAX_HISTORY, PROBE_CAP, XREF_CAP, LIST_CAP = 50, 500, 80, 400
GREP_CAP, RUN_HEAD, RUN_TAIL, RUN_TIMEOUT = 150, 40, 120, 300
SKIP_DIRS = {".git", ".llm_harness", "venv", ".venv", "node_modules", "__pycache__", "dist", "build", "target", ".next"}
BT3 = chr(96) * 3
SENT_BT3 = "@@" + "BT3" + "@@"
T_MID = chr(0x251c) + chr(0x2500) * 2 + " "
T_END = chr(0x2514) + chr(0x2500) * 2 + " "
T_BAR = chr(0x2502)
OLD_M, NEW_M, END_M = "--- [OLD CODE] ---", "+++ [NEW CODE] +++", "=== [END HUNK] ==="
TXT_OLD, TXT_NEW, TXT_END = "--- [OLD TEXT] ---", "+++ [NEW TEXT] +++", "=== [END TEXT] ==="
TEXT_MIN = 12
LINE_M = "+++ [LINE EDITS] +++"

def apply_line_edits(old, edits):
    """New lines from a hunk's verified OLD CODE plus 'N: text' (replace), 'N-' (delete) and 'N+ text' (insert after N; 0 = before line 1). -> (lines, error)"""
    repl, dels, ins = {}, set(), {}
    for raw in edits:
        if not raw.strip():
            continue
        m = re.match(r"^(\d+)([:+-])(.*)$", raw)
        if not m:
            return None, "line edit '%s' is not 'N: text', 'N-' or 'N+ text'" % raw.strip()
        k, op, rest = int(m.group(1)), m.group(2), m.group(3)
        rest = rest[1:] if rest.startswith(" ") else rest
        if not (0 if op == "+" else 1) <= k <= len(old):
            return None, "line %d is outside the %d recited OLD CODE lines" % (k, len(old))
        if op == "+":
            ins.setdefault(k, []).append(rest)
        elif k in repl or k in dels:
            return None, "line %d is edited twice" % k
        elif op == "-":
            if rest.strip():
                return None, "'%d-' deletes the line and takes no text" % k
            dels.add(k)
        else:
            repl[k] = rest
    if not (repl or dels or ins):
        return None, "no line edits under %s" % LINE_M
    out = list(ins.get(0, []))
    for n_, line in enumerate(old, 1):
        if n_ not in dels:
            out.append(repl.get(n_, line))
        out.extend(ins.get(n_, []))
    return out, None

# ---------------------------------------------------------------- Stage: IO helpers

def norm_nl(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=True, indent=1)

def decode_bytes(raw):
    """Bytes -> (LF text, meta). Meta keeps EOL style and BOM so untouched lines round-trip byte-exact."""
    text = raw.decode("utf-8", "surrogateescape")
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    crlf = text.count("\r\n")
    eol = "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"
    return norm_nl(text), {"eol": eol, "bom": bom}

def encode_text(text, meta):
    body = text.rstrip("\n") + "\n"
    if meta["eol"] != "\n":
        body = body.replace("\n", meta["eol"])
    if meta["bom"]:
        body = "\ufeff" + body
    return body.encode("utf-8", "surrogateescape")

def default_meta(rel):
    return {"eol": "\r\n" if rel.lower().endswith((".bat", ".cmd")) else "\n", "bom": False}

def read_bytes(rel):
    with open(rel, "rb") as f:
        return f.read()

def write_bytes(rel, data):
    os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
    with open(rel, "wb") as f:
        f.write(data)

def read_text(rel):
    return decode_bytes(read_bytes(rel))[0]

def sha_bytes(data):
    return None if data is None else hashlib.sha1(data).hexdigest()

def sha_of(rel):
    return sha_bytes(read_bytes(rel)) if os.path.isfile(rel) else None

def pack(data):
    return None if data is None else base64.b64encode(zlib.compress(data, 9)).decode("ascii")

def unpack(s):
    return None if s is None else zlib.decompress(base64.b64decode(s))

def safe_rel(p, allow_root=False):
    """Normalize a payload path to a workspace-relative path; None when it escapes the workspace or targets git or harness internals."""
    p = p.strip().replace("\\", "/").lstrip("/")
    root = os.path.realpath(os.path.abspath("."))
    full = os.path.realpath(os.path.join(root, p or "."))
    try:
        if os.path.commonpath((root, full)) != root:
            return None
    except ValueError:
        return None
    if full == root:
        return "." if allow_root else None
    rel = os.path.relpath(full, root).replace("\\", "/")
    if rel in (".git", WORKSPACE_NOTES_DIR) or rel.startswith((".git/", WORKSPACE_NOTES_DIR + "/")):
        return None
    return rel

def decode_payload(raw):
    """Stdin bytes -> payload text: UTF-8 (BOM tolerated), LF, NBSP -> space, UI-safe sentinels decoded."""
    txt = norm_nl(raw.decode("utf-8-sig", "replace")).replace("\u00a0", " ")
    txt = re.sub(r"([A-Za-z][A-Za-z0-9+.\-]*)@@://", r"\1://", txt)
    return txt.replace(SENT_BT3, BT3)

# ---------------------------------------------------------------- Stage: Line-1 header and tree glyphs

HDR_PREFIX = {".py": "# ", ".sh": "# ", ".txt": "# ", ".yaml": "# ", ".yml": "# ", ".go": "// ", ".js": "// ",
              ".ts": "// ", ".c": "// ", ".cpp": "// ", ".h": "// ", ".rs": "// ", ".vue": "<!-- ", ".html": "<!-- ",
              ".cmd": ":: # ", ".bat": ":: # "}
HDR_SUFFIX = {".vue": " -->", ".html": " -->"}
HDR_LIKE = re.compile(r"^(?:# |// |:: # )/\S+$|^<!-- /\S+ -->$")
CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_PROJECT = []

def project_name():
    if not _PROJECT:
        _PROJECT.append(load_json(CONFIG_PATH, {}).get("project") or os.path.basename(os.path.abspath(".")))
    return _PROJECT[0]

def ensure_line1_header(rel, text):
    """Keep the '/<project>/<path>' comment as the first line, below a shebang or coding cookie when present."""
    ext = os.path.splitext(rel)[1].lower()
    if ext not in HDR_PREFIX or os.path.abspath(".") == SELF_DIR:
        return text, "N/A"
    lines = text.split("\n")
    hdr = HDR_PREFIX[ext] + "/" + project_name() + "/" + rel + HDR_SUFFIX.get(ext, "")
    status = "Valid"
    if len(lines) > 1 and HDR_LIKE.match(lines[0]) and lines[1].startswith("#!"):
        lines[0], lines[1] = lines[1], lines[0]
        status = "Auto-Repaired"
    idx = 1 if lines[0].startswith("#!") else 0
    if ext == ".py" and idx < len(lines) and CODING_RE.match(lines[idx]):
        idx += 1
    if idx < len(lines) and lines[idx] == hdr:
        return "\n".join(lines), status
    if idx < len(lines) and HDR_LIKE.match(lines[idx]):
        lines[idx] = hdr
    else:
        lines.insert(idx, hdr)
    return "\n".join(lines), "Auto-Repaired"

def is_tree_file(rel):
    return rel.startswith("docs/trees/") and rel.endswith(".txt")

def sanitize_tree(rel, text):
    """Tree files only: ASCII connectors -> box glyphs, vertical guide bars -> spaces."""
    if not is_tree_file(rel):
        return text
    t = re.sub(r"(?m)^( *)\|-- +", lambda m: m.group(1) + T_MID, text)
    t = re.sub(r"(?m)^( *)[\\" + chr(96) + r"]-- +", lambda m: m.group(1) + T_END, t)
    return t.replace(T_BAR + "   ", "    ")

# ---------------------------------------------------------------- Stage: Scope resolution

_MOD = r"(?:(?:export|default|public|private|protected|static|abstract|async|final|override|unsafe|pub(?:\([^)]*\))?)\s+)*"
DEF_KW = re.compile(r"^\s*" + _MOD + r"(class|def|func|function|fn|type|interface|struct|enum|trait|impl|namespace)\b\*?\s*(?:\(([^)]*)\)\s*)?([A-Za-z_$][\w$]*)?")
JS_METHOD = re.compile(r"^\s*" + _MOD + r"(?:get\s+|set\s+)?(?!(?:if|for|while|switch|catch|return|else|do|try|with|function|await|new|throw|typeof)\b)([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::\s*[^={;]+)?\{\s*$")
JS_ASSIGN = re.compile(r"^\s*" + _MOD + r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\([^()]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)")
JS_PROP = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
VUE_OPT = re.compile(r"^\s*(methods|computed|watch|props|emits|components)\s*:\s*[\{\[]")
SFC_BLOCK = re.compile(r"^<(template|script|style)\b")
ANCHOR_RE = re.compile(r"^\s*(?:#|//|<!--|--|;|::)\s*#?\s*(?:Stage|Step)\b")
TOC_EXTRA = re.compile(r"^\s*@\w|\b(?:HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE)\(|\b(?:app|router)\.(?:get|post|put|patch|delete|use)\(")
IDENT = re.compile(r"^[A-Za-z_$][\w$]*(?:\(\))?$")
DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)+(?:\(\))?$")
SKIPPABLE = ("#", "//", "/*", "*/", "<!--")
CLOSERS = ("}", ")", "]", "</")

def def_info(line):
    """(defined_name, receiver_type) when the line opens a class/function/method/SFC block, else None."""
    m = SFC_BLOCK.match(line)
    if m:
        return m.group(1), None
    m = DEF_KW.match(line)
    if m:
        if not m.group(3):
            return None
        recv = None
        if m.group(2):
            ids = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[.*?\]", "", m.group(2)))
            recv = ids[-1] if ids else None
        return m.group(3), recv
    for rx in (JS_METHOD, JS_ASSIGN, JS_PROP, VUE_OPT):
        m = rx.match(line)
        if m:
            return m.group(1), None
    return None

def is_toc_line(line):
    return bool(def_info(line) or ANCHOR_RE.match(line) or TOC_EXTRA.search(line))

def indent_of(line):
    return len(line) - len(line.lstrip())

def norm_ws(s):
    return " ".join(s.split())

def block_bounds(lines, i, anchor):
    """Line range [start, end) of the block opened at line i (a definition, or a '# Stage' style anchor)."""
    n, base = len(lines), indent_of(lines[i])
    m = SFC_BLOCK.match(lines[i])
    if m:
        for j in range(i + 1, n):
            if lines[j].startswith("</" + m.group(1)):
                return i, j + 1
        return i, n
    end = n
    for j in range(i + 1, n):
        s = lines[j].strip()
        if not s:
            continue
        ind = indent_of(lines[j])
        if anchor:
            if (ind <= base and ANCHOR_RE.match(lines[j])) or (ind < base and not s.startswith(SKIPPABLE)):
                end = j
                break
        elif not s.startswith(SKIPPABLE) and (ind < base or (ind == base and not s.startswith(CLOSERS))):
            end = j
            break
    start = i
    if not anchor:
        while end - 1 > i and (not lines[end - 1].strip() or (indent_of(lines[end - 1]) <= base and lines[end - 1].strip().startswith(SKIPPABLE + ("@",)))):
            end -= 1
        while start > 0 and lines[start - 1].strip().startswith(SKIPPABLE + ("@", "*")) and 0 <= indent_of(lines[start - 1]) - base <= 1:
            start -= 1
    else:
        while end - 1 > i and not lines[end - 1].strip():
            end -= 1
    return start, end

def split_scope(scope):
    segs = []
    for part in scope.split(" > "):
        part = part.strip()
        if not part:
            continue
        if DOTTED.match(part):
            segs.extend(x for x in re.split(r"\.|::", re.sub(r"\(\)$", "", part)) if x)
        else:
            segs.append(part)
    return segs

def resolve_code_scope(lines, scope):
    """Scope path -> (start, end, note). note is None when every segment resolved to exactly one block."""
    segs = split_scope(scope)
    lo, hi, n, k = 0, len(lines), len(lines), 0
    while k < len(segs):
        seg = segs[k]
        if IDENT.match(seg):
            name = re.sub(r"\(\)$", "", seg)
            if k + 1 < len(segs) and IDENT.match(segs[k + 1]):
                nxt = re.sub(r"\(\)$", "", segs[k + 1])
                pair = [j for j in range(lo, hi) if def_info(lines[j]) == (nxt, name)]
                if len(pair) == 1:
                    lo, hi = block_bounds(lines, pair[0], False)
                    k += 2
                    continue
            hits = [j for j in range(lo, hi) if (def_info(lines[j]) or (None,))[0] == name]
            anchor = False
        else:
            key = norm_ws(seg)
            hits = [j for j in range(lo, hi) if key in norm_ws(lines[j])]
            hits = [j for j in hits if lines[j].strip().startswith(SKIPPABLE)] or hits
            anchor = True
        if not hits:
            return 0, n, "scope segment '%s' not found" % seg
        if len(hits) > 1:
            where = ", ".join(str(j + 1) for j in hits[:6])
            return 0, n, "scope segment '%s' matches %d places (lines %s); qualify it as Parent > %s" % (seg, len(hits), where, seg)
        lo, hi = block_bounds(lines, hits[0], anchor)
        k += 1
    return lo, hi, None

def tree_split(line):
    s = line.lstrip(" ")
    pad = len(line) - len(s)
    for c in (T_MID, T_END, "|-- ", "\\-- "):
        if s.startswith(c):
            return pad // 4 + 1, s[len(c):].strip()
    return pad // 4, s.strip()

def tree_nodes(lines):
    """[(line_index, depth, label)] for every node line of a tree file (Line-1 header and blanks skipped)."""
    out = []
    for i, ln in enumerate(lines):
        if not ln.strip() or (i == 0 and HDR_LIKE.match(ln)):
            continue
        d, label = tree_split(ln)
        out.append((i, d, label))
    return out

def tree_key(label):
    k = label.split(" (", 1)[0].strip()
    k = re.sub(r"^(?:->|\(\))\s*", "", k)
    return re.sub(r"\(\)$", "", k).strip()

def tree_path(nodes, k):
    out, d = [tree_key(nodes[k][2])], nodes[k][1]
    for j in range(k - 1, -1, -1):
        if nodes[j][1] < d:
            d = nodes[j][1]
            out.append(tree_key(nodes[j][2]))
    return " > ".join(reversed(out))

def resolve_tree_scope(lines, scope):
    """Node path -> (start, end, note, (nodes, first, stop)). Segments may skip levels but must be unique."""
    nodes = tree_nodes(lines)
    lo, hi, cur = 0, len(nodes), None
    for seg in [s.strip() for s in scope.split(" > ") if s.strip()]:
        key = tree_key(seg)
        cands = [j for j in range(lo, hi) if seg in nodes[j][2] or key in nodes[j][2]]
        exact = [j for j in cands if tree_key(nodes[j][2]) == key or tree_key(nodes[j][2]).split(" / ")[0] == key]
        cands = exact or cands
        if not cands:
            return 0, len(lines), "node '%s' not found" % seg, None
        if len(cands) > 1:
            paths = "; ".join(tree_path(nodes, j) for j in cands[:6])
            return 0, len(lines), "node '%s' matches %d places (%s); qualify it as Parent > %s" % (seg, len(cands), paths, seg), None
        cur = cands[0]
        end = cur + 1
        while end < len(nodes) and nodes[end][1] > nodes[cur][1]:
            end += 1
        lo, hi = cur + 1, end
    if cur is None:
        return 0, len(lines), None, (nodes, 0, len(nodes))
    stop_line = nodes[hi][0] if hi < len(nodes) else len(lines)
    return nodes[cur][0], stop_line, None, (nodes, cur, hi)

def resolve_scope(rel, lines, scope):
    scope = (scope or "").strip()
    if scope.upper() in ("", "TOP"):
        return 0, len(lines), None
    if is_tree_file(rel):
        return resolve_tree_scope(lines, scope)[:3]
    return resolve_code_scope(lines, scope)

# ---------------------------------------------------------------- Stage: Matching

def kx(s):
    return s.rstrip().replace("\u00a0", " ")

def kf(s):
    return s.strip().replace("\u00a0", " ")

def find_exact(lines, needle, lo, hi):
    keys = [kx(x) for x in needle]
    n = len(keys)
    return [(i, i + n) for i in range(lo, hi - n + 1)
            if kx(lines[i]) == keys[0] and all(kx(lines[i + k]) == keys[k] for k in range(1, n))]

def find_fuzzy(lines, needle, lo, hi):
    """Indentation-agnostic match that tolerates differing blank lines; returns every candidate span."""
    keys = [kf(x) for x in needle if x.strip()]
    out = []
    if not keys:
        return out
    for i in range(lo, hi):
        if kf(lines[i]) != keys[0]:
            continue
        j, k, last = i, 0, i
        while j < hi and k < len(keys):
            s = kf(lines[j])
            if s:
                if s != keys[k]:
                    break
                last, k = j, k + 1
            j += 1
        if k == len(keys):
            out.append((i, last + 1))
    return out

def first_nonblank(xs):
    for x in xs:
        if x.strip():
            return x
    return ""

def reindent(disk_first, needle_first, repl_lines):
    """Shift replacement lines by the indentation delta between the on-disk block and the payload block."""
    dws = disk_first[:indent_of(disk_first)]
    nws = needle_first[:indent_of(needle_first)]
    if dws == nws:
        return list(repl_lines)
    tabs = "\t" in dws
    def width(ws):
        return len(ws.replace("    ", "\t")) if tabs else len(ws.replace("\t", "    "))
    delta, unit, out = width(dws) - width(nws), ("\t" if tabs else " "), []
    for rl in repl_lines:
        if not rl.strip():
            out.append("")
        else:
            out.append(unit * max(0, width(rl[:indent_of(rl)]) + delta) + rl.lstrip())
    return out

def apply_match(rel, content, scope, find_s, repl_s):
    """Scope-first unique matching. -> (new_content, status, detail); status is OK / ALREADY APPLIED / MISMATCH / AMBIGUOUS."""
    lines = content.split("\n")
    repl = repl_s.split("\n") if repl_s else []
    if (scope or "").strip().upper() == "EOF":
        if find_s:
            return content, "MISMATCH", "scope EOF appends to the file: OLD CODE must be empty"
        if not repl:
            return content, "MISMATCH", "scope EOF with empty NEW CODE"
        if find_exact(lines, repl, 0, len(lines)):
            return content, "ALREADY APPLIED", None
        base = content.rstrip("\n")
        return (base + "\n" if base else "") + repl_s + "\n", "OK", None
    if not find_s:
        return content, "MISMATCH", "empty OLD CODE (use scope EOF to append, or include the lines to replace)"
    needle = find_s.split("\n")
    lo, hi, note = resolve_scope(rel, lines, scope)
    spans = [(lo, hi)] if note is None and (lo, hi) != (0, len(lines)) else []
    spans.append((0, len(lines)))
    for idx, (a, b) in enumerate(spans):
        for finder, fuzzy in ((find_exact, False), (find_fuzzy, True)):
            hits = finder(lines, needle, a, b)
            if len(hits) == 1:
                s, e = hits[0]
                block = reindent(lines[s], first_nonblank(needle), repl) if fuzzy else repl
                where = "matched outside the named scope, at line %d" % (s + 1) if idx == 1 and len(spans) == 2 else note
                return "\n".join(lines[:s] + block + lines[e:]), "OK", where
            if len(hits) > 1:
                where = ", ".join(str(h[0] + 1) for h in hits[:8])
                tip = note or "add distinguishing context lines or a narrower scope"
                return content, "AMBIGUOUS", "OLD CODE matches %d places (lines %s); %s" % (len(hits), where, tip)
        if repl:
            rh = find_exact(lines, repl, a, b) or find_fuzzy(lines, repl, a, b)
            if len(rh) == 1:
                return content, "ALREADY APPLIED", None
    if len(needle) == 1 and needle[0].strip():
        part = [i for i, l in enumerate(lines) if needle[0].strip() in l and l.strip() != needle[0].strip()]
        if len(part) == 1:
            return content, "MISMATCH", "OLD CODE is only part of line %d; hunks match whole lines, so copy that full line into OLD CODE" % (part[0] + 1)
    return content, "MISMATCH", note

def print_diagnostics(rel, content, scope, find_s, status, detail):
    """On-disk excerpt for a failed hunk: Stage A best window, Stage B scope head, Stage C signature TOC."""
    lines = content.split("\n")
    tot = len(lines)
    print("\n--- [%s] /%s | Scope: %s%s ---" % (status, rel, scope, (" | " + detail) if detail else ""))
    if status == "AMBIGUOUS" or not find_s:
        return
    lo, hi, note = resolve_scope(rel, lines, scope)
    scoped = note is None and (lo, hi) != (0, tot)
    keys = [kf(l) for l in find_s.split("\n") if len(l.strip()) >= 4]
    toks = set(re.findall(r"[A-Za-z_]\w{2,}", find_s))
    win = max(5, len(find_s.split("\n")))
    line_toks = [set(re.findall(r"[A-Za-z_]\w{2,}", l)) for l in lines]
    best, best_i = 0, -1
    for i in range(lo, hi):
        chunk = range(i, min(hi, i + win))
        cl = set(kf(lines[j]) for j in chunk)
        lh = sum(1 for k in keys if k in cl)
        th = len(toks & set().union(*[line_toks[j] for j in chunk])) if toks else 0
        if (lh >= 1 or (toks and th * 2 >= len(toks))) and lh * 4 + th > best:
            best, best_i = lh * 4 + th, i
    if best_i >= 0:
        a, b, stage = max(lo, best_i - 8), min(hi, best_i + win + 12), "A (closest on-disk block)"
    elif scoped:
        a, b, stage = lo, min(hi, lo + 40), "B (start of resolved scope)"
    else:
        print("--- ON-DISK SIGNATURE TOC [Lines 1-%d of %d | Stage: C] ---" % (tot, tot))
        for i, l in enumerate(lines):
            if is_toc_line(l):
                print("  L%d: %s" % (i + 1, l.rstrip()[:110]))
        return
    print("--- ON-DISK CODE [Lines %d-%d of %d | Stage: %s] ---" % (a + 1, b, tot, stage))
    print("\n".join(lines[a:b]))

# ---------------------------------------------------------------- Stage: Syntax checks and rollback

def restore_pre(rel, raw, mode):
    if raw is None:
        if os.path.isfile(rel):
            os.remove(rel)
        return
    write_bytes(rel, raw)
    if mode is not None and os.name == "posix":
        os.chmod(rel, mode)

def run_tool(args, cwd=None):
    """Run an external tool; returncode None means the tool is not installed."""
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout, r.stderr
    except OSError:
        return None, "", ""

def check_files(changed, pre):
    """Syntax-check changed files; restore the pre-image on Python or gofmt syntax errors. -> ({rel: status}, rolled_back)"""
    status, rolled = {}, set()
    for rel in changed:
        if not os.path.isfile(rel):
            status[rel] = "DELETED"
            continue
        if rel.endswith(".py"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    compile(read_bytes(rel), rel, "exec", dont_inherit=True)
                status[rel] = "PASS"
            except Exception as e:
                restore_pre(rel, pre[rel]["raw"], pre[rel]["mode"])
                rolled.add(rel)
                status[rel] = "SYNTAX ERROR -> restored (%s)" % " ".join(str(e).split())[:160]
        elif rel.endswith(".go"):
            before = sha_of(rel)
            rc, out, err = run_tool(["gofmt", "-e", "-w", rel])
            if rc is None:
                status[rel] = "UNCHECKED (gofmt not found)"
            elif rc != 0:
                msg = ((err or out).strip().splitlines() or ["syntax error"])[0]
                restore_pre(rel, pre[rel]["raw"], pre[rel]["mode"])
                rolled.add(rel)
                status[rel] = "GOFMT ERROR -> restored (%s)" % msg[:160]
            elif sha_of(rel) != before:
                status[rel] = "PASS (formatted by gofmt, e.g. spaces from the chat turned back into tabs)"
            else:
                status[rel] = "PASS"
        elif rel.endswith(".json"):
            try:
                json.loads(read_text(rel))
                status[rel] = "PASS"
            except ValueError as e:
                status[rel] = "WARN_JSON (%s)" % str(e)[:120]
        else:
            status[rel] = "UNCHECKED"
    go_files = [f for f in changed if f.endswith(".go") and os.path.isfile(f) and f not in rolled]
    mod_dir = None
    for gf in go_files:
        d = os.path.dirname(os.path.abspath(gf))
        while d and d != os.path.dirname(d) and not mod_dir:
            if os.path.isfile(os.path.join(d, "go.mod")):
                mod_dir = d
            d = os.path.dirname(d)
    if mod_dir:
        rc, out, err = run_tool(["go", "build", "./..."], cwd=mod_dir)
        if rc is None:
            for gf in go_files:
                status[gf] += " | build UNCHECKED (go not found)"
        elif rc != 0:
            err_txt = (err or out).strip()
            for gf in go_files:
                status[gf] = "BUILD_PENDING (%s)" % (err_txt.splitlines() or ["error"])[0][:160]
            print("\n=== COMPILER DIAGNOSTICS ===\n" + err_txt)
    return status, rolled

# ---------------------------------------------------------------- Stage: Payload parser

DIRECTIVES = ("@SCRIPT @WORKSPACE @GOAL @FILE @HUNK @NEW_FILE @DELETE_FILE @RENAME @RUN "
              "@PROBE @XREF @FIND @GREP @LIST_DIR @LIST_RECURSIVE @INIT_DEBUG @UNDO @GIT_INIT @GIT_STATUS @GIT_COMMIT @GIT_FOLD @SIDE "
              "@NOTE_ADD @NOTE_EDIT @NOTE_REMOVE @NOTE_LIST @HELP @SESSION @SCOPE @SCOPE_END @SESSION_END "
              "@MESSAGE @SCOPE_REQUEST @BRAKE @BRAKE_ACK @REPEATABLE @TEXT_REPLACE @VERDICT @VERDICTS @CAMPAIGN @CAMPAIGN_END")
DEBUG_SKELETON = """# Stage: Symptom & Target Invariant
(replace: exact discrepancy between observed behavior and required state)

# Stage: Functionality & Prerequisite Checklist
(replace: one line per atomic prerequisite, each marked [PASS], [FAIL] or [UNVERIFIED - TEST NEXT])

# Stage: Eliminated Approaches & Touched Scopes
(replace: one line per attempt: [<ACT>-XX] | /path/file (Scope) | Hypothesis | Observed Result. Never retry anything listed here.)

# Stage: Isolation & Telemetry Plan
(replace: next diagnostic action: @XREF on a suspect symbol, @PROBE on an uninspected scope, a minimal reproduction, or a debug-log injection)"""

def parse_payload(txt):
    """One state machine: directives are recognized only outside hunk and file bodies."""
    P = {"id": None, "num": None, "title": "", "workspace": None, "goal": None, "ops": [], "notes": [], "probes": [], "xrefs": [], "helps": [],
         "runs": [], "finds": [], "greps": [],
         "list_dir": [], "list_rec": [], "undo": None, "git": [], "git_status": False, "git_init": False, "side": False, "session": None,
         "scope": None, "scope_end": False, "session_end": False, "session_aim": None, "msgs": [], "brake_ack": False, "repeatable": False,
         "verdicts": [], "verdict_lists": [], "campaigns": [], "warnings": []}
    lines = txt.split("\n")
    n, i, cur_file, stray = len(lines), 0, None, []

    def bad(kind, file, label, why, at):
        P["ops"].append({"type": kind, "file": (file or "?").lstrip("/"), "scope": "-", "label": label, "malformed": "%s (payload line %d)" % (why, at + 1)})

    while i < n:
        ln = lines[i]
        m = re.match(r"^@([A-Z_]+)(?:[ \t]+(.*))?$", ln)
        if not m:
            if ln.strip() and not ln.startswith("#"):
                stray.append(i + 1)
            i += 1
            continue
        d, arg = m.group(1), (m.group(2) or "").strip()
        if d == "SCRIPT":
            mm = re.match(r"^\[([^\]:]+)(?::\s*([^\]]*))?\]", arg)
            if mm:
                P["id"], P["title"] = mm.group(1).strip(), (mm.group(2) or "").strip()
                num = re.search(r"\d+", P["id"])
                P["num"] = int(num.group(0)) if num else None
        elif d == "WORKSPACE":
            P["workspace"] = arg.strip().strip('"').strip("'")
        elif d == "SESSION":
            name, _, aim = arg.partition("|")
            P["session"] = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-") or None
            P["session_aim"] = aim.strip() or None
        elif d == "SCOPE":
            P["scope"] = arg
        elif d == "SCOPE_END":
            P["scope_end"] = arg.strip() or True
        elif d == "SESSION_END":
            P["session_end"] = arg.strip() or True
        elif d in ("MESSAGE", "SCOPE_REQUEST", "BRAKE"):
            P["msgs"].append(({"MESSAGE": "message", "SCOPE_REQUEST": "scope request", "BRAKE": "brake"}[d], arg))
        elif d == "VERDICT":
            P["verdicts"].append(arg)
        elif d == "VERDICTS":
            P["verdict_lists"].append(arg)
        elif d in ("CAMPAIGN", "CAMPAIGN_END"):
            P["campaigns"].append((d, arg))
        elif d == "REPEATABLE":
            P["repeatable"] = True
        elif d == "BRAKE_ACK":
            P["brake_ack"] = True
        elif d == "GOAL":
            if arg:
                P["goal"] = arg
            else:
                P["warnings"].append("@GOAL needs the goal text, e.g. '@GOAL Port every Python service to Go'")
        elif d == "RUN":
            mt = re.match(r"^--timeout\s+(\d+)\s+(.*)$", arg)
            cmd, limit = (mt.group(2), min(3600, int(mt.group(1)))) if mt else (arg, RUN_TIMEOUT)
            if cmd.strip():
                P["runs"].append({"cmd": cmd.strip(), "timeout": max(1, limit)})
            else:
                P["warnings"].append("@RUN needs a command")
        elif d == "FIND":
            P["finds"].append(arg)
        elif d == "GREP":
            P["greps"].append(arg)
        elif d == "INIT_DEBUG":
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", arg).strip("_")
            if not slug:
                P["warnings"].append("@INIT_DEBUG needs a slug, e.g. '@INIT_DEBUG login_loop'")
            else:
                path = "docs/debug/bug_%s.txt" % slug
                P["ops"].append({"type": "new_file", "file": path, "scope": "TOP", "label": "Init debug ledger", "replace": DEBUG_SKELETON, "init": True})
                P["probes"].append("/%s | Lines | 1-200" % path)
        elif d == "FILE":
            cur_file = arg
        elif d == "NEW_FILE":
            j = i + 1
            while j < n and lines[j].rstrip() != "@END_NEW_FILE":
                j += 1
            if j >= n:
                bad("new_file", arg, "Create file", "missing @END_NEW_FILE", i)
            else:
                P["ops"].append({"type": "new_file", "file": arg, "scope": "TOP", "label": "Create file", "replace": "\n".join(lines[i + 1:j])})
                i = j
        elif d == "HUNK":
            scope, _, label = arg.partition("|")
            scope, label = scope.strip() or "TOP", label.strip() or scope.strip()
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n or lines[j].rstrip() != OLD_M:
                bad("hunk", cur_file, label, "expected '%s' right after @HUNK" % OLD_M, i)
            else:
                k = j + 1
                while k < n and lines[k].rstrip() not in (NEW_M, LINE_M):
                    k += 1
                e = k + 1
                while e < n and lines[e].rstrip() != END_M:
                    e += 1
                old, new = lines[j + 1:k], lines[k + 1:e]
                edit_err = None
                if k < n and e < n and lines[k].rstrip() == LINE_M:
                    new, edit_err = apply_line_edits("\n".join(old).strip("\n").split("\n"), new)
                if k >= n:
                    bad("hunk", cur_file, label, "missing '%s' (or '%s')" % (NEW_M, LINE_M), i)
                elif e >= n:
                    bad("hunk", cur_file, label, "missing '%s'" % END_M, i)
                elif edit_err:
                    bad("hunk", cur_file, label, edit_err, i)
                    i = e
                elif any(x.rstrip() in (OLD_M, NEW_M, LINE_M) for x in old + new) or any(x.rstrip() == END_M for x in old):
                    bad("hunk", cur_file, label, "a hunk terminator is missing before the next hunk; resend this hunk and the ones after it", i)
                    i = e
                elif not cur_file:
                    bad("hunk", None, label, "@HUNK before any @FILE", i)
                    i = e
                else:
                    find_s = "\n".join(old).strip("\n")
                    repl_s = "\n".join(new).strip("\n") if find_s else "\n".join(new).rstrip("\n")
                    P["ops"].append({"type": "hunk", "file": cur_file, "scope": scope, "label": label, "find": find_s, "replace": repl_s})
                    i = e
        elif d == "TEXT_REPLACE":
            target, _, rest = arg.partition("|")
            m = re.search(r"\bexpect\s+(\d+)", rest)
            expect = int(m.group(1)) if m else 1
            md = re.search(r"\bdepth\s+(\d+)", rest)
            label = re.sub(r"\b(expect|depth)\s+\d+", "", rest).strip(" |") or "Text replace"
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n or lines[j].rstrip() != TXT_OLD:
                bad("text", target.strip(), label, "expected '%s' right after @TEXT_REPLACE" % TXT_OLD, i)
            else:
                k = j + 1
                while k < n and lines[k].rstrip() != TXT_NEW:
                    k += 1
                e = k + 1
                while e < n and lines[e].rstrip() != TXT_END:
                    e += 1
                old, new = lines[j + 1:k], lines[k + 1:e]
                if k >= n or e >= n:
                    bad("text", target.strip(), label, "missing '%s' or '%s'" % (TXT_NEW, TXT_END), i)
                elif len(old) != 1 or len(new) > 1:
                    bad("text", target.strip(), label, "OLD TEXT and NEW TEXT are one line each; use @HUNK for multi-line changes", i)
                elif len(old[0]) < TEXT_MIN:
                    bad("text", target.strip(), label, "OLD TEXT needs at least %d characters so it cannot hit the wrong place; include more of the line" % TEXT_MIN, i)
                else:
                    P["ops"].append({"type": "text", "file": target.strip(), "scope": "TEXT", "label": label, "find": old[0], "replace": new[0] if new else "", "expect": expect,
                                     "depth": int(md.group(1)) if md else None})
                i = e
        elif d == "DELETE_FILE":
            P["ops"].append({"type": "delete", "file": arg, "scope": "TOP", "label": "Delete file"})
        elif d == "RENAME":
            a, sep, b = arg.partition("->")
            if not sep or not a.strip() or not b.strip():
                bad("rename", arg, "Rename file", "expected '@RENAME /old -> /new'", i)
            else:
                P["ops"].append({"type": "rename", "file": a.strip(), "to": b.strip(), "scope": "TOP", "label": "Rename to " + b.strip()})
        elif d == "NOTE_ADD":
            if not arg:
                P["warnings"].append("@NOTE_ADD needs non-empty text")
            else:
                P["notes"].append({"action": "add", "text": arg})
        elif d == "NOTE_EDIT":
            nm = re.match(r"^(N\d+)\s+(.+)$", arg)
            if not nm:
                P["warnings"].append("expected '@NOTE_EDIT <note-id> <text>'")
            else:
                P["notes"].append({"action": "edit", "id": nm.group(1).upper(), "text": nm.group(2)})
        elif d == "NOTE_REMOVE":
            if not re.fullmatch(r"N\d+", arg, re.IGNORECASE):
                P["warnings"].append("expected '@NOTE_REMOVE <note-id>'")
            else:
                P["notes"].append({"action": "remove", "id": arg.upper()})
        elif d == "NOTE_LIST":
            if arg:
                P["warnings"].append("@NOTE_LIST takes no arguments")
            else:
                P["notes"].append({"action": "list"})
        elif d == "PROBE":
            P["probes"].append(arg)
        elif d == "HELP":
            P["helps"].append(arg)
        elif d == "XREF":
            P["xrefs"].append(arg)
        elif d == "LIST_DIR":
            P["list_dir"].append(arg)
        elif d == "LIST_RECURSIVE":
            P["list_rec"].append(arg)
        elif d == "UNDO":
            P["undo"] = arg
        elif d == "GIT_STATUS":
            P["git_status"] = True
        elif d == "GIT_INIT":
            P["git_init"] = arg or True
        elif d == "SIDE":
            P["side"] = True
        elif d == "GIT_FOLD":
            P["git"].append({"kind": "fold", "n": int(arg) if arg.isdigit() else 0})
        elif d == "GIT_COMMIT":
            j = i + 1
            while j < n and lines[j].rstrip() != "@END_GIT_COMMIT" and not lines[j].lstrip().startswith("@"):
                j += 1
            closed = j < n and lines[j].rstrip() == "@END_GIT_COMMIT"
            paths = [x.strip() for x in lines[i + 1:j] if x.strip() and not x.startswith("#")]
            if not closed:
                P["warnings"].append("@GIT_COMMIT at payload line %d had no @END_GIT_COMMIT; its path list was closed at the %s -> end every path list with @END_GIT_COMMIT" % (i + 1, "next directive" if j < n else "end of the script"))
            if paths:
                P["git"].append({"kind": "commit", "msg": arg, "paths": paths})
            else:
                P["warnings"].append("@GIT_COMMIT at payload line %d lists no paths; ignored" % (i + 1))
            i = j if closed else j - 1
        else:
            P["warnings"].append("unknown directive @%s at payload line %d was ignored -> valid directives: %s" % (d, i + 1, DIRECTIVES))
        i += 1
    if stray:
        P["warnings"].append("%d stray line(s) outside any block (first at payload line %d): a block marker is probably missing -> resend the affected hunks" % (len(stray), stray[0]))
    if re.search(r"\[(\w+://[^\]\s]+)\]\(\1\)", txt):
        P["warnings"].append("the payload contains a markdown-autolinked URL ([url](url)), so the chat UI altered the code -> resend with the scheme written as https" + "@@" + "://")
    return P

# ---------------------------------------------------------------- Stage: Executing file operations

NEXT_STEPS = {
    "MISMATCH": "rebuild ONLY that hunk from the on-disk excerpt printed below (never from memory) and resend it under the next script number.",
    "AMBIGUOUS": "resend ONLY that hunk with 2-3 more distinguishing context lines or a narrower scope.",
    "MALFORMED": "a marker line is missing (if the fence was cut short, write " + SENT_BT3 + " for three backticks); resend that hunk and every hunk after it.",
    "ROLLED BACK": "the file was restored to its pre-script state; resend every hunk for that file, corrected.",
    "REFUSED": "use a path inside the workspace, written from its root (e.g. /src/app.py).",
}

def structure_sig(text):
    """Names of the classes, functions and # Stage anchors in a file: the things the architecture tree mirrors."""
    sig = set()
    for ln in text.split("\n"):
        info = def_info(ln)
        if info:
            sig.add(info[0])
        elif ANCHOR_RE.match(ln):
            sig.add(norm_ws(ln.strip()))
    return sig

def tree_sync_reminder(changed, pre):
    """Say so when a script changed code structure (or added/removed files) without touching any tree file in the same script."""
    if not os.path.isfile(TREE_MASTER) or any(is_tree_file(rel) for rel in changed):
        return
    notes = []
    for rel in changed:
        if rel.startswith("docs/trees/"):
            continue
        before = structure_sig(decode_bytes(pre[rel]["raw"])[0]) if pre[rel]["raw"] is not None else None
        after = structure_sig(read_text(rel)) if os.path.isfile(rel) else None
        if before is None:
            notes.append("/%s: file added" % rel)
        elif after is None:
            notes.append("/%s: file removed" % rel)
        elif before != after:
            diff = ["+" + x for x in sorted(after - before)[:5]] + ["-" + x for x in sorted(before - after)[:5]]
            notes.append("/%s: %s" % (rel, ", ".join(diff)))
    if notes:
        print("\n=== TREE SYNC REMINDER ===")
        for n in notes:
            print("  " + n)
        print("  -> no tree file under /docs/trees/ was updated in this script: send the matching tree hunk(s) in the next script, scoped by parent node path.")

def execute_ops(ops):
    """Apply ops on an in-memory view, flush once, syntax-check, print the scorecard. -> (hunk_log, files_log)"""
    cur, meta, pre, dirty, new_mode, fails = {}, {}, {}, [], {}, []

    def load(rel):
        if rel not in cur:
            if os.path.isfile(rel):
                raw = read_bytes(rel)
                cur[rel], meta[rel] = decode_bytes(raw)
                pre[rel] = {"raw": raw, "mode": os.stat(rel).st_mode & 0o777}
            else:
                cur[rel], meta[rel], pre[rel] = None, None, {"raw": None, "mode": None}
        return cur[rel]

    def touch(rel):
        if rel not in dirty:
            dirty.append(rel)

    for op in ops:
        op["detail"] = None
        if op.get("malformed"):
            op["status"], op["detail"] = "MALFORMED", op["malformed"]
            continue
        if op.get("blocked"):
            op["status"], op["detail"] = "REFUSED", op["blocked"]
            continue
        rel = safe_rel(op["file"])
        if rel is None:
            op["status"], op["detail"] = "REFUSED", "path is outside the workspace or inside git or harness internals"
            continue
        op["file"] = rel
        text = load(rel)
        if op["type"] == "hunk":
            if text is None:
                op["status"], op["detail"] = "MISMATCH", "file does not exist (use @NEW_FILE)"
                continue
            op["find"], op["replace"] = sanitize_tree(rel, op["find"]), sanitize_tree(rel, op["replace"])
            new, st, detail = apply_match(rel, text, op["scope"], op["find"], op["replace"])
            op["status"], op["detail"] = st, detail
            if st == "OK":
                cur[rel] = new
                touch(rel)
            elif st in ("MISMATCH", "AMBIGUOUS"):
                fails.append((op, text))
        elif op["type"] == "text_file":
            if text is None:
                op["status"], op["detail"] = "MISMATCH", "file does not exist"
                continue
            lines = text.split("\n")
            at = [i for i in range(op["lo"], min(op["hi"], len(lines))) if op["find"] in lines[i]]
            if sum(lines[i].count(op["find"]) for i in at) != op["count"]:
                op["status"], op["detail"] = "MISMATCH", "an earlier change in this script moved or altered the matches; send the text replacement in its own script"
                continue
            for i in at:
                lines[i] = lines[i].replace(op["find"], op["replace"])
            cur[rel] = "\n".join(lines)
            touch(rel)
            shown = lines[at[0]].strip()
            op["status"], op["detail"] = "OK", "line %d now: %s" % (at[0] + 1, shown if len(shown) <= 110 else shown[:110] + "...")
        elif op["type"] == "new_file":
            if text is not None and op.get("init"):
                op["status"], op["detail"] = "ALREADY APPLIED", "ledger exists: update it with @FILE hunks scoped by its # Stage anchors"
            elif text is not None:
                op["status"], op["detail"] = "MISMATCH", "file exists: use @FILE hunks"
            else:
                cur[rel], meta[rel] = sanitize_tree(rel, op["replace"]), default_meta(rel)
                op["status"] = "OK"
                touch(rel)
        elif op["type"] == "delete":
            if text is None:
                op["status"] = "ALREADY APPLIED"
            else:
                cur[rel] = None
                op["status"] = "OK"
                touch(rel)
        elif op["type"] == "rename":
            dst = safe_rel(op["to"])
            if dst is None:
                op["status"], op["detail"] = "REFUSED", "target path is outside the repository"
                continue
            op["to"] = dst
            dtext = load(dst)
            if text is None and dtext is not None:
                op["status"] = "ALREADY APPLIED"
            elif text is None or dtext is not None:
                op["status"], op["detail"] = "MISMATCH", "source missing" if text is None else "target already exists"
            else:
                cur[dst], meta[dst], cur[rel] = text, meta[rel], None
                new_mode[dst] = pre[rel]["mode"]
                op["status"] = "OK"
                touch(rel)
                touch(dst)

    changed = []
    for rel in dirty:
        if cur[rel] is None:
            if os.path.isfile(rel):
                os.remove(rel)
                changed.append(rel)
            continue
        text, _ = ensure_line1_header(rel, sanitize_tree(rel, cur[rel]))
        data = encode_text(text, meta[rel] or default_meta(rel))
        if data != pre[rel]["raw"]:
            write_bytes(rel, data)
            changed.append(rel)
            if pre[rel]["raw"] is None and os.name == "posix":
                mode = new_mode.get(rel) or (0o755 if text.startswith("#!") else None)
                if mode:
                    os.chmod(rel, mode)

    checks, rolled = check_files(changed, pre) if changed else ({}, set())
    print("\n=== EXECUTION SCORECARD ===")
    for op in ops:
        files = [op["file"]] + ([op["to"]] if op.get("to") and op["status"] == "OK" else [])
        if op["status"] == "OK" and any(f in rolled for f in files):
            op["status"] = "ROLLED BACK"
        diag = any(f[0] is op for f in fails)
        st = op["status"] + ((" (%s)" % op["detail"]) if op["detail"] and not diag else "")
        chk = (checks.get(files[-1]) or "UNCHANGED") if op["status"] in ("OK", "ROLLED BACK") else "-"
        print("  [%s] /%s | Scope: %s | %s | Check: %s" % (st, op["file"].lstrip("/"), op["scope"], op["label"], chk))
    seen = set(op["status"] for op in ops)
    for st in ("MISMATCH", "AMBIGUOUS", "MALFORMED", "ROLLED BACK", "REFUSED"):
        if st in seen:
            print("  -> [%s]: %s" % (st, NEXT_STEPS[st]))
    if any("BUILD_PENDING" in v for v in checks.values()):
        print("  -> [BUILD_PENDING]: fix the errors listed under COMPILER DIAGNOSTICS in the next script.")
    if seen == {"ALREADY APPLIED"}:
        print("  -> Every hunk was already applied: this is a repeated paste. Do not regenerate; tell the user in one sentence which script to paste next.")
    for op, text in fails:
        print_diagnostics(op["file"], text, op["scope"], op["find"], op["status"], op["detail"])
    live = [rel for rel in changed if rel not in rolled]
    tree_sync_reminder(live, pre)
    if any(os.path.abspath(rel) == SELF_PATH for rel in live) and pre.get(os.path.relpath(SELF_PATH, ".").replace("\\", "/"), {}).get("raw"):
        write_bytes(os.path.join(SELF_DIR, "harness.prev.py"), pre[os.path.relpath(SELF_PATH, ".").replace("\\", "/")]["raw"])
        print("\n  NOTE: harness.py patched itself; the previous version is saved as harness.prev.py. If the harness misbehaves, copy that file over harness.py.")

    files_log = {}
    for rel in changed:
        post = sha_of(rel)
        if post != sha_bytes(pre[rel]["raw"]):
            files_log[rel] = {"pre": pack(pre[rel]["raw"]), "mode": pre[rel]["mode"], "post_sha": post, "undone": False}
    hunk_log = [{k: op.get(k) for k in ("type", "file", "to", "scope", "label", "status", "find", "replace")} for op in ops]
    return hunk_log, files_log

# ---------------------------------------------------------------- Stage: Journal and undo

def history_path(num):
    found = glob.glob(os.path.join(HISTORY_DIR, "%04d_*.json" % num))
    return found[0] if found else None

def save_history(P, raw_txt, hunk_log, files_log):
    """Journal the payload, per-hunk outcomes and pre-image snapshots so undo can restore exactly."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    old_path = history_path(P["num"])
    old = load_json(old_path, None) if old_path else None
    if old and old.get("id") == P["id"] and not all(f.get("undone") for f in old.get("files", {}).values()):
        for rel, fl in old["files"].items():
            if rel in files_log:
                files_log[rel]["pre"], files_log[rel]["mode"] = fl["pre"], fl["mode"]
            else:
                files_log[rel] = fl
        hunk_log = old.get("hunks", []) + hunk_log
    if old_path:
        os.remove(old_path)
    name = "%04d_%s.json" % (P["num"], re.sub(r"[^A-Za-z0-9_-]", "_", P["id"]))
    save_json(os.path.join(HISTORY_DIR, name), {"id": P["id"], "num": P["num"], "title": P["title"], "session": P.get("session"),
                                                 "payload": raw_txt, "hunks": hunk_log, "files": files_log})
    entries = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")))
    for stale in entries[:max(0, len(entries) - MAX_HISTORY)]:
        try:
            os.remove(stale)
        except OSError:
            pass

def run_undo(spec, state):
    """Reverse journaled scripts newest-first: exact snapshot restore when the file is unchanged since, else reverse-diff."""
    nums = [int(x) for x in re.findall(r"\d+", spec)]
    if not nums:
        print("  [UNDO ERROR] expected '@UNDO <hi>..<lo>' or '@UNDO <num>', got: " + spec)
        return
    hi, lo, seen = max(nums), min(nums), 0
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")), reverse=True):
        e = load_json(path, None)
        if not e or not (lo <= e.get("num", -1) <= hi):
            continue
        if CURRENT_SESSION and e.get("session") and e.get("session") != CURRENT_SESSION:
            print("\n  [UNDO SKIPPED] [%s] belongs to session %s; undo it from that session." % (e["id"], e["session"]))
            continue
        seen += 1
        print("\n=== REVERSING [%s: %s] ===" % (e["id"], e["title"]))
        if e["num"] <= state.get("committed_through", 0):
            print("  NOTE: this script was already committed; undo only edits the working tree.")
        for rel, fl in e["files"].items():
            if fl.get("undone"):
                print("  [ALREADY UNDONE] /" + rel)
            elif sha_of(rel) == fl["post_sha"]:
                restore_pre(rel, unpack(fl["pre"]), fl["mode"])
                fl["undone"] = True
                print("  [RESTORED] /%s (exact pre-script snapshot%s)" % (rel, "; file removed" if fl["pre"] is None else ""))
            else:
                hs = [h for h in reversed(e["hunks"]) if h["file"] == rel and h["type"] == "hunk" and h["status"] == "OK"]
                if not hs or not os.path.isfile(rel):
                    print("  [SKIPPED] /%s changed after this script; undo the later scripts first or restore it by hand" % rel)
                    continue
                text, meta = decode_bytes(read_bytes(rel))
                done = True
                for h in hs:
                    if not h["replace"]:
                        done = False
                        print("  [MANUAL] /%s | %s | deleted block cannot be re-placed automatically:\n%s" % (rel, h["label"], h["find"]))
                        continue
                    scope = "TOP" if h["scope"].strip().upper() == "EOF" else h["scope"]
                    text, st, detail = apply_match(rel, text, scope, h["replace"], h["find"])
                    done = done and st == "OK"
                    print("  [%s] /%s | %s%s" % ("REVERSED" if st == "OK" else "SKIPPED: " + st, rel, h["label"], (" | " + detail) if detail and st != "OK" else ""))
                write_bytes(rel, encode_text(text, meta))
                fl["undone"] = done
        save_json(path, e)
    if not seen:
        print("  [UNDO] no journaled scripts in range %d..%d" % (hi, lo))

# ---------------------------------------------------------------- Stage: Git

def run_git(args):
    rc, out, err = run_tool(["git"] + list(args))
    return (127 if rc is None else rc), out.strip("\n"), (err.strip() if rc is not None else "git not found")

def git_blockers():
    names = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "index.lock")
    return [x for x in names if os.path.exists(os.path.join(".git", x))]

GITIGNORE_DEFAULT = """# Machine-local harness state (numbering, undo journal, saved runs, graph stamp).
# goal.md and notes.md are project memory and stay tracked.
.llm_harness/*
!.llm_harness/goal.md
!.llm_harness/notes.md
!.llm_harness/verdicts.json
__pycache__/
"""

def carry_state(src, with_state):
    """Copy machine-local history (journal, saved runs, and optionally the numbering) from src into the current state folder."""
    for sub in ("history", "runs"):
        if os.path.isdir(os.path.join(src, sub)):
            shutil.copytree(os.path.join(src, sub), os.path.join(HARNESS_DIR, sub), dirs_exist_ok=True)
    if with_state and os.path.isfile(os.path.join(src, "state.json")):
        os.makedirs(HARNESS_DIR, exist_ok=True)
        shutil.copyfile(os.path.join(src, "state.json"), STATE_PATH)

def git_init(ident):
    """@GIT_INIT [Name <email>]: create the repository on main, add the default .gitignore, set this repository's identity when given, switch this run to git mode carrying numbering and undo history, and show the commit identity."""
    print("\n=== GIT INIT ===")
    if MODE == "git":
        print("  already a git repository; nothing to do.")
        return
    rc, out, err = run_git(["init", "-b", "main"])
    if rc != 0:
        rc, out, err = run_git(["init"])
        if rc == 0:
            run_git(["symbolic-ref", "HEAD", "refs/heads/main"])
    if rc != 0:
        print("  [FAILED] git init: %s" % (err or out))
        return
    print("  " + out.strip())
    if not os.path.exists(".gitignore"):
        with open(".gitignore", "w", encoding="utf-8", newline="\n") as f:
            f.write(GITIGNORE_DEFAULT)
        print("  wrote .gitignore (machine state ignored; goal.md and notes.md tracked)")
    old = HARNESS_DIR
    set_mode("git")
    carry_state(old, False)
    print("  this run is now in git mode; numbering and undo history carried over")
    m = re.match(r"^(.+?)\s*<([^<>@\s]+@[^<>\s]+)>$", ident) if isinstance(ident, str) else None
    if m:
        run_git(["config", "user.name", m.group(1).strip()])
        run_git(["config", "user.email", m.group(2)])
    elif isinstance(ident, str):
        print("  [WARNING] identity not understood: expected '@GIT_INIT Name <email>'; nothing was set")
    name, email = run_git(["config", "user.name"])[1].strip(), run_git(["config", "user.email"])[1].strip()
    print("  commits will be authored as: %s <%s>" % (name or "(no name set)", email or "(no email set)"))
    if not name or not email:
        print("  -> commits will fail until a name and email are set: the user runs git config user.name / user.email in this folder.")
    elif (name, email) == (run_git(["config", "--global", "user.name"])[1].strip(), run_git(["config", "--global", "user.email"])[1].strip()):
        print("  -> that is the global identity; if this repository needs another, use '@GIT_INIT Name <email>' or set it here with git config user.name / user.email (no --global) before committing.")

def git_status_report():
    print("\n=== GIT STATUS ===")
    if MODE != "git":
        print("  no-git workspace: there is no repository to report on.")
        return
    _, branch, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if run_git(["rev-parse", "-q", "--verify", "HEAD"])[0] != 0:
        branch = (run_git(["symbolic-ref", "--short", "HEAD"])[1] or "main") + " (no commits yet)"
    rc, up, _ = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    line = "  Branch: %s | Upstream: %s" % (branch or "(no commits yet)", up if rc == 0 else "none")
    if rc == 0:
        rc2, cnt, _ = run_git(["rev-list", "--left-right", "--count", "@{u}...HEAD"])
        if rc2 == 0 and len(cnt.split()) == 2:
            line += " | ahead %s, behind %s" % (cnt.split()[1], cnt.split()[0])
    print(line)
    print("  In-progress operations: " + (", ".join(git_blockers()) or "none"))
    _, log, _ = run_git(["log", "--oneline", "-n", "20", "HEAD", "--not", "--remotes"])
    print("  Unpushed commits (foldable, newest first):" + ("" if log else " none"))
    for l in log.splitlines():
        print("    " + l)
    _, st, _ = run_git(["status", "--short"])
    rows = st.splitlines()
    print("  Working tree:" + ("" if rows else " clean"))
    for l in rows[:60]:
        print("    " + l)
    if len(rows) > 60:
        print("    ... +%d more" % (len(rows) - 60))

MEMORY_FILES = {p: p for p in (WORKSPACE_NOTES_DIR + "/goal.md", WORKSPACE_NOTES_DIR + "/notes.md", WORKSPACE_NOTES_DIR + "/verdicts.json", ".gitignore")}

def run_git_ops(ops):
    """Pre-flight guard, optional fold of unpushed commits, path-scoped commits; any failure restores the original HEAD."""
    print("\n=== GIT ACTIONS ===")
    blockers = git_blockers()
    if blockers:
        print("  [ABORTED] in-progress git operation (%s). Nothing was committed. -> the user must finish or abort it first." % ", ".join(blockers))
        return False
    _, branch, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    rc, orig, _ = run_git(["rev-parse", "--verify", "-q", "HEAD"])
    orig = orig if rc == 0 else None
    if orig and branch == "HEAD":
        print("  [ABORTED] detached HEAD. Check out a branch first. Nothing was committed.")
        return False

    def fail(msg):
        print("  [FAILED] " + msg)
        if orig:
            run_git(["reset", "--soft", orig])
            run_git(["reset", "-q"])
            print("  [RESTORED] HEAD is back at %s; working tree untouched; nothing is staged. -> fix the cause above and resend under the next script number." % orig[:10])
        return False

    folded = committed = False
    fold_paths = []
    for op in ops:
        if op["kind"] == "fold":
            n = op["n"]
            busy = [nm for nm, rec in load_json(sessions_path(), {}).items() if nm != CURRENT_SESSION and rec.get("scope")]
            if busy:
                return fail("@GIT_FOLD is blocked while other sessions hold scopes (%s): folding would rewrite their commits too" % ", ".join(busy))
            if folded or committed:
                return fail("@GIT_FOLD must be the first and only fold in the script")
            if not orig or n < 1:
                return fail("@GIT_FOLD needs a positive commit count and existing history")
            if run_git(["diff", "--cached", "--quiet"])[0] != 0:
                return fail("the index has staged changes; unstage them before folding")
            for k in range(n):
                rc, out, _ = run_git(["rev-list", "--parents", "-n", "1", "HEAD~%d" % k])
                if rc != 0 or len(out.split()) != 2:
                    return fail("HEAD~%d is a root or merge commit: cannot fold %d commit(s)" % (k, n))
            rc, refs, _ = run_git(["for-each-ref", "--contains", "HEAD~%d" % (n - 1), "refs/remotes"])
            if rc != 0 or refs.strip():
                return fail("the last %d commit(s) reach pushed history, which is immutable" % n)
            _, names, _ = run_git(["diff", "--name-only", "--no-renames", "-z", "HEAD~%d" % n, "HEAD"])
            fold_paths = [p for p in names.split("\0") if p]
            rc, _, err = run_git(["reset", "-q", "HEAD~%d" % n])
            if rc != 0:
                return fail("git reset: " + err)
            folded = True
            print("  [FOLDED] last %d unpushed commit(s) reset; their changes are back in the working tree for re-slicing" % n)
        else:
            paths = [MEMORY_FILES.get(p.strip().replace("\\", "/").lstrip("/")) or safe_rel(p) for p in op["paths"]]
            if not op["msg"] or not paths or None in paths:
                return fail("@GIT_COMMIT needs a subject and repo-relative paths: '%s'" % op["msg"])
            if CURRENT_SESSION:
                foreign = [(p, foreign_uncommitted(p)) for p in paths]
                foreign = [(p, s) for p, s in foreign if s]
                if foreign:
                    return fail("these paths hold uncommitted changes from other active sessions, and committing them would take that work along: %s (they commit theirs, hand it over, or the user runs --release)" % "; ".join("%s: %s" % (p, ", ".join(s)) for p, s in foreign))
            rc, _, err = run_git(["add", "-A", "--"] + paths)
            if rc != 0:
                return fail("git add: " + err)
            rc, out, err = run_git(["commit", "-m", op["msg"], "--"] + paths)
            if rc != 0:
                return fail("git commit '%s': %s" % (op["msg"], ((err or out).strip().splitlines() or ["error"])[-1]))
            committed = True
            print("  [COMMITTED] %s %s (%d path(s))" % (run_git(["rev-parse", "--short", "HEAD"])[1], op["msg"], len(paths)))
            kind = re.match(r"^(feat|fix|perf)\b", op["msg"])
            if kind and os.path.isfile("CHANGELOG.md") and "CHANGELOG.md" not in paths:
                print("  -> CHANGELOG.md was not in this %s commit: add a line under '## Unreleased' and commit it with the next change" % kind.group(1))
    if folded and fold_paths:
        _, left, _ = run_git(["status", "--porcelain", "--"] + fold_paths)
        if left.strip():
            return fail("the fold left these paths out of the re-sliced commits: " + ", ".join(x[3:] for x in left.splitlines()))
    return committed

# ---------------------------------------------------------------- Stage: Read-only directives

def repo_files(base):
    """Files under base: tracked plus untracked-but-not-ignored in git mode, else a walk that skips tool and build folders."""
    rc, out, _ = run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", base]) if MODE == "git" else (1, "", "")
    if rc == 0:
        files = [p for p in out.split("\0") if p and os.path.isfile(p)]
    else:
        files = []
        for r, dirs, fs in os.walk(base):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            files += [os.path.relpath(os.path.join(r, f), ".").replace("\\", "/") for f in fs]
    return sorted(set(f for f in files if not SKIP_DIRS.intersection(f.split("/")[:-1])))

def run_list(arg, recursive):
    arg, depth = split_depth(arg)
    dp = safe_rel(arg, allow_root=True)
    label = "RECURSIVE" if recursive else "NON-RECURSIVE"
    if dp is None or not os.path.isdir(dp):
        print("\n=== DIR LISTING (%s): /%s (NOT FOUND) ===" % (label, arg.strip().lstrip("/")))
        return
    print("\n=== DIR LISTING (%s): /%s%s ===" % (label, "" if dp == "." else dp, " | depth %d" % depth if depth else ""))
    if recursive:
        rows = [r for r in repo_files(dp) if within_depth(r, "" if dp == "." else dp, depth)]
    else:
        rows = [x + ("/" if os.path.isdir(os.path.join(dp, x)) else "") for x in sorted(os.listdir(dp)) if x != ".git"]
    for x in rows[:LIST_CAP]:
        print("  " + x)
    if len(rows) > LIST_CAP:
        print("  ... +%d more (list a subfolder)" % (len(rows) - LIST_CAP))

def run_xref(arg):
    """@XREF <symbol> is a text search; '| callers', '| callees' or '| impact' asks the code graph."""
    sym, _, mode = arg.partition("|")
    sym, mode = sym.strip(), mode.strip().lower()
    if not mode:
        return xref_text(sym)
    if mode not in GRAPH_MODES:
        print("\n=== XREF: %s | %s ===" % (sym, mode))
        print("  [WARNING] unknown @XREF mode '%s' -> use callers, callees or impact" % mode)
        return
    graph_xref(sym, mode)

def xref_text(sym, header=True):
    """Whole-word references to a symbol across the repo, definitions first."""
    if header:
        print("\n=== XREF: %s ===" % sym)
    if not sym:
        return
    pat = re.compile(r"(?<![\w$])" + re.escape(re.sub(r"\(\)$", "", sym)) + r"(?![\w$])")
    defs, refs = [], []
    for rel in repo_files("."):
        try:
            if os.path.getsize(rel) > 1000000:
                continue
            raw = read_bytes(rel)
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        for i, l in enumerate(norm_nl(raw.decode("utf-8", "replace")).split("\n")):
            if pat.search(l):
                info = def_info(l)
                row = "/%s:%d: %s" % (rel, i + 1, l.strip()[:140])
                (defs if info and pat.fullmatch(info[0]) else refs).append(row)
    for row in defs:
        print("  DEF  " + row)
    for row in refs[:XREF_CAP]:
        print("  ref  " + row)
    if len(refs) > XREF_CAP:
        print("  ... +%d more references (narrow the symbol)" % (len(refs) - XREF_CAP))
    if not defs and not refs:
        print("  (no matches)")

# ---------------------------------------------------------------- Stage: Code graph (optional codebase-memory-mcp backend)

GRAPH_MODES = {"callers": ("inbound", 1), "callees": ("outbound", 1), "impact": ("inbound", 5)}
CBM_NOISE = re.compile(r"^(level=|hint:|Preparing one-shot|Running \w+ locally|Completed \w+ \()")

def find_cbm():
    """The codebase-memory-mcp executable on PATH or in its default install folders, or None."""
    exe = shutil.which("codebase-memory-mcp")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    cands = [os.path.expanduser("~/.local/bin/codebase-memory-mcp")]
    if local:
        cands.insert(0, os.path.join(local, "Programs", "codebase-memory-mcp", "codebase-memory-mcp.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None

def run_cbm(exe, args, timeout):
    """One-shot 'codebase-memory-mcp cli ...' call; CBM keeps every project's index in its own per-user cache -> (returncode or None, stdout, stderr-or-reason)."""
    try:
        r = subprocess.run([exe, "cli"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ds" % timeout
    except OSError as e:
        return None, "", str(e)

def cbm_clean(text):
    """CBM output without its progress, hint and log lines."""
    return "\n".join(l for l in norm_nl(text).split("\n") if not CBM_NOISE.match(l.strip())).strip("\n")

def cbm_json(text):
    """The first JSON object in CBM output, or None."""
    lines = norm_nl(text).split("\n")
    for i, l in enumerate(lines):
        if l.lstrip().startswith("{"):
            for chunk in ("\n".join(lines[i:]), l):
                try:
                    obj = json.loads(chunk)
                    return obj if isinstance(obj, dict) else None
                except ValueError:
                    pass
    return None

def workspace_stamp():
    """Fingerprint of the source files (path, size, mtime) that moves whenever source moves: git HEAD plus changed files, or every file without git."""
    if MODE != "git":
        h = hashlib.sha1()
        for rel in repo_files("."):
            try:
                st = os.stat(rel)
                h.update(("%s:%d:%d|" % (rel, st.st_size, st.st_mtime_ns)).encode("utf-8", "surrogateescape"))
            except OSError:
                pass
        return h.hexdigest()
    h = hashlib.sha1(run_git(["rev-parse", "-q", "--verify", "HEAD"])[1].encode())
    _, out, _ = run_git(["status", "--porcelain", "-z", "--untracked-files=all"])
    for entry in out.split("\0"):
        path = entry[3:] if len(entry) > 3 and entry[2] == " " else entry
        h.update(entry.encode("utf-8", "surrogateescape"))
        try:
            st = os.stat(path)
            h.update(("%d:%d" % (st.st_size, st.st_mtime_ns)).encode())
        except OSError:
            pass
    return h.hexdigest()

GAP_SECTIONS = {"parse_partial": "partly parsed (lines %s)", "parse_unusable": "unusable", "skipped": "skipped"}

def index_gaps(exe, project):
    """{path: problem} for files CBM indexed only partly, could not use, or skipped, from index_status."""
    rc, out, _ = run_cbm(exe, ["index_status", "--project", project, "--diagnostics", "full"], 60)
    gaps, section = {}, None
    if rc != 0:
        return gaps
    for ln in cbm_clean(out).split("\n"):
        s = ln.strip()
        if s and not ln.startswith(" ") and s.endswith(":"):
            section = s[:-1]
            continue
        if section in GAP_SECTIONS and indent_of(ln) >= 4 and s and not re.match(r"^\w+:", s):
            parts = s.split()
            text = GAP_SECTIONS[section]
            gaps[parts[0].replace("\\", "/")] = text % (parts[1] if len(parts) > 1 else "?") if "%s" in text else text
    return gaps

_COVERAGE_SHOWN = []

def print_coverage_note(focus=None):
    """Warn when a graph answer may be incomplete because CBM parsed some source files only partly."""
    gaps = load_json(GRAPH_PATH, {}).get("gaps") or {}
    if focus and focus in gaps:
        print("  -> coverage: /%s itself was %s, so the graph may miss definitions or calls in it" % (focus, gaps[focus]))
    code = [p for p in gaps if p != focus and p.endswith(CODE_EXTS)]
    if code and not _COVERAGE_SHOWN:
        _COVERAGE_SHOWN.append(True)
        print("  -> coverage: %d source file(s) were only partly indexed (%s); graph links inside them may be missing" % (len(code), ", ".join("/%s %s" % (p, gaps[p]) for p in code[:3])))

def ensure_graph_index(exe, force=False):
    """CBM project name for this workspace, re-indexing first whenever the workspace changed (or when forced). -> (project, error)"""
    g = load_json(GRAPH_PATH, {})
    stamp = workspace_stamp()
    if not force and g.get("project") and g.get("stamp") == stamp:
        return g["project"], None
    rc, out, err = run_cbm(exe, ["index_repository", "--repo-path", os.path.abspath(".")], 600)
    if rc is None:
        return None, "index_repository " + err
    data = cbm_json(out) or cbm_json(err)
    if not data or not data.get("project"):
        tail = " | ".join((cbm_clean(err) or cbm_clean(out)).splitlines()[-3:])
        hint = " (if another app is running CBM with a different cache folder, close it and retry)" if "cache" in tail.lower() else ""
        return None, "index_repository failed: " + tail + hint
    save_json(GRAPH_PATH, {"project": data["project"], "stamp": stamp, "gaps": index_gaps(exe, data["project"])})
    print("  (graph index %s for project %s)" % ("refreshed" if g.get("project") else "built", data["project"]))
    return data["project"], None

def norm_key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())

def graph_xref(sym, mode):
    """Callers, callees or impact from codebase-memory-mcp; falls back to text search, labelled as such, when the graph cannot answer."""
    sym = re.sub(r"\(\)$", "", sym)
    parts = [p for p in re.split(r"\.|::", sym) if p]
    leaf = parts[-1] if parts else sym
    print("\n=== XREF: %s | %s (code graph) ===" % (sym, mode))

    def fallback(reason):
        print("  -> graph unavailable (%s); whole-word text matches below are NOT proven %s." % (reason, mode))
        xref_text(leaf, header=False)

    exe = find_cbm()
    if not exe:
        return fallback("codebase-memory-mcp is not installed")
    project, err = ensure_graph_index(exe)
    if not project:
        return fallback(err)
    direction, depth = GRAPH_MODES[mode]
    name = leaf
    for attempt in range(2):
        rc, out, err = run_cbm(exe, ["trace_path", "--project", project, "--function-name", name,
                                     "--direction", direction, "--depth", str(depth)], 120)
        if rc is None:
            return fallback("trace_path " + err)
        body = cbm_clean(out)
        data = cbm_json(body) if body.lstrip().startswith("{") else None
        if data and data.get("status") == "ambiguous":
            sugg = data.get("suggestions") or []
            keys = [norm_key(p) for p in parts[:-1]]
            picks = [x for x in sugg if keys and all(k in norm_key("%s %s" % (x.get("qualified_name", ""), x.get("file_path", ""))) for k in keys)]
            if attempt == 0 and len(picks) == 1 and picks[0].get("qualified_name"):
                name = picks[0]["qualified_name"]
                continue
            print("  [AMBIGUOUS] %d symbols are named %s:" % (len(sugg), leaf))
            for x in sugg:
                print("    %s  (/%s)" % (x.get("qualified_name", "").replace(project + ".", ""), x.get("file_path", "")))
            print("  -> qualify it with its package, type or file, e.g. @XREF <package>.%s | %s" % (leaf, mode))
            return
        lost = data and "not indexed" in str(data.get("error", "")) + str(data.get("message", ""))
        if lost and attempt == 0:
            project, err = ensure_graph_index(exe, force=True)
            if not project:
                return fallback(err)
            continue
        if data and (data.get("error") or data.get("status") not in (None, "ok")):
            print("  [GRAPH ERROR] " + str(data.get("error") or data.get("message") or data))
            return fallback("the graph could not resolve this symbol")
        if rc != 0 or not body:
            return fallback("trace_path exited %s: %s" % (rc, (cbm_clean(err).splitlines() or ["no output"])[-1]))
        print("\n".join("  " + l for l in body.replace(project + ".", "").split("\n")))
        if mode == "callers":
            print_call_sites(leaf)
        print_coverage_note()
        print("  -> static call graph: confirm a relation in source before relying on it.")
        return

# ---------------------------------------------------------------- Stage: File and text search

def split_depth(arg):
    """'<anything> | depth N' -> (<anything>, N); (arg, None) without it."""
    m = re.search(r"\s*\|\s*depth\s+(\d+)\s*$", arg)
    return (arg[:m.start()], int(m.group(1))) if m else (arg, None)

def within_depth(rel, base, depth):
    """True when rel sits at most depth folder levels below base ('' = the workspace root; 1 = directly inside)."""
    if depth is None:
        return True
    if base and not rel.startswith(base + "/"):
        return False
    rest = rel[len(base) + 1:] if base else rel
    return rest.count("/") + 1 <= depth

def glob_files(pattern):
    """Workspace files a glob names: with a '/' it starts at the root and * stays in one folder (** crosses folders); without one it matches file names at any depth."""
    p = pattern.strip().replace("\\", "/").lstrip("/")
    if "/" in p:
        return [f for f in repo_files(".") if path_in(f, p)]
    return [f for f in repo_files(".") if fnmatch.fnmatch(f.rsplit("/", 1)[-1], p)]

def path_filter(pattern):
    """Predicate for a path pattern: a glob (with a '/' it starts at the root and * stays in one folder; without one it matches file names at any depth), else a case-insensitive substring."""
    p = pattern.strip().replace("\\", "/").lstrip("/").lower()
    if any(c in p for c in "*?["):
        if "/" in p:
            return lambda rel: path_in(rel.lower(), p)
        return lambda rel: fnmatch.fnmatch(rel.rsplit("/", 1)[-1].lower(), p)
    return lambda rel: p in rel.lower()

def run_find(pattern):
    """Files whose path or name matches a glob or substring."""
    pattern, depth = split_depth(pattern)
    print("\n=== FIND: %s%s ===" % (pattern.strip(), " | depth %d" % depth if depth else ""))
    if not pattern.strip():
        print("  [WARNING] @FIND needs a name, path fragment or glob, e.g. '@FIND lease' or '@FIND *.vue'")
        return
    keep = path_filter(pattern)
    base = literal_base(pattern) if any(c in pattern for c in "*?[") else ""
    rows = [rel for rel in repo_files(".") if keep(rel) and within_depth(rel, base, depth)]
    for rel in rows[:LIST_CAP]:
        print("  /" + rel)
    if len(rows) > LIST_CAP:
        print("  ... +%d more (narrow the pattern)" % (len(rows) - LIST_CAP))
    print("  (%d file(s))" % len(rows))

def run_grep(arg):
    """Lines containing a text (case-insensitive) or matching a /regex/, optionally limited to paths after '| in'."""
    arg, depth = split_depth(arg)
    text, sep, flt = arg.rpartition(" | in ")
    if not sep:
        text, flt = arg, ""
    text = text.strip()
    print("\n=== GREP: %s%s%s ===" % (text, (" | in " + flt.strip()) if flt.strip() else "", " | depth %d" % depth if depth else ""))
    if not text:
        print("  [WARNING] @GREP needs a text or /regex/")
        return
    mr = re.match(r"^/(.+)/(i?)$", text)
    try:
        rx = re.compile(mr.group(1), re.I if mr.group(2) else 0) if mr else re.compile(re.escape(text), re.I)
    except re.error as e:
        print("  [WARNING] invalid regex: %s" % e)
        return
    in_flt = path_filter(flt) if flt.strip() else (lambda rel: True)
    base = literal_base(flt) if any(c in flt for c in "*?[") else ""
    keep = lambda rel: in_flt(rel) and within_depth(rel, base, depth)
    hits, files = [], set()
    for rel in repo_files("."):
        if not keep(rel):
            continue
        try:
            if os.path.getsize(rel) > 1000000:
                continue
            raw = read_bytes(rel)
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        for i, l in enumerate(norm_nl(raw.decode("utf-8", "replace")).split("\n")):
            if rx.search(l):
                files.add(rel)
                hits.append("  /%s:%d: %s" % (rel, i + 1, l.strip()[:160]))
    for row in hits[:GREP_CAP]:
        print(row)
    if len(hits) > GREP_CAP:
        print("  ... +%d more (narrow the text or add '| in <path>')" % (len(hits) - GREP_CAP))
    print("  (%d match(es) in %d file(s))" % (len(hits), len(files)))

# ---------------------------------------------------------------- Stage: Running commands

GIT_WRITE_RE = re.compile(r"(^|[;&|(]\s*)git(\.exe)?\s+(commit|push|pull|reset|rebase|merge|cherry-pick|revert|checkout|switch|stash|clean|restore|am|apply|tag|branch\s+-[dDmM])\b", re.I)

def kill_tree(proc):
    """Stop a command and every process it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    try:
        proc.kill()
    except OSError:
        pass

def run_command(cmd, timeout, shell):
    """Run one shell command in the workspace with no stdin; print head and tail of its combined output, exit code and time."""
    print("\n=== RUN: %s ===" % cmd)
    if GIT_WRITE_RE.search(cmd):
        print("  [REFUSED] git commands that change history or the working tree go through @GIT_COMMIT / @GIT_FOLD; pushing is left to the user.")
        return
    if re.search(r"harness\.py\b.*--(grant|revoke|release|unbrake)\b", cmd, re.I):
        print("  [REFUSED] --grant, --revoke, --release and --unbrake are the user's own commands, typed in their terminal; a script never runs them.")
        return
    if shell == "powershell":
        prefix = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        args = [shutil.which("powershell") or shutil.which("pwsh") or "powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + cmd]
    else:
        args = [shutil.which("bash") or "/bin/sh", "-c", cmd]
    start = time.time()
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=(os.name != "nt"))
    except OSError as e:
        print("  [FAILED] could not start the shell: %s" % e)
        return
    try:
        raw, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raw = b""
        code = "TIMEOUT"
    out = (raw or b"").decode("utf-8", "replace")
    lines = norm_nl(out).rstrip("\n").split("\n") if out.strip() else []
    if len(lines) > RUN_HEAD + RUN_TAIL:
        shown = lines[:RUN_HEAD] + ["  ... %d lines omitted ..." % (len(lines) - RUN_HEAD - RUN_TAIL)] + lines[-RUN_TAIL:]
    else:
        shown = lines
    for l in shown:
        print("  " + l[:400])
    print("  [exit %s | %.1fs]" % (code, time.time() - start))
    if code == "TIMEOUT":
        print("  -> the command hit the %ds limit and was stopped; use '@RUN --timeout <seconds> <command>' for longer jobs." % timeout)
    elif code != 0:
        print("  -> exited with code %s: judge it from the output above (some tools use non-zero codes for 'nothing found')." % code)

def print_toc(lines):
    for i, l in enumerate(lines):
        if is_toc_line(l):
            print("  L%d: %s" % (i + 1, l.rstrip()[:110]))

def tree_node_paths(lines):
    """{line index: workspace path} for the folder and file nodes of a tree file, resolved the way the drift check does."""
    proj = "/" + project_name()
    out, folders = {}, []
    for i, d, label in tree_nodes(lines):
        while folders and folders[-1][0] >= d:
            folders.pop()
        key = tree_key(label)
        if key.startswith("/"):
            path = key[len(proj):] if key == proj or key.startswith(proj + "/") else key
            segs = [s for s in path.split("/") if s]
            base = ("/".join(segs) if len(segs) != 1 or not folders else folders[-1][1] + "/" + segs[0]).strip("/")
            folders.append((d, base))
            out[i] = base
            continue
        parts = [p.strip() for p in key.split(" / ")]
        if parts and all(re.match(r"^[\w.\-]+\.[A-Za-z0-9]+$", p) for p in parts):
            out[i] = ((folders[-1][1] if folders else "") + "/" + parts[0]).strip("/")
    return out

def tree_verdict_mark(verdicts, path):
    v = verdicts.get(path) if path else None
    if not v:
        return ""
    new, changed, gone = verdict_changes(v, path)
    return "  <<verdict: %s%s%s>>" % (v["verdict"], " (campaign %s)" % v["campaign"] if v.get("campaign") else "", ", STALE" if new or changed or gone else "")

def active_campaigns():
    try:
        return load_json(verdicts_path(), {}).get("campaigns", {}) or {}
    except Exception:
        return {}

def apply_campaigns(P):
    """@CAMPAIGN <name> | <word> = <meaning>; ...  starts a sweep with its verdict legend; @CAMPAIGN_END <name> ends it and removes its verdicts."""
    if not P["campaigns"]:
        return
    with state_lock():
        data = load_json(verdicts_path(), {"verdicts": {}})
        camps = data.setdefault("campaigns", {})
        for kind, arg in P["campaigns"]:
            name, _, legend = arg.partition("|")
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-")
            if not name:
                print("  [CAMPAIGN REFUSED] a campaign needs a name: %s" % arg)
                continue
            if kind == "CAMPAIGN_END":
                if name not in camps:
                    print("  [CAMPAIGN] no active campaign named %s" % name)
                    continue
                gone = [p for p, v in data["verdicts"].items() if v.get("campaign") == name]
                for p in gone:
                    del data["verdicts"][p]
                del camps[name]
                print("  [CAMPAIGN ENDED] %s: %d verdict(s) removed" % (name, len(gone)))
                continue
            words = {}
            for item in legend.split(";"):
                w, _, meaning = item.partition("=")
                if w.strip():
                    words[w.strip()] = meaning.strip()
            if not words:
                print("  [CAMPAIGN REFUSED] expected '@CAMPAIGN <name> | <word> = <meaning>; <word> = <meaning>'")
                continue
            camps[name] = {"legend": words, "started": time.time(), "by": CURRENT_SESSION}
            print("  [CAMPAIGN] %s: %s" % (name, "; ".join("%s = %s" % kv for kv in words.items())))
        os.makedirs(WORKSPACE_NOTES_DIR, exist_ok=True)
        save_json(verdicts_path(), data)

def print_footer_campaigns():
    """While a campaign runs, every footer names it with its legend and how many areas carry its verdicts."""
    camps = active_campaigns()
    if not camps:
        return
    verdicts = load_json(verdicts_path(), {"verdicts": {}}).get("verdicts", {})
    for name, cv in sorted(camps.items()):
        n = sum(1 for v in verdicts.values() if v.get("campaign") == name)
        print("=== CAMPAIGN %s (%d area(s) with a verdict): %s ===" % (name, n, "; ".join("%s = %s" % kv for kv in cv.get("legend", {}).items())))

def probe_tree(lines, target, depth):
    """Print a tree node and its descendants down to a relative depth; deeper levels collapse to a count."""
    if target.strip().upper() in ("", "TOP"):
        nodes = tree_nodes(lines)
        first, stop, limit, path = 0, len(nodes), depth, "TOP"
    else:
        _, _, note, pos = resolve_tree_scope(lines, target)
        if note:
            print("--- [NODE NOT RESOLVED: %s] ---" % note)
            return
        nodes, first, stop = pos
        limit, path = nodes[first][1] + depth, tree_path(nodes, first)
    rows, paths, verdicts = [], tree_node_paths(lines), load_json(verdicts_path(), {"verdicts": {}})["verdicts"]
    for j in range(first, stop):
        i, d, _ = nodes[j]
        if d > limit:
            continue
        e = j + 1
        while e < stop and nodes[e][1] > d:
            e += 1
        hidden = e - j - 1 if d == limit else 0
        rows.append(lines[i] + ("  <<+%d hidden>>" % hidden if hidden else "") + tree_verdict_mark(verdicts, paths.get(i)))
    print("--- [Tree: %s | depth %d | %d of %d nodes shown | <<+N hidden>> and <<verdict: ...>> are added by the harness and are not on disk] ---" % (path, depth, len(rows), stop - first))
    print("\n".join(rows))
    if len(rows) < stop - first:
        print("  -> expand one node with: @PROBE /<this file> | Tree | <node path> | <depth>")

TREE_FILE_RE = re.compile(r"^[\w.\-]+\.[A-Za-z0-9]+$")
TREE_NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\(\)")

def check_tree(rel, lines):
    """Drift check: tree nodes whose file, function/class name or # Stage anchor no longer exists in the code."""
    nodes = tree_nodes(lines)
    proj = "/" + project_name()
    texts, stale, counts = {}, [], {"file": 0, "name": 0, "anchor": 0}
    folders, files = [], []
    for i, d, label in nodes:
        while folders and folders[-1][0] >= d:
            folders.pop()
        while files and files[-1][0] >= d:
            files.pop()
        head = label.split(" (", 1)[0].strip()
        key = tree_key(label)
        if key.startswith("/"):
            path = key[len(proj):] if key == proj or key.startswith(proj + "/") else key
            segs = [s for s in path.split("/") if s]
            base = "/".join(segs) if len(segs) != 1 or not folders else folders[-1][1] + "/" + segs[0]
            folders.append((d, base.strip("/")))
            continue
        parts = [p.strip() for p in key.split(" / ")]
        if parts and all(TREE_FILE_RE.match(p) for p in parts):
            here = folders[-1][1] if folders else ""
            paths = [(here + "/" + p).lstrip("/") for p in parts]
            files.append((d, paths[0]))
            for p in paths:
                counts["file"] += 1
                if not os.path.isfile(p):
                    stale.append((i, label, "file not found: /" + p))
            continue
        if not files or not os.path.isfile(files[-1][1]):
            continue
        fpath = files[-1][1]
        if fpath not in texts:
            texts[fpath] = read_text(fpath)
        body = texts[fpath]
        if head.startswith("#"):
            anchor = norm_ws(head.lstrip("#").strip())
            counts["anchor"] += 1
            if anchor and anchor not in norm_ws(body):
                stale.append((i, label, "anchor not found in /%s: %s" % (fpath, anchor)))
            continue
        names = TREE_NAME_RE.findall(head)
        m = re.match(r"^(?:->|\(\))\s*([A-Za-z_$][\w$]*)", head)
        if m:
            names.append(m.group(1))
        for name in dict.fromkeys(names):
            counts["name"] += 1
            if not re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", body):
                stale.append((i, label, "name not found in /%s: %s" % (fpath, name)))
    print("--- [Tree check: %d file(s), %d name(s), %d anchor(s) checked | %d stale] ---" % (counts["file"], counts["name"], counts["anchor"], len(stale)))
    for i, label, why in stale:
        print("  L%d: %s\n      -> %s" % (i + 1, label[:110], why))
    if stale:
        print("  -> fix a stale node in the script that next touches its file; a tree-only cleanup needs the user's request.")

UNUSED_ROW_RE = re.compile(r"^\s+(\S+)\s+(Function|Method|Class)\s+(\d+)-(\d+)\s+(\d+)\s+(\d+)\s*$")
UNUSED_HEAD_RE = re.compile(r"^\S.*\(([^()]+)\):\s*$")
UNUSED_FLAT_RE = re.compile(r"^\s+(\S+)\s+(Function|Method|Class)\s+(\S+)\s+(\d+)-(\d+)\s+(\d+)\s+(\d+)\s*$")

def graph_definitions(rel):
    """[(name, kind, start, end, callers)] for one file from CBM's search_graph, or (None, reason) when the graph cannot answer."""
    exe = find_cbm()
    if not exe:
        return None, "codebase-memory-mcp is not installed"
    project, err = ensure_graph_index(exe)
    if not project:
        return None, err
    rc, out, err = run_cbm(exe, ["search_graph", "--project", project, "--file-pattern", "*" + rel, "--limit", "2000"], 120)
    if rc is None or rc != 0:
        return None, "search_graph failed: " + ((cbm_clean(err) or cbm_clean(out)).splitlines() or ["no output"])[-1]
    rows, here, refs, in_refs = [], None, {}, False
    expand = lambda tok: (lambda m: refs.get(m.group(1), "") + m.group(2) if m else tok)(re.match(r"^@(\d+)\+(.*)$", tok))
    for ln in cbm_clean(out).split("\n"):
        if ln.startswith("results_refs:"):
            in_refs = True
            continue
        if in_refs:
            r = re.match(r"^\s+(\d+)\s+(\S+)\s*$", ln)
            if r:
                refs[r.group(1)] = r.group(2)
                continue
            in_refs = False
        f = UNUSED_FLAT_RE.match(ln)
        if f:
            if expand(f.group(3)).replace("\\", "/") == rel:
                rows.append((expand(f.group(1)).rsplit(".", 1)[-1], f.group(2), int(f.group(4)), int(f.group(5)), int(f.group(6))))
            continue
        h = UNUSED_HEAD_RE.match(ln)
        if h:
            here = h.group(1).replace("\\", "/")
            continue
        m = UNUSED_ROW_RE.match(ln)
        if m and here == rel:
            rows.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5))))
    if "has_more: true" in out:
        return None, "search_graph returned a partial list"
    return rows, None

def text_definitions(lines):
    """[(name, kind, start, end, None)] from the harness's own definition finder; callers unknown."""
    rows = []
    for i, ln in enumerate(lines):
        info = def_info(ln)
        if info and not SFC_BLOCK.match(ln):
            a, b = block_bounds(lines, i, False)
            rows.append((info[0], "Definition", i + 1, b, None))
    return rows

def check_unused(rel, lines):
    """Candidates for deletion: definitions in one file that nothing calls (call graph) and that no other line mentions (text)."""
    rows, why = graph_definitions(rel)
    source = "call graph + text"
    if rows == [] and text_definitions(lines):
        rows, why = None, "the graph listed no definitions for this file, though it has some"
    if rows is None:
        rows, source = text_definitions(lines), "text only (graph unavailable: %s)" % why
    skip = re.compile(r"^(__\w+__|main|init)$")
    cands = [r for r in rows if not skip.match(r[0]) and (r[4] == 0 if r[4] is not None else True)]
    names = set(r[0] for r in cands)
    hits = {n: [] for n in names}
    if names:
        pat = re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w$])")
        for f in repo_files("."):
            try:
                if os.path.getsize(f) > 1000000:
                    continue
                raw = read_bytes(f)
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue
            for i, ln in enumerate(norm_nl(raw.decode("utf-8", "replace")).split("\n")):
                info = def_info(ln)
                for m in pat.finditer(ln):
                    if info and info[0] == m.group(1):
                        continue
                    call = f.endswith(CODE_EXTS) and re.match(r"\s*\(", ln[m.end():]) is not None
                    hits[m.group(1)].append((f, i + 1, call))
    found = []
    for name, kind, a, b, _ in cands:
        outside = [h for h in hits.get(name, []) if not (h[0] == rel and a <= h[1] <= b)]
        calls = [h for h in outside if h[2]]
        if source.startswith("text") and outside:
            continue
        decorated = a >= 2 and lines[a - 2].strip().startswith("@")
        rank = 2 if calls else (1 if outside else 0)
        found.append((rank, a, name, kind, outside, calls, decorated))
    print("--- [Unused check: /%s | source: %s | %d definition(s), %d candidate(s), strongest first] ---" % (rel, source, len(rows), len(found)))
    if source.startswith("call"):
        print_coverage_note(rel)
    for rank, a, name, kind, outside, calls, decorated in sorted(found):
        where = lambda xs: ", ".join("/%s:%d" % (x[0], x[1]) for x in xs[:3])
        if source.startswith("text"):
            notes = ["no other mentions"]
        elif calls:
            notes = ["no callers in the graph, but called by name %dx (e.g. %s): the graph likely missed it, treat as live" % (len(calls), where(calls))]
        elif outside:
            notes = ["no callers; mentioned %dx but never as a call (%s): check for string or dynamic use" % (len(outside), where(outside))]
        else:
            notes = ["no callers; no other mentions"]
        if decorated:
            notes.append("decorated, so a framework may call it")
        print("  L%d %s() %s -> %s" % (a, name, kind, "; ".join(notes)))
    if found:
        print("  -> candidates only: confirm each in source (routes, decorators, getattr or string names, other languages over HTTP) before deleting.")

CODE_EXTS = (".py", ".go", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".vue")
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
CONST_RE = re.compile(r"^(?:(?:export\s+)?(?:const|let|var)\s+)?([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=(?!=)")
_REPO_LINES = []
NOT_CALLS = set("and or not if elif else for while return in is lambda with as assert await yield def class func function switch case catch typeof new delete void go defer select except raise from import print".split()) - {"print"}

def repo_code_lines():
    """[(rel, lines)] for every source file in the workspace, read once per run."""
    if not _REPO_LINES:
        for f in repo_files("."):
            if not f.endswith(CODE_EXTS):
                continue
            try:
                if os.path.getsize(f) > 1000000:
                    continue
                raw = read_bytes(f)
            except OSError:
                continue
            if b"\0" not in raw[:4096]:
                _REPO_LINES.append((f, norm_nl(raw.decode("utf-8", "replace")).split("\n")))
    return _REPO_LINES

def definition_index():
    """name -> [(file, line, kind)] for every definition and top-level constant in the workspace's source files."""
    index = {}
    for f, lines in repo_code_lines():
        for i, ln in enumerate(lines):
            info = def_info(ln)
            if info and not SFC_BLOCK.match(ln):
                m = DEF_KW.match(ln)
                index.setdefault(info[0], []).append((f, i + 1, m.group(1) if m else "def"))
                continue
            c = CONST_RE.match(ln)
            if c:
                index.setdefault(c.group(1), []).append((f, i + 1, "const"))
    return index

def enclosing_definition(lines, i):
    """Name of the innermost definition whose body contains line index i, or None at top level."""
    ind = indent_of(lines[i])
    for j in range(i, -1, -1):
        info = def_info(lines[j])
        if info and (j == i or indent_of(lines[j]) < ind or not lines[i].strip()):
            return info[0]
        if lines[j].strip() and not lines[j].strip().startswith(SKIPPABLE):
            ind = min(ind, indent_of(lines[j]) if j != i else ind)
    return None

def call_sites(name, limit=20):
    """Call-shaped uses of name across the source files, each with the function it sits in."""
    pat = re.compile(r"(?<![\w$])" + re.escape(name) + r"\s*\(")
    out = []
    for f, lines in repo_code_lines():
        for i, ln in enumerate(lines):
            if pat.search(ln):
                info = def_info(ln)
                if info and info[0] == name:
                    continue
                anon = "(anonymous function)" if f.endswith((".go", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".vue")) and indent_of(ln) > 0 else None
                out.append((f, i + 1, enclosing_definition(lines, i) or anon))
    return out

def print_call_sites(name):
    sites = call_sites(name)
    shared = len(definition_index().get(name, []))
    if shared > 1:
        print("  call sites (text, by name): %d; %d definitions are named %s, so some of these call another one" % (len(sites), shared, name))
    else:
        print("  call sites (text): %d" % len(sites))
    for f, n, fn in sites[:20]:
        print("    /%s:%d in %s" % (f, n, fn or "(top level)"))
    if len(sites) > 20:
        print("    ... +%d more" % (len(sites) - 20))

def refs_in_scope(rel, lines, target):
    """What a scope uses: each project name with its use lines and definition sites, plus the calls that resolve outside the project."""
    lo, hi, note = resolve_scope(rel, lines, target)
    if note:
        print("--- [SCOPE NOT RESOLVED: %s] ---" % note)
        return
    index = definition_index()
    own = def_info(lines[lo])
    uses, external = {}, {}
    for i in range(lo, min(hi, len(lines))):
        ln = lines[i]
        code = re.sub(r"(\"[^\"]*\"|'[^']*'|#.*$|//.*$)", " ", ln)
        for m in IDENT_RE.finditer(code):
            name = m.group(0)
            if own and name == own[0] and i == lo:
                continue
            outside = [d for d in index.get(name, []) if not (d[0] == rel and lo < d[1] <= hi)]
            if outside:
                uses.setdefault(name, []).append(i + 1)
            elif name not in index and name not in NOT_CALLS and re.match(r"\s*\(", code[m.end():]):
                start = m.start()
                while start > 0 and (code[start - 1] == "." or IDENT_RE.match(code[start - 1])):
                    start -= 1
                external.setdefault(code[start:m.end()].lstrip("."), i + 1)
    print("--- [Refs: /%s | %s (L%d-%d) | %d project name(s), %d external call(s)] ---" % (rel, target or "TOP", lo + 1, min(hi, len(lines)), len(uses), len(external)))
    for name in sorted(uses, key=lambda n: uses[n][0]):
        defs = [d for d in index[name] if not (d[0] == rel and lo < d[1] <= hi)]
        at = ", ".join("L%d" % n for n in uses[name][:6]) + (" +%d" % (len(uses[name]) - 6) if len(uses[name]) > 6 else "")
        where = "/%s:%d (%s)" % defs[0] if len(defs) == 1 else "%d definitions: %s" % (len(defs), ", ".join("/%s:%d" % d[:2] for d in defs[:3]))
        print("  %-28s used %-18s -> %s" % (name, at, where))
    if external:
        print("  external calls: " + ", ".join(sorted(external)[:40]))

CURRENT_SCRIPT, CURRENT_RUN = None, None

def inspections_path():
    return os.path.join(HARNESS_DIR, "inspections.json")

def read_units(lines, a, b):
    """The definitions a line range covers (innermost, qualified by their parent), plus whether it touched code outside any definition."""
    defs = []
    for j, ln in enumerate(lines):
        info = def_info(ln)
        if info and not SFC_BLOCK.match(ln):
            s, e = block_bounds(lines, j, False)
            defs.append((info[0], s, e))
    units, outside = {}, False
    for i in range(a, b):
        cont = [d for d in defs if d[1] <= i < d[2]]
        if not cont:
            outside = outside or bool(lines[i].strip())
            continue
        d = min(cont, key=lambda x: x[2] - x[1])
        parents = [p for p in defs if p is not d and p[1] <= d[1] and d[2] <= p[2]]
        name = (min(parents, key=lambda x: x[2] - x[1])[0] + "." if parents else "") + d[0]
        units[name] = (d[1], d[2])
    return units, outside

def note_inspection(rel, lines, a, b):
    """Record what a read covered; say when the same definition was read before and has not changed since (context was lost, or it is still in context)."""
    units, outside = read_units(lines, a, b)
    seen = [(name + "()", hashlib.sha1("\n".join(lines[s:e]).encode("utf-8", "surrogateescape")).hexdigest()) for name, (s, e) in units.items()]
    if outside:
        seen.append(("(code outside definitions)", hashlib.sha1("\n".join(lines).encode("utf-8", "surrogateescape")).hexdigest()))
    again, now = [], time.time()
    with state_lock():
        log = load_json(inspections_path(), {})
        for name, fp in seen:
            key = rel + "#" + name
            prev = log.get(key)
            if prev and prev.get("fp") == fp and prev.get("run") != CURRENT_RUN:
                again.append((name, prev))
            log[key] = {"fp": fp, "session": CURRENT_SESSION, "script": CURRENT_SCRIPT, "run": CURRENT_RUN, "time": now}
        if len(log) > 5000:
            log = dict(sorted(log.items(), key=lambda kv: kv[1].get("time", 0))[-4000:])
        save_json(inspections_path(), log)
    for name, prev in again[:6]:
        age = now - prev.get("time", now)
        when = "%dm" % (age // 60) if age < 7200 else ("%dh" % (age // 3600) if age < 172800 else "%dd" % (age // 86400))
        print("  [REVISIT] /%s %s was read %s ago by %s (%s) and has not changed since" % (rel, name, when, "session " + prev["session"] if prev.get("session") else "a script without a session", prev.get("script") or "?"))
    if len(again) > 6:
        print("  [REVISIT] ... and %d more unchanged definitions read before" % (len(again) - 6))
    show_verdicts_for(rel, ["%s#%s" % (rel, name) for name in units])

_VERDICT_SHOWN = set()

def verdicts_path():
    return os.path.join(WORKSPACE_NOTES_DIR, "verdicts.json")

def verdict_inventory(target):
    """Fingerprints of what a verdict covers: a folder's files, a file, or one file#Class.method; None when the target does not exist."""
    path, _, scope = target.partition("#")
    if scope:
        if not os.path.isfile(path):
            return None
        lines = read_text(path).split("\n")
        lo, hi, note = resolve_scope(path, lines, scope)
        return None if note else {target: hashlib.sha1("\n".join(lines[lo:hi]).encode("utf-8", "surrogateescape")).hexdigest()}
    if os.path.isfile(path):
        return {path: sha_of(path)}
    if not os.path.isdir(path or "."):
        return None
    return {f: sha_of(f) for f in repo_files(".") if path_in(f, path or "**")}

def verdict_changes(v, target):
    """(new, changed, deleted) files under a verdict since it was recorded."""
    inv, old = verdict_inventory(target) or {}, v.get("inventory", {})
    return [f for f in inv if f not in old], [f for f in inv if f in old and inv[f] != old[f]], [f for f in old if f not in inv]

def tree_description(target):
    """The description of the tree node that names this path, from any tree file, or ''."""
    base = target.split("#")[0].strip("/")
    name = base.split("/")[-1]
    for t in sorted(glob.glob(os.path.join("docs", "trees", "*.txt"))):
        for _, _, label in tree_nodes(read_text(t).split("\n")):
            key = tree_key(label).strip("/")
            if key and (key == base or key == name or base.endswith("/" + key)):
                m = re.search(r"\((.*)\)\s*$", label)
                if m:
                    return m.group(1)[:140]
    return ""

def record_verdicts(P):
    """@VERDICT <folder, file or file#Class.method> | <verdict> | <note>: a conclusion about that area, fingerprinted so later changes mark it stale."""
    if not P["verdicts"]:
        return
    with state_lock():
        data = load_json(verdicts_path(), {"verdicts": {}})
        for arg in P["verdicts"]:
            parts = [x.strip() for x in arg.split("|")]
            target = parts[0].replace("\\", "/").strip("/")
            word = parts[1] if len(parts) > 1 else ""
            if not target or not word:
                print("  [VERDICT REFUSED] expected '@VERDICT <path> | <verdict> | <note>' (or '| clear'): %s" % arg)
                continue
            if word.lower() == "clear":
                print("  [VERDICT CLEARED] /%s" % target if data["verdicts"].pop(target, None) else "  [VERDICT] nothing to clear at /%s" % target)
                continue
            inv = verdict_inventory(target)
            if inv is None:
                print("  [VERDICT REFUSED] /%s is not an existing folder, file or resolvable file#Class.method" % target)
                continue
            camps = [c for c, cv in data.get("campaigns", {}).items() if word in cv.get("legend", {})]
            if data.get("campaigns") and not camps:
                print("  -> '%s' is not a word of any active campaign's legend (%s); recorded as an ordinary verdict" % (word, "; ".join("%s: %s" % (c, ", ".join(cv.get("legend", {}))) for c, cv in data["campaigns"].items())))
            data["verdicts"][target] = {"verdict": word, "note": " | ".join(parts[2:]), "session": CURRENT_SESSION, "script": CURRENT_SCRIPT, "time": time.time(), "inventory": inv, "campaign": camps[0] if camps else None}
            print("  [VERDICT] /%s: %s%s (%d fingerprint(s) kept)" % (target, word, (" - " + " | ".join(parts[2:])) if parts[2:] else "", len(inv)))
        os.makedirs(WORKSPACE_NOTES_DIR, exist_ok=True)
        save_json(verdicts_path(), data)
    if MODE == "git" and run_git(["check-ignore", "-q", verdicts_path().replace("\\", "/")])[0] == 0:
        print("  -> .llm_harness/verdicts.json is ignored by .gitignore, so verdicts will not travel with the project: add the line !.llm_harness/verdicts.json")

def show_verdicts_for(rel, scoped):
    """While reading code, show each verdict that covers it (once per run), with a stale warning when its area changed since."""
    data = load_json(verdicts_path(), {"verdicts": {}})["verdicts"]
    for target, v in sorted(data.items()):
        base = target.split("#")[0]
        covers = target in scoped if "#" in target else (rel == base or rel.startswith(base + "/") or not base)
        if not covers or target in _VERDICT_SHOWN:
            continue
        _VERDICT_SHOWN.add(target)
        new, changed, gone = verdict_changes(v, target)
        stale = " [STALE VERDICT: %d new, %d changed, %d deleted since]" % (len(new), len(changed), len(gone)) if new or changed or gone else ""
        print("  [VERDICT] /%s: %s%s (session %s)%s" % (target, v["verdict"], (" - " + v["note"]) if v.get("note") else "", v.get("session") or "none", stale))

def list_verdicts(arg):
    """@VERDICTS [all | stale | session <name>]: verdicts as path, tree description and conclusion; a parent verdict hides children that say the same."""
    data = load_json(verdicts_path(), {"verdicts": {}})["verdicts"]
    a = arg.strip()
    if a.lower().startswith("session "):
        who = a.split(None, 1)[1].strip()
        items, label = {p: v for p, v in data.items() if v.get("session") == who}, "session " + who
    elif a.lower() in ("all", "stale"):
        items, label = dict(data), a.lower()
    elif CURRENT_SESSION:
        items, label = {p: v for p, v in data.items() if v.get("session") == CURRENT_SESSION}, "session " + CURRENT_SESSION
    else:
        items, label = dict(data), "all"
    print("\n=== VERDICTS (%s) ===" % label)
    shown = 0
    for p in sorted(items):
        v, base = items[p], p.split("#")[0]
        above = [q for q in items if q != p and "#" not in q and (base == q or base.startswith(q + "/") or not q)]
        if above and items[max(above, key=len)]["verdict"] == v["verdict"]:
            continue
        new, changed, gone = verdict_changes(v, p)
        if a.lower() == "stale" and not (new or changed or gone):
            continue
        age = time.time() - v.get("time", time.time())
        when = "%dm" % (age // 60) if age < 7200 else ("%dh" % (age // 3600) if age < 172800 else "%dd" % (age // 86400))
        desc = tree_description(p)
        print("  /%s%s [%s]%s" % (p, "/" if "#" not in p and os.path.isdir(base or ".") else "", v["verdict"], (" " + v["note"]) if v.get("note") else ""))
        print("      session %s, %s ago (%s)%s" % (v.get("session") or "none", when, v.get("script") or "?", (" | tree: " + desc) if desc else ""))
        if new or changed or gone:
            print("      [STALE VERDICT] %d new, %d changed, %d deleted since: %s" % (len(new), len(changed), len(gone), ", ".join("/" + f for f in (new + changed + gone)[:6])))
        shown += 1
    if not shown:
        print("  (none)")

def run_probe(spec):
    parts = [p.strip() for p in spec.split("|")]
    rel = safe_rel(parts[0])
    mode = (parts[1] if len(parts) > 1 else "").lower()
    target = parts[2] if len(parts) > 2 else ""
    if rel is None or not os.path.isfile(rel):
        print("\n=== FILE: /%s (NOT FOUND) ===" % parts[0].lstrip("/"))
        return
    text = read_text(rel)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    tot = len(lines)
    print("\n=== FILE: /%s (Total: %dL | Line-1 Header: %s) ===" % (rel, tot, ensure_line1_header(rel, text)[1]))
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", target or (parts[1] if len(parts) > 1 else ""))
    if mode == "toc":
        print("--- [On-Disk Lines 1-%d of %d | Signature TOC] ---" % (tot, tot))
        print_toc(lines)
    elif mode == "tree":
        depth = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 2
        probe_tree(lines, target, depth)
    elif mode == "check":
        check_tree(rel, lines)
    elif mode == "unused":
        check_unused(rel, lines)
    elif mode == "refs":
        refs_in_scope(rel, lines, target)
    elif m:
        a, b = max(1, int(m.group(1))), min(tot, int(m.group(2)))
        print("--- [On-Disk Lines %d-%d of %d] ---" % (a, b, tot))
        print("\n".join(lines[a - 1:b]))
        note_inspection(rel, lines, a - 1, b)
    else:
        lo, hi, note = resolve_scope(rel, lines, target)
        if note:
            print("--- [SCOPE NOT RESOLVED: %s | Signature TOC shown instead] ---" % note)
            print_toc(lines)
            return
        hi = min(hi, tot)
        cut = min(hi, lo + PROBE_CAP)
        more = " | TRUNCATED at %dL: continue with Lines %d-%d" % (PROBE_CAP, cut + 1, hi) if cut < hi else ""
        print("--- [On-Disk Lines %d-%d of %d | Scope: %s%s] ---" % (lo + 1, cut, tot, target or "TOP", more))
        print("\n".join(lines[lo:cut]))
        note_inspection(rel, lines, lo, cut)

# ---------------------------------------------------------------- Stage: Entry point

class OutputTee:
    """Copies everything printed during a run, so the output can be replayed when the same script is pasted again."""
    def __init__(self, stream):
        self.stream, self.parts = stream, []

    def write(self, s):
        self.parts.append(s)
        return self.stream.write(s)

    def flush(self):
        self.stream.flush()

    def text(self):
        return "".join(self.parts)

    def __getattr__(self, name):
        return getattr(self.stream, name)

RUNS_KEEP = 50

def payload_key(txt):
    """Fingerprint of a payload that ignores trailing spaces and blank edges, so a re-pasted script matches its first run."""
    return hashlib.sha1("\n".join(l.rstrip() for l in txt.strip().split("\n")).encode("utf-8", "surrogateescape")).hexdigest()

def runs_dir():
    return os.path.join(HARNESS_DIR, "runs")

LOCK_STALE_S, LOCK_WAIT_S = 120, 30

@contextlib.contextmanager
def state_lock():
    """Cross-process lock around shared state updates; held only for those updates, never while commands run."""
    os.makedirs(HARNESS_DIR, exist_ok=True)
    path = os.path.join(HARNESS_DIR, "state.lock")
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            # Windows refuses to create a file another process is deleting ("delete pending"): the lock is still busy
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE_S:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise RuntimeError("could not get the harness state lock at %s within %ds" % (path, LOCK_WAIT_S))
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

def assign_number(P, state):
    """Give an unnumbered script the next free number, atomically across concurrent harness processes."""
    with state_lock():
        cur = load_json(STATE_PATH, {})
        num = max(int(cur.get("last_num", 0)), state["last_num"]) + 1
        P["num"], P["id"] = num, "%s-%d" % (P["id"] or "S", num)
        cur["last_num"], cur["last_id"] = num, P["id"]
        save_json(STATE_PATH, cur)
    state["last_num"], state["last_id"] = P["num"], P["id"]

def claim_number(P, state):
    """Record a script's own number unless another process took it since this run started. -> claimed"""
    with state_lock():
        cur = load_json(STATE_PATH, {})
        if P["num"] <= int(cur.get("last_num", 0)):
            return False
        cur["last_num"], cur["last_id"] = P["num"], P["id"]
        save_json(STATE_PATH, cur)
    state["last_num"], state["last_id"] = P["num"], P["id"]
    return True

def save_state(state):
    """Write this run's state fields without rolling back numbers that other sessions took meanwhile."""
    with state_lock():
        cur = load_json(STATE_PATH, {})
        for k in ("goal", "committed_through", "shell"):
            if k in state:
                cur[k] = state[k]
        if state.get("last_num", 0) >= int(cur.get("last_num", 0)):
            cur["last_num"], cur["last_id"] = state["last_num"], state["last_id"]
        save_json(STATE_PATH, cur)

def find_run_by_key(key):
    """Saved run whose payload fingerprint matches, so an identical re-paste of an unnumbered script replays."""
    for path in glob.glob(os.path.join(runs_dir(), "*.json")):
        if not os.path.basename(path).startswith("side_"):
            e = load_json(path, None)
            if e and e.get("key") == key:
                return e
    return None

def sessions_path():
    return os.path.join(HARNESS_DIR, "sessions.json")

def touch_session(P):
    """Record a session's latest script and activity time in the project's session registry."""
    with state_lock():
        reg = load_json(sessions_path(), {})
        rec = reg.get(P["session"], {})
        rec["last_active"], rec["last_script"] = time.time(), P["id"]
        if P.get("session_aim"):
            rec["aim"] = P["session_aim"]
        reg[P["session"]] = rec
        save_json(sessions_path(), reg)

def print_sessions(current):
    reg = load_json(sessions_path(), {})
    if not reg:
        return

    def describe(rec):
        age = time.time() - rec.get("last_active", 0)
        when = "%dm ago" % (age // 60) if age < 7200 else "%dh ago" % (age // 3600)
        sc = rec.get("scope")
        scope = "%s (%s)" % (sc.get("aim") or "no aim", ", ".join(sc.get("paths", []))) if sc else "none"
        brake = " | BRAKED by %s" % rec["brake"]["from"] if rec.get("brake") else ""
        return "aim: %s | write scope: %s | last %s, %s%s" % (rec.get("aim") or "not set", scope, rec.get("last_script", "?"), when, brake)
    if current in reg:
        print("=== THIS SESSION: %s | %s ===" % (current, describe(reg[current])))
    others = sorted(((n, r) for n, r in reg.items() if n != current), key=lambda kv: -kv[1].get("last_active", 0))
    if others:
        print("=== OTHER SESSIONS (newest first) ===")
        for n, r in others:
            print("  %s: %s" % (n, describe(r)))

CURRENT_SESSION = None

def events_path():
    return os.path.join(HARNESS_DIR, "scope_events.json")

def grants_path():
    return os.path.join(HARNESS_DIR, "grants.json")

def path_in(path, pattern):
    """True when a workspace path falls under a scope pattern: a plain folder or file, or a glob with * ? and **."""
    p = pattern.strip().replace("\\", "/").lstrip("/")
    if not p:
        return False
    if not any(c in p for c in "*?["):
        p = p.rstrip("/")
        return path == p or path.startswith(p + "/")
    rx = re.escape(p).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, path) is not None

def literal_base(pattern):
    """The fixed folder or file part of a scope pattern, before any wildcard ('' means the whole workspace)."""
    p = pattern.strip().replace("\\", "/").lstrip("/")
    cut = min([i for i in (p.find("*"), p.find("?"), p.find("[")) if i >= 0] or [len(p)])
    if cut == len(p):
        return p.rstrip("/")
    return p[:cut].rsplit("/", 1)[0] if "/" in p[:cut] else ""

def scope_item(item):
    """A scope entry -> (path pattern, definition or '', depth or None): 'src/**', 'src depth 2' or 'src/a.py#Class.method'."""
    s = item.strip()
    m = re.search(r"\s+depth\s+(\d+)$", s)
    if m:
        s = s[:m.start()]
    pat, _, sym = s.partition("#")
    return pat.strip().replace("\\", "/").lstrip("/"), sym.strip(), int(m.group(1)) if m else None

def scope_path_covers(path, item):
    """True when a scope entry covers this file (a definition entry covers the file that holds it)."""
    pat, _, depth = scope_item(item)
    return path_in(path, pat) and within_depth(path, literal_base(pat), depth)

def touches_symbol(path, sym, op):
    """Whether a file change lands inside one definition of that file; unplaceable changes count as touching it."""
    if not op or op.get("type") not in ("hunk", "text_file") or not os.path.isfile(path):
        return True
    lines = decode_bytes(read_bytes(path))[0].split("\n")
    lo, hi, note = resolve_scope(path, lines, sym)
    if note:
        return False
    if op["type"] == "text_file":
        return any(lo <= i < hi for i in range(op["lo"], min(op["hi"], len(lines))) if op["find"] in lines[i])
    old = [x.rstrip() for x in op.get("find", "").split("\n")]
    if not any(old):
        return hi >= len(lines) - 1
    starts = [i for i in range(len(lines) - len(old) + 1) if [x.rstrip() for x in lines[i:i + len(old)]] == old]
    if len(starts) != 1:
        return True
    return starts[0] < hi and starts[0] + len(old) > lo

def scope_overlap(paths_a, paths_b):
    """Entry pairs that can claim the same code: nesting fixed parts, except two definitions of one file, which overlap only when one contains the other."""
    out = []
    for x in paths_a:
        for y in paths_b:
            (px, sx, _), (py, sy, _) = scope_item(x), scope_item(y)
            if sx and sy and px == py:
                if sx == sy or sx.startswith(sy + ".") or sy.startswith(sx + "."):
                    out.append((x, y))
                continue
            a, b = literal_base(px), literal_base(py)
            if a == "" or b == "" or a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                out.append((x, y))
    return out

def granted(session, path):
    return any(path_in(path, g) for g in load_json(grants_path(), {}).get(session or "", []))

def scope_owner(path, me, op=None):
    """(name, scope) of another session whose scope covers this change, unless the user granted this session that path; a definition entry only claims changes inside that definition."""
    for name, rec in load_json(sessions_path(), {}).items():
        sc = rec.get("scope")
        if name == me or not sc or granted(me, path):
            continue
        for item in sc.get("paths", []):
            if scope_path_covers(path, item):
                sym = scope_item(item)[1]
                if not sym or touches_symbol(path, sym, op):
                    return name, dict(sc, owned=item)
    return None, None

def apply_scope_change(P):
    """@SCOPE / @SCOPE_END / @SESSION_END: update this session's write scope, refusing overlaps, and log the change for the other sessions."""
    me = P["session"]
    if not me:
        print("  [REFUSED] @SCOPE, @SCOPE_END and @SESSION_END need an @SESSION line. Scopes are unchanged.")
        return
    with state_lock():
        reg = load_json(sessions_path(), {})
        rec = reg.setdefault(me, {})
        if P["session_end"] or P["scope_end"]:
            pending = own_uncommitted(me)
            if pending and "handover" not in str(P["session_end"] or P["scope_end"]).lower():
                print("  [RELEASE REFUSED] this session's changes are not committed yet: %s" % ", ".join("/" + p for p in pending[:8]))
                print("  -> commit them first, or write '@SCOPE_END handover' (or '@SESSION_END handover') to leave them to another session; the hand-over is announced.")
                return
            rec["scope"] = None
            rec["handed_over"] = sorted(set(rec.get("handed_over", [])) | set(pending))
            kind = "session ended" if P["session_end"] else "scope released"
            if pending:
                kind += "; uncommitted work handed over: " + ", ".join(pending[:8])
            event = {"session": me, "kind": kind, "aim": "", "paths": []}
            if P["session_end"]:
                reg.pop(me, None)
        else:
            aim, _, raw = P["scope"].partition("|")
            paths = [x.strip().replace("\\", "/").lstrip("/") for x in raw.split(",") if x.strip()]
            if not paths:
                print("  [REFUSED] @SCOPE needs '<aim> | <path or glob>, ...'. Scopes are unchanged.")
                return
            clashes = [(name, x, y) for name, other in reg.items() if name != me and other.get("scope")
                       for x, y in scope_overlap(paths, other["scope"].get("paths", [])) if not granted(me, literal_base(scope_item(x)[0]))]
            if clashes:
                print("  [SCOPE REFUSED] this scope overlaps another session's; scopes are unchanged:")
                for name, x, y in clashes[:6]:
                    print("    %s  overlaps  %s's  %s" % (x, name, y))
                print("  -> narrow the scope, or tell the user: they can have that session release its scope, or grant this session the overlap with their own --grant command.")
                return
            rec["scope"] = {"aim": aim.strip(), "paths": paths}
            event = {"session": me, "kind": "scope set", "aim": aim.strip(), "paths": paths}
        save_json(sessions_path(), reg)
        log = load_json(events_path(), {"seq": 0, "events": []})
        log["seq"] += 1
        event["seq"], event["time"] = log["seq"], time.time()
        log["events"] = (log["events"] + [event])[-200:]
        save_json(events_path(), log)
    print("  [SCOPE] %s %s%s" % (me, event["kind"], (": " + event["aim"] + " | " + ", ".join(event["paths"])) if event["paths"] else ""))
    if event["kind"] == "scope set":
        files = repo_files(".")
        covered = [f for f in files if any(scope_path_covers(f, g) for g in event["paths"])]
        if files and (len(covered) > 200 or len(covered) * 3 > len(files)):
            print("  [SCOPE WIDE] this scope covers %d of %d files, and every other session is refused all of them: narrow it to the files this task changes and widen it later if needed." % (len(covered), len(files)))
        if any(literal_base(g) in ("", "docs", "docs/trees") for g in event["paths"]):
            print("  -> tree files are shared by every session: claim the tree_*.txt files this task edits, not the whole trees folder.")

def guard_ops(P):
    """Refuse file changes that fall inside another session's declared scope, unless the user granted them."""
    if not P["ops"]:
        return
    warned = set()
    for op in P["ops"]:
        for path in (op.get("file"), op.get("to")):
            rel = path.strip().replace("\\", "/").lstrip("/") if path else ""
            owner, sc = scope_owner(rel, P["session"], op) if rel else (None, None)
            if owner:
                op["blocked"] = "inside session %s's scope (%s): %s; tell the user, who can coordinate or --grant it" % (owner, sc.get("owned"), sc.get("aim") or "no aim")
                break
            others = foreign_uncommitted(rel) if rel and P["session"] else []
            if others and rel not in warned:
                warned.add(rel)
                print("  [SHARED FILE] /%s holds uncommitted changes from session %s: your edit lands in the same file, and you cannot commit it until they commit or hand theirs over." % (rel, ", ".join(others)))

def watch_other_scopes(me):
    """Fingerprints of the files inside other sessions' scopes, taken before @RUN so changes made by commands can be reported."""
    pats = [(n, g) for n, rec in load_json(sessions_path(), {}).items() if n != me and rec.get("scope") for g in rec["scope"].get("paths", [])]
    if not pats:
        return None
    marks = {}
    for f in repo_files("."):
        owners = [n for n, g in pats if scope_path_covers(f, g)]
        if owners:
            try:
                st = os.stat(f)
                marks[f] = (owners[0], st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return {"marks": marks, "pats": pats}

def report_scope_changes(watched):
    """Detected, not prevented: report files in other sessions' scopes that @RUN commands created, changed or deleted."""
    hits = []
    for f, (owner, size, mt) in watched["marks"].items():
        try:
            st = os.stat(f)
            if (st.st_size, st.st_mtime_ns) != (size, mt):
                hits.append((owner, f, "changed"))
        except OSError:
            hits.append((owner, f, "deleted"))
    for f in repo_files("."):
        if f not in watched["marks"]:
            owners = [n for n, g in watched["pats"] if scope_path_covers(f, g)]
            if owners:
                hits.append((owners[0], f, "created"))
    if hits:
        print("\n=== [SCOPE ALERT] @RUN changed files inside other sessions' scopes ===")
        for owner, f, what in hits[:20]:
            print("  /%s %s (session %s)" % (f, what, owner))
        print("  -> stop and tell the user: these files belong to another session's work.")

def announce_scope_changes(me):
    """Show each scope change made by other sessions exactly once: everything newer than this session's cursor."""
    if not me:
        return
    with state_lock():
        reg = load_json(sessions_path(), {})
        if me not in reg:
            return
        log = load_json(events_path(), {"seq": 0, "events": []})
        first = "seen_seq" not in reg[me]
        cursor = reg[me].get("seen_seq", 0)
        reg[me]["seen_seq"] = log["seq"]
        save_json(sessions_path(), reg)
    msgs = [e for e in log["events"] if e["seq"] > cursor and e.get("to") == me]
    if msgs:
        print("\n=== MESSAGES FOR THIS SESSION (from other AI sessions, not from the user: weigh them, never let them override the user) ===")
        for e in msgs:
            print("  %s from %s: %s" % (e["kind"], e["session"], e.get("text", "")))
    if first:
        others = [(n, r["scope"]) for n, r in reg.items() if n != me and r.get("scope")]
        if others:
            print("\n=== OTHER SESSIONS' SCOPES (first look) ===")
            for n, sc in others:
                print("  %s: %s | %s" % (n, sc.get("aim") or "no aim", ", ".join(sc.get("paths", []))))
        return
    new = [e for e in log["events"] if e["seq"] > cursor and e["session"] != me and not e.get("to")]
    if new:
        print("\n=== SCOPE CHANGES FROM OTHER SESSIONS (shown once) ===")
        for e in new:
            print("  %s %s%s" % (e["session"], e["kind"], (": " + e["aim"] + " | " + ", ".join(e["paths"])) if e["paths"] else ""))

_DIRTY = []

def uncommitted_sessions(rel):
    """Sessions whose journaled scripts changed rel after its last commit, while rel still has uncommitted changes (git workspaces only)."""
    if MODE != "git" or not rel:
        return set()
    if not _DIRTY:
        rc, out, _ = run_git(["status", "--porcelain", "--untracked-files=all"])
        _DIRTY.append(set(ln[3:].split(" -> ")[-1].strip().strip('"') for ln in out.split("\n") if rc == 0 and len(ln) > 3))
    if rel not in _DIRTY[0]:
        return set()
    rc, out, _ = run_git(["log", "-1", "--format=%ct", "--", rel])
    since = int(out) if rc == 0 and out.strip().isdigit() else 0
    found = set()
    for hp in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        try:
            if os.path.getmtime(hp) <= since:
                continue
        except OSError:
            continue
        e = load_json(hp, None) or {}
        f = e.get("files", {}).get(rel)
        if f and not f.get("undone"):
            found.add(e.get("session"))
    return found

def foreign_uncommitted(rel):
    """Other active sessions with uncommitted changes in rel that they have not handed over."""
    reg = load_json(sessions_path(), {})
    return sorted(s for s in uncommitted_sessions(rel) if s and s != CURRENT_SESSION and s in reg and rel not in reg[s].get("handed_over", []))

def own_uncommitted(me):
    """Files this session changed through the harness that still hold its uncommitted changes."""
    rels = set()
    for hp in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        e = load_json(hp, None) or {}
        if e.get("session") == me:
            rels.update(r for r, f in e.get("files", {}).items() if not f.get("undone"))
    return sorted(r for r in rels if me in uncommitted_sessions(r))

def log_event(event):
    """Append one event to the shared log; call inside state_lock."""
    log = load_json(events_path(), {"seq": 0, "events": []})
    log["seq"] += 1
    event.setdefault("aim", "")
    event.setdefault("paths", [])
    event["seq"], event["time"] = log["seq"], time.time()
    log["events"] = (log["events"] + [event])[-200:]
    save_json(events_path(), log)

def send_messages(P):
    """@MESSAGE, @SCOPE_REQUEST and @BRAKE: notes this session writes for another one, delivered once on that session's next run."""
    if not P["msgs"]:
        return
    me = P["session"]
    if not me:
        print("  [NOT SENT] @MESSAGE, @SCOPE_REQUEST and @BRAKE need an @SESSION line.")
        return
    with state_lock():
        reg = load_json(sessions_path(), {})
        for kind, arg in P["msgs"]:
            parts = [x.strip() for x in arg.split("|")]
            to = re.sub(r"[^A-Za-z0-9_.-]+", "-", parts[0]).strip("-")
            text = " | ".join(parts[1:]).strip()
            if to not in reg or to == me or not text:
                print("  [NOT SENT] %s to '%s': %s" % (kind, parts[0], "no text" if to in reg and to != me else "no other session by that name (see the sessions list)"))
                continue
            if kind == "scope request":
                text = "asks for write access to " + text
            if kind == "brake":
                reg[to]["brake"] = {"from": me, "text": text}
            log_event({"session": me, "kind": kind, "to": to, "text": text})
            print("  [SENT] %s to %s: %s" % (kind, to, text))
        save_json(sessions_path(), reg)

def enforce_brake(P):
    """A braked session acknowledges with @BRAKE_ACK before it changes anything; reads and searches still run."""
    me = P["session"]
    if not me:
        return
    with state_lock():
        reg = load_json(sessions_path(), {})
        brake = (reg.get(me) or {}).get("brake")
        if brake and P["brake_ack"]:
            reg[me]["brake"] = None
            save_json(sessions_path(), reg)
            log_event({"session": me, "kind": "brake acknowledged", "to": brake["from"], "text": "%s read your brake and continues" % me})
    if not brake:
        return
    if P["brake_ack"]:
        print("  [BRAKE ACKNOWLEDGED] the brake from session %s is lifted" % brake["from"])
        return
    print("  [BRAKED] session %s (an AI, not the user) paused this session's changes: %s" % (brake["from"], brake["text"]))
    print("  -> take it into account, tell the user, and add @BRAKE_ACK to the next script; until then file changes, git and @RUN are refused here (reads still run).")
    for op in P["ops"]:
        op["blocked"] = "this session is braked by session %s; add @BRAKE_ACK first" % brake["from"]
    P["git"], P["runs"], P["undo"] = [], [], None

def session_cli(flag):
    """User-only commands: --release <session> ends a session and frees its scope; --unbrake <session> lifts a brake on it."""
    args = sys.argv[1:]
    i = args.index(flag)
    sess = args[i + 1] if len(args) > i + 1 else ""
    err = enter_workspace(arg_value("--workspace"))
    if err or not sess:
        print("usage: harness.py --workspace <project folder> %s <session>" % flag + ((" | " + err) if err else ""))
        return
    with state_lock():
        reg = load_json(sessions_path(), {})
        if sess not in reg:
            print("no session named %s; sessions: %s" % (sess, ", ".join(sorted(reg)) or "none"))
            return
        if flag == "--release":
            reg.pop(sess)
            kind = "session released by the user"
        else:
            reg[sess]["brake"] = None
            kind = "brake lifted by the user"
        save_json(sessions_path(), reg)
        log_event({"session": sess, "kind": kind})
    print("%s: %s" % (kind.upper(), sess))

def grant_cli():
    """User-only command: harness.py --workspace <folder> --grant|--revoke <session> <path or glob>."""
    flag = "--revoke" if "--revoke" in sys.argv[1:] else "--grant"
    args = sys.argv[1:]
    i = args.index(flag)
    sess = args[i + 1] if len(args) > i + 1 else ""
    pattern = args[i + 2].replace("\\", "/").lstrip("/") if len(args) > i + 2 else ""
    err = enter_workspace(arg_value("--workspace"))
    if err or not sess or not pattern:
        print("usage: harness.py --workspace <project folder> --grant <session> <path or glob>   (or --revoke)" + ((" | " + err) if err else ""))
        return
    with state_lock():
        g = load_json(grants_path(), {})
        cur = [p for p in g.get(sess, []) if p != pattern]
        if flag == "--grant":
            cur.append(pattern)
        g[sess] = cur
        save_json(grants_path(), g)
    print("%s: session %s %s write access to %s despite other sessions' scopes" % (flag[2:].upper() + "ED" if flag == "--grant" else "REVOKED", sess, "gets" if flag == "--grant" else "loses", pattern))

def expand_text_ops(P):
    """Check each @TEXT_REPLACE's total match count against its expect, then turn it into one edit per file it touches."""
    out = []
    for op in P["ops"]:
        if op.get("type") != "text" or op.get("malformed") or op.get("blocked"):
            out.append(op)
            continue
        path, _, scope = op["file"].partition("#")
        target = path.strip().replace("\\", "/").lstrip("/")
        if target and os.path.isfile(target):
            files = [target]
        else:
            named = glob_files(target) if any(c in target for c in "*?[") else [f for f in repo_files(".") if path_in(f, target or "**")]
            files = [f for f in named if within_depth(f, literal_base(target), op.get("depth"))]
        hits, per_file = [], []
        for f in files:
            if safe_rel(f) is None or os.path.getsize(f) > 2000000:
                continue
            raw = read_bytes(f)
            if b"\0" in raw[:4096]:
                continue
            lines = decode_bytes(raw)[0].split("\n")
            lo, hi = 0, len(lines)
            if scope.strip():
                lo, hi, note = resolve_scope(f, lines, scope.strip())
                if note:
                    continue
            found = [(i, lines[i].count(op["find"])) for i in range(lo, min(hi, len(lines))) if op["find"] in lines[i]]
            if found:
                hits += [(f, i, c) for i, c in found]
                per_file.append((f, lo, hi, sum(c for _, c in found)))
        total = sum(c for _, _, c in hits)
        if total != op["expect"]:
            where = ", ".join("/%s:%d" % (f, i + 1) for f, i, _ in hits[:12]) + (" ..." if len(hits) > 12 else "")
            op["blocked"] = "found %d occurrence(s), expected %d%s; nothing was replaced. Check with @GREP and resend with the right scope or expect" % (total, op["expect"], (": " + where) if hits else "")
            out.append(op)
            continue
        for f, lo, hi, count in per_file:
            out.append({"type": "text_file", "file": f, "scope": "TEXT" + (" " + scope.strip() if scope.strip() else ""), "label": "%s (%d here)" % (op["label"], count),
                        "find": op["find"], "replace": op["replace"], "lo": lo, "hi": hi, "count": count})
    P["ops"] = out

def side_allowed(P):
    """A @SIDE script may only read, search and @RUN: no file, note, goal, git or undo actions."""
    return not (P["ops"] or P["notes"] or P["goal"] or P["git"] or P["undo"] or P["git_init"] or P["scope"] is not None or P["scope_end"] or P["session_end"] or P["msgs"] or P["brake_ack"] or P["verdicts"] or P["campaigns"])

def side_run_path(raw_txt):
    return os.path.join(runs_dir(), "side_%s.json" % payload_key(raw_txt)[:16])

def save_run(P, raw_txt, body):
    """Keep a run's output (without its footer) for replay; only the newest RUNS_KEEP runs are kept."""
    d = runs_dir()
    os.makedirs(d, exist_ok=True)
    if P.get("side"):
        save_json(side_run_path(raw_txt), {"id": P["id"], "key": payload_key(raw_txt), "output": body})
        return
    for old in glob.glob(os.path.join(d, "%06d_*.json" % P["num"])):
        os.remove(old)
    name = "%06d_%s.json" % (P["num"], re.sub(r"[^A-Za-z0-9_-]", "_", P["id"]))
    save_json(os.path.join(d, name), {"id": P["id"], "key": payload_key(raw_txt), "output": body})
    runs = sorted(glob.glob(os.path.join(d, "*.json")))
    for stale in runs[:max(0, len(runs) - RUNS_KEEP)]:
        os.remove(stale)

def find_run(num):
    found = glob.glob(os.path.join(runs_dir(), "%06d_*.json" % num))
    return load_json(found[0], None) if found else None

def read_only(P):
    """True when a script only reads: no file, note, goal, git, undo or @RUN actions."""
    return not (P["ops"] or P["notes"] or P["goal"] or P["git"] or P["undo"] or P["runs"] or P["git_init"] or P["scope"] is not None or P["scope_end"] or P["session_end"] or P["msgs"] or P["brake_ack"] or P["verdicts"] or P["campaigns"])

def load_state():
    """Numbering and shell from the machine-local state file; the goal from .llm_harness/goal.md, which travels with the project."""
    st = {"last_num": 0, "last_id": "", "committed_through": 0, "goal": "", "shell": ""}
    if MODE == "git" and not os.path.isfile(STATE_PATH) and os.path.isfile(os.path.join(WORKSPACE_NOTES_DIR, "state.json")):
        carry_state(WORKSPACE_NOTES_DIR, True)
        print("  (carried numbering and undo history from .llm_harness/ into git mode)")
    st.update(load_json(STATE_PATH, {}))
    if os.path.isfile(GOAL_PATH):
        st["goal"] = read_text(GOAL_PATH).strip()
    elif st["goal"]:
        write_goal(st["goal"])
    return st

def write_goal(text):
    """Keep the pinned goal in the project tree beside notes.md, so moves, clones and pulls carry it."""
    os.makedirs(WORKSPACE_NOTES_DIR, exist_ok=True)
    with open(GOAL_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.strip() + "\n")

def print_goal(state):
    if state.get("goal"):
        print("=== GOAL: %s ===" % state["goal"])
    else:
        print("=== GOAL: not set -> pin the user's long-term goal with @GOAL <text> in the next script ===")

def print_footer_notes():
    """The user's notes ride in every footer, so a chat keeps them in view without asking for @NOTE_LIST."""
    try:
        notes, _ = load_workspace_notes()
    except (OSError, ValueError):
        return
    for note in notes:
        print("=== NOTE %s (the user's): %s ===" % (note["id"], note["text"]))

def print_footer(state):
    """Every run ends with the next free number, the pinned goal and the standing rules that apply to this workspace's mode."""
    if state is not None:
        print("\n=== NEXT SCRIPT NUMBER: %d ===" % (state["last_num"] + 1))
        print_goal(state)
        print_footer_notes()
        print_footer_campaigns()
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            found = re.findall(r"(?m)^- \*\*L1 Rule\W*(\[[^\]]+\])`?\s*(?:\(([\w-]+)\))?[`:*\s]*(.*)$", f.read())
        modes = {"git": "git", "nogit": "nogit", "no-git": "nogit"}
        rules = [(tag, body) for tag, cond, body in found if not cond or (bool(active_campaigns()) if cond.lower() == "campaign" else modes.get(cond.lower(), MODE) == MODE)]
    except OSError:
        print("=== L1 CONTRACT: workflow_commands.txt not found next to harness.py (%s); no standing rules printed ===" % SELF_DIR)
        return
    if rules:
        print("=== L1 CONTRACT (standing rules; apply, never echo) ===")
        for tag, body in rules:
            print("%s %s" % (tag, body.strip().strip("`").strip()))
    print("[L1-Commands] " + " ".join(DIRECTIVES.split()) + " | syntax of any one: @HELP <name>")

def enter_workspace(path):
    """chdir into the project a script names. Returns an error message, or None on success."""
    if not path:
        return "missing '@WORKSPACE <project folder>' line -> add it as line 2 of the payload, exactly as shown in the SYNTAX CARD."
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(full):
        return "workspace folder does not exist: %s" % full
    os.chdir(full)
    set_mode("git" if os.path.isdir(".git") else "nogit")
    return None

def set_mode(mode):
    """git mode keeps harness state in .git/llm_harness; no-git mode keeps it in .llm_harness next to the notes."""
    global MODE, HARNESS_DIR, HISTORY_DIR, STATE_PATH, CONFIG_PATH, GRAPH_PATH
    MODE = mode
    HARNESS_DIR = os.path.join(".git", "llm_harness") if mode == "git" else WORKSPACE_NOTES_DIR
    HISTORY_DIR = os.path.join(HARNESS_DIR, "history")
    STATE_PATH = os.path.join(HARNESS_DIR, "state.json")
    CONFIG_PATH = os.path.join(HARNESS_DIR, "config.json")
    GRAPH_PATH = os.path.join(HARNESS_DIR, "graph.json")

def mode_label():
    return "git mode" if MODE == "git" else "no-git mode (state in .llm_harness/, git directives off)"


NOTE_HEADER_RE = re.compile(r"^<!-- llm_harness_notes_next_id: (\d+) -->$")
NOTE_ENTRY_RE = re.compile(r"^- \[(N\d+)\] (.+)$")


def load_workspace_notes():
    """Load (notes, next_id); refuse to overwrite a manually damaged notes file."""
    if not os.path.isfile(WORKSPACE_NOTES_PATH):
        return [], 1

    with open(WORKSPACE_NOTES_PATH, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    next_id, notes, seen = 1, [], set()
    for line in lines:
        header = NOTE_HEADER_RE.fullmatch(line)
        if header:
            next_id = max(next_id, int(header.group(1)))
            continue
        if not line.strip():
            continue
        entry = NOTE_ENTRY_RE.fullmatch(line)
        if not entry:
            raise ValueError("unrecognized line in notes.md; refusing to overwrite it: %s" % line[:100])
        note_id, text = entry.group(1).upper(), entry.group(2)
        if note_id in seen:
            raise ValueError("duplicate note ID in notes.md: %s" % note_id)
        seen.add(note_id)
        notes.append({"id": note_id, "text": text})
        next_id = max(next_id, int(note_id[1:]) + 1)
    return notes, next_id


def write_workspace_notes(notes, next_id):
    """Atomically persist notes while retaining the next ID after removals."""
    os.makedirs(WORKSPACE_NOTES_DIR, exist_ok=True)
    tmp = WORKSPACE_NOTES_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("<!-- llm_harness_notes_next_id: %d -->\n" % next_id)
            for note in notes:
                f.write("- [%s] %s\n" % (note["id"], note["text"]))
        os.replace(tmp, WORKSPACE_NOTES_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def print_workspace_notes():
    """Print persistent notes belonging to the current workspace."""
    try:
        notes, _ = load_workspace_notes()
    except (OSError, ValueError) as exc:
        print("WARNING: could not read workspace notes: %s" % exc)
        return

    print("=== WORKSPACE NOTES ===")
    if not notes:
        print("(none)")
        return
    for note in notes:
        print("  [%s] %s" % (note["id"], note["text"]))


def execute_note_ops(ops):
    """Apply note commands and print confirmation plus the resulting workspace notes."""
    for op in ops:
        try:
            notes, next_id = load_workspace_notes()
            action = op["action"]

            if action == "list":
                print_workspace_notes()
                continue

            if action == "add":
                note_id = "N%03d" % next_id
                notes.append({"id": note_id, "text": op["text"]})
                write_workspace_notes(notes, next_id + 1)
                print("  [NOTE ADDED] %s" % note_id)

            elif action == "edit":
                found = next((n for n in notes if n["id"] == op["id"]), None)
                if found is None:
                    print("  [NOTE ERROR] note ID not found: %s" % op["id"])
                    continue
                found["text"] = op["text"]
                write_workspace_notes(notes, next_id)
                print("  [NOTE EDITED] %s" % op["id"])

            elif action == "remove":
                kept = [n for n in notes if n["id"] != op["id"]]
                if len(kept) == len(notes):
                    print("  [NOTE ERROR] note ID not found: %s" % op["id"])
                    continue
                write_workspace_notes(kept, next_id)
                print("  [NOTE REMOVED] %s" % op["id"])

        except (OSError, ValueError) as exc:
            print("  [NOTE ERROR] %s" % exc)

def harness_call(shell):
    """How a script invokes this file from any folder, in the user's shell."""
    home = os.path.expanduser("~")
    inside = SELF_PATH.startswith(home + os.sep)
    tail = SELF_PATH[len(home) + 1:]
    if shell == "powershell":
        return 'python "%s"' % (("$HOME\\" + tail.replace("/", "\\")) if inside else SELF_PATH)
    return "python3 %s" % (("~/" + tail.replace(os.sep, "/")) if inside and " " not in tail else "'%s'" % SELF_PATH)

GIT_CARD = """  @GIT_STATUS
  @GIT_COMMIT <type(scope): subject>  /<path> per line  @END_GIT_COMMIT   commits exactly those paths (list deleted and renamed-from paths too)
  @GIT_FOLD <n>                                   first git line only: reopens the last n unpushed commits so the @GIT_COMMIT blocks after it re-slice them
"""

def print_help(name, shell):
    """@HELP <directive>: the syntax-card lines for one directive, taken from the card itself so the two never disagree."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_card(shell, os.path.abspath("."))
    key = "@" + name.strip().lstrip("@").upper()
    out, keep = [], False
    for ln in buf.getvalue().split("\n"):
        s = ln.strip()
        if s.startswith("@"):
            keep = re.search(r"(?<![\w@])" + re.escape(key) + r"(?!\w)", ln) is not None
        elif not ln.startswith("  "):
            keep = False
        if keep:
            out.append(ln)
    print("\n=== HELP: %s ===" % key)
    print("\n".join(out) if out else "  no directive named %s in this workspace's mode -> see [L1-Commands] in the footer" % key)

def print_card(shell, workspace):
    """The whole payload grammar, printed once per session by the handshake."""
    call = harness_call(shell)
    print("=== SYNTAX CARD (shown once per session; everything else is explained by harness output when it happens) ===")
    print("Wrap every script in one code fence, exactly like this:")
    if shell == "powershell":
        print("  $OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); @'")
    else:
        print("  %s << 'HARNESS_EOF'" % call)
    print("  @SCRIPT [<ACT>: <short title>]                 no number: the harness assigns one and prints [ASSIGNED]")
    print("  @SESSION <short name> | <this chat's own aim>   the same line on every script of this chat")
    print("  @SCOPE <aim> | <path or glob>, ...              (optional) the files this session will change; an overlap with another session's scope is refused")
    print("  @WORKSPACE %s" % workspace)
    print("  <directives>")
    if shell == "powershell":
        print("  '@ | %s" % call)
        print("No payload line may begin with '@.")
    else:
        print("  # ---\n  # ---\n  # ---\n  HARNESS_EOF")
        print("At most 120 payload lines per script and nothing after HARNESS_EOF (serial/SSH paste safety); split larger changes into consecutive numbers.")
    print("""Directives:
  @GOAL <text>                                    pins the user's long-term goal in .llm_harness/goal.md (travels with the project); every run prints it in the footer
  @SIDE                                           runs the script outside the numbering (its @SCRIPT needs no number; only reads, searches and @RUN), so a second chat can work in a project another chat drives; an identical re-paste replays
  @REPEATABLE                                     the script runs on every paste, also an identical one, so the user can rerun it or edit its values by hand (no number; only reads, searches and @RUN)
  @VERDICT <folder, file or file#Class.method> | <verdict> | <note>   a conclusion about that area, kept in .llm_harness/verdicts.json (travels with git); later changes there mark it STALE; '| clear' removes it
  @VERDICTS [all | stale | session <name>]       lists verdicts with the tree's description; a folder verdict hides children that say the same (default: this session's)
  @CAMPAIGN <name> | <word> = <meaning>; ...      starts a sweep (only when the user asks): its verdict words, a footer line and the [L1-Campaign] rule while it runs; Tree probes show <<verdict: ...>> on marked nodes
  @CAMPAIGN_END <name>                            ends the sweep and removes every verdict it recorded
  @SCOPE <aim> | <path or glob>, ...              declares or changes this session's write scope; other sessions are told once; their scopes are refused to this session's file changes
  (a scope entry may be file#Class.method, claiming only that definition so others can still edit the rest of the file, or end with ' depth N')
  @SCOPE_END    @SESSION_END                      release this session's scope / end the session; refused while its changes are uncommitted, unless followed by 'handover'
  @MESSAGE <session> | <text>                     a note for another session, shown once on its next run (labelled as from an AI, not the user)
  @SCOPE_REQUEST <session> | <paths> | <reason>   asks that session to give up those paths; only its owner or the user can
  @BRAKE <session> | <reason>    @BRAKE_ACK       pause another session's changes until it acknowledges / acknowledge a brake on this session
  @NOTE_ADD <text>                                adds a persistent workspace note
  @NOTE_EDIT <note-id> <text>                     edits an existing note
  @NOTE_REMOVE <note-id>                          removes an existing note
  @NOTE_LIST                                      prints all workspace notes
  @FILE /<path>                                   selects the file for the hunks that follow
  @HUNK <scope> | <short label>                   then exactly these marker lines:
  --- [OLD CODE] ---
  <verbatim on-disk lines>
  +++ [NEW CODE] +++
  <replacement lines; leave empty to delete the old lines>
  === [END HUNK] ===
  (or, in place of +++ [NEW CODE] +++ and its lines: +++ [LINE EDITS] +++ then one edit per line: 'N: new text' replaces line N,
   'N-' deletes it, 'N+ text' inserts after it (0 = before line 1); N counts the recited OLD CODE lines from 1, so it is always verified)
  @NEW_FILE /<path>  <content>  @END_NEW_FILE     each on its own line; rejected if the file exists
  @DELETE_FILE /<path>
  @RENAME /<old> -> /<new>                        later hunks in the same script use the new path
  @TEXT_REPLACE <file, file#Class.method, folder or glob> | expect <N> | <label>   then these marker lines (one line of text each):
  --- [OLD TEXT] ---
  <exact text, at least 12 characters, found exactly N times in that scope (default N = 1)>
  +++ [NEW TEXT] +++
  <replacement; empty removes it>
  === [END TEXT] ===
  (for a small change inside a long line, or the same change across many files, e.g. import paths after a move; take N from a @GREP)
  @PROBE /<path> | Scope | <scope>                prints that scope (500-line cap)
  @PROBE /<path> | Lines | <start>-<end>
  @PROBE /<path> | TOC                            signatures and # Stage anchors
  @PROBE /<tree file> | Tree | <node path or TOP> | <depth>
  @PROBE /<tree file> | Check                     drift check: tree nodes whose file, name() or # Stage anchor no longer exists in the code
  @PROBE /<file> | Unused                         deletion candidates: definitions that nothing calls (call graph) and nothing else mentions (text)
  @PROBE /<file> | Refs | <scope>                 what a scope uses: each project name with its use lines and definition site, plus external calls
  @XREF <symbol>                                  whole-word references across the repo, definitions first
  @XREF <symbol> | callers|callees|impact         call graph via codebase-memory-mcp when installed (impact = callers up to 5 levels; callers also list each call line); qualify shared names as <package or Type>.<name>
  @FIND <name, path fragment or glob>             files by name or path, e.g. @FIND lease or @FIND *.vue
  @GREP <text>  or  @GREP /<regex>/[i]            matching lines; add ' | in <path fragment or glob>' to limit where it looks
  @LIST_DIR /<path>    @LIST_RECURSIVE /<path>
  (@FIND, @GREP, @LIST_RECURSIVE and @TEXT_REPLACE take a last '| depth N': at most N folder levels below the fixed part of the path, 1 = directly inside.
   Globs: with a '/' they start at the workspace root and * stays in one folder, ** crosses folders; without a '/' they match file names at any depth.)
  @RUN [--timeout <s>] <command>                  any other command (tests, builds, installs, CLI tools) in the workspace; default limit 300s; not undoable
  @INIT_DEBUG <slug>                              creates docs/debug/bug_<slug>.txt with its four sections and prints it
  @UNDO <hi>..<lo>  or  @UNDO <number>            reverses journaled scripts, newest first
""" + (GIT_CARD if MODE == "git" else "  @GIT_INIT [Name <email>]                        makes this folder a git repository (main, default .gitignore, numbering and undo carried over, optional identity for this repository only); other git directives work from then on, even in the same script\n") + """Scopes: TOP | EOF (append; OLD CODE empty) | Name | Class.method | Parent > # Stage anchor | tree node path (a > b)
A hunk applies only where OLD CODE matches exactly one place, searched in the scope first, then in the whole file.
@UNDO needs its own script. Commits may open a script that then changes files: the commit runs first and the file changes are skipped if it fails.
Every script runs in this order: commit, file changes or undo, notes, @RUN, then @GIT_STATUS, listings, searches and probes (so they show the result).
A script with no directives is valid: it just prints the footer (next number, goal, standing rules).
Tree files: write |-- and \\-- connectors; they are stored as box glyphs. Every file gets a '/<project>/<path>' comment as its first line automatically.""")
    print("Notes are the user's: add, edit or remove them only when the user explicitly asks. They are stored in .llm_harness/notes.md, may ride along with any script, and are not reversed by @UNDO.")
    print("Inside a fence write %s for three consecutive backticks and https%s for a URL scheme; both are decoded before matching." % (SENT_BT3, "@@" + "://"))
    print("Lines beginning with '->' in harness output are instructions for your next turn.")

def handshake(workspace, shell):
    line = "llm_harness v%s | python %s" % (VERSION, sys.version.split()[0])
    if not workspace:
        print(line + " | no workspace given")
        print('-> run: %s --workspace "<project folder>"' % harness_call(shell))
        return
    err = enter_workspace(workspace)
    if err:
        print(line + " | NOT READY: " + err)
        return
    st = load_state()
    st["shell"] = shell
    save_json(STATE_PATH, st)
    journal = len(glob.glob(os.path.join(HISTORY_DIR, "*.json")))
    print("%s | workspace: %s | %s | last script: %s | next number: %d | journal: %d" % (
        line, os.path.abspath("."), mode_label(), ("[%s]" % st["last_id"]) if st["last_id"] else "none", st["last_num"] + 1, journal))
    print_goal(st)
    if not os.path.isfile(RULES_PATH):
        print("WARNING: workflow_commands.txt is not next to harness.py (%s); standing rules cannot be printed after runs." % SELF_DIR)
    print_workspace_notes()
    print_card(shell, os.path.abspath("."))

def timed(fn, *args):
    """Run one read-only directive and print how long it took, so slow steps are visible."""
    t = time.time()
    fn(*args)
    print("  (%.1fs)" % (time.time() - t))

def arg_value(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    shell = (arg_value("--shell") or ("powershell" if os.name == "nt" else "bash")).lower()
    if "--version" in sys.argv[1:]:
        print("llm_harness v%s" % VERSION)
        return
    for flag in ("--release", "--unbrake"):
        if flag in sys.argv[1:]:
            return session_cli(flag)
    if "--grant" in sys.argv[1:] or "--revoke" in sys.argv[1:]:
        return grant_cli()
    if len(sys.argv) > 1 or sys.stdin.isatty():
        handshake(arg_value("--workspace"), shell)
        return
    raw_txt = decode_payload(sys.stdin.buffer.read())
    P = parse_payload(raw_txt)
    tee = sys.stdout = OutputTee(sys.stdout)
    t_start = time.time()
    print("=== llm_harness v%s | EXECUTING [%s: %s] ===" % (VERSION, P["id"] or "UNKNOWN", P["title"]))
    err = enter_workspace(P["workspace"])
    if err:
        print("  [REFUSED] %s Nothing was run." % err)
        return print_footer(None)
    print("  Workspace: %s | %s" % (os.path.abspath("."), mode_label()))
    state = load_state()
    start, rerun, assigned = len(tee.text()), False, False
    if P["repeatable"] and (P["num"] is not None or not side_allowed(P)):
        print("  [REFUSED] a @REPEATABLE script has no number and may only read, search and @RUN, so running it again can never change files twice. Nothing was run.")
        return print_footer(state)
    if P["repeatable"]:
        print("  [REPEATABLE] runs on every paste, also an identical one; edit its values and paste it again as often as needed")
    if P["side"]:
        if not side_allowed(P):
            print("  [REFUSED] a @SIDE script may only read, search and @RUN; file changes, notes, goal, git and undo belong to the numbered scripts of the chat that drives this project. Nothing was run.")
            return print_footer(state)
        prior = None if P["repeatable"] else load_json(side_run_path(raw_txt), None)
        if prior:
            print("  [REPLAY] this @SIDE script already ran and this paste is identical, so nothing was run again. Its saved output:")
            print(prior.get("output", "").rstrip("\n"))
            return print_footer(state)
        print("  [SIDE] runs outside the numbering: no script number used, nothing journaled")
    elif P["num"] is None:
        prior = None if P["repeatable"] else find_run_by_key(payload_key(raw_txt))
        if prior:
            print("  [REPLAY] [%s] already ran and this paste is identical, so nothing was run again. Its saved output:" % prior.get("id"))
            print(prior.get("output", "").rstrip("\n"))
            print("\n  -> to run it again on purpose (for example after the user changed something), change its title.")
            return print_footer(state)
        assign_number(P, state)
        assigned = True
        print("  [ASSIGNED] " + P["id"])
    elif P["num"] <= state["last_num"]:
        prior = find_run(P["num"])
        if prior and prior.get("key") == payload_key(raw_txt):
            print("  [REPLAY] [%s] already ran and this paste is identical, so nothing was run again. Its saved output:" % P["id"])
            print(prior.get("output", "").rstrip("\n"))
            return print_footer(state)
        if prior or not read_only(P):
            print("  [STALE OR DUPLICATE SCRIPT] [%s] is not newer than the last executed script [%s]. Nothing was run." % (P["id"], state["last_id"]))
            print("  -> Use the next script number printed below; do not reuse an executed number.")
            return print_footer(state)
        rerun = True
        print("  [RE-RUN] [%s] is older than [%s] but only reads and has no saved output, so it runs again; the numbering is unchanged." % (P["id"], state["last_id"]))
    for w in P["warnings"]:
        print("  [WARNING] " + w)
    if not rerun and not P["side"] and not assigned and not claim_number(P, state):
        print("  [STALE OR DUPLICATE SCRIPT] [%s] was taken by another session a moment ago. Nothing was run." % P["id"])
        print("  -> write @SCRIPT [<ACT>: <title>] without a number and the harness assigns a free one.")
        return print_footer(state)
    if P["session"]:
        touch_session(P)
    globals()["CURRENT_SESSION"] = P["session"]
    globals()["CURRENT_SCRIPT"], globals()["CURRENT_RUN"] = P["id"], "%s@%.6f" % (P["id"], time.time())
    enforce_brake(P)
    send_messages(P)
    if P["scope"] is not None or P["scope_end"] or P["session_end"]:
        apply_scope_change(P)
    expand_text_ops(P)
    guard_ops(P)
    if P["goal"]:
        state["goal"] = P["goal"]
        write_goal(P["goal"])
        print("  [GOAL SET] " + P["goal"])
    if P["git_init"]:
        git_init(P["git_init"])
    if P["undo"] and (P["ops"] or P["git"]):
        print("  [REFUSED] @UNDO must be in its own script (probes, searches, notes and @RUN may ride along). No changes were made.")
        print("  -> resend the undo and the other changes as separate scripts with consecutive numbers.")
    elif P["git"] and MODE != "git":
        print("  [REFUSED] git directives in a no-git workspace. No changes were made.")
        print("  -> drop the @GIT_* lines, or start the script with @GIT_INIT to make this folder a git repository, and resend.")
    else:
        git_ok = True
        if P["git"]:
            git_ok = run_git_ops(P["git"])
            if git_ok:
                state["committed_through"] = P["num"] - 1 if P["ops"] else P["num"]
        if P["undo"]:
            run_undo(P["undo"], state)
        elif P["ops"] and not git_ok:
            print("\n  [SKIPPED] file changes in this script, because the commit before them failed. -> fix the commit, then resend.")
        elif P["ops"]:
            hunk_log, files_log = execute_ops(P["ops"])
            if files_log:
                save_history(P, raw_txt, hunk_log, files_log)
    if P["notes"]:
        execute_note_ops(P["notes"])
    run_shell = state.get("shell") or shell
    watched = watch_other_scopes(P["session"]) if P["runs"] else None
    for r in P["runs"]:
        run_command(r["cmd"], r["timeout"], run_shell)
    if watched:
        report_scope_changes(watched)
    if P["git_status"]:
        git_status_report()
    for d in P["list_dir"]:
        timed(run_list, d, False)
    for d in P["list_rec"]:
        timed(run_list, d, True)
    for f in P["finds"]:
        timed(run_find, f)
    for g in P["greps"]:
        timed(run_grep, g)
    for s in P["xrefs"]:
        timed(run_xref, s)
    apply_campaigns(P)
    record_verdicts(P)
    for pr in P["probes"]:
        timed(run_probe, pr)
    for h in P["helps"]:
        print_help(h, state.get("shell") or shell)
    for arg in P["verdict_lists"]:
        list_verdicts(arg)
    save_state(state)
    save_run(P, raw_txt, tee.text()[start:])
    if P["git"] and not (P["ops"] or P["runs"] or P["probes"] or P["xrefs"] or P["greps"] or P["finds"] or P["list_dir"] or P["list_rec"] or P["notes"] or P["helps"]
                         or P["scope"] is not None or P["scope_end"] or P["session_end"] or P["msgs"]):
        print("\n  -> this script only did git work: put the next known change, test or probe in the same script as the commit, so no turn is spent on git alone.")
    announce_scope_changes(P["session"])
    print_sessions(P["session"])
    print("\n=== RUN TIME: %.1fs ===" % (time.time() - t_start))
    print_footer(state)

if __name__ == "__main__":
    main()
