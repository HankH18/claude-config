#!/usr/bin/env python3
"""Regression battery for swarm-loop's PreToolUse delegation guard.

Runs ../hooks/delegation-guard.py against SEQUENCES of PreToolUse payloads and
asserts the exit code and the message text its contract promises:

    exit 2 = DENIED    exit 0 = ALLOWED (silently, or with a reminder on stdout)

The unit here is a SEQUENCE, not a single command, because the thing under test
is a counter: "the 5th consecutive mutating call reminds" is unstateable about
one payload. Every sequence gets its own session id and its own state
directory, so sequences are independent and can run concurrently while the
steps inside one run strictly in order.

Three rules govern what belongs here, taken from the guard's own docstring:

  1. A missed count is cheap; a FALSE COUNT IS THE FAILURE MODE THAT MATTERS.
     A guard that fires on correct orchestration gets switched off, and a
     switched-off guard enforces nothing. So the NEVER-COUNTS half of this
     battery is the load-bearing half, and the single most important case in
     the file is `phase7-evaluation-sequence`: `verify`, `measure`, `analyze`,
     `checkpoint` run one at a time, in a row, is the orchestrator's own work
     and must not trip anything.
  2. Every fault allows. There is no fail-closed rule in the guard, so every
     fault case below asserts exit 0 — an unparseable payload, a corrupt
     counter, a state directory that is a file, a directory with no write
     permission, an unbalanced quote, a tool with no rules.
  3. Thresholds are UNCALIBRATED (5 / 8). They are asserted here as the
     DEFAULTS and as OVERRIDABLE, never as correct values.

Self-contained: builds one throwaway git repo, one linked worktree, one repo
with a merge in progress, and a directory outside any repo, under a temp dir it
creates and removes. Global/system git config is neutralized so a developer's
own ~/.gitconfig cannot change a verdict.

Usage:   python3 test_delegation_guard.py [-v]
Exit:    0 = every cell matched, 1 = at least one regression.
"""

import concurrent.futures
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, os.pardir, "hooks", "delegation-guard.py")

DENY = 2
ALLOW = 0

# Fragments the guard's own messages are built from. Kept as constants so a
# reworded message is one obvious failure rather than forty mysterious ones.
REMINDER = "CONSECUTIVE repo-mutating"
BLOCKED = "BLOCKED (swarm-loop delegation-guard)"

# One token past the guard's MAX_WORD_CHARS — the elision path.
BLOB = "A" * 20001


# ---------------------------------------------------------------------------
# Step constructors. A step is a dict; the tables below read as prose.
# ---------------------------------------------------------------------------

def _step(tool, want=ALLOW, must=(), must_not=None, **kw):
    """The DEFAULT assertion on every step is "this did not remind" — silence is
    the interesting property and it has to be asserted, or a guard that counts
    everything would pass most of this file. A step that explicitly expects a
    fragment drops it from the default denial rather than contradicting
    itself."""
    must = list(must)
    if must_not is None:
        # A DENY carries the same "N CONSECUTIVE repo-mutating" phrasing as the
        # reminder, so the default silence assertion only applies to an ALLOW.
        must_not = [] if want == DENY else [f for f in (REMINDER,) if f not in must]
    else:
        must_not = [f for f in must_not if f not in must]
    s = {"tool": tool, "want": want, "must": must, "must_not": must_not}
    s.update(kw)
    return s


def B(cmd, want=ALLOW, must=(), must_not=None, cwd="{REPO}"):
    """A Bash call."""
    return _step("Bash", want, must, must_not, cmd=cmd, cwd=cwd)


def E(path, want=ALLOW, must=(), must_not=None, cwd="{REPO}", tool="Edit"):
    """An Edit/Write/MultiEdit call (file_path), or NotebookEdit if named."""
    return _step(tool, want, must, must_not, path=path, cwd=cwd)


def N(path, want=ALLOW, must=(), must_not=None, cwd="{REPO}"):
    """A NotebookEdit call — the one file tool keyed on `notebook_path`."""
    return _step("NotebookEdit", want, must, must_not, npath=path, cwd=cwd)


def AGENT(tool="Agent", cwd="{REPO}"):
    """A delegation. The whole point of the counter is that this clears it."""
    return _step(tool, ALLOW, (), None, cwd=cwd)


def RAW(stdin, want=ALLOW, must=(), must_not=None):
    """A literal stdin string — for payloads that are not objects at all."""
    return _step(None, want, must, must_not, raw=stdin)


def PAYLOAD(obj, want=ALLOW, must=(), must_not=None):
    """A payload dict used verbatim (missing/ill-typed fields)."""
    return _step(None, want, must, must_not, payload=obj)


def rep(n, fn, *a, **kw):
    return [fn(*a, **kw) for _ in range(n)]


def edits(n, prefix="{REPO}/src/f", **kw):
    """n edits with no expectation beyond "did not remind" — only correct while
    the streak stays under the reminder threshold. Use `streak` past that."""
    return [E("%s%d.py" % (prefix, i), **kw) for i in range(n)]


def streak(n, prefix="{REPO}/src/f", start=1, remind=5, deny=8, **kw):
    """n consecutive edits whose expectation is DERIVED from the thresholds
    rather than restated by hand: silent below `remind`, reminding from
    `remind`, denied from `deny`. Writing those out by hand is how a table ends
    up asserting that the 6th call is silent."""
    out = []
    for i in range(n):
        c = start + i
        path = "%s%d.py" % (prefix, i)
        if c >= deny:
            out.append(E(path, want=DENY, must=[BLOCKED, "%d CONSECUTIVE" % c],
                         **kw))
        elif c >= remind:
            out.append(E(path, must=[REMINDER, "%d CONSECUTIVE" % c],
                         must_not=[BLOCKED], **kw))
        else:
            out.append(E(path, **kw))
    return out


