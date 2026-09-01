# swarm-loop regression suite

Everything the skill promises, asserted against the **installed** skill.

```sh
./tests/run.sh
```

Exit `0` = all green. Anything else = something regressed. That exit code is the
whole contract; run it before any edit to `SKILL.md`, `scripts/` or `hooks/`.

```sh
./tests/run.sh --guard      # just the git-guard battery
./tests/run.sh --harness    # just the swarmloop.py end-to-end suite
./tests/run.sh -v           # list every passing guard case, not just failures
```

Takes well under a minute. It is meant to be run casually, on a whim, before you
touch anything.

---

## What is here

| file | covers |
| --- | --- |
| `run.sh` | the entry point: runs both suites, prints a summary, owns the exit code |
| `test_git_guard.py` | `../hooks/git-guard.py` — 316 command strings (138 BLOCK, 178 ALLOW), each asserted against exit 2 / exit 0 |
| `test_harness.sh` | `../scripts/swarmloop.py` — 174 assertions, end-to-end runs of the frozen metric harness against throwaway repos |

490 assertions, ~35 seconds, exit 0.

Both suites are addressed **relative to this directory** (`../hooks/`,
`../scripts/`), never by absolute path, so the suite keeps working after the
skill is moved, cloned, or installed under a different home.

Pure stdlib Python 3.9 + bash (portable to the bash 3.2 that ships with macOS).
No pytest, no pip, no network. It runs on a bare machine.

Each suite builds its own throwaway git repos under a temp directory it creates
and deletes, sets `user.email` / `user.name` **locally** in each, and neutralizes
global/system git config so a developer's own `~/.gitconfig` cannot change a
verdict. Nothing outside that temp directory is ever written or deleted. Set
`SWARMLOOP_TESTS_KEEP=1` to keep the temp dirs for debugging.

---

## `test_git_guard.py`

The guard is a `PreToolUse` Bash hook: it reads a tool-call JSON payload on
stdin and exits `2` to block or `0` to allow. Every case is
`(name, command, expected_exit)`.

**BLOCK cases** — the destructive repo-global ops, reached however the shell
dresses them up: on a later line of a multi-line command, after a shell keyword,
across a backslash continuation, behind a wrapper (`timeout`, `nice`, `xargs`,
`sudo`, `env`, `command -p`), inside `bash -c` / `eval` up to three levels deep,
through a **git alias** (`git co .`, `git -c alias.w='!git stash' w`), via
`$GIT stash`, or through the plumbing equivalents (`checkout-index -f -a`,
`read-tree -u --reset`, `apply -R`, repo-wide `git rm`). Long options are
matched by git's own abbreviation rule, so `git clean --for` and `git reset --ha`
are here too. Includes the arithmetic-shift regression — `echo $((1<<2))` must
not be mistaken for a heredoc that swallows the `git stash` on the next line.

**ALLOW cases** — the load-bearing half. Per the guard's own docstring a false
block is *worse* than a missed block: the skill mandates writing task packets by
heredoc, and packet prose quotes `git stash`, `git reset --hard` and
`| 1 | git checkout . was run; ban it |` verbatim. False-blocking that wedges an
entire overnight run. So the allow half covers heredoc prose in every form
(`<<'EOF'`, `<<EOF`, `<<-EOF`, `<<"EOF"`, unterminated, nested in `bash -c`),
commit messages quoting banned ops, the loop's own worktree teardown and merge
protocol, index-only `git restore --stag`, `git clean --dry -d`, harmless
aliases, wrappers around non-git programs, and the documented fail-open on
unparseable input.

Deliberately **absent**: the cases the guard documents as out of scope — a
heredoc body piped *into a shell*, `eval "$cmd"` where the payload only exists at
run time, `python3 -c 'os.system("git stash")'`, a destructive op inside a script
file. Asserting those would encode a promise the guard never made.

## `test_harness.sh`

Drives `swarmloop.py` end to end against throwaway repos carrying a frozen
two-metric harness. Covers: `analyze` refusing to fabricate a verdict from stale
rows; a genuinely all-green cycle still exiting 2; `nan`/`inf` rejected at both
freeze and measure; `check-branch` vetoing a frozen test smuggled out by
`git mv` or `git rm` while leaving a legitimate `src/` rename and a stray
`__pycache__` alone; the re-freeze amendment gate and its `freeze-log.jsonl`;
`init --force`; `restore-harness`; `record`'s MANUAL flag and
`--trust-manual-records`; `checkpoint` idempotence per cycle; the no-progress
kill switch; escalations (inert, blocking, outranking every other terminal
condition in `resume`); and backward compatibility with a run that has no
`escalations.jsonl` at all.

---

## Adding a case

**Guard.** Append a `(name, command, expected)` tuple to `MUST_BLOCK` or
`MUST_ALLOW` in `test_git_guard.py`. Names must be unique — the suite refuses to
run on a duplicate. If the case needs a file, a branch or an alias to exist for
git to answer "ref or path?", add it in `build_repo()`; the guard resolves those
questions against the cwd, so a case whose fixture is missing fails open and
proves nothing.

**Harness.** Add a block to `test_harness.sh` using the existing `mk_repo` helper
and the `ck` / `ckgrep` / `cknot` assertion helpers. Every assertion must name
itself and print expected-vs-actual on failure.

## When a case fails

A red line is a **finding about the skill**, not a licence to edit the test.
Read the FAIL block — it prints the case name, what was expected, what happened,
and the literal command. Fix the thing under test, or, if the expectation is
genuinely wrong, say so out loud and change it deliberately.

Neither suite is vacuously green — both were checked by sabotage, not by
inspection:

- Guard, pointed at a stubbed **always-allow** hook: all 138 block cases fail.
  Pointed at an **always-block** hook: all 178 allow cases fail.
- Harness, run against a copy of `swarmloop.py` with
  `stale_metrics` forced empty (the exact defect fixed in the analyze path):
  the three `(a)` stale-verdict assertions go red and nothing else does.
  With `all_done` widened to ignore manual rows: the three `(k)` assertions go
  red. With `escalate` made to write `state.json`: exactly one assertion goes
  red, and it names the file that moved.

Both suites were also run from a copy of the whole skill relocated to an
unrelated directory, from `cwd=/`, and stayed green — the relative-path
resolution is verified by relocation, not by reading the code.
