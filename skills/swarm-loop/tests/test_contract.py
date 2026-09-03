#!/usr/bin/env python3
"""Three-way contract test: build-docs emits a ticket, swarm-loop consumes it.

`build-docs` (skills/build-docs/references/templates.md) defines a JSON ticket
contract. `swarm-loop` reads that graph and REFUSES on violations of it —
`freeze` dies when a ticket carries no attributed frozen test and again when a
frozen test imports a module owned by a ticket outside its dependency closure;
`red-check` refuses a ticket whose `verify` cannot go red. So a field one side
starts emitting and the other never reads does NOT fail at the boundary. It
fails mid-run, at freeze, hours in.

Two documents agreeing with each other is not enough, and this repo has the
receipts: `merge-protocol.md` documented a comparison the code does not
perform, and `SKILL.md` told every worker to use an index that resolves to
another project entirely. A doc-to-doc test would have caught neither. So this
test is THREE-legged:

  1. PRODUCER   skills/build-docs/references/templates.md — the JSON ticket
                block, the "Field semantics" list, and the markdown ticket form.
  2. DECLARED   references/build-docs-intake.md — the ticket-field consumer
     CONSUMER   ledger, between the CONTRACT-LEDGER markers. For every field:
                WHO consumes it, and for code-consumed fields what happens when
                it is missing.
  3. CODE       scripts/swarmloop.py — for every field the ledger declares
                `code` or `advisory-recorded` or `loop-minted`, prove the code
                actually reads it off a ticket.

Consumer classes, and exactly what each one asserts:

    code               producer emits it; swarmloop.py reads it off a ticket;
                       the "On absence" note must say what breaks without it.
    advisory-recorded  producer emits it; swarmloop.py reads it, but never as a
                       predicate — it is recorded and printed, never trusted.
    packet-prose       producer emits it; NO code path. It is mapped into the
                       worker's task packet as prose and a human/agent consumes
                       it. Silence here is a DECISION, not an oversight — which
                       is the whole reason the ledger exists. If code starts
                       reading it, the declaration is stale and this fails.
    loop-minted        NOT a build-docs field. swarmloop.py writes it onto the
                       tickets it mints itself (`findings` -> tickets) and reads
                       it back. Declared so that "a field the code reads that
                       the producer never emits" stays a real failure signal.

Failure vocabulary — every failure names a FIELD and a LEG:

    UNDECLARED          producer emits it, the ledger has no row: pure drift.
    NO-PRODUCER         the ledger declares a field templates.md does not emit.
    PHANTOM-CONSUMER    the ledger declares `code`, swarmloop.py never reads it:
                        a documented consumer that does not exist.
    UNDECLARED-CONSUMER swarmloop.py reads a ticket field no leg declares.
    STALE-PROSE         declared `packet-prose`, but code reads it.
    MINTED-IS-PRODUCED  declared `loop-minted`, but templates.md emits it.
    NO-ABSENCE-NOTE     a code row that does not say what missing costs.
    PRODUCER-INTERNAL   templates.md's markdown form and JSON form disagree.
    DETECTOR-BLIND      the code detector found none of the fields it is known
                        to read: it has been broken by a rename, and every
                        PHANTOM-CONSUMER below it would be a lie.
    META                the mutation battery: this test's own sensitivity.

THE CODE DETECTOR, and its proven limit.
A regex over quoted field names is what this started as, and it is wrong in a
way that matters: `swarmloop.py` contains `os.path.join(d, "acceptance")` twice
— a DIRECTORY name — so a regex reports `acceptance` as code-consumed when no
line reads it off a ticket. Same for `res["reads"]` (the reads-scanner's own
result dict, 8 hits) and the argparse subcommand names `"verify"` and `"reads"`.
So the detector is an AST pass that recognizes four access forms —
`T.get("f")`, `T["f"]`, `"f" in T`, and this codebase's own accessor
`_graph_strs(T, "f", ...)` — AND requires the RECEIVER to denote a ticket:
the name `t`/`ticket`/`tkt`, or a subscript/`.get` on `idx` (the graph index),
with `X or {}` reduced to `X`.

Its limit is the receiver test, and it is pinned, not hand-waved:
  * FALSE NEGATIVE (safe): a ticket bound to a name outside that set is
    invisible, so a real read reads as PHANTOM-CONSUMER — loud and actionable.
    DETECTOR-BLIND is the backstop for the rename that would cause it.
  * FALSE POSITIVE (the real limit): a non-ticket dict bound to `t` is read as
    a ticket. `meta-fp-limit-unhandled` below INJECTS exactly that and asserts
    the detector IS fooled. It is a pinned wrong answer: strengthen the
    detector and that cell goes red, on purpose, so the limit cannot quietly
    change.

Self-contained: the mutation battery copies the three files into a temp dir it
creates and removes. Nothing outside it is ever written. Pure stdlib.

Usage:   python3 test_contract.py [-v]
Exit:    0 = every cell passed, 1 = at least one divergence.
"""

