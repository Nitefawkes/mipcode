# History replay protocol

This offline study replays the commit history of a Python repository through `mip-delta`. It needs no model calls. Each non-merge commit is treated as one edit: the parent tree is the baseline and the commit tree is the result.

Upstream maintainers reviewed and merged each commit, so a commit is a reasonable stand-in for a change that its authors considered complete. That gives two measurements the paired agent studies cannot provide at this scale.

- **Silence on complete changes.** Of the commits that change a contract other files can depend on, how many produce no report? A report on such a commit is either a false report or a latent problem upstream.
- **What later commits did.** For each reported site, the replay looks ahead a fixed number of commits and records whether the report disappeared, and whether the enclosing function was edited when it did.

## What the replay does

For each commit that changes production Python, the script reads the changed files first. If none of them changes a contract visible to other files, the commit is local and only intra-file findings can appear. Otherwise it analyzes the whole tree at the parent and at the commit.

A contract-changing commit is one where a function was removed, its parameters or async kind changed, a fixed tuple return changed arity or became nullable, or it raises a new exception type.

Findings in test, documentation, example, and benchmark paths are excluded from the counts. Test files still take part in the analysis.

For each reported site, the script compares the original parent with each of the next commits, up to the horizon. When the site is no longer reported, the resolution is one of:

| Resolution | Meaning |
| --- | --- |
| `site_function_edit` | The function containing the site changed in that commit |
| `same_file_other_edit` | The file changed, but that function did not |
| `file_edit` | The file changed and the site is at module or class level |
| `target_side_change` | The site's file did not change, so the changed definition did |
| `open_at_horizon` | Still reported after the last commit examined |

## Limits

- A later edit to the enclosing function is weak evidence. Functions change for many reasons, and a site in the changed function itself is edited again for unrelated work. Read the resolving commit before counting a site as a confirmed omission.
- The selected repositories are small, typed, well-tested libraries. Larger applications and untyped code may behave differently.
- Shallow clones replay only the commits whose parents are present.
- A silent commit says nothing about omissions the check cannot see.

## Run it

Clone the repositories you want to replay, then pass each one with `--repo`. Use a new output directory for each run.

```sh
python experiments/history_replay/replay_history.py \
  --repo ../oss/packaging --repo ../oss/click \
  --horizon 25 --out ./artifacts/history-replay
```

The script reads the target repositories through Git plumbing. It does not check out, import, or execute their code. It writes one JSON report per repository and a `summary.json` with totals and resolutions by detector. `--limit N` replays only the most recent N commits.

The [results](../../results/history_replay/README.md) describe one run over five repositories and the changes made in response.
