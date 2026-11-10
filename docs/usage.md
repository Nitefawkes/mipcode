# Using mip-delta

Run Git checks in a repository with an existing commit. Use `--base-ref` to compare the working tree with an existing ref.

```sh
mip-delta --check-git --root . --base-ref HEAD --json
```

JSON output includes changed paths, findings, and loaded runtime edges. Compact text defaults to a 250-byte advisory budget. The check normally returns zero when it reports findings. Add `--fail-on-findings` to return status 1 for findings.

The [bundled example](../README.md#quick-start) uses candidate mode. It compares a proposed replacement with an existing file and reports locations to review.

## Observe calls during tests

Use CPython 3.12 or later and install pytest into the same environment. From the MipCode repository root:

```sh
python -m pip install -e ".[test]"
mip-delta --run-pytest --root . --pytest-args="tests -q"
```

The command records observations in `.mipdelta/runtime/`, then checks the Git delta after pytest returns. It preserves pytest's failure status. Use `--runtime-dir PATH` with a fresh directory when collecting evidence for a new source state, since the check can load earlier observations from that directory.

`[static]` marks a relationship resolved from source. `[type-cand]` marks a candidate inferred from types. `[rt-obs]` uses a call observed during tests. Sampling and test coverage limit runtime observations. A quiet check means the configured analysis emitted no advisory.