import ast
import os
import re
import json
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, os.pardir)
SKILLS_ROOT = os.path.join(SKILL, os.pardir)

PRODUCER = os.path.normpath(
    os.path.join(SKILLS_ROOT, "build-docs", "references", "templates.md"))
INTAKE = os.path.normpath(
    os.path.join(SKILL, "references", "build-docs-intake.md"))
CODE = os.path.normpath(os.path.join(SKILL, "scripts", "swarmloop.py"))

LEDGER_BEGIN = "<!-- CONTRACT-LEDGER:BEGIN -->"
LEDGER_END = "<!-- CONTRACT-LEDGER:END -->"

CLASSES = ("code", "advisory-recorded", "packet-prose", "loop-minted")
# Classes whose contract is "swarmloop.py reads this off a ticket".
CODE_BACKED = ("code", "advisory-recorded", "loop-minted")

# Fields this test KNOWS swarmloop.py reads off a ticket. If the detector finds
# none of them, it has been broken by a rename and every verdict under it is
# worthless — so that is its own failure, not a quiet pass.
DETECTOR_CANARIES = ("depends_on", "scope", "verify", "id")

# Receiver spellings that denote a ticket in scripts/swarmloop.py. Pinned here
# rather than inferred, because inferring it is how a detector silently starts
# answering a different question.
TICKET_NAMES = ("t", "ticket", "tkt")
TICKET_INDEX = "idx"


# ---------------------------------------------------------------------------
# LEG 1 — the producer contract
# ---------------------------------------------------------------------------

def producer_fields(path):
    """(fields, markdown_labels, errors) from templates.md.

    Three independent statements of the contract live in that file and all
    three are read:
      * the ```json ticket block  — the machine form, authoritative for keys;
      * the "Field semantics:" bullets — the prose definition of each key;
      * the ```markdown ticket form — what a human writes in TASKS.md.
    """
    errors = []
    try:
        src = open(path).read()
    except OSError as e:
        return set(), set(), ["cannot read producer contract %s: %s" % (path, e)]

    fields = set()

    blocks = re.findall(r"```json\n(.*?)```", src, re.S)
    if not blocks:
        errors.append("templates.md no longer contains a ```json ticket block "
                      "— the machine form of the contract is gone")
    for b in blocks:
        try:
            obj = json.loads(b)
        except ValueError as e:
            errors.append("templates.md's ```json ticket block does not parse "
                          "(%s) — the contract cannot be read mechanically" % e)
            continue
        if isinstance(obj, dict):
            fields |= set(obj)

    sem = re.search(r"^Field semantics:\n((?:^[-\s].*\n)+)", src, re.M)
    if not sem:
        errors.append("templates.md no longer contains a 'Field semantics:' "
                      "list — the prose half of the contract is gone")
    else:
        heads = re.findall(r"^- `([a-z_]+)", sem.group(1), re.M)
        if not heads:
            errors.append("templates.md's 'Field semantics:' list names no "
                          "backticked fields")
        fields |= set(heads)

    # "(`owned_files` is an accepted alias)" — an alias the consumer must
    # tolerate is part of the contract, so the producer has to keep SAYING it.
    fields |= set(re.findall(r"`([A-Za-z_]+)`\s+is an accepted alias", src))

    md = re.search(r"```markdown\n(###\s+T-\d+:.*?)```", src, re.S)
    labels = set()
    if not md:
        errors.append("templates.md no longer contains a ```markdown ticket "
                      "form — the human half of the contract is gone")
    else:
        for lab in re.findall(r"^- \*\*([A-Za-z][A-Za-z \-]*)\*\*", md.group(1), re.M):
            labels.add(lab.strip().lower().replace(" ", "_").replace("-", "_"))

    return fields, labels, errors


