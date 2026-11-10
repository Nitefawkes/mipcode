# Live Codex Multi-Stratum OSS & Runtime/Type Gateway Benchmark v2

- Raw episode records retained separately.
- Tasks / Live Episodes Evaluated: **`8` tasks (`16` live episodes)**
- **Stratum 1 (Real OSS Local Edits — `platformdirs`, `werkzeug`):** Arm A Oracle `2/2`, Arm B Oracle `2/2`, Arm B Advisory Bytes = **`0`**
- **Strata 2–4 (Real OSS Static `[static]` + `sys.monitoring` `[rt-obs]` + `.pyi` `[type-cand]`):** Arm A Oracle `2/6` -> Arm B Oracle **`6/6`**; Unresolved Contract Seams `4 (Arm A) -> 0 (Arm B)`

| Task ID | Stratum | Pkg (Files) | `A` Calls (`Bytes`) | `B` Calls (`Bytes`) | `B` Adv Bytes (`Uptake`) | `A -> B` Unresolved | `A -> B` Oracle | Emitted `[mip-delta]` Advisory in Arm B |
|---|---|---|---|---:|---|---:|---|---|
| `OSS01-platformdirs-xdg-unique` | `stratum_1_oss_local_edit` | `platformdirs` (12) | 4 (`7,777`) | 4 (`7,004`) | 0 (`False`) | **`0 -> 0`** | **`PASS -> PASS`** | *(silent)* |
| `OSS02-werkzeug-response-freeze` | `stratum_1_oss_local_edit` | `werkzeug` (54) | 5 (`15,758`) | 5 (`16,266`) | 0 (`False`) | **`0 -> 0`** | **`PASS -> PASS`** | *(silent)* |
| `OSS03-werkzeug-dump-header-callbacks` | `stratum_2_oss_static_contract` | `werkzeug` (52) | 17 (`60,758`) | 14 (`59,924`) | 241 (`True`) | **`0 -> 0`** | **`PASS -> PASS`** | `[mip-delta] werkzeug/sansio/response.py:713 callback_binding_review[static] dump_header; werkzeug/sansio/response.py:720 callback_binding_review[static] dump_header; werkzeug/sansio/response.py:732 callback_binding_review[static] dump_header` |
| `OSS04-packaging-parse-requirement-exception` | `stratum_2_oss_static_contract` | `packaging` (22) | 7 (`27,061`) | 12 (`41,473`) | 197 (`True`) | **`1 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] packaging/requirements.py:102 exception_contract_review[static] parse_requirement (parse_requirement newly raises EmptyRequirementError, bypassing caller try/except (ParserSyntaxError))` |
| `OSS05-rdflib-runtime-plugin-serializer` | `stratum_3_runtime_monitoring` | `rdflib_pkg` (10) | 4 (`1,256`) | 11 (`5,555`) | 430 (`True`) | **`1 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] rdflib_pkg/exporter.py:7 delegating_wrapper_review[rt-obs] GraphExporter.serialize (delegating wrapper GraphExporter.serialize calls NTSerializer.serialize without forwarding optional parameter base_iri)` |
| `OSS06-click-runtime-command-loader` | `stratum_3_runtime_monitoring` | `cli_pkg` (9) | 5 (`1,952`) | 8 (`3,509`) | 197 (`True`) | **`1 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] cli_pkg/dispatcher.py:8 exception_contract_review[rt-obs] OptionValidator.validate (OptionValidator.validate newly raises FlagSyntaxError, bypassing caller try/except (ValidationError))` |
| `OSS07-docutils-stub-reporter-chain` | `stratum_4_pyi_type_candidate` | `docutils` (109) | 12 (`50,387`) | 11 (`49,073`) | 202 (`True`) | **`0 -> 0`** | **`PASS -> PASS`** | `[mip-delta] docutils/parsers/__init__.py:87 required_keyword_missing[type-cand] Reporter.attach_observer; docutils/transforms/__init__.py:183 required_keyword_missing[type-cand] Reporter.attach_observer` |
| `OSS08-web-stub-session-chain` | `stratum_4_pyi_type_candidate` | `web_pkg` (9) | 3 (`746`) | 6 (`2,005`) | 158 (`True`) | **`1 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] web_pkg/views.py:5 return_contract_review[type-cand] TokenStore.issue (caller unpacks 2 elements from TokenStore.issue, which now returns 3-tuple)` |
