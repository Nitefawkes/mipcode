# Stale-file mutation recall protocol

The [history replay](../history_replay/README.md) measures how often `mip-delta` stays silent on complete upstream commits. That is a precision measure. This study is its recall counterpart. It builds edits that are known to be incomplete, with ground truth that does not come from the tool under test, and counts how many of the stale sites the tool reports.

It needs no model calls. The target repository is read through Git plumbing only. Nothing is checked out, imported, or executed.

## How a mutation is built

For each non-merge commit C with parent P:

1. **Changed definitions.** The script parses the production Python files that C changed, at P and at C, with the standard library `ast` module. It matches module-level functions and methods by path and qualified name. A definition is kept when its parameter list differs: names, order, kind, or whether a parameter has a default. A definition present at P and absent at C is kept as removed when its name is no longer defined anywhere in C's production files, as a function, class, or module- or class-level assignment. Dunder names are excluded.
2. **Co-updated call sites.** In each other production file Y that exists at P and at C and that C changed, the script finds calls in P's version of Y whose callee name (`f(...)` or `x.f(...)`) is the changed definition's name. A call is a ground-truth site when one of its lines, from the callee name to the closing parenthesis, was replaced or deleted by C, or when C inserted lines inside it. The line diff uses `difflib.SequenceMatcher`. These are sites that upstream edited in the same commit as the definition.
3. **Mutation.** M is C's tree with Y replaced by P's blob of Y. There is one mutation per commit and file; it can hold several sites. M stands for "the author changed the definition and everything else, but forgot Y".

## Strata

Each site gets two labels computed without any `mip-delta` code.

**Binding.** The script builds an `inspect.Signature` from the new definition's parameters and binds the old call's shape to it: the number of positional arguments and the keyword names.

| Value | Meaning |
| --- | --- |
| `breaking` | The old call does not bind to the new signature |
| `compatible` | The old call still binds |
| `unknown` | The old call uses `*` or `**` unpacking |
| `breaking_removed` | The definition was removed |
| `removed_shimmed` | The definition was removed, but a module-level `__getattr__` in its file names it, usually a deprecation shim that keeps the old name working |

For a method that is not a `@staticmethod`, called as `x.f(...)`, the first parameter is dropped before binding, unless the call is an explicit unbound call such as `Base.f(self, ...)`.

**Name uniqueness.** `unique` means the leaf name is defined exactly once, as a function or class at any depth, among the production files at P, and is not also a builtin or a method of a builtin type such as `dict.get` or `list.append`. Every other site is `ambiguous`, with the reason in `uniqueness_reason`. Leaf-name matching is noisy, so the unique stratum is the high-confidence ground truth. Both strata are always reported.

## Scoring

The script analyzes P to M and P to C with the default detector set, using the same pattern as the history replay: changed files first, then the whole tree when a cross-file contract changed. A finding is new when it appears for M and not for C, keyed by detector, symbol, path, and enclosing function.

| Measure | Hit when |
| --- | --- |
| `site_hit` | A new finding in Y is on the call's lines or inside the call's enclosing function |
| `file_hit` | Any new finding is in Y |

New findings outside Y are recorded as `collateral`. Rates are reported per stratum at the site level, at the mutation level (a mutation counts in a stratum when it holds at least one site of that stratum), and as the number of distinct commits involved, because one commit can contribute dozens of sites.

## Limits

- **The omissions are constructed.** Every mutation is of the kind the tool models: a signature change or a removal, with an upstream edit at the call. Recall here is an upper bound for real omissions, which also include changes the tool does not model and omissions nobody fixed in the same commit.
- **Return-shape, exception, and behavior changes are not covered.** Only parameter lists and removals define ground truth. Changes to `__init__` and other dunder methods are excluded, so constructor calls are never ground truth.
- **Leaf-name matching is noisy outside the unique stratum.** A call `x.update(...)` matches every changed `update`, including `dict.update`. Even in the unique stratum the receiver is not resolved: a unique leaf name can still be called on an object from another library.
- **Co-location is not causation.** A call on a line that C edited for an unrelated reason counts as a site. `call_rewritten_in_commit` records whether the identical call disappeared from Y at C; a site where it is false is weaker evidence.
- **The binding label ignores how the definition is reached.** An explicit unbound call is recognized only when the receiver looks like a class and the first argument is `self` or `cls`, as in `Base.method(self, ...)`. Decorators that change the signature, `functools.wraps` wrappers, and `*args` forwarding are not modeled.
- **Removed is a static judgment.** A removed function may still be reachable through a re-export from another module, a `__getattr__` in a different file, or dynamic registration. Only a `__getattr__` in the definition's own file is detected.
- **Moved definitions are invisible.** A function moved to a different path with a new signature is neither changed nor removed.
- **Definitions that occur more than once** under the same qualified name in one file, such as conditional definitions and property setters, are skipped. `@overload` stubs are ignored.
- **Clustering.** A single refactoring commit can supply most of a repository's sites. Read the commit counts alongside the site counts.
- **Only the stale file is reverted.** A real omission can leave several files stale at once; M has exactly one.
- Shallow clones use only commits whose parents are present.

## Run it

```sh
python experiments/recall_mutation/build_and_score.py \
  --repo ../oss/packaging --repo ../oss/click \
  --limit 400 --out ./artifacts/recall-mutation
```

`--limit N` uses the most recent N non-merge commits. `--enable DETECTOR` also runs an opt-in detector, as in the history replay. Use a new output directory for each run.

The script writes `<repository>/corpus.json` for each repository and a `summary.json` with counts and rates by stratum, per repository and pooled, and prints the same tables. Each mutation in a corpus records the full commit and parent ids, `stale_path`, `stale_blob` (the parent's blob of that file), and `commit_blob`. M is the commit's tree with `stale_path` set to `stale_blob`, so another script can materialize it with `git cat-file` and run a type checker on it. Each site records the changed definition with its old and new parameter lists, the call's line in the stale file, its source text, both strata, the hit flags, and the detectors that hit.
