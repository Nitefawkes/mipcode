# Cold Repo Bug-Hunter Sweep Results (`mip-delta --audit-repo`)

> **Zero-diff snapshot audit at rest:** Evaluated [`experiments/cold_audit/run_sweep.py`](../../experiments/cold_audit/run_sweep.py) ([protocol](../../experiments/cold_audit/README.md)) across **24 Python repositories** (`5,708` production files, `77,052` candidate call sites) at upstream `HEAD` with **no diff history** (`0` baseline commits required). Full machine-readable output is stored in [`summary.json`](summary.json).

---

## Summary

- **Quiet on mature codebases:** **22 of 24 repositories (`91.7%`)** emitted `0` Stage-1 findings; the remaining `2` repositories emitted `10` total Stage-1 findings (`rich`: `9`, `click`: `1`). Median Stage-1 scan time was **`1,355.3 ms`** per repository.
- **High deterministic precision without a diff:** **`9 / 10` (`90.0%`)** Stage-1 findings are real standing bugs in `Textualize/rich` (`rich/traceback.py`).
- **Stage-2 `--review` reaches `100%` precision for `$0.009854` total:** Running `--review` (`claude_cli` / `claude-haiku-5-5`) on the `2` flagged repositories (`$0.008752` for `rich`, `$0.001102` for `click`, **`$0.009854`** combined) suppressed the benign `LazyFile` stdin `"-"` alarm in `click` (reading the injected `[callee contract]` for `open_stream`) and retained `rich` (`confirm_omission`, `9/9` real bugs).
- **Standing fixes since initial sweep:** The initial sweep (`mipdelta@238c50947035`) flagged MipCode's own `ContainedGatewaySession.__init__` storing an unread `self.max_visible_bytes` quota (`1` finding, `review_worthy`) and `fastapi/typer`'s vendored `typer/_click/utils.py` shim (`1` benign finding, suppressed by `--review`). Enforcing `self.max_visible_bytes` in [`src/mipdelta/gateway.py`](../../src/mipdelta/gateway.py) and adding `_click` to `_VENDORED_DIRS` in [`src/mipdelta/delta.py`](../../src/mipdelta/delta.py) reduced both `mipcode` and `typer` to **`0` Stage-1 findings**.

---

## Verified Standing Contract Defects

### 1. `Textualize/rich` (`3a109ccdffe5`) — `rich/traceback.py` (`9` Stage-1 findings, Stage-2: `confirm_omission`)

1. **`Traceback.__init__` drops 5 `locals_*` options when extracting inside `except:` blocks (`rich/traceback.py:309`, 5 `delegating_wrapper_review` findings):**
   - `Traceback.__init__` (lines `283-311`) accepts `locals_max_length`, `locals_max_string`, `locals_max_depth`, `locals_hide_dunder`, and `locals_hide_sunder`, yet when `trace is None` it calls `self.extract(exc_type, exc_value, traceback, show_locals=show_locals)` without forwarding any of those five `locals_*` arguments (whereas sibling `@classmethod def from_exception` at lines `389-399` forwards all five).
2. **`Traceback.extract` drops 2 `locals_*` options recursively on `ExceptionGroup`s (`rich/traceback.py:487`, 2 `delegating_wrapper_review` findings):**
   - Inside Python 3.11+ `BaseExceptionGroup` / `ExceptionGroup` recursion (lines `479-497`), the recursive call to `Traceback.extract(...)` forwards `locals_max_length`, `locals_hide_dunder`, and `locals_hide_sunder`, yet silently omits **`locals_max_depth`** and **`locals_max_string`**.
3. **Dead self-stores on `Traceback.__init__` (`rich/traceback.py:283-338`, 2 `parameter_sink_review` findings):**
   - `Traceback.__init__` assigns `self.locals_hide_dunder = locals_hide_dunder` and `self.locals_hide_sunder = locals_hide_sunder` (lines `323-324`), but dunder/sunder filtering occurs strictly inside `extract()`; neither attribute is read anywhere in `rich`.

### Suppressed Benign Finding (`1 / 10` Stage-1 False Alarm)

