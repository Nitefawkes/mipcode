"""Compare a regression replay with an earlier replay, using the earlier site labels.

Usage: compare_replays.py OLD_REPLAY_DIR NEW_REPLAY_DIR ADJUDICATION_JSON [--json OUT]

The earlier replay ran with removed_callable_review and callback_binding_review on.
The new one runs the current default set, which leaves callback_binding_review off, so
callback sites are dropped from the earlier side before comparing.
"""
import collections
import json
import sys
from pathlib import Path

OFF_NOW = {"callback_binding_review"}


def load(directory):
    out = {}
    for path in sorted(Path(directory).glob("*.json")):
        if path.name != "summary.json":
            report = json.loads(path.read_text(encoding="utf-8"))
            out[report["repository"]] = report
    return out


def site_keys(report, drop=()):
    keys = {}
    for commit in report["flagged_commits"]:
        for site in commit["sites"]:
            if site["detector"] in drop:
                continue
            key = (report["repository"], commit["commit"], site["detector"], site["path"], site["line"], site["symbol"], site.get("evidence", ""))
            keys[key] = (commit["contract_changing"], site.get("evidence", ""))
    return keys


def main():
    old_dir, new_dir, adjudication = sys.argv[1:4]
    old, new = load(old_dir), load(new_dir)
    labels = {}
    rows = json.loads(Path(adjudication).read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows["sites"]
    for row in rows:
        key = (row["repository"], row["commit"], row["detector"], row["site_path"], row["site_line"], row["changed_symbol"], row["tool_message"])
        labels[key] = row["final_label"]

    result = {"repositories": {}, "pooled": {}}
    pooled = collections.Counter()
    removed_by = collections.Counter()
    kept_by = collections.Counter()
    added = []
    removed_real = []
    for name in sorted(new):
        old_keys = site_keys(old[name], OFF_NOW)
        new_keys = site_keys(new[name])
        assert old[name]["head"] == new[name]["head"], name
        assert old[name]["contract_changing_commits"] == new[name]["contract_changing_commits"], name
        contract = new[name]["contract_changing_commits"]
        old_flagged = len({k[1] for k, (cc, _) in old_keys.items() if cc})
        new_flagged = new[name]["contract_changing_commits_flagged"]
        assert new_flagged == len({k[1] for k, (cc, _) in new_keys.items() if cc}), name
        row = {
            "contract_changing_commits": contract,
            "silent_before": contract - old_flagged,
            "silent_after": contract - new_flagged,
            "sites_before": len(old_keys),
            "sites_after": len(new_keys),
            "analysis_errors": new[name]["analysis_errors"],
        }
        result["repositories"][name] = row
        for field, value in row.items():
            pooled[field] += value
        for key in old_keys.keys() - new_keys.keys():
            label = labels.get(key, "not_read")
            removed_by[(key[2], label)] += 1
            if label in {"real_omission", "review_worthy"}:
                removed_real.append(key)
        for key in old_keys.keys() & new_keys.keys():
            kept_by[(key[2], labels.get(key, "not_read"))] += 1
        for key in new_keys.keys() - old_keys.keys():
            added.append((key, new_keys[key][1]))
    result["pooled"] = dict(pooled)
    result["removed_sites"] = {f"{d}|{l}": n for (d, l), n in sorted(removed_by.items())}
    result["kept_sites"] = {f"{d}|{l}": n for (d, l), n in sorted(kept_by.items())}
    result["added_sites"] = [{"key": list(k), "evidence": e} for k, e in sorted(added)]
    result["removed_real_or_review_worthy"] = [list(k) for k in sorted(removed_real)]
    text = json.dumps(result, indent=2)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(text + "\n", encoding="utf-8")
    print(text)


main()
