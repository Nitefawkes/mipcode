# Using mip-delta

Run Git checks in a repository with an existing commit. Use `--base-ref` to compare the working tree with an existing ref.

```sh
mip-delta --check-git --root . --base-ref HEAD --json
```

JSON output includes changed paths, findings, and loaded runtime edges. Compact text defaults to a 250-byte advisory budget. The check normally returns zero when it reports findings. Add `--fail-on-findings` to return status 1 for findings.

Use `--root` to limit the analysis to a subdirectory. Git mode reads tracked and non-ignored untracked Python files. Paths stay relative to the top of the repository so imports and reported locations use the same coordinates. A failed Git command, unreadable source, or syntax error in a changed or proposed Python file returns status 2. Text diagnostics go to stderr. With `--json`, an analysis failure returns an object with `status` set to `"error"` and an `error` message.

The [bundled example](../README.md#quick-start) uses candidate mode. It compares a proposed replacement with an existing file and reports locations to review.

At resolved calls, the check can flag new required arguments, removed parameters, renamed keywords, changes to positional binding, and changes between synchronous and async definitions. It also reports surviving calls whose previous resolved definition was removed. Signature binding uses explicit positional and keyword arguments. Decorators, argument unpacking, dynamic dispatch, and indirect imports can limit coverage.

## Observe calls during tests

Use CPython 3.12 or later and install pytest into the same environment. From the MipCode repository root:

```sh
python -m pip install -e ".[test]"
mip-delta --run-pytest --root . --pytest-args="tests -q"
```

The command records observations in `.mipdelta/runtime/`, then checks the Git delta after pytest returns. It preserves pytest's failure status. Use `--runtime-dir PATH` with a fresh directory when collecting evidence for a new source state, since the check can load earlier observations from that directory.

`[static]` marks a relationship resolved from source. `[type-cand]` marks a candidate inferred from types. `[rt-obs]` uses a call observed during tests. Sampling and test coverage limit runtime observations. A quiet check means the configured analysis emitted no advisory.