# ---------------------------------------------------------------------------
# LEG 2 — the declared consumer contract
# ---------------------------------------------------------------------------

def ledger_rows(path):
    """(rows, errors). rows = {field: {"consumer": str, "absence": str}}."""
    errors = []
    try:
        src = open(path).read()
    except OSError as e:
        return {}, ["cannot read intake doc %s: %s" % (path, e)]

    if LEDGER_BEGIN not in src or LEDGER_END not in src:
        return {}, [
            "build-docs-intake.md carries no ticket-field consumer ledger: the "
            "%s / %s markers are missing. Leg 2 of the contract does not exist, "
            "so nothing below can be checked." % (LEDGER_BEGIN, LEDGER_END)]

    block = src.split(LEDGER_BEGIN, 1)[1].split(LEDGER_END, 1)[0]
    rows = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        field = cells[0].strip().strip("`").strip()
        if not field or field.lower() == "field" or set(field) <= set("-: "):
            continue
        consumer = cells[1].strip().strip("`").strip()
        absence = cells[2].strip()
        if field in rows:
            errors.append("ledger declares `%s` twice — one of the two rows is "
                          "unread by anything and will drift" % field)
        rows[field] = {"consumer": consumer, "absence": absence}

    if not rows:
        errors.append("the ledger markers are present but the table between "
                      "them has no field rows")
    for f, r in sorted(rows.items()):
        if r["consumer"] not in CLASSES:
            errors.append("ledger row `%s` declares consumer %r, which is not "
                          "one of %s" % (f, r["consumer"], ", ".join(CLASSES)))
    return rows, errors


# ---------------------------------------------------------------------------
# LEG 3 — the code
# ---------------------------------------------------------------------------

def _unwrap(node):
    """Reduce `X or {}` (and `X or []`) to X. The codebase writes
    `(idx[t] or {}).get("title")`, which is a ticket read wearing a BoolOp."""
    while (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
           and len(node.values) == 2):
        node = node.values[0]
    return node


def _is_ticket_receiver(node):
    node = _unwrap(node)
    if isinstance(node, ast.Name) and node.id in TICKET_NAMES:
        return True
    if isinstance(node, ast.Subscript):
        base = _unwrap(node.value)
        return isinstance(base, ast.Name) and base.id == TICKET_INDEX
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"):
        base = _unwrap(node.func.value)
        return isinstance(base, ast.Name) and base.id == TICKET_INDEX
    return False


def code_field_sites(path):
    """({field: [(lineno, form)]}, errors) — ticket-field reads in swarmloop.py."""
    try:
        src = open(path).read()
    except OSError as e:
        return {}, ["cannot read consumer code %s: %s" % (path, e)]
    try:
        tree = ast.parse(src, path)
    except SyntaxError as e:
        return {}, ["scripts/swarmloop.py does not parse (%s) — the code leg "
                    "cannot be read" % e]

    sites = {}

    def add(field, lineno, form):
        sites.setdefault(field, []).append((lineno, form))

    for node in ast.walk(tree):
        # T.get("f")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_ticket_receiver(node.func.value)):
            add(node.args[0].value, node.lineno, '.get("%s")' % node.args[0].value)
        # _graph_strs(T, "f", "g") — the codebase's own ticket-field accessor.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_graph_strs" and node.args
                and _is_ticket_receiver(node.args[0])):
            for a in node.args[1:]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    add(a.value, node.lineno, '_graph_strs(…, "%s")' % a.value)
        # T["f"]  (load, store and del all count — all three are a read of the
        # contract's name, and `del t["reads"]` at the reconcile is one of them)
        if (isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and _is_ticket_receiver(node.value)):
            add(node.slice.value, node.lineno, '["%s"]' % node.slice.value)
        # "f" in T
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and node.comparators
                and _is_ticket_receiver(node.comparators[0])):
            add(node.left.value, node.lineno, '"%s" in …' % node.left.value)

    return sites, []


