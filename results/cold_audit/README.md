# Cold Repo Bug-Hunter Sweep Results (`mip-delta --audit-repo`)

> **Zero-diff snapshot audit at rest:** Evaluated [`experiments/cold_audit/run_sweep.py`](../../experiments/cold_audit/run_sweep.py) ([protocol](../../experiments/cold_audit/README.md)) across **24 Python repositories** (`5,719` production files, `77,324` candidate call sites) at current upstream `HEAD` with **no diff history** (`0` baseline commits required). Full machine-readable output is stored in [`summary.json`](summary.json).

---

## Summary

- **Quiet on mature codebases:** **20 of 24 repositories (`83.3%`)** emitted `0` Stage-1 findings; the remaining `4` repositories emitted `12` total Stage-1 findings (`rich`: `9`, `click`: `1`, `typer`: `1`, `mipcode`: `1`). Median Stage-1 scan time was **`1,274.2 ms`** per repository.
- **High deterministic precision without a diff:** Manual inspection of all `12` emitted findings confirmed **`10 / 12` (`83.3%`)** represent real standing defects or dead configuration sinks (`9` in `rich/traceback.py`, `1` in `src/mipdelta/gateway.py`).
- **Stage-2 `--review` reaches `100%` precision for `$0.0124` total:** Running `--review` (`claude_cli` / `claude-haiku-5-5`) strictly on the `4` flagged repositories cost **`$0.012381`** combined, suppressed both benign `LazyFile` false alarms (`click`, `typer` — reading the injected `[callee contract]` for `open_stream` to see `"-" ignores atomic`), and retained both repositories with genuine standing defects (`rich`: `confirm_omission`, `mipcode`: `review_worthy`).

---

## Verified Standing Contract Defects

### 1. `Textualize/rich` (`3a109ccdffe5`) — `rich/traceback.py` (`9` Stage-1 findings, Stage-2: `confirm_omission`)

1. **`Traceback.__init__` drops 5 `locals_*` options when extracting inside `except:` blocks (`rich/traceback.py:309`, 5 `delegating_wrapper_review` findings):**
   - `Traceback.__init__` (lines `283-311`) accepts `locals_max_length`, `locals_max_string`, `locals_max_depth`, `locals_hide_dunder`, and `locals_hide_sunder`, yet when `trace is None` it calls `self.extract(exc_type, exc_value, traceback, show_locals=show_locals)` without forwarding any of those five `locals_*` arguments (whereas sibling `@classmethod def from_exception` at lines `389-399` forwards all five).
2. **`Traceback.extract` drops 2 `locals_*` options recursively on `ExceptionGroup`s (`rich/traceback.py:487`, 2 `delegating_wrapper_review` findings):**
   - Inside Python 3.11+ `BaseExceptionGroup` / `ExceptionGroup` recursion (lines `479-497`), the recursive call to `Traceback.extract(...)` forwards `locals_max_length`, `locals_hide_dunder`, and `locals_hide_sunder`, yet silently omits **`locals_max_depth`** and **`locals_max_string`**.
3. **Dead self-stores on `Traceback.__init__` (`rich/traceback.py:283-338`, 2 `parameter_sink_review` findings):**
   - `Traceback.__init__` assigns `self.locals_hide_dunder = locals_hide_dunder` and `self.locals_hide_sunder = locals_hide_sunder` (lines `323-324`), but dunder/sunder filtering occurs strictly inside `extract()`; neither attribute is read anywhere in `rich`.

### 2. `Nitefawkes/mipcode` (`238c50947035` working tree) — `src/mipdelta/gateway.py` (`1` Stage-1 finding, Stage-2: `review_worthy`)

- **`ContainedGatewaySession.__init__` dead cap parameter (`src/mipdelta/gateway.py:354-383`, `parameter_sink_review`):**
  - Accepts `max_visible_bytes: int = 4 * 1024 * 1024` and assigns `self.max_visible_bytes = max_visible_bytes` at line `383`, but no reader exists anywhere in the repository (unlike sibling `self.max_response_bytes`).

### Suppressed Benign Findings (`2 / 12` Stage-1 False Alarms)

