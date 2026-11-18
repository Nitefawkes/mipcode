# MipCode

MipCode's `mip-delta` command checks Python changes for call sites, wrappers,
and overrides that may need to change with them. It can also review a Python
repository snapshot and run structural checks on Solidity source. This is a
research preview. Findings are prompts for review.

## Install

Use Python 3.10 or later. The base package has no third-party Python runtime
dependencies. Install Git for Python repository auditing and Git comparisons.

```sh
python -m pip install coherion-mipcode
mip-delta --version
```

## Run

From your project's root, inspect Python source for existing issues:

```sh
mip-delta --root . --audit-repo
```

In a Git checkout, compare your working tree with `HEAD` and get JSON output:

```sh
mip-delta --root . --check-git --json
```

For a Solidity source tree:

```sh
mip-delta --root . --audit-solidity
```

These commands analyze source locally. Runtime observation with
`--run-pytest` is opt-in and requires Python 3.12 or later and pytest.
Model-assisted review with `--review` is also opt-in and uses configured
Anthropic API credentials or an authenticated Claude CLI.

See the [usage guide](https://github.com/Nitefawkes/mipcode/blob/v0.6.4/docs/usage.md)
for CI, pre-commit hooks, and command options, or the
[source repository](https://github.com/Nitefawkes/mipcode) for evaluation results.

MipCode is licensed under Apache-2.0.
