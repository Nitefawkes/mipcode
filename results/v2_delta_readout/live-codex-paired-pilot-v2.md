# Live Codex Paired Gateway Benchmark Readout v2

- Raw episode records retained separately.
- Tasks / Live Episodes Evaluated: **`6` tasks (`12` live episodes)**
- **Stratum 1 (Local Edits):** Arm A Oracle `2/2`, Arm B Oracle `2/2`, Arm B Advisory Bytes = **`0`**
- **Stratum 2 (Contract Mutations):** Arm A Oracle `2/4` -> Arm B Oracle **`4/4`**; Unresolved Contract Seams `3 (Arm A) -> 0 (Arm B)`

| Task ID | Stratum | `A` Calls (`Bytes`) | `B` Calls (`Bytes`) | `B` Adv Bytes (`Uptake`) | `A -> B` Unresolved Seams | `A -> B` Oracle | Emitted `[mip-delta]` Advisory in Arm B |
|---|---|---:|---:|---|---:|---|---|
| `L01-local-cache-ttl` | `1` | 3 (`1,546`) | 3 (`1,546`) | 0 (`False`) | **`0 -> 0`** | **`PASS -> PASS`** | *(silent)* |
| `L02-local-retry-backoff` | `1` | 3 (`788`) | 3 (`788`) | 0 (`False`) | **`0 -> 0`** | **`PASS -> PASS`** | *(silent)* |
| `L03-callback-descriptor-binding` | `2` | 12 (`5,081`) | 10 (`4,250`) | 213 (`True`) | **`0 -> 0`** | **`PASS -> PASS`** | `[mip-delta] http_pkg/client.py:4 required_keyword_missing[static] dump_header; http_pkg/response.py:5 callback_binding_review[static] dump_header; http_pkg/response.py:6 callback_binding_review[static] dump_header` |
| `L04-parallel-state-symmetry` | `2` | 4 (`10,785`) | 4 (`10,761`) | 0 (`False`) | **`0 -> 0`** | **`PASS -> PASS`** | *(silent)* |
| `L05-delegating-wrapper-forwarding` | `2` | 3 (`1,172`) | 8 (`3,519`) | 89 (`True`) | **`1 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] rdf_pkg/dataset.py:8 override_signature_review[static] ConjunctiveGraph.parse` |
| `L06-return-and-exception-contract` | `2` | 4 (`1,413`) | 7 (`2,972`) | 153 (`True`) | **`2 -> 0`** | **`FAIL -> PASS`** | `[mip-delta] auth_pkg/middleware.py:6 return_contract_review[static] verify_token; auth_pkg/middleware.py:6 exception_contract_review[static] verify_token` |