- **`pallets/click` (`e38188e9e205`) — `src/click/utils.py:131` (`delegating_wrapper_review`, Stage-2: `suppress_false_alarm`):** `_LazyFile.__init__` omits `atomic` in its `if self.name == "-":` call to `open_stream(...)`. By reading the injected `[callee contract]` for `open_stream`, `--review` saw that `open_stream` ignores `atomic` and returns early on standard stream `"-"`, while `_LazyFile.open()` (line `157`) passes `atomic=self.atomic` for regular files.
- **`fastapi/typer` (`73500169e87b`) — `typer/_click/utils.py:135` (`delegating_wrapper_review`, Stage-2: `suppress_false_alarm`):** Typer's vendored copy (`typer/_click/utils.py`) of Click's `_LazyFile.__init__` (`atomic` omitted only on the `"-"` branch of `open_stream`), likewise correctly suppressed by `--review`.

---

## Per-Repository Sweep Results (`24` Repositories)

| Repository | `HEAD` SHA | Files Scanned | Candidate Calls | Stage-1 Wall (`ms`) | Stage-1 Findings | Stage-2 `--review` Verdict (Cost) | Manual Adjudication |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `httpx` | `b5addb64f016` | 60 | 1,391 | 663.5 | 0 | — (`$0`) | Silent |
| `marshmallow` | `777ff66039fd` | 38 | 731 | 765.0 | 0 | — (`$0`) | Silent |
| `rich` | `3a109ccdffe5` | 212 | 3,325 | 1,910.5 | **9** | `confirm_omission` (`$0.008752`) | **9/9 real standing bugs** |
| `attrs` | `2a1fb86d4481` | 56 | 723 | 748.5 | 0 | — (`$0`) | Silent |
| `mkdocs` | `2862536793b3` | 65 | 1,507 | 954.9 | 0 | — (`$0`) | Silent |
| `paramiko` | `0e9ccb91e0f9` | 70 | 3,734 | 1,010.7 | 0 | — (`$0`) | Silent |
| `pytest` | `fc9a7ef678f6` | 274 | 7,770 | 4,805.8 | 0 | — (`$0`) | Silent |
| `requests` | `3c7baaa05ab4` | 37 | 907 | 612.0 | 0 | — (`$0`) | Silent |
| `scrapy` | `e63a574c767d` | 546 | 7,909 | 6,229.7 | 0 | — (`$0`) | Silent |
| `urllib3` | `d9f9b0ad5b99` | 83 | 2,072 | 1,325.2 | 0 | — (`$0`) | Silent |
| `click` | `e38188e9e205` | 91 | 1,861 | 1,274.2 | 1 | `suppress_false_alarm` (`$0.001102`) | 1 benign (`LazyFile` `"-"`) |
| `flask` | `49943b5a9441` | 83 | 1,061 | 635.6 | 0 | — (`$0`) | Silent |
| `werkzeug` | `0f43c6ebd426` | 142 | 3,462 | 1,351.5 | 0 | — (`$0`) | Silent |
| `jinja2` | `5ef70112a1ff` | 60 | 2,822 | 928.7 | 0 | — (`$0`) | Silent |
| `fastapi` | `913c68062c89` | 1,223 | 2,641 | 4,722.5 | 0 | — (`$0`) | Silent |
| `typer` | `73500169e87b` | 643 | 2,511 | 1,443.6 | 1 | `suppress_false_alarm` (`$0.001233`) | 1 benign (vendored `LazyFile` `"-"`) |
| `starlette` | `271c277da0ff` | 84 | 1,687 | 963.1 | 0 | — (`$0`) | Silent |
| `uvicorn` | `a99c1b76ee9c` | 84 | 1,870 | 807.8 | 0 | — (`$0`) | Silent |
| `anyio` | `4d53ea15daec` | 194 | 2,701 | 1,411.7 | 0 | — (`$0`) | Silent |
| `tenacity` | `aac059c90617` | 20 | 379 | 419.3 | 0 | — (`$0`) | Silent |
| `aiohttp` | `fe099491d275` | 173 | 4,499 | 5,053.9 | 0 | — (`$0`) | Silent |
| `black` | `b10cbe43b816` | 422 | 3,532 | 4,399.3 | 0 | — (`$0`) | Silent (`18` unparsed fixtures skipped) |
| `textual` | `5e5b7ef58b8c` | 987 | 11,829 | 7,704.1 | 0 | — (`$0`) | Silent |
| `mipcode` | `238c50947035` | 72 | 6,400 | 1,127.3 | 1 | `review_worthy` (`$0.001294`) | **1 real dead sink** (`max_visible_bytes`) |
| **Total (24)** | — | **5,719** | **77,324** | median **1,274.2** | **12** (`83.3%` real) | **`$0.012381`** (`2` kept, `2` suppressed) | **`10/10` kept real (`100%`)** |