# ---------------------------------------------------------------------------
# The read-only corpus. Named explicitly rather than left implicit in the
# absence of a rule: the guard pins a READ_ONLY_PROGRAMS set and this is what
# makes that set load-bearing instead of decorative.
# ---------------------------------------------------------------------------

READ_ONLY_COMMANDS = [
    "git log --oneline -20",
    "git status --porcelain",
    "git diff main...HEAD",
    "git rev-parse HEAD",
    "git show HEAD:src/a.py",
    "git branch -vv",
    "git worktree list",
    "git ls-files src/",
    "ls -la {REPO}/src",
    "cat {REPO}/src/a.py",
    "wc -l {REPO}/src/a.py",
    "grep -rn 'def ' {REPO}/src",
    "find {REPO}/src -name '*.py'",
    "head -50 {REPO}/src/a.py",
    "tail -20 {REPO}/src/a.py",
    "sed -n '1,40p' {REPO}/src/a.py",
    "pytest -q .swarm-loop/acceptance",
    "python3 -m py_compile {REPO}/src/a.py",
    "git diff --stat > /dev/null 2>&1",
    "grep -c 'rm -rf' {REPO}/src/a.py",
]

# The Phase 7 evaluation sequence, verbatim from the loop's own runbook. THIS IS
# THE CASE THAT DECIDES WHETHER THE FEATURE IS WORTH HAVING.
PHASE7 = [
    "python3 {SKILL}/scripts/swarmloop.py verify",
    "python3 {SKILL}/scripts/swarmloop.py measure",
    "python3 {SKILL}/scripts/swarmloop.py analyze",
    "python3 {SKILL}/scripts/swarmloop.py checkpoint",
]

# Collection: references/merge-protocol.md steps 1 and 4 — a check per branch,
# then one merge per branch, verify after each.
COLLECTION = []
for _i in range(9):
    COLLECTION += [
        "python3 {SKILL}/scripts/swarmloop.py check-branch --branch task/T-%03d --base main" % _i,
        "git merge --no-ff task/T-%03d" % _i,
        "pytest -q",
    ]

# Landing + teardown: merge-protocol.md steps 9 and 10.
LANDING = [
    "git checkout main",
    "git merge --ff-only integration/e1-b1",
    "git rebase main integration/e1-b1",
    "git push origin main",
    "git worktree remove {WT}",
    "git worktree prune",
    "git branch -D task/T-000",
]


# ---------------------------------------------------------------------------
# SEQUENCES — (name, env-overrides, steps)
# ---------------------------------------------------------------------------

