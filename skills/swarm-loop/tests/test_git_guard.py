#!/usr/bin/env python3
"""Regression battery for swarm-loop's PreToolUse git guard.

Runs ../hooks/git-guard.py against a merged corpus of command strings and
asserts the exit code the guard's own contract promises:

    exit 2 = BLOCKED   exit 0 = ALLOWED

Every case is a (name, command, expected_exit) triple. On failure the case
name, the expected code, the actual code, the literal command and the guard's
stderr are printed, so a red line is debuggable without re-deriving anything.

Two rules govern what belongs here, both taken from the guard's own docstring:

  1. A missed block is bad; a FALSE BLOCK IS WORSE. The loop mandates writing
     task packets by heredoc whose prose quotes `git stash` and
     `git reset --hard`; false-blocking that wedges an entire run. So the ALLOW
     half of this battery is the load-bearing half.
  2. Coverage is bounded by what the tokenizer can see as argv. Cases the guard
     documents as OUT OF SCOPE (a heredoc body piped into a shell, `eval "$cmd"`
     where the payload only exists at run time, `os.system(...)` inside a
     -c program) are deliberately absent — asserting them would encode a
     promise the guard never made.

Self-contained: builds one throwaway git repo under a temp dir it creates and
removes. Sets git identity and aliases LOCALLY; neutralizes global/system git
config so a developer's own ~/.gitconfig cannot change the verdicts.

Usage:   python3 test_git_guard.py [-v]
Exit:    0 = every case matched, 1 = at least one regression.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, os.pardir, "hooks", "git-guard.py")

BLOCK = 2
ALLOW = 0
SWARM_DIR = ".swarm-loop"   # must match hooks/git-guard.py

# One token past the guard's MAX_WORD_CHARS. The guard used to bail out of the
# WHOLE command above that limit, so a base64 blob or a minified-JSON paste
# anywhere in a script disarmed every segment of it, across lines. Kept as a
# constant so the two halves of that regression (blob -> still blocked, blob
# alone -> still allowed) can never drift apart.
BLOB = "A" * 20001

# ---------------------------------------------------------------------------
# MUST BLOCK — exit 2
# ---------------------------------------------------------------------------

MUST_BLOCK = [
    # force-spellings the flag/cluster scans never reached (docstring claimed blocked)
    ("branch-force-move-M","git branch -M old new"),
    ("branch-force-copy",  "git branch -C old new"),
    ("branch-mf-cluster",  "git branch -mf old new"),

    # -- multi-line: the destructive op is not on line 1 ---------------------
    ("nl-basic",              "git status\ngit stash"),
    ("nl-cd",                 "cd /repo\ngit reset --hard"),
    ("nl-worktree-force",     "echo hi\ngit worktree remove --force wt-a"),
    ("nl-multi",              "set -e\ncd /repo\ngit fetch\ngit reset --hard origin/main\necho done"),
    ("nl-crlf",               "git status\r\ngit stash"),
    ("nl-indent",             "echo a\n    git stash pop"),
    ("nl-comment",            "# clean up\ngit clean -fdx"),
    ("nl-trailing-and",       "git fetch &&\ngit reset --hard"),

    # -- Patch 3: the cwd hole, and the shapes that used to walk past it -----
    #    `rm -rf "$WT/$ID"` (fully collapsed) was blocked; leave ONE literal
    #    component and the same delete was allowed. The guard already knows the
    #    NAME of every live worktree and the operand states it verbatim.
    ("var-root-literal-id",    'rm -rf "$WT/T-010"'),
    # NB: the variable is `${WTREE}`, not `${WT}` — `{WT}` is a fixture
    # placeholder and would be substituted out from under the test.
    ("var-root-literal-brace", 'rm -rf "${WTREE}/T-010"'),
    ("var-root-literal-bare",  "rm -rf $WT/T-010"),
    ("var-root-literal-tail2", 'rm -rf "$ROOT/worktrees/T-010"'),
    ("var-root-literal-repo",  'rm -rf "$P/repo/.swarm-loop"'),
    #    Both of these NAME their target, and both were exit 0 for a file that
    #    exists only in that worktree — the question was asked from the session
    #    root, where the file does not exist.
    ("gitC-worktree-own-file", "git -C {WT} checkout only_in_worktree.py"),
    ("cd-worktree-own-file",   "cd {WT} && git checkout only_in_worktree.py"),
    ("cd-worktree-own-file-nl","cd {WT}\ngit checkout only_in_worktree.py"),
    ("gitC-worktree-dot",      "git -C {WT} checkout ."),
    ("cd-outside-rm-repo",     "cd {OUTSIDE} && rm -rf {REPO}"),
    ("cd-outside-rm-worktree", "cd {OUTSIDE} && rm -rf {WT}"),
    ("cd-outside-rm-glob",     "cd {OUTSIDE} && rm -rf ./*"),

    # -- ref destroyers the guard recognised by SPELLING only. `update-ref`,
    #    `symbolic-ref` and `push --force` were exit 2 while every line below —
    #    the same effects, differently spelled — was exit 0.
    ("branch-force-move",      "git branch -f main HEAD~1"),
    ("branch-force-long",      "git branch --force main HEAD~1"),
    ("branch-force-abbrev",    "git branch --for main HEAD~1"),
    ("checkout-force-branch",  "git checkout -B main HEAD~1"),
    ("checkout-force-cluster", "git checkout -qB main HEAD~1"),
    ("switch-force-branch",    "git switch -C main HEAD~1"),
    ("push-mirror",            "git push --mirror origin"),
    ("push-mirror-abbrev",     "git push --mir origin"),
    ("push-delete-refspec",    "git push origin :refs/heads/main"),
    ("push-delete-long",       "git push origin --delete main"),
    ("push-delete-short",      "git push -d origin main"),
    ("push-plus-refspec",      "git push origin +main:main"),
    ("fetch-force-local-ref",  "git fetch origin +main:main"),
    ("fetch-force-local-full", "git fetch origin +refs/heads/main:refs/heads/main"),

    # A command substitution's interior is scanned at the directory the line
    # STARTED in, not after a `cd` on the same line (see `has_substitution`).
    # Pinned here in the conservative direction so the bound is measured.
    ("cd-then-subst-scanned-early",
     "cd {WT} && echo \"cleanup: `rm -rf {WT}`\""),

    # -- ...and the shapes the SAME document records as BLOCKED, probed
    #    against the live hook when it was written. If one of these starts
    #    passing, the runbook's prose is wrong and an orchestrator following it
    #    ends up stuck mid-merge with a conflicted index.
    ("runbook-5-blocked-ours",   "git checkout --ours src/b.py"),
    ("runbook-5-blocked-theirs", "git checkout --theirs src/b.py"),
    ("runbook-5-blocked-2",      "git checkout -2 src/b.py"),
    ("runbook-5-blocked-3",      "git checkout -3 src/b.py"),
    ("runbook-5-blocked-dashdash", "git checkout --ours -- src/b.py"),
    ("runbook-5-blocked-dot",    "git checkout --ours ."),
    ("runbook-10-blocked-force", "git worktree remove --force {WT}"),
    ("runbook-10-blocked-move",  "git worktree move {WT} {OUTSIDE}/T-999"),
    ("runbook-10-blocked-rm",    "rm -rf {OUTSIDE}/T-010"),
    ("runbook-10-blocked-collapsed", 'rm -rf "$WT/$ID"'),

    # -- Round 2 blocks. `rm -rf .git` and `rm -rf $PWD` were missed by the
    #    baseline too; the rest are the round-1 patch's own regression.
    ("rm-rf-dotgit-repo",      "rm -rf {REPO}/.git"),
    ("rm-rf-dotgit-worktree",  "rm -rf {WT}/.git"),
    ("cd-into-swarm-then-rm",  "cd {SWARM} && rm -rf *"),
    ("cd-into-swarm-then-mv",  "cd {SWARM} && mv state.json /tmp/"),

    # -- shell keywords / compound commands ----------------------------------
    ("kw-if",                 "if true; then git stash; fi"),
    ("kw-for",                "for d in wt-*; do git reset --hard; done"),
    ("kw-brace",              "{ git stash; }"),
    ("kw-func",               "f() { git stash; }"),
    ("kw-until",              "until false; do git checkout -- .; done"),
    ("kw-if-sudo",            "if true; then sudo git clean -fdx; fi"),
    ("kw-for-env",            "for i in 1; do GIT_DIR=.git git stash; done"),
    ("kw-while",              "while read l; do git restore src/a.py; done < f"),
    ("kw-else",               "if false; then echo a; else git reset --hard HEAD; fi"),
    ("kw-case",               "case $x in a) git clean -fd;; esac"),
    ("kw-elif",               "if a; then b; elif c; then git checkout .; fi"),
    ("kw-negate",             "! git stash"),
    ("kw-dbracket",           "if [[ -d .git ]]; then git stash; fi"),
    ("kw-subshell-nl",        "(\ncd /repo\ngit stash\n)"),
    ("kw-do-tab-indent",      "for i in 1; do\n\tgit reset --hard\ndone"),
    ("kw-nested-func",        "cleanup() {\n  cd /repo\n  git clean -fdx\n}\ncleanup"),
    ("subshell-oneline",      "(cd /repo && git stash)"),
    ("subshell-cd-wt",        "(cd wt && git stash)"),
    ("bg-job",                "git stash &"),

    # -- backslash continuation ---------------------------------------------
    ("cont-reset",            "git reset \\\n--hard"),
    ("cont-clean",            "git clean \\\n-fdx"),
    ("cont-stash",            "git \\\nstash"),

    # -- plain single-line destructive ops ----------------------------------
    ("plain-stash",           "git stash"),
    ("plain-stash-pop",       "git stash pop"),
    ("plain-reset-hard",      "git reset --hard HEAD"),
    ("plain-reset-abbrev",    "git reset --ha HEAD"),
    ("plain-clean-fdx",       "git clean -fdx"),
    ("plain-clean-fd",        "git clean -fd"),
    ("plain-co-dot",          "git checkout ."),
    ("plain-co-dashdash",     "git checkout -- src/a.py"),
    ("plain-co-force",        "git checkout -f main"),
    ("plain-wt-remove-force", "git worktree remove --force foo"),
    ("plain-wt-remove-f",     "git worktree remove -f foo"),
    ("plain-wt-move",         "git worktree move a b"),
    ("plain-and-stash",       "git add -A && git stash"),
    ("switch-discard",        "git switch --discard-changes main"),
    ("restore-path",          "git restore src/a.py"),
    ("restore-worktree",      "git restore --worktree src/a.py"),
    ("restore-w-abbrev",      "git restore --w src/a.py"),
    ("restore-stage-wt",      "git restore --stage --worktree src/a.py"),
    ("restore-W-short",       "git restore -W src/a.py"),
    ("cd-and-stash",          "cd /tmp && git stash"),
    ("git-C-stash",           "git -C /repo stash"),
    ("commit-then-clean",     'git commit -m "x" && git clean -fd'),
    ("stash-then-comment",    "git stash # trailing comment"),

    # -- `git clean` long-option abbreviations (git's own abbreviation rule) --
    ("clean-abbrev--for",     "git clean --for"),
    ("clean-abbrev--f",       "git clean --f"),
    ("clean-abbrev--fo",      "git clean --fo"),
    ("clean-abbrev--forc",    "git clean --forc"),
    ("clean--force",          "git clean --force"),

    # -- command wrappers, incl. wrappers carrying their own options/values --
    ("w-timeout-30",          "timeout 30 git stash"),
    ("w-timeout-5s",          "timeout 5s git reset --hard"),
    ("w-timeout-preserve",    "timeout --preserve-status 30 git stash"),
    ("w-timeout-s-KILL",      "timeout -s KILL 30 git stash"),
    ("w-timeout-k-value",     "timeout -k 5 30 git stash"),
    ("w-timeout-dashdash",    "timeout -- 30 git stash"),
    ("w-stdbuf",              "stdbuf -oL git stash"),
    ("w-xargs-pipe",          "ls | xargs git checkout --"),
    ("w-xargs-n1",            "xargs -n1 git checkout --"),
    ("w-xargs-I",             "echo x | xargs -I {} git stash"),
    ("w-diff-xargs-co",       "git diff --name-only | xargs git checkout --"),
    ("w-ionice-c3",           "ionice -c3 git clean -fd"),
    ("w-ionice-c-3",          "ionice -c 3 git clean -fd"),
    ("w-setsid",              "setsid git reset --hard HEAD"),
    ("w-nice-n-5",            "nice -n 5 git stash"),
    ("w-nice-n5-attached",    "nice -n5 git stash"),
    ("w-nice-n-10",           "nice -n 10 git stash"),
    ("w-nice-adjustment",     "nice --adjustment 5 git stash"),
    ("w-nice-unknown-opt",    "nice --zzz git stash"),
    ("w-env-i",               "env -i git stash"),
    ("w-env-u",               "env -u GIT_DIR git reset --hard"),
    ("w-env-assign",          "env FOO=bar git stash"),
    ("w-sudo-u-root",         "sudo -u root git reset --hard"),
    ("w-sudo-u-x",            "sudo -u x git stash"),
    ("w-sudo-dashdash",       "sudo -- git stash"),
    ("w-command-p",           "command -p git stash"),
    ("w-time-p",              "time -p git stash"),

    # -- payload re-entered through a shell ---------------------------------
    ("sh-bash-c",             "bash -c 'git stash'"),
    ("sh-sh-c",               'sh -c "git reset --hard"'),
    ("sh-bash-lc",            "bash -lc 'git clean -fd'"),
    ("sh-bash-c-cd",          "bash -c 'cd /x && git checkout -- .'"),
    ("sh-nested-2deep",       "bash -c \"bash -c 'git stash'\""),
    ("sh-nested-3deep",       "bash -c \"bash -c \\\"bash -c 'git stash'\\\"\""),
    ("sh-bash-euc",           "bash -euc 'git reset --hard'"),
    ("sh-timeout-bash-c",     "timeout 60 bash -c 'git clean -fd'"),
    ("sh-abs-bin-bash-c",     "/bin/bash -c 'git stash'"),
    ("sh-bash-norc",          "bash --norc -c 'git stash'"),
    ("sh-zsh-c",              "zsh -c 'git restore src/a.py'"),
    ("sh-combined-wrapper",   "sh -c 'cd wt && nice -n 5 git checkout -- .'"),

    # -- `git checkout <pathspec>` with ONE operand --------------------------
    ("co-file",               "git checkout a.txt"),
    ("co-dir-trailing-slash", "git checkout src/"),
    ("co-repo-root-magic",    "git checkout :/"),
    ("co-glob",               "git checkout '*.py'"),
    ("co-nested-file",        "git checkout src/b.py"),
    ("co-pathspec-from-file", "git checkout --pathspec-from-file=list.txt"),
    ("co-pathspec-file-nul",  "git checkout --pathspec-file-nul --pathspec-from-file=list.txt"),
    ("co-magic-top",          "git checkout :(top)"),
    ("co-nl-then-path",       "git checkout main\ngit checkout -- src/a.py"),

    # -- git ALIASES (configured in the test repo) ---------------------------
    ("alias-co-dot",          "git co ."),
    ("alias-co-file",         "git co a.txt"),
    ("alias-co-dashdash",     "git co -- src/b.py"),
    ("alias-shell-body",      "git wipe"),
    ("alias-inline-wipe",     "git -c alias.wipe='!git stash' wipe"),
    ("alias-inline-w",        "git -c alias.w='!git stash' w"),
    ("alias-inline-checkout", "git -c alias.nuke=checkout nuke ."),
    ("alias-inline-attached", "git -calias.nuke=checkout nuke ."),
    ("alias-in-bash-c",       "bash -c 'git co .'"),

    # -- eval / variable indirection ----------------------------------------
    ("eval-quoted",           'eval "git stash"'),
    ("eval-bare",             "eval git reset --hard"),
    ("eval-single-quoted",    "eval 'git clean -fdx'"),
    ("eval-in-bash-c",        "bash -c 'eval \"git stash\"'"),
    ("var-git-semicolon",     "GIT=git; $GIT stash"),
    ("var-git-newline",       "GIT=git\n$GIT stash"),
    ("var-git-braced",        "${GIT} reset --hard"),
    ("var-gitbin-checkout",   "$GIT_BIN checkout ."),

    # -- plumbing equivalents ------------------------------------------------
    ("plumb-coidx-short",     "git checkout-index -f -a"),
    ("plumb-coidx-long",      "git checkout-index --force --all"),
    ("plumb-readtree-short",  "git read-tree -u --reset HEAD"),
    ("plumb-readtree-long",   "git read-tree --update --reset HEAD"),
    ("plumb-rm-dot",          "git rm -r ."),
    ("plumb-rm-repo-root",    "git rm -r :/"),
    ("plumb-rm-glob",         "git rm '*.py'"),
    ("plumb-apply-R",         "git apply -R patch.diff"),
    ("plumb-apply-reverse",   "git apply --reverse patch.diff"),

    # -- a heredoc is present AND a real destructive op sits outside it -------
    ("hd-then-stash",         "cat > r.md <<'EOF'\n| 1 | git checkout . was run |\nEOF\ngit stash"),
    ("hd-before-reset",       "cat <<'EOF' > n.md\nprose\nEOF\ngit reset --hard"),
    ("hd-plain-then-stash",   "cat > a <<'EOF'\nhi\nEOF\ngit stash"),

    # -- `<<` that is NOT a heredoc (the arithmetic-shift regression) --------
    ("arith-shift-then-stash", "echo $((1<<2))\ngit stash"),
    ("quoted-shift-then-stash", "echo 'a << b'\ngit stash"),
    ("here-string-then-reset", "grep x <<< 'git stash'\ngit reset --hard"),
    ("quote-spans-nl-stash",  "echo 'oops\nstill open'\ngit stash"),

    # -- backticks: shlex's own punctuation set omits `` ` ``, so the closing
    #    backtick used to fuse onto the LAST token and only SOME shapes blocked.
    #    Measured before the fix: `git stash pop` blocked, `git stash` allowed.
    ("bt-stash",              "`git stash`"),
    ("bt-reset-hard",         "`git reset --hard`"),
    ("bt-clean-fdx",          "`git clean -fdx`"),
    ("bt-var-assign",         "X=`git stash`"),
    ("bt-echo",               "echo `git stash`"),
    ("bt-nested-in-dq",       'echo "before" && `git stash`'),

    # -- backticks INSIDE double quotes: the union of the two holes above, and
    #    the last one left open. Unquoted backticks were closed by
    #    PUNCTUATION_CHARS and `$(...)`-in-double-quotes was closed by
    #    `expandable_substitutions`, but shlex applies punctuation only OUTSIDE
    #    quotes, so a backtick pair inside `"..."` survived as inert-looking
    #    token text. Bash executes it: verified with `bash -c 'echo "`id`"'`,
    #    which prints the uid. `bt-inside-dq-prose` used to assert ALLOW here
    #    and was simply wrong about bash — a substitution that runs is not
    #    prose. Its genuinely-inert neighbours (single quotes, a heredoc body,
    #    an escaped backtick, a harmless `git status` body) stay in MUST_ALLOW.
    ("bt-in-dq-stash",        'echo "do not run `git stash` here"'),
    ("bt-in-dq-reset",        'echo "`git reset --hard`"'),
    ("bt-in-dq-assign",       'MSG="`git stash`"'),
    ("bt-in-dq-commit-m",     'git commit -m "`git clean -fdx`"'),
    ("bt-in-dq-rm-worktree",  'echo "cleanup: `rm -rf ../worktrees/T-010`"'),
    # ...and one level down, inside a `bash -c` payload. Deliberately NOT
    # `echo "$(echo "`git stash`")"`: the nested double quotes make posix shlex
    # read the backticks as OUTSIDE any quote, so that shape blocks by
    # tokenizer accident whether the rule exists or not, and asserting it would
    # be a case that cannot fail. This one is allowed with the rule reverted.
    ("bt-in-dq-bash-c",       'bash -c \'echo "`git stash`"\''),

    # -- `$'...'` ANSI-C quoting: valid bash (`bash -n` clean) that raised in
    #    shlex's POSIX mode, failing the WHOLE command open.
    ("ansic-then-stash",      "echo $'don\\'t' ; git stash"),
    ("ansic-after-reset",     "git reset --hard && echo $'it\\'s'"),

    # -- a >20 KB token used to disarm the guard for EVERY segment, across
    #    lines. The blob need not be near the destructive op; now it is elided
    #    and the rest of the command is still evaluated.
    ("blob-then-stash-nl",    "git status\necho " + BLOB + "\ngit stash"),
    ("blob-after-stash",      "git stash ; echo " + BLOB),
    ("blob-quoted-then-clean", 'echo "' + BLOB + '" && git clean -fdx'),
    ("blob-heredocless-reset", "printf %s " + BLOB + "\ngit reset --hard"),

    # -- `$(...)` surviving inside double quotes is one shlex token, and its
    #    interior was never re-scanned. Bash really does run it.
    ("dq-cmdsub-reset",       'echo "$(git reset --hard)"'),
    ("dq-cmdsub-assign",      'MSG="$(git stash)"'),
    ("dq-cmdsub-commit-m",    'git commit -m "$(git stash)"'),
    ("dq-cmdsub-nested",      'echo "$(echo "$(git stash)")"'),
    ("dq-cmdsub-after-heredoc",
     'cat > p.md <<\'EOF\'\nprose\nEOF\necho "$(git stash)"'),

    # -- ref/object destroyers that had NO checker at all --------------------
    ("push-force",            "git push --force origin main"),
    ("push-f",                "git push -f origin main"),
    ("update-ref-delete",     "git update-ref -d refs/heads/task/t1"),
    ("update-ref-set",        "git update-ref refs/heads/main HEAD~5"),
    ("reflog-expire",         "git reflog expire --expire=now --all"),
    ("reflog-delete",         "git reflog delete refs/stash@{0}"),
    ("gc-prune-now",          "git gc --prune=now"),
    ("prune",                 "git prune"),
    ("repack-ad",             "git repack -ad"),
    ("filter-branch",         "git filter-branch --force -- --all"),
    ("symbolic-ref-set",      "git symbolic-ref HEAD refs/heads/other"),
    ("submodule-deinit-f",    "git submodule deinit -f ."),
    ("sparse-checkout-set",   "git sparse-checkout set src"),

    # -- non-git destruction: rules keyed on the PATH ------------------------
    #    `git worktree remove` is the checked teardown and is now ALLOWED; the
    #    `rm -rf` the guard's own hint used to prescribe is what it never saw.
    ("rm-rf-worktree-root",   "rm -rf ../worktrees/T-010"),
    ("rm-rf-worktree-parent", "rm -rf ../worktrees"),
    ("rm-rf-empty-var",       "rm -rf ../worktrees/$ID && git worktree prune"),
    ("rm-rf-slash-var",       "rm -rf /$ID"),
    ("rm-rf-two-vars-slash",  "rm -rf $ROOT/$ID"),
    ("rm-rf-repo-root-dot",   "rm -rf ."),
    ("rm-rf-repo-parent",     "rm -rf .."),
    ("rm-rf-swarm-dir",       "rm -rf .swarm-loop"),
    ("rm-swarm-state",        "rm .swarm-loop/state.json"),
    ("rm-rf-swarm-sudo",      "sudo rm -rf .swarm-loop"),
    ("rm-rf-swarm-bash-c",    "bash -c 'rm -rf .swarm-loop'"),
    ("rm-rf-swarm-nl",        "echo cleaning\nrm -rf .swarm-loop/acceptance"),
    ("mv-swarm-dir",          "mv .swarm-loop /tmp/"),
    ("truncate-state",        "truncate -s 0 .swarm-loop/state.json"),
    ("sed-i-goals",           "sed -i '' 's/x/y/' .swarm-loop/goals.json"),
    ("redirect-state",        "> .swarm-loop/state.json"),
    ("redirect-echo-history", "echo x > .swarm-loop/history.csv"),
    ("append-manifest",       "echo x >> .swarm-loop/manifest.json"),
    ("tee-manifest",          "echo x | tee .swarm-loop/manifest.json"),
    ("dd-state",              "dd if=/dev/null of=.swarm-loop/state.json"),
    ("find-swarm-delete",     "find .swarm-loop -name '*.json' -delete"),

    # -- the three `critical_dir_reason` branches that had no case at all.
    #    Each is a DIFFERENT clause of that function, so one case cannot stand
    #    in for another: `/` is the literal-separator compare, `~` only fires
    #    because the operand is expanduser'd BEFORE the realpath compare (a
    #    guard that merely abspath'd it would resolve `~` to `./~` and allow),
    #    and `-- .` only fires because operands_after_options honours the
    #    end-of-options marker instead of discarding every `-`-led token.
    ("rm-rf-fs-root",         "rm -rf /"),
    ("rm-rf-home-dir",        "rm -rf ~"),
    ("rm-rf-ddash-repo-root", "rm -rf -- ."),
    # The recursion test is `-rRd` as a CLUSTER, not the literal string `-rf`:
    # a lone `-r` is just as recursive and the ledger clause does not even need
    # recursion. Both halves are asserted, because a rule keyed on "-rf" would
    # pass every other rm case in this file and still miss these.
    ("rm-r-swarm-no-f",       "rm -r .swarm-loop"),
    ("rm-d-swarm",            "rm -d .swarm-loop/reports"),

    # -- GLOBS. Every path rule above resolves the LITERAL operand to a real
    #    path, and a glob never names one: `../worktrees/*` realpaths to a path
    #    whose last component is a literal `*`, which is neither equal to nor
    #    under any live worktree root. Measured before the fix:
    #    `rm -rf ../worktrees/*` ALLOWED while `rm -rf ../worktrees` and
    #    `rm -rf ../worktrees/T-010` were both BLOCKED — the one shape that got
    #    through was the natural bulk-teardown spelling, which takes every live
    #    worktree's uncommitted work. The three metacharacters are separate
    #    cases because they are three separate branches of fnmatch, and a rule
    #    keyed on `*` alone would pass the first and miss the other two.
    ("rm-rf-glob-worktrees",  "rm -rf ../worktrees/*"),
    ("rm-rf-glob-star-prefix", "rm -rf ../worktrees/T-0*"),
    ("rm-rf-glob-question",   "rm -rf ../worktrees/?-010"),
    ("rm-rf-glob-bracket",    "rm -rf ../worktrees/[Tt]-010"),
    # A glob in a MIDDLE component still names a worktree root; and a glob on
    # the worktree PARENT's own name reaches "contains live worktrees".
    ("rm-rf-glob-mid",        "rm -rf ../*/T-010"),
    ("rm-rf-glob-parent-name", "rm -rf ../worktree*"),
    # Brace expansion is not a glob and fnmatch knows nothing about it; it is
    # expanded lexically into alternatives, each judged as a path in its own
    # right.
    ("rm-rf-brace-worktrees", "rm -rf ../worktrees/{T-010,T-011}"),
    ("rm-rf-brace-swarm",     "rm -rf {.swarm-loop,build}"),
    # A catch-all last component names the ENTIRE contents of its parent, so it
    # inherits the parent's blast radius: the repo root, `/`, `$HOME`, and the
    # run-state directory.
    ("rm-rf-glob-cwd",        "rm -rf *"),
    ("rm-rf-glob-dot-slash",  "rm -rf ./*"),
    ("rm-rf-glob-dotfiles",   "rm -rf .*"),
    ("rm-rf-glob-fs-root",    "rm -rf /*"),
    ("rm-rf-glob-home",       "rm -rf ~/*"),
    # Already blocked before the glob rule existed — `realpath` keeps the `*`
    # as a component UNDER `.swarm-loop/`, so the `in_swarm_tree` prefilter fires
    # on the literal string. Asserted anyway, because it is the one glob shape
    # whose block does not depend on the new rule and a refactor could quietly
    # move it onto the rule and off the prefilter.
    ("rm-rf-glob-swarm",      "rm -rf .swarm-loop/*"),
    # The collapse rule and the glob rule compose: `$ROOT` unset makes this
    # `rm -rf /*`.
    ("rm-rf-glob-empty-var",  "rm -rf $ROOT/*"),
    # ...and the glob is still seen through the wrappers everything else is.
    ("rm-rf-glob-bash-c",     'bash -c "rm -rf ../worktrees/*"'),
    ("rm-rf-glob-after-and",  "echo tearing down && rm -rf ../worktrees/*"),
]

# ---------------------------------------------------------------------------
# MUST ALLOW — exit 0.  A false block here is worse than a missed block above.
# ---------------------------------------------------------------------------

MUST_ALLOW = [
    ("branch-rename-plain", "git branch -m old new"),
    # the neighbours proving the force fix did not over-block

    # -- the loop's own worktree / merge protocol ----------------------------
    ("wt-add",                "git worktree add .swarm-loop/worktrees/t1 -b task/t1 main"),
    ("wt-teardown",           "git worktree remove .swarm-loop/worktrees/t1 && git worktree prune && git branch -D task/t1"),
    ("wt-list",               "git worktree list"),
    ("wt-prune",              "git worktree prune"),
    ("int-branch",            "git checkout -b integration/e3-b2 main"),
    ("merge-no-ff",           "git merge --no-ff task/t1"),
    ("merge-abort",           "git merge --abort"),
    ("merge-ff-only",         "git checkout main && git merge --ff-only integration/e3-b2"),
    ("branch-D",              "git branch -D task/t1"),

    # -- read-only / informational git --------------------------------------
    ("ls-tree",               "git ls-tree -r HEAD --name-only | wc -l"),
    ("ls-remote",             "git ls-remote --heads origin"),
    ("diff-tests",            "git diff main..task/t1 -- '*test*'"),
    ("diff-head",             "git diff HEAD -- src/a.py"),
    ("show-head",             "git show HEAD:src/app.py"),
    ("harness-restore",       "git show HEAD:src/b.py > src/b.py"),
    ("status",                "git status"),
    ("status-in-worktree",    "git -C .swarm-loop/worktrees/t1 status"),
    ("log",                   "git log --oneline -20"),
    ("log-oneline",           "git log --oneline"),
    ("rev-parse",             "git rev-parse --show-toplevel"),
    ("stash-list",            "git stash list"),
    ("stash-show",            "git stash show"),
    ("stash-create",          "git stash create"),

    # -- normal mutating git a worker really runs ---------------------------
    ("add-A",                 "git add -A"),
    ("add-then-commit",       "git add -A && git commit -m 'feat: thing'"),
    ("commit-feat",           'git commit -m "feat: thing"'),
    ("push",                  "git push origin main"),
    ("push-force-with-lease", "git push --force-with-lease origin main"),
    ("co-force-with-lease",   "git checkout --force-with-lease"),
    ("init",                  "git init -b main"),
    ("fetch",                 "git fetch origin"),
    ("rebase",                "git rebase main"),
    ("cherry-pick",           "git cherry-pick abc123"),
    ("revert",                "git revert --no-edit abc123"),
    ("reset-soft",            "git reset --soft HEAD~1"),
    ("reset-mixed-explicit",  "git reset --mixed HEAD"),
    ("reset-default",         "git reset HEAD~1"),

    # -- commit messages that QUOTE a banned op ------------------------------
    ("msg-stash",             "git commit -m 'do not use git stash here'"),
    ("msg-reset",             'git commit -m "revert: git reset --hard was wrong"'),
    ("msg-if-then-stash",     'git commit -m "if true; then git stash; fi"'),
    ("msg-checkout-dot",      "git commit -m 'never git checkout . in a shared tree'"),
    ("msg-stash-and-clean",   'git commit -m "never run git stash or git clean -fd in a shared tree"'),
    ("msg-timeout-stash",     "git commit -m 'timeout 30 git stash'"),

    # -- `git restore` that only touches the INDEX --------------------------
    ("restore-staged",        "git restore --staged src/a.py"),
    ("restore-stage",         "git restore --stage src/a.py"),
    ("restore-st-abbrev",     "git restore --st src/a.py"),
    ("restore-stag-abbrev",   "git restore --stag a.txt"),
    ("restore-S-short",       "git restore -S src/a.py"),
    ("restore-source-staged", "git restore --source=HEAD --staged src/a.py"),

    # -- `git checkout` operands that really ARE refs -----------------------
    ("co-main",               "git checkout main"),
    ("co-main-redirect",      "git checkout main 2>/dev/null"),
    ("co-branch-with-slash",  "git checkout task/t1"),
    ("co-HEAD",               "git checkout HEAD"),
    ("co-previous",           "git checkout -"),
    ("co-b",                  "git checkout -b fix/thing"),
    ("co-b-attached",         "git checkout -bfix/thing"),
    ("co-b-with-base",        "git checkout -b task/newthing main"),
    ("co-b-feat-with-base",   "git checkout -b feat/x main"),
    ("co-nonexistent-ref",    "git checkout does-not-exist-anywhere"),
    ("co-detach",             "git checkout --detach main"),
    ("switch-branch",         "git switch main"),
    ("switch-c",              "git switch -c feat/x"),
    ("switch-c-attached",     "git switch -cfeat/x"),

    # -- `git clean` that cannot delete anything ----------------------------
    ("clean-n",               "git clean -n"),
    ("clean-dry-run",         "git clean --dry-run"),
    ("clean-dry-abbrev-d",    "git clean --dry -d"),
    ("clean-nd",              "git clean -nd"),
    ("clean-dry-run-fd",      "git clean --dry-run -fd"),

    # -- scoped / index-only plumbing ---------------------------------------
    ("rm-single-file",        "git rm old.py"),
    ("rm-cached-r-dot",       "git rm --cached -r ."),
    ("rm-r-subdir",           "git rm -r src/legacy"),
    ("apply-plain",           "git apply patch.diff"),
    ("apply-check",           "git apply --check patch.diff"),
    ("apply-reject",          "git apply --reject patch.diff"),
    ("read-tree-plain",       "git read-tree HEAD"),

    # -- multi-line flows that are entirely legitimate -----------------------
    ("nl-add-commit-push",    "git add -A\ngit commit -m 'wip'\ngit push"),
    ("nl-cd-status-log",      "cd .swarm-loop/worktrees/t1\ngit status\ngit log --oneline -5"),
    ("nl-retire-loop",        "for id in t1 t2; do\n  git worktree remove .swarm-loop/worktrees/$id\n  git worktree prune\ndone"),
    ("nl-if-else-commit",     "if git diff --quiet; then\n  echo clean\nelse\n  git add -A && git commit -m wip\nfi"),
    ("nl-func-retire",        "retire() {\n  git worktree remove .swarm-loop/worktrees/$1\n  git worktree prune\n}"),
    ("nl-commit-msg-stash",   "git add -A\ngit commit -m 'never git stash in a shared tree'"),
    ("nl-echo-stash-doc",     "echo 'git stash is forbidden' >> learnings.md"),
    ("nl-commit-msg-2line",   "git commit -m 'line one\nline two'"),
    ("nl-commit-msg-2line-dq", 'git commit -m "line1\nline2"'),
    ("nl-two-python-runs",    "python3 skill/scripts/swarmloop.py verify\npython3 skill/scripts/swarmloop.py measure --cycle 1"),
    ("nl-grep-for-reset",     "grep -rn 'git reset --hard' docs/\necho ok"),
    ("nl-case-status",        "case $x in a) git status;; esac"),
    ("nl-cd-then-log",        "cd /repo\ngit log -1 --format=%H"),
    ("nl-dbracket-status",    "if [[ -d .git ]]; then git status; fi"),
    ("nl-blank-lines",        "git status\n\n\ngit log -1"),
    ("nl-leading-ws-only",    "\n  git status  \n"),
    ("nl-co-then-status",     "git checkout main\ngit status"),
    ("nl-co-two-branches",    "git checkout main\ngit checkout task/t1"),
    ("nl-restore-staged-x2",  "git restore --staged a.py\ngit restore --staged b.py"),
    ("nl-clean-n-status",     "git clean -n\ngit status"),
    ("nl-stash-list-show",    "git stash list\ngit stash show"),
    ("nl-cmd-subst-cont",     "FILES=$(git diff --name-only \\\n  main..task/t1)\ngit add $FILES"),
    ("nl-pipe-at-eol",        "git log --oneline |\n  head -5"),
    ("nl-trailing-backslash", "git status \\"),
    ("cont-commit",           "git commit \\\n  -m 'msg' \\\n  --no-verify"),
    ("cont-worktree-add",     "git worktree add \\\n  .swarm-loop/worktrees/t9 \\\n  -b task/t9 main"),
    ("nl-retire-then-prune",  "for id in t1 t2 t3; do\n  git worktree remove .swarm-loop/worktrees/$id\ndone\ngit worktree prune"),
    ("nl-measure-loop",       "for c in 0 1 2; do\n  python3 swarmloop.py measure --cycle $c\ndone"),
    ("nl-if-commit-staged",   "if ! git diff --cached --quiet; then\n  git commit -m 'wave 1'\nfi"),
    ("nl-checkbranch-merge",  "python3 swarmloop.py check-branch task/t1 && \\\n  git merge --no-ff task/t1"),
    ("nl-echo-doc-dblquote",  'echo "never run git reset --hard"\ngit status'),
    ("nl-awk-braces",         "git log --format=%H | awk '{print $1}'\ngit status"),
    ("nl-find-exec-braces",   "find . -name '*.pyc' -exec rm {} \\;\ngit status"),
    ("config-alias-quoted",   "git config alias.st '!f() { git stash list; }; f'"),
    ("nl-json-single-quotes", "echo '{\n  \"a\": 1\n}' > f.json\ngit add f.json"),
    ("for-in-ls-files",       "for f in $(git ls-files); do echo $f; done"),

    # -- HEREDOC PROSE.  The single most important allow class: the skill
    #    mandates writing task packets by heredoc, and packet text quotes every
    #    banned op verbatim.  A false block here wedges the whole run.
    ("hd-packet-mandated",
     "cat > packet.md <<'EOF'\n"
     "## Guard rails\n"
     "- NEVER run `git stash` in a shared tree.\n"
     "- NEVER run `git reset --hard` — it reverts every sibling agent.\n"
     "\n"
     "| # | finding |\n"
     "| 1 | git checkout . was run; ban it |\n"
     "EOF"),
    ("hd-packet-short",       "cat > packet.md <<'EOF'\n- NEVER run `git stash` or `git reset --hard`\n- use `git diff HEAD -- <file>`\nEOF"),
    ("hd-md-table",           "cat > report.md <<'EOF'\n| 1 | git checkout . was run; ban it |\n| 2 | git reset --hard | bad |\nEOF"),
    ("hd-blockquote",         "cat >> notes.md <<'EOF'\n> git checkout . was run by the fix agent\nEOF"),
    ("hd-parenthesized",      "cat > n.md <<'EOF'\nthe agent ran (git reset --hard) and lost work\nEOF"),
    ("hd-pipe-prose",         "cat > n.md <<'EOF'\nfinding | severity | fix\ngit stash | high | ban it\nEOF"),
    ("hd-unterminated",       "cat > r.md <<'EOF'\n| 1 | git checkout . was run |"),
    ("hd-then-git-add",       "cat > a.txt <<'EOF'\ngit clean -fdx\nEOF\ngit add a.txt"),
    ("hd-innocent-then-add",  "cat > a.txt <<'EOF'\nhello\nEOF\ngit add a.txt"),
    ("hd-packet-then-add",    "cat > packet.md <<'EOF'\nNEVER run git stash or git reset --hard\nEOF\ngit add packet.md"),
    ("hd-unquoted-delim",     "cat <<EOF > note.txt\ngit checkout .\nEOF"),
    ("hd-in-bash-c",          "bash -c \"cat > packet.md <<'EOF'\n- NEVER run git stash\nEOF\""),
    ("hd-dash-redirect",      "cat <<-EOF > n.md\n\tgit stash\n\tEOF"),
    ("hd-dash-plain",         "cat <<-EOF\n\tgit stash\n\tEOF"),
    ("hd-double-quoted-delim", 'cat <<"EOF"\ngit reset --hard\nEOF'),
    ("hd-python-payload",     "python3 - <<'PY'\nprint('git clean -fdx')\nPY"),

    # -- harmless aliases ----------------------------------------------------
    ("alias-co-main",         "git co main"),
    ("alias-st",              "git st"),
    ("alias-unstage",         "git unstage a.txt"),
    ("alias-shell-ls-files",  "git ls"),
    ("alias-unknown-sub",     "git totally-unknown-subcommand"),
    ("alias-inline-status",   "git -c alias.s=status s"),

    # -- wrappers around NON-git programs -----------------------------------
    ("w-timeout-pytest",      "timeout 600 pytest -q"),
    ("w-timeout-npm-test",    "timeout 600 npm test"),
    ("w-nice-pytest",         "nice -n 5 pytest -q"),
    ("w-nice-timeout-pytest", "nice -n 10 timeout 600 pytest -q"),
    ("w-env-node-build",      "env NODE_ENV=test npm run build"),
    ("w-xargs-rm",            "xargs rm -f"),
    ("w-find-xargs-rm",       "find . -name '*.pyc' | xargs rm -f"),
    ("w-xargs-git-add",       "git ls-files -m | xargs -r git add"),
    ("w-xargs-0-git-add",     "git ls-files -z | xargs -0 git add"),
    ("w-xargs-a-git-add",     "xargs -a files.txt git add"),
    ("w-xargs-grep",          "git ls-files | xargs grep -l TODO"),
    ("w-timeout-5m-status",   "timeout 5m git status"),
    ("w-timeout-bash-retire", "timeout 120 bash -c 'git worktree remove .swarm-loop/worktrees/t1 && git worktree prune'"),
    ("w-timeout-bash-npm",    "timeout 600 bash -c 'npm ci && npm test'"),
    ("w-env-u-commit",        "env -u GIT_INDEX_FILE git commit -m 'wip'"),
    ("npm-ci",                "npm ci"),
    ("npm-run-build",         "npm run build"),
    ("uv-pip-install",        "uv pip install -e '.[dev]'"),
    ("command-v-git",         "command -v git"),

    # -- prose about banned ops written by non-heredoc means -----------------
    ("echo-append-packet",    "echo 'do not run git stash' >> packet.md"),
    ("printf-append-packet",  "printf '%s\\n' '- NEVER run git stash' >> packet.md"),
    ("echo-tee-learnings",    "echo '  git clean -fd is forbidden' | tee -a learnings.md"),
    ("sed-i-rewrite",         "sed -i '' 's/git stash/git diff HEAD/' packet.md"),
    ("grep-c-reset",          "grep -c 'git reset --hard' references/task-packets.md"),
    ("grep-rn-stash",         "grep -rn 'git stash' references/"),
    ("cat-pipe-grep",         "cat packet.md | grep 'git clean -fd'"),
    ("python-c-print-stash",  "python3 -c \"print('git stash')\""),

    # -- shells running harmless payloads ------------------------------------
    ("bash-c-pytest",         "bash -c 'pytest -q'"),
    ("bash-c-echo-stash",     "bash -c 'echo \"never run git stash\"'"),
    ("bash-c-nested-echo",    "bash -c \"bash -c \\\"bash -c 'echo ok'\\\"\""),
    ("bash-c-python",         "bash -c 'python3 -c \"print(1)\"'"),
    ("sh-c-npm",              "sh -c 'npm ci && npm test'"),
    ("python-swarmloop",      "python3 scripts/swarmloop.py measure --cycle 1"),
    ("subshell-cd-status",    "(cd wt && git status)"),
    ("cd-chain-commit",       "cd wt && git add -A && git commit -m x && cd - "),

    # -- variables that are not git, and eval of harmless payloads -----------
    ("var-python",            "$PYTHON script.py"),
    ("var-dotnet",            "$DOTNET restore"),
    ("var-make-clean",        "$MAKE clean"),
    ("var-npm-braced",        "${NPM} run clean"),
    ("var-git-status",        "$GIT status"),
    ("var-git-add",           "$GIT add -A"),
    ("eval-ssh-agent",        'eval "$(ssh-agent -s)"'),
    ("eval-pytest",           "eval 'pytest -q'"),

    # -- unparseable input must FAIL OPEN, never block all bash --------------
    ("failopen-unterminated", 'echo "unterminated'),
    ("failopen-unbalanced",   "git stash 'unbalanced"),

    # -- the teardown, the other way round. `git worktree remove` verifies the
    #    target IS a worktree and REFUSES one with uncommitted changes; that is
    #    the whole reason it is the sanctioned form now.
    ("wt-remove-plain",       "git worktree remove ../worktrees/T-010"),
    ("wt-remove-then-prune",  "git worktree remove ../worktrees/T-010 && git worktree prune"),

    # -- ordinary housekeeping the path rules must never touch ---------------
    ("rm-rf-node-modules",    "rm -rf node_modules"),
    ("rm-rf-build-dirs",      "rm -rf dist build .venv"),
    ("rm-rf-var-elsewhere",   "rm -rf $TMPDIR/scratch"),
    # A path that is ENTIRELY a variable collapses to the empty string, which
    # bash hands to `rm` verbatim and `rm` refuses — it is not the cwd. Treating
    # it as the cwd made these two false blocks on the repo root.
    ("rm-rf-mktemp-sub",      'rm -rf "$(mktemp -d)"'),
    ("rm-rf-lone-var",        'rm -rf "$VENV"'),
    ("rm-rf-lone-var-bare",   "rm -rf $BUILD_DIR"),
    # Only the variable REFERENCE collapses, not the whole path component:
    # `cache-$ID` with ID unset is `cache-`, not the parent directory. Blanking
    # the component made `rm -rf /tmp/scratch-$USER` a false block.
    ("rm-rf-prefixed-var",    "rm -rf build/cache-$ID"),
    ("rm-rf-braced-var-tail", "rm -rf build/${ID}"),
    ("rm-rf-nested",          "rm -rf src/generated"),
    ("rm-f-one-file",         "rm -f coverage.xml"),
    ("rm-rf-pytest-cache",    "rm -rf .pytest_cache .mypy_cache"),

    # -- the must-allow NEIGHBOURS of the glob block cases. `rm -rf <dir>/*` is
    #    how build junk is actually cleaned, and it is one character away from
    #    the worktree-teardown shape that now blocks — so each of these proves
    #    the rule is keyed on WHICH directory, not on the presence of a glob.
    ("rm-rf-glob-build",      "rm -rf build/*"),
    ("rm-rf-glob-dist",       "rm -rf dist/* node_modules/*"),
    ("rm-rf-glob-nested-dir", "rm -rf src/generated/* target/debug/*"),
    ("rm-rf-glob-dot-build",  "rm -rf ./build/*"),
    ("rm-rf-glob-home-sub",   "rm -rf ~/.cache/pip/*"),
    ("rm-rf-glob-braces-ok",  "rm -rf build/{a,b}"),
    # A pattern with LITERAL text is not a catch-all, so its parent's blast
    # radius is not its own: `*.pyc` in the repo root deletes .pyc files, not
    # the repo. Reading it as "the cwd" would false-block ordinary housekeeping,
    # which is the failure this guard is least allowed to have.
    ("rm-rf-glob-pyc-in-cwd", "rm -rf *.pyc"),
    ("rm-rf-glob-logs-in-cwd", "rm -rf *.log build-*.tmp"),
    # A glob that is not the LAST component targets things INSIDE the matches,
    # not the matches themselves — cleaning node_modules out of every worktree
    # is not deleting the worktrees.
    ("rm-rf-glob-inside-wts", "rm -rf ../worktrees/*/node_modules"),
    # The pattern is matched against what GIT says is live, not against the
    # word "worktrees": `T-999` was never `worktree add`ed, so `T-999*` names
    # no live worktree even though its parent contains several.
    ("rm-rf-glob-stale-wt",   "rm -rf ../worktrees/T-999*"),
    ("find-pyc-delete",       "find . -name '*.pyc' -delete"),
    ("mv-source-file",        "mv src/a.py src/b.py"),
    ("truncate-a-log",        "truncate -s 0 build.log"),
    ("sed-no-inplace",        "sed 's/x/y/' .swarm-loop/goals.json | head -5"),

    # -- the must-allow NEIGHBOURS of the three block cases just added. Each
    #    one is a step away from a blocked path, and each proves the rule is
    #    keyed on the thing it claims to be keyed on rather than on a prefix.
    #    Without these, a guard that blocked everything under `~` or everything
    #    matching `worktrees` would pass the block half and wedge real work.
    ("rm-rf-home-subdir",     "rm -rf ~/.cache/pip"),
    # The worktree rules ASK GIT which paths are live worktrees; they do not
    # pattern-match the word. `T-999` was never `worktree add`ed, so the
    # directory of a retired ticket is ordinary housekeeping. This is the case
    # that separates "consults git worktree list" from "greps for worktrees".
    ("rm-rf-stale-worktree",  "rm -rf ../worktrees/T-999"),
    # H-30's elision must not cost the heredoc suppression. A task packet that
    # pastes a big fixture AND quotes the banned ops in prose is the exact
    # document the loop mandates writing, and it now contains an oversized
    # token; the old whole-command bailout allowed it for the wrong reason,
    # and the new per-token elision has to keep allowing it for the right one.
    ("hd-blob-with-stash-prose",
     "cat > packet.md <<'EOF'\n" + BLOB + "\n| 1 | git stash was run; ban it |\nEOF"),

    # -- `.swarm-loop/` writes the loop actually mandates ---------------------
    #    The preamble every worker reads is written by heredoc; the frozen suite
    #    is restored from git; and RUNNING the frozen suite must never be
    #    blocked while writing it is not — that inversion was observed live.
    ("preamble-heredoc",      "cat > .swarm-loop/worker-preamble.md <<'EOF'\nnever run git stash\nEOF"),
    ("acceptance-restore",    "git show HEAD:.swarm-loop/acceptance/t.py > .swarm-loop/acceptance/t.py"),
    ("pytest-acceptance",     "pytest .swarm-loop/acceptance -q"),
    ("append-non-ledger",     "echo note >> .swarm-loop/notes.md"),
    ("cat-state",             "cat .swarm-loop/state.json | jq .cycle"),

    # -- the near-misses of the newly-checked git verbs ----------------------
    ("gc-plain",              "git gc"),
    ("gc-prune-never",        "git gc --prune=never"),
    ("reflog-show",           "git reflog show main"),
    ("repack-d-only",         "git repack -d"),
    ("symbolic-ref-read",     "git symbolic-ref HEAD"),
    ("submodule-update",      "git submodule update --init --recursive"),
    ("sparse-checkout-list",  "git sparse-checkout list"),
    ("rebase-interactive",    "git rebase -i main"),
    ("git-mv",                "git mv a.txt c.txt"),
    ("update-index-refresh",  "git update-index --refresh"),

    # -- Patch 3 controls. Every one of these is a shape the widened rules
    #    could plausibly have swallowed, and the widening is only worth having
    #    if they stay allowed.
    ("gitC-worktree-branch",   "git -C {WT} checkout main"),
    ("gitC-worktree-status",   "git -C {WT} status"),
    ("cd-worktree-branch",     "cd {WT} && git checkout main"),
    ("cd-worktree-commit",     "cd {WT} && git add -A && git commit -m 'feat: x'"),
    ("cd-worktree-build-junk", "cd {WT} && rm -rf build"),
    ("cd-outside-teardown",    "cd {OUTSIDE} && git worktree remove {WT}"),
    ("cd-elsewhere-scratch",   "cd {ELSEWHERE} && rm -rf scratch"),
    ("rm-rf-elsewhere",        "rm -rf {ELSEWHERE}"),
    #    A vendored checkout under node_modules has a `.git` DIRECTORY, not the
    #    `.git` FILE a linked worktree has. The child scan that sees
    #    `rm -rf ../worktrees` must not see this one.
    ("rm-rf-vendored-clone",   "rm -rf {REPO}/node_modules"),
    ("rm-rf-vendored-glob",    "rm -rf {REPO}/node_modules/*"),
    ("rm-rf-wt-build",         "rm -rf {WT}/build"),
    ("rm-rf-wt-build-glob",    "rm -rf {WT}/build/*"),
    ("var-root-unprotected",   'rm -rf "$BUILD/dist"'),
    ("var-root-tail-nomatch",  'rm -rf "$WT/T-999"'),
    #    `-D` is `--delete --force`; the force test must run AFTER the delete
    #    test or `git branch -D task/<id>` — the documented teardown — breaks.
    ("branch-delete-force",    "git branch -D task/t1"),
    ("branch-delete-long",     "git branch --delete task/t1"),
    ("branch-rename",          "git branch -m old new"),
    ("branch-list",            "git branch -a"),
    ("branch-set-upstream",    "git branch -u origin/main"),
    ("fetch-force-remote-ref", "git fetch origin +refs/heads/*:refs/remotes/origin/*"),
    ("fetch-force-tags",       "git fetch origin +refs/tags/*:refs/tags/*"),
    ("fetch-plain-refspec",    "git fetch origin main:refs/remotes/origin/main"),
    ("fetch-all-prune",        "git fetch --all --prune"),
    ("push-plain-refspec",     "git push origin main:main"),
    ("push-dry-run",           "git push --dry-run origin main"),
    ("push-lease",             "git push --force-with-lease origin main"),
    ("checkout-create-branch", "git checkout -b task/T-011 main"),
    ("switch-create-branch",   "git switch -c task/T-012"),
    ("checkout-create-cluster","git checkout -qb task/T-013 main"),
    #    `-Bar` is a BRANCH NAME attached to -b, not the -B force flag.
    ("checkout-b-attached",    "git checkout -bBranchName main"),
    ("switch-c-attached-caps", "git switch -cCoolBranch"),

    # ...and the same shape with nothing destructive inside stays allowed, so
    # the line above is a real classification and not "substitutions block".
    ("cd-then-subst-harmless",
     "cd {WT} && echo \"branch: `git rev-parse --abbrev-ref HEAD`\""),

    # -- THE RUNBOOK. Every command `references/merge-protocol.md` PRESCRIBES
    #    in steps 5-10, verbatim from the document with the fixture's own
    #    values substituted for its placeholders. (The document has steps 0-10;
    #    there is no step 11-15 and there never were "19 lines" — 7 logical
    #    commands live in its fenced blocks and the rest are inline in prose.)
    #    If the guard blocks one of these, the merge protocol has no path
    #    through it and the run stalls at integration, so these are the load-
    #    bearing allow cases in this file.
    ("runbook-5-show-ours",    "git show :2:src/b.py > src/b.py"),
    ("runbook-5-show-theirs",  "git show :3:src/b.py > src/b.py"),
    ("runbook-5-add",          "git add src/b.py"),
    ("runbook-7-merge-abort",  "git merge --abort"),
    ("runbook-8-uv-sync",      "uv sync --locked"),
    ("runbook-8-assert-effect",
     ".venv/bin/python -c \"import somepkg, importlib.metadata as m; \\\n"
     "    print(somepkg.__file__, m.version('somepkg'))\""),
    ("runbook-9-land",         "git checkout main && git merge --ff-only integration/e3-b2"),
    ("runbook-9-tripwire",     "git ls-tree -r HEAD --name-only | wc -l"),
    ("runbook-10-remove",      "git worktree remove {WT}"),
    ("runbook-10-salvage",     "git -C {WT} status"),
    ("runbook-10-list",        "git worktree list"),
    ("runbook-10-du",          "du -sh {OUTSIDE}"),
    ("runbook-10-branch-del",  "git branch -D task/t1"),
    ("runbook-10-teardown-chain",
     "git worktree remove {WT} && git worktree prune && git branch -D wt/t010"),
    ("runbook-10-escalate",
     "swarmloop.py escalate --kind harness --subject \"{WT}\" --reason \"uncommitted\""),
    ("runbook-10-background",  "(git worktree remove {WT} &) ; git worktree list"),

    # -- Round 2. Every line here is a FALSE BLOCK the round-1 patch shipped,
    #    found by an adversarial verifier and reproduced before fixing. A false
    #    block is the worse failure mode by this guard's own contract, and all
    #    four classes came from widening resolution without scoping it.
    #
    #    A submodule's `.git` is a FILE, so the linked-worktree scan walked into
    #    it and reported the submodule as "the repository root".
    ("rm-rf-submodule-parent", "rm -rf {REPO}/vendor"),
    ("rm-rf-submodule-itself", "rm -rf {SUB}"),
    ("rm-rf-submodule-glob",   "rm -rf {REPO}/vendor/*"),
    #    The guard is SESSION-scoped. Resolving from the operand made every
    #    repository on the machine undeletable, this suite's own temp dir
    #    included.
    ("rm-rf-foreign-repo",     "rm -rf {FOREIGN}"),
    ("rm-rf-scratch-with-repo","rm -rf {SCRATCH}"),
    ("rm-rf-scratch-fixrepo",  "rm -rf {SCRATCH}/fixrepo"),
    ("rm-rf-scratch-fixwt",    "rm -rf {SCRATCH}/fixwt"),
    #    T1: the suite tested the PARENT of the vendored clone and not the
    #    clone, which is the half that broke.
    ("rm-rf-vendored-inner",   "rm -rf {REPO}/node_modules/vendored-pkg"),
    #    A `+` refspec creating a branch that does not exist destroys nothing;
    #    this is the documented GitHub PR-fetch idiom.
    ("fetch-force-new-branch", "git fetch origin +refs/pull/7/head:pr-7"),
    ("fetch-force-new-local",  "git fetch origin +refs/heads/main:brand-new"),

    # -- prose and inert quoting around the new tokenizer paths --------------
    # `bt-inside-dq-prose` used to live here asserting ALLOW for
    # `echo "do not run `git stash` here"`. That was a wrong claim about bash,
    # not a policy choice — `bash -c 'echo "`id`"'` prints the uid, so the
    # substitution runs. It is now `bt-in-dq-stash` in MUST_BLOCK. What stays
    # allowed is everything that is genuinely inert, and the four shapes below
    # are exactly the ways a backtick can appear without executing.
    ("bt-inside-sq-prose",    "grep -rn '`git stash`' references/"),
    ("bt-harmless-sub",       "echo `git status`"),
    # A backtick body that is not a blocked op stays allowed whether or not it
    # is inside double quotes — the interior is SCANNED, not banned.
    ("bt-in-dq-harmless",     'echo "branch: `git rev-parse --abbrev-ref HEAD`"'),
    ("bt-in-dq-nongit",       'echo "listing: `ls -1`"'),
    # An ESCAPED backtick inside double quotes is a literal backtick to bash,
    # and an unpaired one opens no substitution at all.
    ("bt-in-dq-escaped",      'echo "use \\`git stash\\` carefully"'),
    ("bt-in-dq-unpaired",     'echo "a stray ` in prose about git stash"'),
    ("sq-cmdsub-prose",       "echo 'never run $(git stash)'"),
    # A heredoc BODY is document text. The loop writes every task packet this
    # way, and its prose quotes the banned ops verbatim; re-scanning a body for
    # `$(...)` would false-block the dispatch step itself.
    ("hd-body-cmdsub-prose",
     "cat > packet.md <<'EOF'\n- never run $(git stash)\nEOF"),
    ("hd-body-backtick-prose",
     "cat > packet.md <<'EOF'\n- never run `git reset --hard`\nEOF"),
    ("ansic-harmless",        "printf $'%s\\n' ok"),
    ("blob-alone",            "echo " + BLOB),
    ("blob-in-commit-msg",    'git commit -m "' + BLOB + '"'),
]


# ---------------------------------------------------------------------------
# PAYLOAD-SHAPED cases — the parts of the contract a command string cannot
# express: which directory the command will run in, and which tool is asking.
#
# `{WT}` / `{REPO}` are substituted with the throwaway worktree and repo roots.
# The hook PROCESS always runs with cwd = the primary checkout, which is what
# makes the cwd cases a real differential rather than a restatement.
# ---------------------------------------------------------------------------

PAYLOAD_CASES = [
    # `--force`/`-f` forces EVERY refspec, so `src:dst` is then exactly as
    # destructive as `+src:dst`. check_fetch skipped every dash-token, so it
    # never saw the flag. Pinned to a repo cwd on purpose: outside one, git
    # cannot resolve whether the destination branch exists and the command
    # would fail anyway, so ALLOW there is correct, not a miss.
    ("fetch-force-flag-in-repo", BLOCK,
     {"tool_name": "Bash", "cwd": "{REPO}",
      "tool_input": {"command": "git fetch --force origin main:main"}}),
    ("fetch-force-short-in-repo", BLOCK,
     {"tool_name": "Bash", "cwd": "{REPO}",
      "tool_input": {"command": "git fetch -f origin main:main"}}),
    # -- cwd. Reproduced against the live hook before the fix: payload
    #    byte-identical, always declaring the worktree; hook process cwd =
    #    primary -> ALLOWED, hook process cwd = worktree -> BLOCKED. The control
    #    below is what makes it conclusive — the rule works, so the allow was a
    #    failed CLASSIFICATION: a worker could destructively check out any file
    #    it had created, i.e. most of its own output.
    ("cwd-worktree-own-file", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "git checkout only_in_worktree.py"}}),
    ("cwd-control-both-trees", BLOCK,
     {"tool_name": "Bash",
      "tool_input": {"command": "git checkout a.txt"}}),
    ("cwd-worktree-branch-ok", ALLOW,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "git checkout main"}}),
    ("cwd-worktree-add-commit", ALLOW,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "git add -A && git commit -m 'feat: x'"}}),
    # A cwd that cannot be used must change nothing — never a block of its own.
    ("cwd-missing-dir-allow", ALLOW,
     {"tool_name": "Bash", "cwd": "{REPO}/no/such/dir",
      "tool_input": {"command": "git status"}}),
    ("cwd-missing-dir-block", BLOCK,
     {"tool_name": "Bash", "cwd": "{REPO}/no/such/dir",
      "tool_input": {"command": "git stash"}}),
    ("cwd-not-a-string", BLOCK,
     {"tool_name": "Bash", "cwd": 17,
      "tool_input": {"command": "git stash"}}),

    # -- THE SHAPE THE OLD `cwd` SECTION DID NOT HAVE. All four of its cases
    #    resolved to a git repository — a worktree, the repo, a nonexistent dir
    #    UNDER the repo (git's parent walk still finds it), and a malformed
    #    `cwd: 17` (which falls back to the process cwd, the repo). An EXISTING
    #    DIRECTORY OUTSIDE ANY REPO had no case at all, and it is the one that
    #    made `repo_root()` None and `worktree_roots()` [] and fell every path
    #    rule through to "no reason to block". The run's worktree PARENT is
    #    exactly that directory, it is granted by `--add-dir`, and it is the
    #    natural place to stand for a teardown loop.
    ("cwd-outside-rm-repo-root", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf {REPO}"}}),
    ("cwd-outside-rm-worktree", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf {WT}"}}),
    ("cwd-outside-rm-worktrees-glob", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf {OUTSIDE}/*"}}),
    ("cwd-outside-rm-swarm-dir", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf {REPO}/.swarm-loop"}}),
    ("cwd-outside-truncate-ledger", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "> {REPO}/.swarm-loop/state.json"}}),
    ("cwd-nowhere-rm-repo-root", BLOCK,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "rm -rf {REPO}"}}),
    # ...and it must not become a blanket refusal: an ordinary delete issued
    # from that same directory stays allowed.
    ("cwd-outside-teardown-ok", ALLOW,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "git worktree remove {WT}"}}),
    ("cwd-outside-scratch-ok", ALLOW,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf {ELSEWHERE}"}}),
    ("cwd-outside-node-modules-ok", ALLOW,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "rm -rf node_modules"}}),

    # -- SECOND undiscovered shape, and the likelier one: a worker's OWN
    #    worktree. Every dispatched worker runs there, and `swarm_dir()`
    #    resolved to `<worktree>/.swarm-loop` — which does not exist — so the
    #    ledger rules were disarmed for absolute paths from the one cwd the
    #    swarm uses most. Measured before the fix: all four exit 0.
    ("cwd-worktree-rm-swarm-dir", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "rm -rf {REPO}/.swarm-loop"}}),
    ("cwd-worktree-truncate-ledger", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "> {REPO}/.swarm-loop/state.json"}}),
    ("cwd-worktree-sed-goals", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "sed -i '' 's/1/0/' {REPO}/.swarm-loop/goals.json"}}),
    ("cwd-worktree-mv-swarm", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "mv {REPO}/.swarm-loop /tmp/"}}),
    ("cwd-worktree-edit-ledger", BLOCK,
     {"tool_name": "Edit", "cwd": "{WT}",
      "tool_input": {"file_path": "{REPO}/.swarm-loop/state.json"}}),

    # -- Round 2, cwd-dependent. `_CWD` is what these are about, so they can
    #    only be said as payloads.
    #
    #    THE REGRESSION. Standing inside the run's state directory, an operand
    #    no longer contains the string `.swarm-loop`, and the prefilter that
    #    reads the operand TEXT switched every ledger rule off. Round 1's own
    #    cd-tracking is what could put `_CWD` there, and the baseline blocked
    #    `cd .swarm-loop && rm -rf *` that round 1 allowed.
    ("cwd-swarm-rm-glob", BLOCK,
     {"tool_name": "Bash", "cwd": "{SWARM}", "tool_input": {"command": "rm -rf *"}}),
    ("cwd-swarm-rm-ledger", BLOCK,
     {"tool_name": "Bash", "cwd": "{SWARM}", "tool_input": {"command": "rm -f state.json"}}),
    ("cwd-swarm-truncate-ledger", BLOCK,
     {"tool_name": "Bash", "cwd": "{SWARM}", "tool_input": {"command": "> state.json"}}),
    ("cwd-swarm-sed-ledger", BLOCK,
     {"tool_name": "Bash", "cwd": "{SWARM}",
      "tool_input": {"command": "sed -i '' 's/a/b/' goals.json"}}),
    ("cwd-swarm-edit-ledger", BLOCK,
     {"tool_name": "Edit", "cwd": "{SWARM}", "tool_input": {"file_path": "state.json"}}),
    #    `$PWD` is a variable whose value the guard is tracking, and the
    #    lone-variable exemption swallowed it.
    ("cwd-worktree-rm-pwd", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}", "tool_input": {"command": "rm -rf $PWD"}}),
    ("cwd-worktree-rm-pwd-brace", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}", "tool_input": {"command": "rm -rf ${PWD}"}}),
    ("cwd-worktree-rm-pwd-sub", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}", "tool_input": {"command": "rm -rf \"$(pwd)\""}}),
    # The UNQUOTED `rm -rf $(pwd)` is deliberately absent, not overlooked: bare
    # `(`/`)` are tokenizer punctuation, so it splits into an `rm -rf $` segment
    # and a separate `pwd` command and the operand never exists as a string.
    # That is the guard's documented substitution bound, and asserting either
    # verdict for it would encode a promise it never made.
    #    T3: THE cd AXIS IS BLIND HERE BY CONSTRUCTION — `with_cd` only ever
    #    emits a leading `cd`, and a leading `cd` is the one shape bash and the
    #    guard already agreed on. Every `cd` below sits where bash does NOT keep
    #    it, and round 1 applied all of them to the rest of the line: false
    #    blocks when the cd pointed somewhere protected, missed blocks when it
    #    pointed away.
    ("cd-in-subshell-not-applied", ALLOW,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "( cd {OUTSIDE} && ls ) ; rm -rf T-010"}}),
    ("cd-in-cmdsub-not-applied", ALLOW,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "X=$(cd {OUTSIDE} && pwd); rm -rf T-010"}}),
    ("cd-in-dead-branch-not-applied", ALLOW,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "if false; then cd {OUTSIDE}; fi; rm -rf T-010"}}),
    ("cd-conditional-not-applied", ALLOW,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "[ -d {OUTSIDE} ] && cd {OUTSIDE}; rm -rf T-010"}}),
    ("cd-in-pipeline-not-applied", ALLOW,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "echo x | cd {OUTSIDE} ; rm -rf T-010"}}),
    #    ...and the same shapes from the OTHER side: the cd points away from a
    #    live worktree, so ignoring it must still leave the delete blocked.
    ("cd-subshell-no-bypass", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "( cd /tmp ) ; rm -rf T-010"}}),
    ("cd-cmdsub-no-bypass", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "X=$(cd /tmp && pwd); rm -rf T-010"}}),
    ("cd-dash-no-bypass", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "cd /tmp && cd - >/dev/null; rm -rf T-010"}}),
    ("cd-pipeline-no-bypass", BLOCK,
     {"tool_name": "Bash", "cwd": "{OUTSIDE}",
      "tool_input": {"command": "true | cd /tmp ; rm -rf T-010"}}),
    #    ...while the leading `cd` this patch exists to honour still works.
    ("cd-leading-still-applied", BLOCK,
     {"tool_name": "Bash", "cwd": "{ELSEWHERE}",
      "tool_input": {"command": "cd {OUTSIDE} && rm -rf T-010"}}),

    # -- non-Bash tools. The registration matcher is `Bash`-only today, so the
    #    guard ALSO self-exempted: widening the matcher alone would have changed
    #    nothing. Measured then: Bash exit 2, Edit/Write/NotebookEdit exit 0.
    ("edit-ledger-state", BLOCK,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/state.json"}}),
    ("write-ledger-goals", BLOCK,
     {"tool_name": "Write", "cwd": "{REPO}",
      "tool_input": {"file_path": "{REPO}/.swarm-loop/goals.json"}}),
    ("notebookedit-ledger", BLOCK,
     {"tool_name": "NotebookEdit", "cwd": "{REPO}",
      "tool_input": {"notebook_path": ".swarm-loop/history.csv"}}),
    # `acceptance/` is deliberately writable: it is tamper-EVIDENT via the
    # manifest hash, and the only amendment path seen to work in a live run is
    # `Edit` into that directory under a user-approved `freeze --amend`.
    ("edit-acceptance-ok", ALLOW,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/acceptance/test_frozen.py"}}),
    ("edit-product-source", ALLOW,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": "src/b.py"}}),
    ("read-ledger-ok", ALLOW,
     {"tool_name": "Read", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/state.json"}}),
    ("unknown-tool-ok", ALLOW,
     {"tool_name": "Grep", "cwd": "{REPO}", "tool_input": {"pattern": "x"}}),

    # -- the documented asymmetry: no tool_name at all is treated as Bash.
    ("no-tool-name-is-bash", BLOCK,
     {"cwd": "{REPO}", "tool_input": {"command": "git stash"}}),

    # -- MultiEdit. The fix reads four write-tools; three of them had cases and
    #    this one had none, so "MultiEdit is handled" rested on reading the
    #    tuple rather than on a measurement. It is the tool an agent reaches for
    #    to rewrite a ledger in several hunks, which is the shape that matters.
    ("multiedit-ledger-history", BLOCK,
     {"tool_name": "MultiEdit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/history.csv"}}),
    ("multiedit-product-source", ALLOW,
     {"tool_name": "MultiEdit", "cwd": "{REPO}",
      "tool_input": {"file_path": "src/b.py"}}),

    # -- the ledger rule must not depend on how the path is SPELLED. An agent
    #    writing an absolute path, or one whose payload omits `cwd` entirely,
    #    is doing the same thing as one writing a relative path.
    ("edit-ledger-absolute", BLOCK,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": "{REPO}/.swarm-loop/manifest.json"}}),
    ("write-ledger-no-cwd-key", BLOCK,
     {"tool_name": "Write", "tool_input": {"file_path": ".swarm-loop/state.json"}}),
    # A worker's cwd is its worktree, which has no `.swarm-loop/` of its own;
    # the ledger it would reach is still the run's. Resolving from the payload
    # cwd must not turn that into an allow.
    ("write-ledger-from-worktree", BLOCK,
     {"tool_name": "Write", "cwd": "{WT}",
      "tool_input": {"file_path": ".swarm-loop/state.json"}}),

    # -- the rest of LEDGER_NAMES. The freeze log is the append-only record the
    #    A1 manifest check reconciles against, and the escalation files are the
    #    only channel a worker has for a frozen thing that is wrong: hand-edit
    #    either and the harness's two independent witnesses become one.
    ("edit-freeze-log", BLOCK,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/freeze-log.jsonl"}}),
    ("edit-escalations-jsonl", BLOCK,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/escalations.jsonl"}}),
    ("edit-escalations-md", BLOCK,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/ESCALATIONS.md"}}),
    # ...and the neighbour that proves the rule is the LEDGER, not the
    # directory. `backlog.md`, `learnings.md`, `decisions.md` live in
    # `.swarm-loop/` and the loop mandates editing them every cycle; blocking
    # those would wedge the run harder than any tamper.
    ("edit-swarm-prose-doc", ALLOW,
     {"tool_name": "Edit", "cwd": "{REPO}",
      "tool_input": {"file_path": ".swarm-loop/backlog.md"}}),
    # A malformed payload must fall through to allow, never block on its own —
    # the same fail-open contract the command parser has.
    ("edit-no-file-path", ALLOW,
     {"tool_name": "Edit", "cwd": "{REPO}", "tool_input": {}}),

    # -- `rm -rf` of a worktree by ABSOLUTE path. The string battery can only
    #    write relative paths; this is the form a script that computed the path
    #    would use, and it exercises the `ap == wt` compare rather than the
    #    ancestor walk.
    ("rm-rf-worktree-absolute", BLOCK,
     {"tool_name": "Bash", "cwd": "{REPO}",
      "tool_input": {"command": "rm -rf {WT}"}}),
    # ...and the same worktree emptied by a glob instead of named. The catch-all
    # branch keys on the PREFIX, so this exercises a different clause from the
    # relative-path glob cases in the string battery.
    ("rm-rf-worktree-absolute-glob", BLOCK,
     {"tool_name": "Bash", "cwd": "{REPO}",
      "tool_input": {"command": "rm -rf {WT}/*"}}),

    # -- a worker's cwd is its OWN worktree, so `rm -rf *` there is that
    #    worktree's entire uncommitted contents. The prefix that makes it
    #    critical is the cwd, which only the payload can express — and the
    #    neighbour proves the same command in a build directory is untouched.
    ("rm-rf-glob-cwd-in-worktree", BLOCK,
     {"tool_name": "Bash", "cwd": "{WT}",
      "tool_input": {"command": "rm -rf *"}}),
    ("rm-rf-glob-cwd-not-critical", ALLOW,
     {"tool_name": "Bash", "cwd": "{WT}/build",
      "tool_input": {"command": "rm -rf *"}}),
]


# ---------------------------------------------------------------------------
# throwaway repo
# ---------------------------------------------------------------------------

def build_repo(root):
    """A throwaway repo the guard can ASK questions of.

    The guard resolves two questions against the cwd: 'is this checkout operand
    a ref or a path on disk?' and 'is this subcommand an alias?'. Both need a
    real repo with real files, real branches and real aliases, or half the
    battery silently fails open and proves nothing.
    """
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "src"))
    for name, body in (("a.txt", "hello\n"), ("list.txt", "a.txt\n")):
        with open(os.path.join(repo, name), "w") as f:
            f.write(body)
    with open(os.path.join(repo, "src", "b.py"), "w") as f:
        f.write("x = 1\n")

    def g(*args):
        p = subprocess.run(["git"] + list(args), cwd=repo, env=git_env(root),
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit("test setup failed: git %s\n%s%s"
                             % (" ".join(args), p.stdout, p.stderr))

    g("init", "-q")
    g("symbolic-ref", "HEAD", "refs/heads/main")   # portable `init -b main`
    g("config", "user.email", "swarm-loop-tests@example.invalid")
    g("config", "user.name", "swarm-loop tests")
    g("config", "commit.gpgsign", "false")
    # Aliases the battery exercises. `co = checkout` is dotfile boilerplate and
    # needs no adversarial intent; `wipe` is the shell-body form.
    g("config", "alias.co", "checkout")
    g("config", "alias.st", "status")
    g("config", "alias.unstage", "reset HEAD --")
    g("config", "alias.wipe", "!git stash")
    g("config", "alias.ls", "!git ls-files")
    g("add", "-A")
    g("commit", "-qm", "seed")
    g("branch", "task/t1")

    # A REAL worktree, at the layout the skill's default teardown uses
    # (`../<repo>-worktrees/<id>`), because three rule families ask git about it
    # rather than pattern-matching a path: `rm -r` of a worktree root, `rm -r` of
    # a directory that CONTAINS worktree roots (the `$ID`-collapses-to-empty
    # disaster), and the ref-vs-path classification that must resolve from the
    # payload's cwd. Without a live worktree those rules would silently no-op and
    # their cases would pass for the wrong reason.
    g("worktree", "add", "-q", "-b", "wt/t010",
      os.path.join(root, "worktrees", "T-010"), "main")
    # A file that exists ONLY in the worktree — the whole point of the cwd
    # differential: from the primary tree it does not look like a path at all.
    with open(os.path.join(root, "worktrees", "T-010", "only_in_worktree.py"), "w") as f:
        f.write("y = 2\n")
    # An ordinary build directory INSIDE the worktree. `rm -rf *` run from the
    # worktree root is that worktree's whole uncommitted contents; run from here
    # it is build junk. Same command string, different cwd — which is the only
    # way to prove the catch-all glob rule keys on the directory, not the glob.
    os.makedirs(os.path.join(root, "worktrees", "T-010", "build"))

    # An EXISTING DIRECTORY OUTSIDE ANY REPOSITORY, holding nothing. `{OUTSIDE}`
    # (the worktrees parent) is outside every repo too, but it CONTAINS live
    # worktrees, which is itself a protected shape — so it cannot on its own
    # show that a barren outside directory behaves. This one can.
    os.makedirs(os.path.join(root, "elsewhere", "scratch"))

    # A VENDORED CHECKOUT: a real git repository whose `.git` is a DIRECTORY,
    # sitting where build junk sits. The rule that finds worktrees BELOW an
    # operand is restricted to the LINKED shape (`.git` is a FILE) precisely so
    # this stays deletable; without a fixture for it that restriction is a
    # comment, not a tested property.
    # A SUBMODULE. Its `.git` is a FILE — exactly like a linked worktree's —
    # and that is the collision that made `rm -rf vendor` exit 2 with "that is
    # the repository root". The suite had a fixture for the SAFE half of this
    # distinction (a plain clone, `.git` a directory) and none for the half
    # that actually breaks, which is also the commoner one in a monorepo.
    subsrc = os.path.join(root, "submodule-source")
    os.makedirs(subsrc)
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", subsrc] + cmd, env=git_env(root),
                       capture_output=True, text=True)
    with open(os.path.join(subsrc, "s.py"), "w") as f:
        f.write("s = 1\n")
    for cmd in (["add", "-A"], ["commit", "-qm", "sub"]):
        subprocess.run(["git", "-C", subsrc] + cmd, env=git_env(root),
                       capture_output=True, text=True)
    subprocess.run(["git", "-C", repo, "-c", "protocol.file.allow=always",
                    "submodule", "add", "-q", subsrc, "vendor/sub"],
                   env=git_env(root), capture_output=True, text=True)
    if not os.path.isfile(os.path.join(repo, "vendor", "sub", ".git")):
        raise SystemExit("test setup failed: submodule `.git` is not a FILE — the "
                         "fixture cannot exercise the worktree/submodule collision")

    # A FOREIGN repository, and a scratch directory holding a repository AND a
    # linked worktree of it. Neither belongs to this run — no `.swarm-loop`, not
    # a worktree of the repo under test — and both must stay deletable. Nothing
    # in the corpus deleted a repo that was not the run's, so "every git repo on
    # the machine is now undeletable" had no cell. This suite's own temp dir is
    # this shape.
    foreign = os.path.join(root, "foreign-clone")
    subprocess.run(["git", "clone", "-q", subsrc, foreign], env=git_env(root),
                   capture_output=True, text=True)
    scratch = os.path.join(root, "scratch")
    fixrepo = os.path.join(scratch, "fixrepo")
    os.makedirs(fixrepo)
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", fixrepo] + cmd, env=git_env(root),
                       capture_output=True, text=True)
    with open(os.path.join(fixrepo, "f.py"), "w") as f:
        f.write("f = 1\n")
    for cmd in (["add", "-A"], ["commit", "-qm", "f"],
                ["worktree", "add", "-q", "-b", "fixwt",
                 os.path.join(scratch, "fixwt"), "HEAD"]):
        subprocess.run(["git", "-C", fixrepo] + cmd, env=git_env(root),
                       capture_output=True, text=True)

    vend = os.path.join(repo, "node_modules", "vendored-pkg")
    os.makedirs(vend)
    p = subprocess.run(["git", "init", "-q", vend], env=git_env(root),
                       capture_output=True, text=True)
    if p.returncode != 0 or not os.path.isdir(os.path.join(vend, ".git")):
        raise SystemExit("test setup failed: vendored clone\n%s%s" % (p.stdout, p.stderr))

    # The run-state directory. Left UNTRACKED so no existing verdict shifts.
    os.makedirs(os.path.join(repo, SWARM_DIR, "acceptance"))
    os.makedirs(os.path.join(repo, SWARM_DIR, "worktrees"))
    for rel in ("state.json", "goals.json", "manifest.json", "history.csv",
                "acceptance/test_frozen.py", "worker-preamble.md"):
        with open(os.path.join(repo, SWARM_DIR, rel), "w") as f:
            f.write("{}\n")
    return repo


def git_env(root):
    """Environment that pins git to the test repo's own config.

    Without this a developer's ~/.gitconfig (an alias, a default branch, an
    includeIf) changes the verdicts, and the suite means something different on
    every machine.
    """
    env = dict(os.environ)
    env["HOME"] = os.path.join(root, "home")
    env["XDG_CONFIG_HOME"] = os.path.join(root, "home", ".config")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


# ---------------------------------------------------------------------------
# THE CROSS-PRODUCT — the axis the battery used to hold fixed
# ---------------------------------------------------------------------------
#
# 482 green cases coexisted with four reproducible bypasses because every case
# ran from ONE cwd. The old `cwd` section had four shapes and all four resolved
# to a git repository; the shape that disarms every path rule — an EXISTING
# DIRECTORY OUTSIDE ANY REPO — had no case, `git -C` appeared in 2 of 482, and
# the variable-root `rm -rf "$WT/T-010"` form in none. The suite was evidence
# that the guard behaved as its authors imagined, not that it was sound.
#
# So the whole battery now runs across three cwds and three command prefixes.
# Three of the four axes below need NO hand-written expectations at all, which
# is the point — an expectation table I author by running the guard and writing
# down what it did would encode the bug as the spec.
#
#   1. CWD MATRIX. Every string case at cwd in {repo, worktree, outside}.
#      Expected = the case's own declared verdict, unless `CWD_EXPECT` says
#      otherwise. Every entry there is a case naming a RELATIVE path, which
#      genuinely means a different file from a different directory; each one
#      carries the reason.
#
#   2. ABSOLUTE-PATH INVARIANCE (derived — no expectations). Rewrite the
#      fixture-relative paths in a case to absolute ones. An absolute path
#      names the same file from everywhere, so the verdict MUST equal the
#      case's declared verdict at ALL THREE cwds. This is the axis that
#      catches the bug: measured against the unpatched guard, 29 of these 49
#      were wrong at one cwd or both.
#
#   3. `cd D && …` EQUIVALENCE (derived). `cd D && cmd` issued anywhere must
#      give the same verdict as `cmd` issued from D. bash's cd is not a
#      suggestion.
#
#   4. `git -C D …` EQUIVALENCE (derived). Same, for the git-only subset.
#
# Cost control, stated exactly: axis 1 runs over EVERY case. Axis 2 runs over
# every case that HAS a fixture-relative path to rewrite — 49 of them, which is
# every case the rewrite can apply to and not a sample. Axes 3 and 4
# run every case whose verdict actually VARIES with the directory (it has a
# CWD_EXPECT entry) against all three targets, and every other case against one
# representative target — `outside`, the directory that used to disarm every
# path rule. Running all three for all of them costs ~35s more and found nothing
# the single target did not.

CWDS = ("repo", "worktree", "outside")

# The only case whose verdict under `cd D && …` differs from the same command
# run from D. See `has_substitution` for why, and MUST_BLOCK/MUST_ALLOW for the
# two cases that pin the behaviour instead.
CD_AXIS_EXEMPT = frozenset({"bt-in-dq-rm-worktree"})

# Cases whose verdict legitimately DEPENDS on where the command runs. Every one
# names a RELATIVE path: `../worktrees/T-010` is the live worktree from the
# repo, a directory that does not exist from inside that worktree, and the
# worktrees parent itself from the worktrees parent. `.swarm-loop` is the run's
# state directory from the repo and names nothing from outside it.
#
# These are expectations, not observations: each says what the path ACTUALLY
# RESOLVES TO from that directory, checkable by hand from the fixture — and
# checked that way, against the shell's own resolution rather than the guard's.
#
# About half of these cells RESTATE the case's declared verdict rather than
# changing it (the `worktree` entries, mostly). That is deliberate — every cell
# of the matrix should be stated rather than inferred — but it means the table
# is about half as load-bearing as its size suggests.
_REL_SWARM = {"worktree": BLOCK, "outside": ALLOW}
_REL_WORKTREES = {"worktree": ALLOW, "outside": BLOCK}
_NEEDS_REPO_CFG = {"worktree": BLOCK, "outside": ALLOW}

CWD_EXPECT = {}
# `.swarm-loop` relative to the cwd. From the repo it is the run's state
# directory. From a worktree it is `<worktree>/.swarm-loop` — which does not
# exist, and is blocked anyway because that worktree IS a git root and the rule
# is "the run-state directory of the repository you are standing in". From
# outside any repo there is no repository, so the name is protected by nothing
# and deleting it destroys nothing.
for _n in ("append-manifest", "dd-state", "find-swarm-delete", "mv-swarm-dir",
           "redirect-echo-history", "redirect-state", "rm-d-swarm",
           "rm-r-swarm-no-f", "rm-rf-brace-swarm", "rm-rf-glob-dotfiles",
           "rm-rf-glob-swarm", "rm-rf-swarm-bash-c", "rm-rf-swarm-dir",
           "rm-rf-swarm-nl", "rm-rf-swarm-sudo", "rm-swarm-state",
           "sed-i-goals", "tee-manifest", "truncate-state"):
    CWD_EXPECT[_n] = dict(_REL_SWARM)
# `../worktrees…` relative to the cwd. From the repo: the live worktree tree.
# From INSIDE the worktree `../worktrees` is `<worktrees>/worktrees`, which
# does not exist and protects nothing. From the worktrees parent itself, `..`
# is its parent, so `../worktrees` is the worktrees directory — still live,
# still blocked, and on the unpatched guard it was allowed.
for _n in ("bt-in-dq-rm-worktree", "rm-rf-brace-worktrees", "rm-rf-empty-var",
           "rm-rf-glob-after-and", "rm-rf-glob-bash-c", "rm-rf-glob-bracket",
           "rm-rf-glob-mid", "rm-rf-glob-parent-name", "rm-rf-glob-question",
           "rm-rf-glob-star-prefix", "rm-rf-glob-worktrees",
           "rm-rf-worktree-parent", "rm-rf-worktree-root"):
    CWD_EXPECT[_n] = dict(_REL_WORKTREES)
# Aliases live in the repository's own config, and a one-operand `git checkout`
# is classified by ASKING git. Outside a repository git answers neither, and the
# guard's documented contract for an unanswerable question is ALLOW — which is
# also what git itself does with the command: it errors out.
# The four conflict-stage spellings are the same family: the flags are stripped
# as options, so the file arrives as a LONE non-ref operand and the verdict is
# whatever git says about it. Outside a repository git says nothing — and the
# command itself errors there, so there is no working tree to overwrite.
for _n in ("alias-co-dashdash", "alias-co-dot", "alias-co-file",
           "alias-in-bash-c", "alias-shell-body", "co-file", "co-nested-file",
           "runbook-5-blocked-ours", "runbook-5-blocked-theirs",
           "runbook-5-blocked-2", "runbook-5-blocked-3",
           # `+<src>:<dst>` is only destructive if <dst> is an EXISTING local
           # branch, and only a repository can say. Outside one, allow.
           "fetch-force-local-ref", "fetch-force-local-full"):
    CWD_EXPECT[_n] = dict(_NEEDS_REPO_CFG)
# Cases that NAME an absolute fixture path already, via a placeholder: those
# resolve identically everywhere and need no entry at all.


def cwd_want(name, base):
    """The verdict this case must produce at each cwd."""
    over = CWD_EXPECT.get(name, {})
    return dict((c, over.get(c, base)) for c in CWDS)


_ABS_WORKTREES = re.compile(r"\.\./worktrees")
_ABS_SWARM = re.compile(r"(?<![\w/.\-]).swarm-loop")


def absolutize(cmd, repo, root):
    """The same command with its fixture-relative paths made ABSOLUTE.

    An absolute path names one file from every directory on the machine, so
    every verdict about it must be cwd-invariant. That removes "the path means
    something else there" as an explanation and leaves only one: the guard
    asked git from the wrong place. Returns None when nothing was rewritten."""
    out = _ABS_WORKTREES.sub(os.path.join(root, "worktrees"), cmd)
    out = _ABS_SWARM.sub(os.path.join(repo, SWARM_DIR), out)
    return out if out != cmd else None


def has_substitution(name):
    """Does this command contain a `$(...)` or backtick body that the guard
    scans as a command of its own?

    Those bodies are scanned off the RAW STRING — that is deliberate and load
    bearing, because single quotes decide whether bash executes them and posix
    tokenization erases quotes. The cost is that a substitution's interior is
    evaluated at the directory the line STARTED in, while the rest of the line
    is evaluated after any `cd` on it. So `cd <wt> && echo "`rm -rf
    ../worktrees/T-010`"` is judged from the pre-cd directory and blocks, where
    the same string run from the worktree is inert and allowed.

    That is a real bound, not a rounding error, and it is EXEMPTED from the cd
    axis rather than papered over with an expectation fitted to it. It errs
    toward BLOCK, which is the survivable direction, and it narrows — it does
    not remove — the guard's long-standing "anything reached after a `cd`
    WITHIN the command" limitation. Two explicit cases pin the behaviour so it
    stays known rather than merely unmeasured.

    Exempted BY NAME, not by a `"$(" in cmd` test. That test removed 35 cases
    from the axis, most of them single-quoted prose or heredoc bodies the guard
    never scans as commands at all — an exemption far wider than the bound it
    was justifying, and the difference between the two is exactly where a
    defect hides."""
    return name in CD_AXIS_EXEMPT


def with_cd(cmd, target):
    """`cd <target>` in front of the command, spelled so the result is always
    valid bash: `&&` for a plain single line, a newline for anything with its
    own structure (a function definition, a leading comment, more lines)."""
    if "\n" in cmd or "(" in cmd or cmd.lstrip().startswith("#"):
        return "cd %s\n%s" % (target, cmd)
    return "cd %s && %s" % (target, cmd)


_GIT_C_SAFE = re.compile(r"^git\s")


def with_git_c(cmd, target):
    """`git -C <target>` for a command that is exactly one git invocation.
    Returns None when the command is anything else — `-C` redirects git, not
    the shell, so it is only equivalent for a lone git call."""
    if not _GIT_C_SAFE.match(cmd):
        return None
    if any(ch in cmd for ch in "\n|;`") or "&&" in cmd or "$(" in cmd:
        return None
    return "git -C %s %s" % (target, cmd[4:])


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def run_payload(repo, env, payload):
    """The hook process ALWAYS starts in the primary checkout. For the cwd cases
    that is the point: any verdict that changes must come from the payload."""
    p = subprocess.run([sys.executable, HOOK], input=payload, cwd=repo, env=env,
                       capture_output=True, text=True)
    return p.returncode, p.stderr


def bash_payload(command, cwd=None):
    d = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        d["cwd"] = cwd
    return json.dumps(d)


# ---------------------------------------------------------------------------
# ENVIRONMENT cases — the contract that cannot be expressed as a command string
# ---------------------------------------------------------------------------
#
# An empty protected set used to mean two different things: "nothing is
# protected here" and "I could not find out". A recursive delete is the one
# operation where guessing the second is unrecoverable, so it now fails CLOSED
# — and that is the ONLY rule in the hook that does. The rest of this list is
# the proof that it did not leak into the global fail-OPEN contract, which is
# the more important half: a guard that blocks all bash when it hits a bug
# wedges an unattended run.
#
# `nogit` runs the hook with a PATH containing no git binary at all.

ENV_CASES = [
    ("nogit-rm-r-fails-closed",   BLOCK, True,  "rm -rf node_modules"),
    ("nogit-rm-r-abs-closed",     BLOCK, True,  "rm -rf {ELSEWHERE}/scratch"),
    ("nogit-rm-r-brace-closed",   BLOCK, True,  "rm -rf {ELSEWHERE}/{a,b}"),
    # ...and no further than that. A non-recursive rm cannot take a directory.
    ("nogit-rm-plain-allows",     ALLOW, True,  "rm -f somefile"),
    # Rules that need no git at all keep working.
    ("nogit-stash-still-blocks",  BLOCK, True,  "git stash"),
    ("nogit-reset-still-blocks",  BLOCK, True,  "git reset --hard"),
    # ...and every FAIL-OPEN path stays open.
    ("nogit-checkout-fails-open", ALLOW, True,  "git checkout a.txt"),
    ("nogit-status-allows",       ALLOW, True,  "git status"),
    ("nogit-ordinary-allows",     ALLOW, True,  "ls -la && echo hi"),
    ("nogit-unbalanced-quote",    ALLOW, True,  "echo 'unterminated"),
    ("nogit-heredoc-packet",      ALLOW, True,
     "cat > p.md <<'EOF'\n- never run git stash\nEOF"),
    # The control: with git present the same delete is allowed, so the block
    # above is the missing ANSWER and not the command.
    ("git-present-rm-r-allows",   ALLOW, False, "rm -rf node_modules"),
]

MALFORMED_PAYLOADS = [
    ("payload-not-json",      ALLOW, "{not json"),
    ("payload-json-list",     ALLOW, "[1, 2, 3]"),
    ("payload-json-null",     ALLOW, "null"),
    ("payload-json-string",   ALLOW, '"just a string"'),
    ("payload-json-number",   ALLOW, "17"),
    ("payload-empty",         ALLOW, ""),
    ("payload-no-tool-input", ALLOW, '{"tool_name": "Bash"}'),
]


def build_matrix(repo, root):
    """Every case, at every cwd, under every prefix. Returns (name, payload,
    want, kind) tuples. Three of the four axes derive their expectation instead
    of declaring one."""
    dirs = {"repo": repo,
            "worktree": os.path.join(root, "worktrees", "T-010"),
            "outside": os.path.join(root, "worktrees")}
    string_cases = ([(n, fill_paths(c, repo, root), BLOCK) for n, c in MUST_BLOCK]
                    + [(n, fill_paths(c, repo, root), ALLOW) for n, c in MUST_ALLOW])
    jobs = []

    # 1. cwd matrix — declared expectations, with CWD_EXPECT overrides.
    for name, cmd, want in string_cases:
        wants = cwd_want(name, want)
        for c in CWDS:
            jobs.append(("%s@%s" % (name, c), bash_payload(cmd, dirs[c]),
                         wants[c], "cwd"))

    # 2. absolute-path invariance — DERIVED: an absolute path names the same
    #    file everywhere, so the declared verdict must hold at every cwd.
    for name, cmd, want in string_cases:
        acmd = absolutize(cmd, repo, root)
        if acmd is None:
            continue
        for c in CWDS:
            jobs.append(("%s@abs:%s" % (name, c), bash_payload(acmd, dirs[c]),
                         want, "abs"))

    # 3/4. prefix equivalence — DERIVED: `cd D && cmd` and `git -C D cmd` must
    #      equal `cmd` run from D. Path-relevant cases get all three targets;
    #      the rest get one representative target, which is the runtime trade
    #      and it is stated here rather than hidden.
    #      THE RUNTIME TRADE, STATED: a case whose verdict actually VARIES with
    #      the directory (it has a CWD_EXPECT entry) gets all three targets,
    #      because that is where a prefix bug can hide. Everything else gets one
    #      representative target — `outside`, the directory that used to disarm
    #      every path rule. Running all three for all of them costs ~35s more
    #      and found nothing the one target did not: the redirect-ordering
    #      defect this axis exists to catch reddens at `outside` alone.
    for name, cmd, want in string_cases:
        wants = cwd_want(name, want)
        targets = CWDS if name in CWD_EXPECT else ("outside",)
        for t in targets:
            start = dirs["repo"] if t != "repo" else dirs["outside"]
            if not has_substitution(name):
                jobs.append(("%s@cd:%s" % (name, t),
                             bash_payload(with_cd(cmd, dirs[t]), start),
                             wants[t], "cd"))
            gc = with_git_c(cmd, dirs[t])
            if gc is not None:
                jobs.append(("%s@gitC:%s" % (name, t), bash_payload(gc, start),
                             wants[t], "gitC"))

    # 5. payload-shaped cases: cwd and tool_name, which no command string says.
    for name, want, d in PAYLOAD_CASES:
        jobs.append((name, json.dumps(fill_paths(d, repo, root)), want, "payload"))

    # 6. malformed payloads: the fail-open contract at the outermost layer.
    for name, want, raw in MALFORMED_PAYLOADS:
        jobs.append((name, raw, want, "payload"))

    return jobs


def fill_paths(obj, repo, root):
    """Substitute the fixture placeholders in a case, at any depth.

    {REPO}      the primary checkout
    {WT}        a live linked worktree of it
    {OUTSIDE}   the worktrees PARENT — an existing directory outside any
                repository that CONTAINS live worktrees. The live layout.
    {ELSEWHERE} an existing directory outside any repository that contains
                nothing. The control for {OUTSIDE}.
    {SWARM}     the run's state directory, as a place to STAND — from inside it
                an operand no longer spells `.swarm-loop` at all.
    {SUB}       a SUBMODULE: `.git` is a FILE, like a linked worktree's.
    {FOREIGN}   a git repository that is not this run's.
    {SCRATCH}   a scratch directory holding a repository AND a worktree of it,
                still not this run's. This suite's own temp dir is this shape.
    """
    if isinstance(obj, str):
        return (obj.replace("{REPO}", repo)
                   .replace("{WT}", os.path.join(root, "worktrees", "T-010"))
                   .replace("{OUTSIDE}", os.path.join(root, "worktrees"))
                   .replace("{ELSEWHERE}", os.path.join(root, "elsewhere"))
                   .replace("{SWARM}", os.path.join(repo, SWARM_DIR))
                   .replace("{SUB}", os.path.join(repo, "vendor", "sub"))
                   .replace("{FOREIGN}", os.path.join(root, "foreign-clone"))
                   .replace("{SCRATCH}", os.path.join(root, "scratch")))
    if isinstance(obj, dict):
        return dict((k, fill_paths(v, repo, root)) for k, v in obj.items())
    return obj


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    if not os.path.isfile(HOOK):
        print("TEST_GIT_GUARD: hook not found at %s" % HOOK)
        return 1

    root = tempfile.mkdtemp(prefix="swarmloop-guard-tests-")
    os.makedirs(os.path.join(root, "home"))
    nogit_dir = os.path.join(root, "nogit-bin")
    os.makedirs(nogit_dir)
    try:
        repo = build_repo(root)
        env = git_env(root)
        nogit_env = dict(env)
        nogit_env["PATH"] = nogit_dir           # a PATH with no git binary at all

        # Declared names must be unique BEFORE the matrix multiplies them; a
        # duplicate would otherwise silently become a duplicate in every cell.
        declared = ([n for n, _ in MUST_BLOCK] + [n for n, _ in MUST_ALLOW]
                    + [n for n, _, _ in PAYLOAD_CASES]
                    + [n for n, _, _, _ in ENV_CASES]
                    + [n for n, _, _ in MALFORMED_PAYLOADS])
        dupes = sorted({n for n in declared if declared.count(n) > 1})
        if dupes:
            print("TEST_GIT_GUARD: duplicate case names: %s" % ", ".join(dupes))
            return 1

        jobs = build_matrix(repo, root)
        jobs += [(n, bash_payload(fill_paths(c, repo, root), repo), want,
                  "nogit" if nogit else "env")
                 for n, want, nogit, c in ENV_CASES]
        nogit_names = set(n for n, _w, ng, _c in ENV_CASES if ng)

        def run_one(job):
            name, payload, _want, _kind = job
            e = nogit_env if name in nogit_names else env
            return run_payload(repo, e, payload)

        workers = min(16, max(4, (os.cpu_count() or 2) * 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_one, jobs))

        passed, failures = 0, []
        by_kind = {}
        for (name, payload, want, kind), (got, stderr) in zip(jobs, results):
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if got == want:
                passed += 1
                if verbose:
                    print("  ok    %-40s %s  %s"
                          % (name, "BLOCK" if want == BLOCK else "allow", payload))
            else:
                failures.append((name, payload, want, got, stderr, kind))

        label = {BLOCK: "BLOCK (exit 2)", ALLOW: "ALLOW (exit 0)"}
        for name, payload, want, got, stderr, kind in failures:
            kindlabel = "MISSED BLOCK" if want == BLOCK else "FALSE BLOCK"
            print("")
            print("FAIL [%s]  %s   (axis: %s)" % (kindlabel, name, kind))
            print("  expected : %s" % label[want])
            print("  actual   : exit %d" % got)
            print("  payload  : %s" % payload)
            first = (stderr or "").strip().splitlines()
            if first:
                print("  guard    : %s" % first[0])

        print("")
        print("  declared: %d block  %d allow  %d payload  %d env  %d malformed"
              % (len(MUST_BLOCK), len(MUST_ALLOW), len(PAYLOAD_CASES),
                 len(ENV_CASES), len(MALFORMED_PAYLOADS)))
        print("  cells run: " + "  ".join(
            "%s=%d" % (k, by_kind[k]) for k in sorted(by_kind)))
        print("TEST_GIT_GUARD: PASS=%d FAIL=%d" % (passed, len(failures)))
        return 1 if failures else 0
    finally:
        if not os.environ.get("SWARMLOOP_TESTS_KEEP"):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("  (kept: %s)" % root)


if __name__ == "__main__":
    sys.exit(main())
