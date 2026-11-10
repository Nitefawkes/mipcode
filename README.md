# MipCode v0.6 research preview

MipCode checks for possible cross-file Python contract breaks after a coding agent edits code or runs tests. The `mip-delta` command points to the affected file and line. For example, it can flag a caller that omits a newly required keyword argument. The agent can keep using its usual search and editing tools.

The preview covers selected call, override, callback, return, and exception patterns. Treat findings as review prompts. Coverage depends on what the tool can resolve from source, type information, or executed tests.

## Quick start

From the repository root, activate a fresh virtual environment with Python 3.10 or later.

```sh
python -m pip install -e .
mip-delta --help
mip-delta --check-git --root . --json
```

The Git check compares the working tree with `HEAD` by default. A clean checkout returns an empty findings list. Omit `--json` for compact advisory text.

Try a bundled example that adds a required keyword. The command checks the proposed edit in memory.

```sh
mip-delta --root examples/contract-delta/before --write-path formatter.py --candidate-file examples/contract-delta/after/formatter.py
```

The advisory points to `consumer.py:5` and reports `required_keyword_missing`. The [usage guide](docs/usage.md) covers Git checks and observing calls during tests.

## Research and development

- [Engineering article](docs/engineering_blog_post_mipdelta.md) explains the move from search tools to checks after edits and tests.
- [Evidence index](results/v2_delta_readout/README.md) links the reports, data, and study limits.
- [One-shot brief pilot](results/one_shot/README.md) compares no brief, a lexical map, and a Semantic LOD brief.
- [Documentation index](docs/README.md) covers the preview and the earlier architecture.

The paired evaluations cover 14 selected tasks. A generic reminder recovered five of six selected historical misses in a later diagnostic comparison. The added benefit of cross-file analysis over that reminder still needs a fresh comparison.

```sh
python -m pip install -e ".[test]"
python -m pytest tests -q
```

[Apache 2.0 license](LICENSE)