def naive_regex_count(path, field):
    """What the CHEAP detector would have said. Only used to prove the AST pass
    is not the same answer with more machinery."""
    try:
        return len(re.findall(r'"%s"' % re.escape(field), open(path).read()))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# The three-way check
# ---------------------------------------------------------------------------

def run_checks(producer_path, intake_path, code_path):
    """(cells, failures). failures = [(code, field, leg, message)]."""
    cells = 0
    fails = []

    fields, md_labels, perr = producer_fields(producer_path)
    rows, ierr = ledger_rows(intake_path)
    sites, cerr = code_field_sites(code_path)

    for m in perr:
        fails.append(("PRODUCER-UNREADABLE", "(file)", "producer", m))
    for m in ierr:
        fails.append(("LEDGER-UNREADABLE", "(file)", "declared-consumer", m))
    for m in cerr:
        fails.append(("CODE-UNREADABLE", "(file)", "code", m))

    # --- detector self-check. Runs before anything that trusts it.
    for canary in DETECTOR_CANARIES:
        cells += 1
        if not sites.get(canary):
            fails.append((
                "DETECTOR-BLIND", canary, "code",
                "the AST detector found ZERO ticket reads of `%s`, which "
                "scripts/swarmloop.py is known to read. The receiver test "
                "(names %s, or a subscript/.get on `%s`) or the accessor name "
                "`_graph_strs` has been renamed out from under this test. Every "
                "PHANTOM-CONSUMER verdict below is untrustworthy until this is "
                "fixed." % (canary, "/".join(TICKET_NAMES), TICKET_INDEX)))

    # --- producer -> ledger. A field one side emits that NO leg declares.
    for f in sorted(fields):
        cells += 1
        if f not in rows:
            fails.append((
                "UNDECLARED", f, "producer -> declared-consumer",
                "templates.md emits `%s` and the ticket-field consumer ledger in "
                "build-docs-intake.md does not declare it. Nobody has said "
                "whether it is read by code, mapped into the packet as prose, or "
                "recorded and ignored — so its silence is an accident, not a "
                "decision. Add a row." % f))

    # --- ledger -> producer, and ledger -> code, per class.
    for f in sorted(rows):
        r = rows[f]
        cls = r["consumer"]
        seen = sites.get(f) or []
        where = ", ".join("line %d %s" % (ln, form) for ln, form in sorted(seen)[:4])

        cells += 1
        if cls == "loop-minted":
            if f in fields:
                fails.append((
                    "MINTED-IS-PRODUCED", f, "declared-consumer -> producer",
                    "the ledger calls `%s` loop-minted (a field swarm-loop puts "
                    "on its OWN tickets), but templates.md emits it — so it is a "
                    "build-docs field and must be classified as one." % f))
        elif f not in fields:
            fails.append((
                "NO-PRODUCER", f, "declared-consumer -> producer",
                "the ledger declares `%s` as %s, but templates.md's ticket "
                "contract does not emit it. Either the producer dropped the "
                "field (and every consumer of it is now reading nothing), or "
                "the ledger row is fiction." % (f, cls)))

        cells += 1
        if cls in CODE_BACKED:
            if not seen:
                fails.append((
                    "PHANTOM-CONSUMER", f, "declared-consumer -> code",
                    "the ledger declares `%s` as %s, and scripts/swarmloop.py "
                    "reads it off a ticket ZERO times. A documented consumer "
                    "that does not exist is worse than an undocumented one: it "
                    "is why build-docs keeps emitting the field.%s"
                    % (f, cls,
                       ("\n              ledger note: " + r["absence"])
                       if r["absence"] else "")))
        elif cls == "packet-prose":
            if seen:
                fails.append((
                    "STALE-PROSE", f, "declared-consumer -> code",
                    "the ledger declares `%s` packet-prose (no code path, a "
                    "human/agent reads it out of the task packet), but "
                    "scripts/swarmloop.py now reads it off a ticket at %s. The "
                    "declaration is stale — reclassify it, and say what happens "
                    "when it is missing." % (f, where)))

        cells += 1
        if cls in ("code", "advisory-recorded"):
            note = re.sub(r"[\s\-—–]+", "", r["absence"])
            if len(note) < 12 or note.lower() in ("na", "none", "tbd"):
                fails.append((
                    "NO-ABSENCE-NOTE", f, "declared-consumer",
                    "`%s` is declared %s but its 'On absence' cell is %r. A "
                    "code-consumed field whose absence is undocumented is the "
                    "shape of the failure this whole test exists to catch: it "
                    "does not fail at the boundary, it fails mid-run."
                    % (f, cls, r["absence"])))

    # --- code -> ledger. A field the code reads that no leg declares.
    for f in sorted(sites):
        cells += 1
        if f not in rows:
            fails.append((
                "UNDECLARED-CONSUMER", f, "code -> declared-consumer",
                "scripts/swarmloop.py reads `%s` off a ticket (%s) and no leg "
                "declares it. If build-docs never emits it, this is a consumer "
                "with no producer — the read silently yields nothing on every "
                "real doc set." % (f, where_of(sites, f))))

    # --- producer-internal: the markdown form and the JSON form must agree.
    for lab in sorted(md_labels):
        cells += 1
        if lab not in fields:
            fails.append((
                "PRODUCER-INTERNAL", lab, "producer",
                "templates.md's markdown ticket form carries a **%s** line that "
                "maps to no key in its own JSON form. Half the contract was "
                "updated." % lab.replace("_", " ").title()))

    return cells, fails


