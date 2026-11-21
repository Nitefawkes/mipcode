# Same-defect check

You compare one paid Sherlock audit finding with one report from a static analyzer, for a research study. You see the finding's title and write-up sections, the analyzer report (detector, symbol, location and evidence), the code the finding cites, and the code at the analyzer's location.

Decide whether the analyzer report identifies the same root-cause defect as the finding. The analyzer often reports the peer, base or declaration side of a mismatch rather than the line the auditor cited; that still counts as the same defect when fixing the finding's root cause would also resolve what the analyzer reports, and the analyzer report points at that root cause.

Answer with one verdict:

- `same_defect`: the analyzer report identifies the finding's root-cause defect, at the cited site or at its peer, base or declaration side.
- `related`: the analyzer report concerns the same code or a neighboring concern, but not the finding's root cause.
- `unrelated`: the analyzer report has nothing to do with the finding's defect.

Answer with one JSON object that matches the provided schema: `verdict`, and a `rationale` of at most 300 characters.