- **`pallets/click` (`e38188e9e205`) — `src/click/utils.py:131` (`delegating_wrapper_review`, Stage-2: `suppress_false_alarm`):** `_LazyFile.__init__` omits `atomic` in its `if self.name == "-":` call to `open_stream(...)`. By reading the injected `[callee contract]` for `open_stream`, `--review` saw that `open_stream` ignores `atomic` and returns early on standard stream `"-"`, while `_LazyFile.open()` (line `157`) passes `atomic=self.atomic` for regular files.

---

## Per-Repository Sweep Results (`24` Repositories)

| Repository | `HEAD` SHA | Files Scanned | Candidate Calls | Stage-1 Wall (`ms`) | Stage-1 Findings | Stage-2 `--review` Verdict (Cost) | Manual Adjudication |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `httpx` | `b5addb64f016` | 60 | 1,391 | 758.3 | 0 | — (`$0`) | Silent |
| `marshmallow` | `777ff66039fd` | 38 | 731 | 798.3 | 0 | — (`$0`) | Silent |
| `rich` | `3a109ccdffe5` | 212 | 3,325 | 1,857.9 | **9** | `confirm_omission` (`$0.008752`) | **9/9 real standing bugs** |
| `attrs` | `2a1fb86d4481` | 56 | 723 | 833.6 | 0 | — (`$0`) | Silent |
| `mkdocs` | `2862536793b3` | 65 | 1,507 | 980.3 | 0 | — (`$0`) | Silent |
| `paramiko` | `0e9ccb91e0f9` | 70 | 3,734 | 970.1 | 0 | — (`$0`) | Silent |
| `pytest` | `fc9a7ef678f6` | 274 | 7,770 | 4,801.6 | 0 | — (`$0`) | Silent |
| `requests` | `3c7baaa05ab4` | 37 | 907 | 573.0 | 0 | — (`$0`) | Silent |
| `scrapy` | `e63a574c767d` | 546 | 7,909 | 7,001.8 | 0 | — (`$0`) | Silent |
| `urllib3` | `d9f9b0ad5b99` | 83 | 2,072 | 1,612.3 | 0 | — (`$0`) | Silent |
| `click` | `e38188e9e205` | 91 | 1,861 | 1,087.2 | 1 | `suppress_false_alarm` (`$0.001102`) | 1 benign (`LazyFile` `"-"`) |
| `flask` | `49943b5a9441` | 83 | 1,061 | 658.8 | 0 | — (`$0`) | Silent |
| `werkzeug` | `0f43c6ebd426` | 142 | 3,462 | 1,561.9 | 0 | — (`$0`) | Silent |
| `jinja2` | `5ef70112a1ff` | 60 | 2,822 | 1,087.8 | 0 | — (`$0`) | Silent |
| `fastapi` | `913c68062c89` | 1,223 | 2,641 | 4,781.9 | 0 | — (`$0`) | Silent |
| `typer` | `73500169e87b` | 629 | 1,713 | 1,355.3 | 0 | — (`$0`) | Silent (`_click/` excluded) |
| `starlette` | `271c277da0ff` | 84 | 1,687 | 1,159.7 | 0 | — (`$0`) | Silent |
| `uvicorn` | `a99c1b76ee9c` | 84 | 1,870 | 900.0 | 0 | — (`$0`) | Silent |
| `anyio` | `4d53ea15daec` | 194 | 2,701 | 1,679.9 | 0 | — (`$0`) | Silent |
| `tenacity` | `aac059c90617` | 20 | 379 | 332.8 | 0 | — (`$0`) | Silent |
| `aiohttp` | `fe099491d275` | 173 | 4,499 | 4,394.5 | 0 | — (`$0`) | Silent |
| `black` | `b10cbe43b816` | 422 | 3,532 | 5,647.5 | 0 | — (`$0`) | Silent (`18` unparsed fixtures skipped) |
| `textual` | `5e5b7ef58b8c` | 987 | 11,829 | 7,828.9 | 0 | — (`$0`) | Silent |
| `mipcode` | `917dd7154ceb` | 75 | 6,926 | 1,400.1 | 0 | — (`$0`) | Silent (`max_visible_bytes` fixed) |
| **Total (24)** | — | **5,708** | **77,052** | median **1,355.3** | **10** (`90.0%` real) | **`$0.009854`** (`1` kept, `1` suppressed) | **`9/9` kept real (`100%`)** |