def where_of(sites, f):
    return ", ".join("line %d %s" % (ln, form) for ln, form in sorted(sites[f])[:4])


# ---------------------------------------------------------------------------
# META — mutation battery. Does this test actually detect anything?
# ---------------------------------------------------------------------------

def _mut_producer_drop(prod, intake, code):
    """Producer stops emitting `reads`."""
    s = open(prod).read()
    s = s.replace('  "reads": ["src/contracts/session.py"],\n', "")
    s = re.sub(r"^- `reads` —.*(?:\n  .*)*\n", "", s, flags=re.M)
    open(prod, "w").write(s)


def _mut_producer_add(prod, intake, code):
    """Producer starts emitting a field nobody declared."""
    s = open(prod).read()
    s = s.replace('  "bootstrap": false\n', '  "bootstrap": false,\n  "cost_estimate": 3\n')
    open(prod, "w").write(s)


def _mut_ledger_drop(prod, intake, code):
    """The ledger loses the row for a field the producer still emits."""
    s = open(intake).read()
    s = re.sub(r"^\| `scope` \|.*\n", "", s, flags=re.M)
    open(intake, "w").write(s)


def _mut_code_break(prod, intake, code):
    """A declared code consumer is renamed away."""
    s = open(code).read()
    s = s.replace('.get("verify")', '.get("verify_cmd")')
    open(code, "w").write(s)


def _mut_prose_goes_code(prod, intake, code):
    """Code starts reading a field the ledger calls packet-prose."""
    s = open(code).read()
    s += ('\n\ndef _mutation_probe(t):\n'
          '    return t.get("objective")\n')
    open(code, "w").write(s)


def _mut_fp_handled(prod, intake, code):
    """A quoted field name that is NOT a ticket read — the naive-regex trap.

    `os.path.join(d, "provides")` is a path segment. A regex detector would
    report `provides` as code-consumed off the back of it. The AST detector
    must not."""
    s = open(code).read()
    s += ('\n\ndef _mutation_probe_path(d):\n'
          '    return os.path.join(d, "provides")\n')
    open(code, "w").write(s)


def _mut_fp_limit(prod, intake, code):
    """THE DOCUMENTED LIMIT. `t` bound to something that is not a ticket.

    The receiver test is a name test. A non-ticket dict iterated as `t` is
    indistinguishable from a ticket, and the detector WILL report its keys as
    ticket reads. This mutation asserts the WRONG answer on purpose: if the
    detector is ever strengthened (a real type/flow analysis, or narrowing the
    receiver to the graph-loading functions), this cell goes red and the limit
    gets re-documented deliberately instead of drifting."""
    s = open(code).read()
    s += ('\n\ndef _mutation_probe_notaticket(metrics):\n'
          '    for t in metrics:\n'
          '        if t.get("non_goals"):\n'
          '            return t\n'
          '    return None\n')
    open(code, "w").write(s)


