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
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, os.pardir, "hooks", "git-guard.py")

BLOCK = 2
ALLOW = 0

# ---------------------------------------------------------------------------
# MUST BLOCK — exit 2
# ---------------------------------------------------------------------------

MUST_BLOCK = [
    # -- multi-line: the destructive op is not on line 1 ---------------------
    ("nl-basic",              "git status\ngit stash"),
    ("nl-cd",                 "cd /repo\ngit reset --hard"),
    ("nl-worktree",           "echo hi\ngit worktree remove wt-a"),
    ("nl-multi",              "set -e\ncd /repo\ngit fetch\ngit reset --hard origin/main\necho done"),
    ("nl-crlf",               "git status\r\ngit stash"),
    ("nl-indent",             "echo a\n    git stash pop"),
    ("nl-comment",            "# clean up\ngit clean -fdx"),
    ("nl-trailing-and",       "git fetch &&\ngit reset --hard"),

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
    ("plain-wt-remove",       "git worktree remove foo"),
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
]

# ---------------------------------------------------------------------------
# MUST ALLOW — exit 0.  A false block here is worse than a missed block above.
# ---------------------------------------------------------------------------

MUST_ALLOW = [
    # -- the loop's own worktree / merge protocol ----------------------------
    ("wt-add",                "git worktree add .swarm-loop/worktrees/t1 -b task/t1 main"),
    ("wt-teardown",           "rm -rf .swarm-loop/worktrees/t1 && git worktree prune && git branch -D task/t1"),
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
    ("nl-retire-loop",        "for id in t1 t2; do\n  rm -rf .swarm-loop/worktrees/$id\n  git worktree prune\ndone"),
    ("nl-if-else-commit",     "if git diff --quiet; then\n  echo clean\nelse\n  git add -A && git commit -m wip\nfi"),
    ("nl-func-retire",        "retire() {\n  rm -rf .swarm-loop/worktrees/$1\n  git worktree prune\n}"),
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
    ("nl-retire-then-prune",  "for id in t1 t2 t3; do\n  rm -rf .swarm-loop/worktrees/$id\ndone\ngit worktree prune"),
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
    ("w-timeout-bash-retire", "timeout 120 bash -c 'rm -rf .swarm-loop/worktrees/t1 && git worktree prune'"),
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
# runner
# ---------------------------------------------------------------------------

def run_case(repo, env, command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    p = subprocess.run([sys.executable, HOOK], input=payload, cwd=repo, env=env,
                       capture_output=True, text=True)
    return p.returncode, p.stderr


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    if not os.path.isfile(HOOK):
        print("TEST_GIT_GUARD: hook not found at %s" % HOOK)
        return 1

    root = tempfile.mkdtemp(prefix="swarmloop-guard-tests-")
    os.makedirs(os.path.join(root, "home"))
    try:
        repo = build_repo(root)
        env = git_env(root)

        cases = ([(n, c, BLOCK) for n, c in MUST_BLOCK]
                 + [(n, c, ALLOW) for n, c in MUST_ALLOW])

        dupes = sorted({n for n in [c[0] for c in cases]
                        if [c[0] for c in cases].count(n) > 1})
        if dupes:
            print("TEST_GIT_GUARD: duplicate case names: %s" % ", ".join(dupes))
            return 1

        workers = min(8, (os.cpu_count() or 2) * 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda c: run_case(repo, env, c[1]), cases))

        passed, failures = 0, []
        for (name, command, want), (got, stderr) in zip(cases, results):
            if got == want:
                passed += 1
                if verbose:
                    print("  ok    %-24s %s  %r"
                          % (name, "BLOCK" if want == BLOCK else "allow", command))
            else:
                failures.append((name, command, want, got, stderr))

        label = {BLOCK: "BLOCK (exit 2)", ALLOW: "ALLOW (exit 0)"}
        for name, command, want, got, stderr in failures:
            kind = "MISSED BLOCK" if want == BLOCK else "FALSE BLOCK"
            print("")
            print("FAIL [%s]  %s" % (kind, name))
            print("  expected : %s" % label[want])
            print("  actual   : exit %d" % got)
            print("  command  : %r" % command)
            first = (stderr or "").strip().splitlines()
            if first:
                print("  guard    : %s" % first[0])

        print("")
        print("  block cases: %d   allow cases: %d" % (len(MUST_BLOCK), len(MUST_ALLOW)))
        print("TEST_GIT_GUARD: PASS=%d FAIL=%d" % (passed, len(failures)))
        return 1 if failures else 0
    finally:
        if not os.environ.get("SWARMLOOP_TESTS_KEEP"):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("  (kept: %s)" % root)


if __name__ == "__main__":
    sys.exit(main())
