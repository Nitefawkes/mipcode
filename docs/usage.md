# Using mip-delta

Run Git checks in a repository with an existing commit. Use `--base-ref` to compare the working tree with an existing ref.

```sh
mip-delta --check-git --root . --base-ref HEAD --json
```

JSON output includes changed paths, findings, and loaded runtime edges. Compact text defaults to a 250-byte advisory budget. The check normally returns zero when it reports findings. Add `--fail-on-findings` to return status 1 for findings.

Use `--root` to limit the analysis to a subdirectory. Git mode reads tracked and non-ignored untracked Python files. Paths stay relative to the top of the repository so imports and reported locations use the same coordinates. A failed Git command, unreadable source, or syntax error in a changed or proposed Python file returns status 2. Text diagnostics go to stderr. With `--json`, an analysis failure returns an object with `status` set to `"error"` and an `error` message. An unchanged file that does not parse, such as a template or a legacy script, is skipped. It contributes no callers or definitions. Text mode names skipped files on stderr, and JSON lists them in `skipped_unparsed_paths`.

The [bundled example](../README.md#quick-start) uses candidate mode. It compares a proposed replacement with an existing file and reports locations to review.

At resolved calls, the check can flag new required arguments, removed parameters, renamed keywords, changes to positional binding, and changes between synchronous and async definitions. It also reports surviving calls whose previous resolved definition was removed. Signature binding uses explicit positional and keyword arguments. Decorators, argument unpacking, dynamic dispatch, and indirect imports can limit coverage.

A newly raised exception is compared with narrow `try`/`except` handlers at the call and at one unguarded caller above it. A handler for a base class counts as catching the exception. Builtin classes and classes defined in the workspace are followed through their bases. The check stays quiet when both the raised class and a handler class come from outside the workspace, because their relationship is unknown.

## Track what is still open

A contract change can leave work at other sites. Pass `--ledger` to keep those sites in a JSON file between checks.

```sh
mip-delta --check-git --ledger .mipdelta/ledger.json
mip-delta --check-git --ledger .mipdelta/ledger.json --open
mip-delta --waive 5f39d331 --reason "caller is removed in the next change" --ledger .mipdelta/ledger.json
```

With a ledger, text output names only sites that this check newly opened. A site leaves the ledger when a later edit resolves it or the contract change is reverted. Edits that move a site to another line do not reopen it. `--open` lists every site still open with its id, up to 600 bytes by default, and summarizes the rest as a count per file. `--waive` records that a site is intentional. A waived site is not reported again, and `--fail-on-findings` then fails only while unwaived sites remain. JSON output adds `opened`, `discharged`, `open`, and `waived` lists. The `findings` list still holds every current finding.

A ledger stored in a `.mipdelta` directory adds a `.gitignore` there so the state stays out of Git status.

## Run as Claude Code hooks

`--hook` reads a Claude Code hook event on standard input and answers in that hook's JSON format. Register both hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "mip-delta --hook claude-post-edit", "timeout": 30}]
      }
    ],
    "Stop": [
      {
        "hooks": [{"type": "command", "command": "mip-delta --hook claude-stop", "timeout": 30}]
      }
    ]
  }
}
```

After an edit to a Python file, the first hook adds newly opened sites as context for the model. It prints nothing for other files, for local edits, and for sites it already reported. When the model tries to finish, the second hook blocks once and lists the sites still open. It does not block again for sites it has already listed, so the model can state that a remaining site is intentional and finish.

The hooks use `CLAUDE_PROJECT_DIR` as the workspace and keep one ledger per session under `.mipdelta/` at the top of the repository. The comparison baseline is the commit that was current when the session's first site opened, so a commit made during the session does not hide open sites. A hook reads only the changed files unless one of them changes a contract that other files can depend on. A hook never fails the tool call: a file that does not parse yet, or a directory outside a Git repository, produces no output. Set `MIPDELTA_HOOK_DEBUG=1` to see the reason on stderr.

Other agent harnesses can call `mip-delta --check-git --ledger PATH` after edits and add `--open` before finishing.

## Check a merge

Two branches can merge without a textual conflict and still disagree. One changes a function's contract and updates its callers. The other adds a caller that uses the old contract. After merging, compare the merged tree with the merge base:

```sh
mip-delta --check-git --base-ref "$(git merge-base HEAD~1 HEAD^2)"
```

A branch that completed its own migration contributes no findings, so what remains comes from the combination. Use the two branch names in place of `HEAD~1` and `HEAD^2` when checking before the merge commit exists.

## Observe calls during tests

Use CPython 3.12 or later and install pytest into the same environment. From the MipCode repository root:

```sh
python -m pip install -e ".[test]"
mip-delta --run-pytest --root . --pytest-args="tests -q"
```

The command records observations in `.mipdelta/runtime/`, then checks the Git delta after pytest returns. It preserves pytest's failure status. Use `--runtime-dir PATH` with a fresh directory when collecting evidence for a new source state, since the check can load earlier observations from that directory.

`[static]` marks a relationship resolved from source. `[type-cand]` marks a candidate inferred from types. `[rt-obs]` uses a call observed during tests. Sampling and test coverage limit runtime observations. A quiet check means the configured analysis emitted no advisory.