# (name, mutate_fn, [(code, field), ...] that must appear ON TOP of baseline,
#  [(code, field), ...] that must NOT appear)
MUTATIONS = [
    ("meta-producer-drop-field", _mut_producer_drop,
     [("NO-PRODUCER", "reads")], []),
    ("meta-producer-add-undeclared", _mut_producer_add,
     [("UNDECLARED", "cost_estimate")], []),
    ("meta-ledger-drop-row", _mut_ledger_drop,
     [("UNDECLARED", "scope")], []),
    ("meta-code-break-consumer", _mut_code_break,
     [("PHANTOM-CONSUMER", "verify"),
      ("UNDECLARED-CONSUMER", "verify_cmd")], []),
    ("meta-prose-becomes-code", _mut_prose_goes_code,
     [("STALE-PROSE", "objective")], []),
    ("meta-fp-trap-handled", _mut_fp_handled,
     [], [("STALE-PROSE", "provides"), ("UNDECLARED-CONSUMER", "provides")]),
    ("meta-fp-limit-unhandled", _mut_fp_limit,
     [("STALE-PROSE", "non_goals")], []),
]


def run_mutations(verbose):
    """(cells, failures) — one cell per expected/forbidden signature."""
    cells, fails = 0, []
    baseline = {(c, f) for c, f, _leg, _m in run_checks(PRODUCER, INTAKE, CODE)[1]}

    root = tempfile.mkdtemp(prefix="swarmloop-contract-meta-")
    try:
        for name, fn, want, forbid in MUTATIONS:
            work = os.path.join(root, name)
            os.makedirs(work)
            p = os.path.join(work, "templates.md")
            i = os.path.join(work, "build-docs-intake.md")
            c = os.path.join(work, "swarmloop.py")
            for src, dst in ((PRODUCER, p), (INTAKE, i), (CODE, c)):
                shutil.copyfile(src, dst)
            before = [open(x, "rb").read() for x in (p, i, c)]
            fn(p, i, c)
            after = [open(x, "rb").read() for x in (p, i, c)]

            # A mutation that no longer applies is the worst outcome available:
            # it silently degrades into "the unmutated files still pass", and
            # every cell under it becomes a lie. Several of these anchor on
            # literal text in files other agents edit, so this is checked, not
            # assumed.
            cells += 1
            if before == after:
                fails.append((
                    "META", name, name,
                    "mutation %r changed NOTHING — the text it anchors on is gone "
                    "from the copied files, so the cells below it were measuring "
                    "the unmutated contract. Re-aim the mutation at text that "
                    "exists; do not delete it." % name))
                continue
            if verbose:
                print("  ok    %-30s mutation applied" % name)

            got = {(cc, ff) for cc, ff, _l, _m in run_checks(p, i, c)[1]}
            new = got - baseline
            for sig in want:
                cells += 1
                if sig in new:
                    if verbose:
                        print("  ok    %-30s detected %s %s" % (name, sig[0], sig[1]))
                else:
                    fails.append((
                        "META", sig[1], name,
                        "mutation %r did not produce a NEW %s failure for `%s`. "
                        "This test cannot detect the very drift it claims to. "
                        "New signatures it did produce: %s"
                        % (name, sig[0], sig[1],
                           ", ".join("%s/%s" % s for s in sorted(new)) or "(none)")))
            for sig in forbid:
                cells += 1
                if sig not in new:
                    if verbose:
                        print("  ok    %-30s correctly ignored %s %s"
                              % (name, sig[0], sig[1]))
                else:
                    fails.append((
                        "META", sig[1], name,
                        "mutation %r WAS mistaken for a ticket read: the "
                        "detector reported %s for `%s` off a string that is not "
                        "a field access at all. The AST pass has regressed to "
                        "regex behaviour." % (name, sig[0], sig[1])))
    finally:
        if not os.environ.get("SWARMLOOP_TESTS_KEEP"):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("  (kept: %s)" % root)
    return cells, fails


# ---------------------------------------------------------------------------
# PINNED: the AST pass is not the regex, measured on the REAL file
# ---------------------------------------------------------------------------

