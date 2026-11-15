# Cold Repo Bug-Hunter Sweep (`--audit-repo`)

## Question

Does running `mip-delta --audit-repo` statically on **resting `HEAD` snapshots** of mature Python open-source repositories—with **zero diff history**—discover real, unfixed standing contract bugs (`delegating_wrapper_review`, dead `self.<param>` stores, hard `override_signature_review` parameter-kind conflicts) while keeping Stage-1 false-alarm volume bounded enough for `$0.002` Stage-2 `--review` micro-triage?

## Targets (24 Repositories)

Shallow `--depth 1` checkouts of current upstream `HEAD` across 23 popular open-source Python projects plus MipCode's own local checkout (`.`), spanning CLI frameworks, async HTTP/web frameworks, ORMs/serializers, terminal UI, testing tools, and utilities:

1. `httpx` (`encode/httpx`)
2. `marshmallow` (`marshmallow-code/marshmallow`)
3. `rich` (`Textualize/rich`)
4. `attrs` (`python-attrs/attrs`)
5. `mkdocs` (`mkdocs/mkdocs`)
6. `paramiko` (`paramiko/paramiko`)
7. `pytest` (`pytest-dev/pytest`)
8. `requests` (`psf/requests`)
9. `scrapy` (`scrapy/scrapy`)
10. `urllib3` (`urllib3/urllib3`)
11. `click` (`pallets/click`)
12. `flask` (`pallets/flask`)
13. `werkzeug` (`pallets/werkzeug`)
14. `jinja2` (`pallets/jinja`)
15. `fastapi` (`fastapi/fastapi`)
16. `typer` (`fastapi/typer`)
17. `starlette` (`encode/starlette`)
18. `uvicorn` (`encode/uvicorn`)
19. `anyio` (`agronholm/anyio`)
20. `tenacity` (`jd/tenacity`)
21. `aiohttp` (`aio-libs/aiohttp`)
22. `black` (`psf/black`)
23. `textual` (`Textualize/textual`)
24. `mipcode` (`Nitefawkes/mipcode`, local workspace `.`)

## Protocol

1. **Stage 1 (`$0` API cost):** For each repository, run `mip-delta --root <checkout> --audit-repo --json` via `experiments/cold_audit/run_sweep.py`. Record upstream URL, checked-out `HEAD` SHA, `files_scanned`, `skipped_unparsed_paths`, `candidate_calls_inspected`, deterministic `wall_ms`, and every Stage-1 finding.
2. **Noise gate:** Stop before Stage 2 if any single repository exceeds 50 Stage-1 findings so noisy deterministic patterns are investigated before spending review tokens.
3. **Stage 2 (`--review` + manual verification):** On every repository with `>= 1` Stage-1 finding, run `mip-delta --root <checkout> --audit-repo --review --json` (invoking `claude` Haiku only on non-empty findings) AND read every flagged site and callee target in source to adjudicate true standing bugs, review-worthy seams, and false alarms.
4. **Sanitized reporting:** Persist aggregate + per-repository results in `results/cold_audit/summary.json` and `results/cold_audit/README.md` with relative repository paths only (no local drive paths).

## Running the Sweep

```sh
python experiments/cold_audit/run_sweep.py --work-dir C:\tmp\cold-audit-repos
python experiments/cold_audit/run_sweep.py --work-dir C:\tmp\cold-audit-repos --review
```