SEQUENCES = [

    # ---------------------------------------------------------- the counter
    ("count-rises-on-mutating-calls", {},
     edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"],
                   must_not=[BLOCKED])]),

    ("reminder-names-the-count-and-the-deny-threshold", {},
     edits(4) + [E("{REPO}/src/f5.py",
                   must=[REMINDER, "5 CONSECUTIVE", "At 8 this hook refuses",
                         "dispatch a lane with the Agent tool",
                         "resets this counter to zero"],
                   must_not=[BLOCKED])]),

    ("reminder-is-not-a-permission-allow", {},
     # `permissionDecision: "allow"` would bypass the user's own permission
     # rules, which is not this hook's business. git-guard makes the same
     # promise about its own exit 0.
     edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER],
                   must_not=["permissionDecision"])]),

    ("deny-at-8", {},
     streak(7) + [E("{REPO}/src/f8.py", want=DENY,
                    must=[BLOCKED, "8 CONSECUTIVE", "The reminder at 5",
                          "write a task packet"])]),

    ("deny-persists-until-a-dispatch", {},
     streak(7) + [E("{REPO}/src/f8.py", want=DENY, must=[BLOCKED]),
                  E("{REPO}/src/f9.py", want=DENY, must=[BLOCKED, "9 CONSECUTIVE"]),
                  AGENT(),
                  E("{REPO}/src/f10.py")]),

    # ------------------------------------------------------------- the reset
    ("agent-dispatch-resets", {},
     streak(7) + [AGENT("Agent")] + streak(7, prefix="{REPO}/src/g")),

    ("task-dispatch-resets", {},
     streak(7) + [AGENT("Task")] + streak(7, prefix="{REPO}/src/g")),

    ("sendmessage-resets", {},
     streak(7) + [AGENT("SendMessage")] + streak(7, prefix="{REPO}/src/g")),

    ("reset-then-remind-again", {},
     edits(4) + [AGENT()] + edits(4, prefix="{REPO}/src/g")
     + [E("{REPO}/src/g9.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    # ------------------------------------------------- read-only never counts
    ("readonly-never-counts", {},
     [B(c) for c in READ_ONLY_COMMANDS] * 2
     # ...and the counter really is still zero: four edits stay silent, the
     # fifth reminds. Asserting only "no reminder" would pass on a guard that
     # had silently counted to four.
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("read-tool-never-counts", {},
     rep(12, PAYLOAD, {"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}})
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    # ============ HAZARD 1 — the orchestrator's own work must never trip ====
    ("phase7-evaluation-sequence", {},
     [B(c) for c in PHASE7 * 6]
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("swarmloop-output-captured-into-the-repo", {},
     # THE CASE THAT ACTUALLY EXERCISES THE EXEMPTION. Every command above has
     # no mutating shape of its own, so removing the swarmloop rule entirely
     # left the whole Phase 7 battery green (measured, by mutation). These
     # capture the harness's output INTO the repo, so each one is a real write
     # redirect and counts the moment the exemption stops firing.
     [B("python3 {SKILL}/scripts/swarmloop.py analyze > {REPO}/cycle-report.md"),
      B("python3 {SKILL}/scripts/swarmloop.py measure 2>&1 | tee {REPO}/measure.log"),
      B("python3 {SKILL}/scripts/swarmloop.py status > {REPO}/status.txt"),
      B("python3 {SKILL}/scripts/swarmloop.py frontier --json > {REPO}/frontier.json"),
      B("python3 {SKILL}/scripts/swarmloop.py verify > {REPO}/verify.txt && "
        "python3 {SKILL}/scripts/swarmloop.py checkpoint >> {REPO}/verify.txt"),
      B("swarmloop.py reads > {REPO}/reads.txt")] * 3
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("phase7-piped-and-logged", {},
     # The same commands as an orchestrator actually writes them: output
     # captured, stderr folded in, teed to a log. `tee` is a mutating shape and
     # a redirect is a mutating shape; neither may resurrect an exempt call.
     [B("python3 {SKILL}/scripts/swarmloop.py measure 2>&1 | tee /tmp/measure.log"),
      B("python3 {SKILL}/scripts/swarmloop.py analyze > /tmp/analyze.txt"),
      B("python3 {SKILL}/scripts/swarmloop.py status | head -40"),
      B("python3 {SKILL}/scripts/swarmloop.py verify && python3 {SKILL}/scripts/swarmloop.py checkpoint"),
      ] * 4
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("swarmloop-ledger-writes-never-count", {},
     [B("python3 {SKILL}/scripts/swarmloop.py " + sub) for sub in (
         "init --spec SPEC.md", "freeze", "record --metric tests --value 3",
         "findings --file f.json", "dispatch --open T-001 --agent lane-1",
         "dispatch --list", "retire --task T-001", "distil --lesson x",
         "escalate --kind harness --subject x --reason y --proposed z",
         "audit-loop-done", "restore-harness", "red-check --ticket T-001",
         "check-wave --tasks T-001,T-002", "frontier", "resume", "reachable",
         "push-gate", "reads", "conflicts")]
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("collection-nine-branches", {},
     [B(c) for c in COLLECTION]
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("landing-and-teardown", {},
     [B(c) for c in LANDING * 3]
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("env-resync-before-measurement", {},
     # merge-protocol.md step 8 mandates these. A package ADD counts; a
     # manifest SYNC does not.
     [B(c) for c in ("npm ci", "uv sync", "npm install", "yarn install",
                     "pip install -r requirements.txt",
                     "uv pip install -e '.[dev]'", "poetry install",
                     "bundle install", "npm ci && pytest -q")] * 2
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("merge-in-progress-exempts-everything", {},
     # merge-protocol.md step 5: textual conflicts are resolved BY THE
     # ORCHESTRATOR, in the primary checkout. That is `git show :2:<f> > <f>`,
     # `git add`, `git commit` — three counted shapes per conflicted file.
     [B("git show :2:src/a.py > {MERGING}/src/a.py", cwd="{MERGING}"),
      B("git add src/a.py", cwd="{MERGING}"),
      E("{MERGING}/src/a.py", cwd="{MERGING}"),
      B("git commit -m 'resolve'", cwd="{MERGING}")] * 4),

    ("worker-worktree-exempt", {},
     # A lane doing twenty consecutive edits in its own worktree is doing
     # exactly what its packet asked. Denying it would enforce the opposite of
     # this guard's own rule.
     edits(10, prefix="{WT}/src/w", cwd="{WT}")
     + [B("sed -i 's/a/b/' {WT}/src/w1.py", cwd="{WT}"),
        B("git add -A && git commit -m work", cwd="{WT}")] * 5),

    ("ledger-and-settings-writes-exempt", {},
     rep(4, E, "{SWARM}/state.json")
     + rep(4, E, "{SWARM}/acceptance/test_frozen.py")
     + rep(4, E, "{REPO}/.claude/settings.local.json")
     + [B("cat > {SWARM}/worker-preamble.md <<'EOF'\nbe careful\nEOF"),
        B("git add .swarm-loop/state.json"),
        B("sed -n '1,20p' {SWARM}/history.csv")] * 3
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("phase7-ledger-commit-costs-exactly-one", {},
     # HONEST COST, pinned rather than hidden: `git commit` is counted, and
     # Phase 7's ledger commit is a real `git commit`. So the epoch's own
     # bookkeeping spends ONE of the five, and the `git add`/`git push` either
     # side of it spend none. One is affordable; if it ever stops being, this
     # is the case that will say so.
     [B("git add .swarm-loop"),
      B("git commit -m 'epoch 1 evidence'"),
      B("git push origin main"),
      B("python3 {SKILL}/scripts/swarmloop.py checkpoint")]
     + edits(3, prefix="{REPO}/src/h")
     + [E("{REPO}/src/h9.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("outside-the-work-area-exempt", {},
     [B("echo hi > /tmp/scratch-note.txt"),
      B("rm -rf {OUTSIDE}/junk"),
      B("mkdir -p /tmp/swarmloop-probe"),
      # A copy OUT of the repo: the source is in-area and the destination is
      # not, and only the destination is written.
      B("cp {REPO}/src/a.py /tmp/a.py"),
      B("sed -i 's/x/y/' /tmp/a.py"),
      B("truncate -s 0 /tmp/a.py"),
      B("git diff > /dev/null 2>&1"),
      E("{OUTSIDE}/notes.md")] * 3
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    ("heredoc-bodies-are-data", {},
     # The loop MANDATES writing task packets by heredoc, and a packet's prose
     # quotes every shape this guard counts. git-guard drops heredoc bodies for
     # the same reason.
     # NOT comment lines. The first cut of this case wrote the body as `#`
     # comments, which shlex drops on its own, so it passed with heredoc
     # handling deleted (measured, by mutation). Every line below is a real
     # command that WOULD count if the body were tokenized.
     rep(12, B,
         "cat > /tmp/packet-T-001.md <<'PACKET'\n"
         "Fix the parser in src/parser.py.\n"
         "sed -i 's/old/new/' src/parser.py\n"
         "echo done > src/report.txt\n"
         "git commit -am 'T-001'\n"
         "rm -rf src/legacy\n"
         "PACKET")
     + edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),

    # -------------------------------------------- what DOES count, per shape
    ("write-redirect-into-the-repo-counts", {},
     [B("echo x > {REPO}/src/gen1.py"), B("echo x >> {REPO}/src/gen2.py"),
      B("python3 gen.py > {REPO}/src/gen3.py"),
      B("cat {REPO}/src/a.py > {REPO}/src/gen4.py")]
     + [B("git show HEAD:src/a.py > {REPO}/src/gen5.py",
          must=[REMINDER, "5 CONSECUTIVE"])]),

    ("heredoc-write-into-the-repo-counts", {},
     # The BODY is data; the `> path` on the opening line is a real file write.
     rep(4, B, "cat > {REPO}/src/new.py <<'EOF'\nprint(1)\nEOF")
     + [B("cat > {REPO}/src/new5.py <<'EOF'\nprint(1)\nEOF", must=[REMINDER])]),

    ("sed-i-counts-sed-n-does-not", {},
     rep(8, B, "sed -n '1,20p' {REPO}/src/a.py")
     + rep(4, B, "sed -i 's/a/b/' {REPO}/src/a.py")
     + [B("sed -i.bak 's/a/b/' {REPO}/src/a.py", must=[REMINDER])]),

    ("path-mutators-count", {},
     [B("rm {REPO}/src/tmp.py"), B("mv {REPO}/src/a.py {REPO}/src/b.py"),
      B("cp {REPO}/src/a.py {REPO}/src/c.py"), B("mkdir -p {REPO}/src/pkg")]
     + [B("chmod +x {REPO}/src/a.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("tee-and-dd-and-truncate-count", {},
     [B("echo x | tee {REPO}/src/t1.py"), B("dd of={REPO}/src/t2.py if=/dev/zero"),
      B("truncate -s 0 {REPO}/src/t3.py"), B("touch {REPO}/src/t4.py")]
     + [B("echo y | tee -a {REPO}/src/t5.py", must=[REMINDER])]),

    ("git-add-and-commit-count", {},
     [B("git add -A"), B("git commit -m 'wip'"), B("git add src/a.py"),
      B("git commit --amend --no-edit")]
     + [B("git add -A && git commit -m 'more'", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("package-add-counts", {},
     [B("npm install lodash"), B("pip install requests"), B("uv add httpx"),
      B("cargo add serde")]
     + [B("yarn add react", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("file-tools-all-count", {},
     [E("{REPO}/src/a.py", tool="Edit"), E("{REPO}/src/b.py", tool="Write"),
      E("{REPO}/src/c.py", tool="MultiEdit"), N("{REPO}/nb.ipynb")]
     + [E("{REPO}/src/d.py", tool="Write", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("bash-c-payload-is-rescanned", {},
     # `sh -c 'cat > src/x.py'` walks past every rule if the payload is not
     # re-tokenized, exactly as it does in git-guard.
     rep(4, B, "bash -c 'echo x > {REPO}/src/inner.py'")
     + [B("sh -lc 'sed -i s/a/b/ {REPO}/src/a.py'", must=[REMINDER])]),

    ("the-measured-incident", {},
     # The shape that produced this hook: a verifier finding, then twenty
     # consecutive reactive tool calls. Reads are free; the eighth authoring
     # call is refused.
     [B("sed -n '1,120p' {REPO}/src/a.py"),          # read the module   free
      B("sed -n '1,120p' {REPO}/src/b.py"),          # read the other    free
      E("{REPO}/tests/test_repro.py", tool="Write"), # write a repro     1
      E("{REPO}/src/a.py"),                          # patch             2
      E("{REPO}/src/b.py"),                          # patch             3
      B("pytest -q"),                                # run the suite     free
      E("{REPO}/src/a.py"),                          # revert half       4
      E("{REPO}/src/b.py",                           # second approach   5
        must=[REMINDER, "5 CONSECUTIVE"], must_not=[BLOCKED]),
      E("{REPO}/tests/test_reg1.py", tool="Write",   # regression test   6
        must=[REMINDER, "6 CONSECUTIVE"], must_not=[BLOCKED]),
      E("{REPO}/tests/test_reg2.py", tool="Write",   # regression test   7
        must=[REMINDER, "7 CONSECUTIVE"], must_not=[BLOCKED]),
      B("git checkout src/a.py && pytest -q"),       # adversarial revert free
      E("{REPO}/src/a.py", want=DENY,                # lint fix          8
        must=[BLOCKED, "8 CONSECUTIVE"])]),

    # ------------------------------------------------------------ thresholds
    ("thresholds-are-overridable", {"SWARMLOOP_DELEGATION_REMIND": "2",
                                    "SWARMLOOP_DELEGATION_DENY": "3"},
     [E("{REPO}/src/f1.py"),
      E("{REPO}/src/f2.py", must=[REMINDER, "2 CONSECUTIVE", "At 3 this hook refuses"]),
      E("{REPO}/src/f3.py", want=DENY, must=[BLOCKED, "3 CONSECUTIVE"], must_not=[])]),

    ("thresholds-can-be-raised-past-the-defaults",
     {"SWARMLOOP_DELEGATION_REMIND": "20", "SWARMLOOP_DELEGATION_DENY": "40"},
     edits(15)),

    ("malformed-thresholds-fall-back-to-the-defaults",
     {"SWARMLOOP_DELEGATION_REMIND": "not-a-number",
      "SWARMLOOP_DELEGATION_DENY": "0"},
     # A threshold of 0 would deny the first mutating call of every run.
     streak(8)),

    ("guard-can-be-switched-off", {"SWARMLOOP_DELEGATION_GUARD": "off"},
     edits(15)),

    ("guard-off-accepts-zero-and-false", {"SWARMLOOP_DELEGATION_GUARD": "false"},
     edits(12)),

    # ------------------------------------------------------- every fault path
    ("fault-stdin-is-not-json", {}, [RAW("this is not json at all")]),
    ("fault-stdin-is-empty", {}, [RAW("")]),
    ("fault-stdin-is-a-list", {}, [RAW("[1, 2, 3]")]),
    ("fault-stdin-is-a-string", {}, [RAW('"hello"')]),
    ("fault-stdin-is-null", {}, [RAW("null")]),
    ("fault-stdin-is-truncated-json", {}, [RAW('{"tool_name": "Edit", "too')]),

    ("fault-no-session-id", {},
     # A consecutive streak cannot be attributed to nobody, so nothing counts.
     rep(15, PAYLOAD,
         {"tool_name": "Edit", "cwd": "{REPO}",
          "tool_input": {"file_path": "{REPO}/src/a.py"}})),

    ("fault-empty-session-id", {},
     rep(12, PAYLOAD,
         {"session_id": "   ", "tool_name": "Edit", "cwd": "{REPO}",
          "tool_input": {"file_path": "{REPO}/src/a.py"}})),

    ("fault-session-id-is-not-a-string", {},
     rep(12, PAYLOAD,
         {"session_id": 12345, "tool_name": "Edit", "cwd": "{REPO}",
          "tool_input": {"file_path": "{REPO}/src/a.py"}})),

    ("fault-session-id-path-traversal", {},
     # A session id is payload data. Unfiltered into os.path.join it is a
     # path-traversal write; sanitized it just makes an oddly-named counter.
     rep(4, PAYLOAD,
         {"session_id": "../../../pwned", "tool_name": "Edit", "cwd": "{REPO}",
          "tool_input": {"file_path": "{REPO}/src/a.py"}})),

    ("fault-no-tool-input", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Bash", "cwd": "{REPO}"})),

    ("fault-tool-input-not-an-object", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Bash", "cwd": "{REPO}",
                       "tool_input": "rm -rf {REPO}/src"})),

    ("fault-command-not-a-string", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Bash", "cwd": "{REPO}",
                       "tool_input": {"command": 12345}})),

    ("fault-empty-command", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Bash", "cwd": "{REPO}",
                       "tool_input": {"command": "   "}})),

    ("fault-file-path-missing", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Edit", "cwd": "{REPO}",
                       "tool_input": {"old_string": "a", "new_string": "b"}})),

    ("fault-file-path-not-a-string", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "Write", "cwd": "{REPO}",
                       "tool_input": {"file_path": ["{REPO}/src/a.py"]}})),

    ("fault-unknown-tool", {},
     rep(12, PAYLOAD, {"session_id": "s", "tool_name": "WebFetch", "cwd": "{REPO}",
                       "tool_input": {"url": "https://example.com"}})),

    ("no-cwd-falls-back-to-the-process-cwd", {},
     # Not a fault: a payload with no `cwd` is answered from the hook process's
     # own directory, which in a real session IS the repo. The runner starts it
     # in a non-repo directory, so the work root is that directory and a
     # relative `src/a.py` still resolves in-area and still counts.
     rep(4, PAYLOAD, {"session_id": "s", "tool_name": "Edit",
                      "tool_input": {"file_path": "src/a.py"}})
     + [PAYLOAD({"session_id": "s", "tool_name": "Edit",
                 "tool_input": {"file_path": "src/a.py"}},
                must=[REMINDER, "5 CONSECUTIVE"])]),

    ("fault-cwd-does-not-exist", {},
     rep(4, PAYLOAD, {"session_id": "s", "tool_name": "Bash",
                      "cwd": "{OUTSIDE}/gone/missing",
                      "tool_input": {"command": "echo x > out.txt"}})),

    ("fault-unbalanced-quote", {},
     # The tokenizer raises in both posix and non-posix mode; the guard treats
     # that as not-counted rather than as a mutation it could not classify.
     rep(12, B, 'echo "unterminated > {REPO}/src/x.py')),

    ("fault-oversized-token", {},
     rep(12, B, "echo " + BLOB + " > /tmp/blob.txt")),

    ("fault-oversized-token-does-not-disarm-the-rest", {},
     # git-guard measured that BAILING on an oversized command disarms every
     # segment of it. Elision must leave the real write visible.
     rep(4, B, "echo " + BLOB + "\necho x > {REPO}/src/blob.py")
     + [B("echo " + BLOB + "\necho x > {REPO}/src/blob5.py", must=[REMINDER])]),

    ("fault-state-dir-is-a-regular-file",
     {"SWARMLOOP_DELEGATION_STATE_DIR": "{STATEFILE}"}, edits(15)),

    ("fault-state-dir-is-unwritable",
     {"SWARMLOOP_DELEGATION_STATE_DIR": "{ROSTATE}"}, edits(15)),

    ("fault-corrupt-counter-file", {},
     # Pre-seeded with garbage by the runner (see PRESEED). A corrupt counter
     # reads as zero — never as a refusal, and never as a number it invented.
     edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("fault-negative-counter-file", {},
     edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER, "5 CONSECUTIVE"])]),

    ("fault-counter-file-is-a-directory", {}, edits(15)),

    ("default-state-location-is-outside-the-repo", {},
     # STATE_DIR unset (the runner drops it for this one) and HOME pointed at a
     # temp dir: the counter must land under $HOME/.claude, never in the repo.
     edits(4) + [E("{REPO}/src/f5.py", must=[REMINDER])]),
]

# Sequences whose counter file the runner pre-seeds before step 1.
PRESEED = {
    "fault-corrupt-counter-file": "}{ not json at all",
    "fault-negative-counter-file": '{"count": -17}',
}

# Sequences the runner treats specially.
NO_STATE_DIR_ENV = {"default-state-location-is-outside-the-repo"}
COUNTER_IS_A_DIR = {"fault-counter-file-is-a-directory"}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def git_env(root):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("SWARMLOOP_DELEGATION"):
            env.pop(k)
    env.update({
        "HOME": os.path.join(root, "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.path.join(root, "home", ".gitconfig"),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    })
    return env


def _git(env, *args):
    subprocess.run(["git"] + list(args), capture_output=True, text=True,
                   env=env, check=False)


def build_fixture(root):
    """One repo, one linked worktree of it, one repo mid-merge, one directory
    outside any repository."""
    env = git_env(root)
    repo = os.path.join(root, "repo")
    for d in (os.path.join(repo, "src"), os.path.join(repo, "tests"),
              os.path.join(repo, ".swarm-loop", "acceptance"),
              os.path.join(repo, ".claude"),
              os.path.join(root, "outside"), os.path.join(root, "home")):
        os.makedirs(d, exist_ok=True)
    for f in ("src/a.py", "src/b.py", "src/tmp.py"):
        with open(os.path.join(repo, f), "w") as fh:
            fh.write("def x():\n    return 1\n")
    with open(os.path.join(repo, ".swarm-loop", "state.json"), "w") as fh:
        fh.write("{}\n")
    _git(env, "-C", repo, "init", "-q")
    _git(env, "-C", repo, "add", "-A")
    _git(env, "-C", repo, "commit", "-q", "-m", "base")

    wt = os.path.join(root, "repo-worktrees", "T-010")
    _git(env, "-C", repo, "worktree", "add", "-q", "-b", "task/T-010", wt)
    os.makedirs(os.path.join(wt, "src"), exist_ok=True)

    # A repo with a merge in progress — the marker file is what the guard reads.
    merging = os.path.join(root, "merging")
    os.makedirs(os.path.join(merging, "src"), exist_ok=True)
    with open(os.path.join(merging, "src", "a.py"), "w") as fh:
        fh.write("x = 1\n")
    _git(env, "-C", merging, "init", "-q")
    _git(env, "-C", merging, "add", "-A")
    _git(env, "-C", merging, "commit", "-q", "-m", "base")
    head = subprocess.run(["git", "-C", merging, "rev-parse", "HEAD"],
                          capture_output=True, text=True, env=env).stdout.strip()
    with open(os.path.join(merging, ".git", "MERGE_HEAD"), "w") as fh:
        fh.write(head + "\n")

    # A state directory that is a regular FILE, and one with no write bit.
    statefile = os.path.join(root, "state-is-a-file")
    with open(statefile, "w") as fh:
        fh.write("not a directory\n")
    rostate = os.path.join(root, "state-readonly")
    os.makedirs(rostate, exist_ok=True)
    os.chmod(rostate, stat.S_IRUSR | stat.S_IXUSR)
    return repo


def fill(obj, root):
    if isinstance(obj, str):
        return (obj.replace("{REPO}", os.path.join(root, "repo"))
                   .replace("{WT}", os.path.join(root, "repo-worktrees", "T-010"))
                   .replace("{MERGING}", os.path.join(root, "merging"))
                   .replace("{OUTSIDE}", os.path.join(root, "outside"))
                   .replace("{SWARM}", os.path.join(root, "repo", ".swarm-loop"))
                   .replace("{STATEFILE}", os.path.join(root, "state-is-a-file"))
                   .replace("{ROSTATE}", os.path.join(root, "state-readonly"))
                   .replace("{HOME}", os.path.join(root, "home"))
                   .replace("{SKILL}", os.path.join(root, "skill")))
    if isinstance(obj, dict):
        return dict((k, fill(v, root)) for k, v in obj.items())
    if isinstance(obj, list):
        return [fill(v, root) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

SESSION_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def payload_for(step, sid, root):
    if "payload" in step:
        p = fill(dict(step["payload"]), root)
        # `"s"` is the placeholder meaning "give me this sequence's own id".
        # Substituting EVERY string session id clobbered the two cases whose
        # whole point is a malformed one ("   ", "../../../pwned"), and both
        # then passed for the wrong reason.
        if p.get("session_id") == "s":
            p["session_id"] = sid
        return json.dumps(p)
    p = {"session_id": sid, "hook_event_name": "PreToolUse"}
    if step.get("tool"):
        p["tool_name"] = step["tool"]
    if step.get("cwd"):
        p["cwd"] = fill(step["cwd"], root)
    if "cmd" in step:
        p["tool_input"] = {"command": fill(step["cmd"], root)}
    elif "path" in step:
        p["tool_input"] = {"file_path": fill(step["path"], root)}
    elif "npath" in step:
        p["tool_input"] = {"notebook_path": fill(step["npath"], root)}
    else:
        p["tool_input"] = {}
    return json.dumps(p)


def run_sequence(job):
    name, envover, steps, root, idx = job
    env = git_env(root)
    statedir = os.path.join(root, "state", "seq%03d" % idx)
    if name not in NO_STATE_DIR_ENV:
        env["SWARMLOOP_DELEGATION_STATE_DIR"] = statedir
    env.update(fill(dict(envover), root))
    sid = SESSION_KEY_RE.sub("_", name)[:80]

    seed = PRESEED.get(name)
    if seed is not None:
        os.makedirs(statedir, exist_ok=True)
        with open(os.path.join(statedir, sid + ".json"), "w") as fh:
            fh.write(seed)
    if name in COUNTER_IS_A_DIR:
        os.makedirs(os.path.join(statedir, sid + ".json"), exist_ok=True)

    # The hook process's OWN cwd is deliberately outside the fixture repo, so a
    # payload with no `cwd` cannot accidentally resolve in-area.
    proc_cwd = os.path.join(root, "outside")

    out = []
    for step in steps:
        stdin = step["raw"] if "raw" in step else payload_for(step, sid, root)
        p = subprocess.run([sys.executable, HOOK], input=stdin,
                           capture_output=True, text=True, env=env, cwd=proc_cwd)
        out.append((step, stdin, p.returncode, (p.stdout or "") + (p.stderr or "")))
    return name, out


def post_checks(root):
    """Assertions about the world the hook leaves behind, not about one call.

    Returns (cells_run, problems) so a clean check is a counted PASS rather
    than silence."""
    problems = []
    cells = 0
    home = os.path.join(root, "home")
    default_dir = os.path.join(home, ".claude", "swarm-loop", "delegation")
    cells += 1
    if not os.path.isdir(default_dir):
        problems.append(("default-state-dir-created",
                         "expected the default counter directory at %s" % default_dir))
    else:
        files = [f for f in os.listdir(default_dir) if f.endswith(".json")]
        if not files:
            problems.append(("default-state-dir-holds-a-counter",
                             "no counter file under %s" % default_dir))
    # Nothing may have been written into the measured repo — this skill has a
    # measured incident about files inside the repo being swept into
    # tree-walking metric commands, and a counter that changes on every tool
    # call is the worst possible thing to hand one.
    cells += 1
    repo = os.path.join(root, "repo")
    stray = []
    for dirpath, _dirs, files in os.walk(repo):
        # RELATIVE to the repo: the temp root's own name contains "delegation",
        # and matching on the absolute path flagged the fixture's own
        # `.swarm-loop/state.json` as a stray counter.
        rel = os.path.relpath(dirpath, repo)
        if "delegation" not in rel.split(os.sep):
            continue
        for f in files:
            if f.endswith(".json"):
                stray.append(os.path.join(dirpath, f))
    if stray:
        problems.append(("no-counter-inside-the-repo",
                         "counter state written into the measured repo: %s" % stray))
    # Path traversal: the sanitized key must stay inside the state directory.
    for candidate in (os.path.join(root, "pwned.json"),
                      os.path.join(root, "state", "pwned.json"),
                      os.path.join(os.path.dirname(root), "pwned.json")):
        cells += 1
        if os.path.exists(candidate):
            problems.append(("session-id-traversal-contained",
                             "a payload session id escaped the state dir: %s" % candidate))
    return cells, problems


def static_checks():
    """The matcher constant. `REQUIRED_MATCHER` is the only written statement of
    what the settings entry must say, and the RESET tools are the half that gets
    dropped — without them the guard stops being a delegation clock and becomes
    a blanket cap on mutating calls, which is strictly worse than not installing
    it."""
    problems = []
    cells = 0
    try:
        src = open(HOOK).read()
    except Exception as exc:
        return 1, [("hook-is-readable", "cannot read %s: %s" % (HOOK, exc))]
    cells += 1
    m = re.search(r'^REQUIRED_MATCHER\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        return cells, [("required-matcher-declared",
                        "delegation-guard.py no longer defines REQUIRED_MATCHER")]
    parts = set(m.group(1).split("|"))
    for tool in ("Bash", "Edit", "Write", "NotebookEdit", "MultiEdit",
                 "Task", "Agent", "SendMessage"):
        cells += 1
        if tool not in parts:
            problems.append(("required-matcher-routes-%s" % tool,
                             "REQUIRED_MATCHER is %r and does not route %s"
                             % (m.group(1), tool)))
    for const, want in (("DEFAULT_REMIND", "5"), ("DEFAULT_DENY", "8")):
        cells += 1
        if not re.search(r'^%s\s*=\s*%s\s*$' % (const, want), src, re.M):
            problems.append(("default-%s" % const.lower(),
                             "%s is no longer %s — the battery's thresholds "
                             "assume the documented defaults" % (const, want)))
    # The fail-open wrapper. Nothing this battery can feed the hook from
    # OUTSIDE reaches it — every fault case is caught by an inner handler — so
    # deleting it left the suite green (measured, by mutation). It is the net
    # under every future rule, so it is asserted structurally.
    cells += 1
    if not re.search(r"except SystemExit:\s*\n\s*raise\s*\n\s*except BaseException:\s*\n\s*pass", src):
        problems.append(("fail-open-wrapper-intact",
                         "main() no longer re-raises SystemExit and swallows "
                         "BaseException — a fault can now escape as a nonzero "
                         "exit, which is a DENY"))
    cells += 1
    if re.search(r"^\s*sys\.exit\(2\)", src, re.M) is None:
        problems.append(("deny-path-exists", "no exit-2 path left in the guard"))
    cells += 1
    if len(re.findall(r"^\s*sys\.exit\(2\)", src, re.M)) != 1:
        problems.append(("single-deny-path",
                         "more than one exit-2 site: every refusal must go "
                         "through deny()"))
    cells += 1
    if "UNCALIBRATED" not in src:
        problems.append(("thresholds-declared-uncalibrated",
                         "the docstring no longer says the thresholds are "
                         "uncalibrated; they still are"))
    return cells, problems


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    if not os.path.isfile(HOOK):
        print("TEST_DELEGATION_GUARD: hook not found at %s" % HOOK)
        return 1

    names = [n for n, _e, _s in SEQUENCES]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print("TEST_DELEGATION_GUARD: duplicate sequence names: %s"
              % ", ".join(dupes))
        return 1

    root = tempfile.mkdtemp(prefix="swarmloop-delegation-tests-")
    try:
        os.makedirs(os.path.join(root, "home"), exist_ok=True)
        os.makedirs(os.path.join(root, "skill", "scripts"), exist_ok=True)
        build_fixture(root)

        jobs = [(n, e, s, root, i) for i, (n, e, s) in enumerate(SEQUENCES)]
        workers = min(16, max(4, (os.cpu_count() or 2) * 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_sequence, jobs))

        passed, failures = 0, []
        cells = {"exit": 0, "text": 0, "static": 0, "post": 0}

        for name, out in results:
            for i, (step, stdin, got, text) in enumerate(out):
                want = step["want"]
                cells["exit"] += 1
                if got == want:
                    passed += 1
                    if verbose:
                        print("  ok    %-52s step %2d  exit %d" % (name, i + 1, got))
                else:
                    failures.append((("%s :: step %d" % (name, i + 1)), stdin,
                                     want, got, text, "exit"))
                for frag, want_present in ([(f, True) for f in step["must"]]
                                           + [(f, False) for f in step["must_not"]]):
                    cells["text"] += 1
                    if (frag in text) == want_present:
                        passed += 1
                    else:
                        failures.append((
                            "%s :: step %d must %s %r"
                            % (name, i + 1,
                               "CONTAIN" if want_present else "NOT contain", frag),
                            stdin, want, got, text, "text"))

        for kind, (ran, problems) in (("static", static_checks()),
                                      ("post", post_checks(root))):
            cells[kind] += ran
            passed += ran - len(problems)
            for cname, problem in problems:
                failures.append((cname, "(%s check)" % kind, 0, 0, problem, kind))

        label = {DENY: "DENY (exit 2)", ALLOW: "ALLOW (exit 0)"}
        for name, stdin, want, got, text, kind in failures:
            if kind == "exit":
                head = "FALSE COUNT" if want == ALLOW else "MISSED COUNT"
            elif kind == "text":
                head = "WRONG MESSAGE TEXT"
            else:
                head = kind.upper() + " CHECK"
            print("")
            print("FAIL [%s]  %s   (axis: %s)" % (head, name, kind))
            if kind == "exit":
                print("  expected : %s" % label[want])
                print("  actual   : exit %d" % got)
            print("  payload  : %s" % (stdin[:400]))
            first = (text or "").strip().splitlines()
            if first:
                print("  guard    : %s" % first[0][:200])

        total_steps = sum(len(s) for _n, _e, s in SEQUENCES)
        print("")
        print("  declared: %d sequences  %d steps" % (len(SEQUENCES), total_steps))
        print("  cells run: " + "  ".join("%s=%d" % (k, cells[k])
                                          for k in sorted(cells)))
        print("TEST_DELEGATION_GUARD: PASS=%d FAIL=%d" % (passed, len(failures)))
        return 1 if failures else 0
    finally:
        ro = os.path.join(root, "state-readonly")
        try:
            if os.path.isdir(ro):
                os.chmod(ro, stat.S_IRWXU)
        except Exception:
            pass
        if not os.environ.get("SWARMLOOP_TESTS_KEEP"):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("  (kept: %s)" % root)


if __name__ == "__main__":
    sys.exit(main())