# (field, min gap, exact ticket-read sites or None, what the extra hits are)
# These are the naive-detector traps that actually exist in swarmloop.py at
# HEAD. If one of them disappears, this pin must be RE-AIMED at another trap,
# not deleted — deleting it removes the only proof the detector earns its cost.
#
# What is pinned is the GAP (regex hits minus real ticket reads), not the raw
# counts, because scripts/swarmloop.py is a moving file and pinning absolutes
# is how a test goes stale over a change it should not care about. Measured:
# a merge that added the `push-gate` and `conflicts` subcommands took the naive
# count for `verify` from 5 to 8 while the AST pass held at 2 — the gap grew,
# which is the claim, and an absolute pin would have gone red over nothing.
# `acceptance` keeps an exact 0 because "the AST pass reads it ZERO times" IS
# the load-bearing half of that case.
REGEX_TRAPS = [
    ("acceptance", 2, 0,
     'os.path.join(d, "acceptance") — a DIRECTORY name, twice'),
    ("reads", 4, None,
     'res["reads"] (the reads-scanner result dict) and the "reads" subcommand'),
    ("verify", 2, None,
     'the "verify" argparse subcommand and a minted-ticket dict literal'),
]


def run_pins(verbose):
    cells, fails = 0, []
    sites, _ = code_field_sites(CODE)
    for field, min_gap, want_sites, why in REGEX_TRAPS:
        got_regex = naive_regex_count(CODE, field)
        got_sites = len(sites.get(field) or [])
        gap = got_regex - got_sites
        ok = gap >= min_gap
        if want_sites is None:
            ok = ok and got_sites >= 1
        else:
            ok = ok and got_sites == want_sites
        cells += 1
        if ok:
            if verbose:
                print("  ok    %-30s regex=%d ast=%d gap=%d (%s)"
                      % ("pin-regex-trap-" + field, got_regex, got_sites, gap, why))
            continue
        fails.append((
            "PIN", field, "code-detector",
            "the pinned regex trap for `%s` no longer holds: a naive quoted-name "
            "scan finds %d occurrence(s), the AST pass finds %d ticket read(s), so "
            "the over-count gap is %d (pinned >= %d, sites pinned %s). Expected "
            "extras: %s.\n"
            "              If swarmloop.py legitimately changed, RE-AIM this pin "
            "at another trap; do not delete it — it is the only proof the AST "
            "pass answers a different question than the regex."
            % (field, got_regex, got_sites, gap, min_gap,
               want_sites if want_sites is not None else ">= 1", why)))
    return cells, fails


# ---------------------------------------------------------------------------

def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    missing = [p for p in (PRODUCER, INTAKE, CODE) if not os.path.isfile(p)]
    if missing:
        for p in missing:
            print("TEST_CONTRACT: MISSING leg: %s" % p)
        print("TEST_CONTRACT: PASS=0 FAIL=%d" % len(missing))
        return 1

    print("three-way ticket-contract check")
    print("  producer          : %s" % PRODUCER)
    print("  declared consumer : %s" % INTAKE)
    print("  code              : %s" % CODE)
    print("")

    cells, fails = run_checks(PRODUCER, INTAKE, CODE)
    pc, pf = run_pins(verbose)
    mc, mf = run_mutations(verbose)
    cells += pc + mc
    fails += pf + mf

    if verbose:
        rows, _ = ledger_rows(INTAKE)
        sites, _ = code_field_sites(CODE)
        for f in sorted(rows):
            print("  ledger  %-16s %-18s %d code site(s)"
                  % (f, rows[f]["consumer"], len(sites.get(f) or [])))

    for code, field, leg, msg in fails:
        print("")
        print("FAIL [%s]  `%s`   (leg: %s)" % (code, field, leg))
        for line in msg.splitlines():
            print("  %s" % line.strip() if not line.startswith("  ") else line)

    fields, _labels, _e = producer_fields(PRODUCER)
    rows, _e2 = ledger_rows(INTAKE)
    sites, _e3 = code_field_sites(CODE)
    by_class = {}
    for r in rows.values():
        by_class[r["consumer"]] = by_class.get(r["consumer"], 0) + 1
    print("")
    print("  producer fields: %d   ledger rows: %d   ticket fields read by code: %d"
          % (len(fields), len(rows), len(sites)))
    print("  ledger by class: " + ("  ".join(
        "%s=%d" % (k, by_class[k]) for k in sorted(by_class)) or "(none)"))
    print("  cells run: contract=%d  pins=%d  mutations=%d" % (cells - pc - mc, pc, mc))
    print("TEST_CONTRACT: PASS=%d FAIL=%d" % (cells - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
