# Live Codex Generic Contract Reminder Ablation (`Arm A` vs `Arm R` vs `Arm B`)

- Raw episode records retained separately.
- Discordant Contract-Change Tasks Evaluated: **`6`** (`L05`, `L06`, `OSS04`, `OSS05`, `OSS06`, `OSS08`)
- **Oracle Pass Rate:** Arm A (`Control`) **`0/6`** | Arm R (`Generic Reminder`) **`5/6`** | Arm B (`mipdelta` Specific Advisory) **`6/6`**

| Task ID | Stratum | `A` Oracle (`Calls`, `Bytes`) | `R` Generic Reminder Oracle (`Calls`, `Bytes`, `Non-Cached Tok`) | `B` `mipdelta` Oracle (`Calls`, `Bytes`, `Non-Cached Tok`) |
|---|---|---|---|---|
| `L05-delegating-wrapper-forwarding` | `stratum_2_contract_mutation` | **`FAIL`** (3, `1,172`) | **`PASS`** (9, `4,797`, `38,750` tok) | **`PASS`** (8, `3,519`, `33,677` tok) |
| `L06-return-and-exception-contract` | `stratum_2_contract_mutation` | **`FAIL`** (4, `1,413`) | **`PASS`** (11, `6,827`, `21,005` tok) | **`PASS`** (7, `2,972`, `17,060` tok) |
| `OSS04-packaging-parse-requirement-exception` | `stratum_2_oss_static_contract` | **`FAIL`** (7, `27,061`) | **`PASS`** (12, `41,458`, `31,362` tok) | **`PASS`** (12, `41,473`, `36,589` tok) |
| `OSS05-rdflib-runtime-plugin-serializer` | `stratum_3_runtime_monitoring` | **`FAIL`** (4, `1,256`) | **`FAIL`** (8, `5,045`, `20,302` tok) | **`PASS`** (11, `5,555`, `39,425` tok) |
| `OSS06-click-runtime-command-loader` | `stratum_3_runtime_monitoring` | **`FAIL`** (5, `1,952`) | **`PASS`** (11, `6,331`, `38,128` tok) | **`PASS`** (8, `3,509`, `34,148` tok) |
| `OSS08-web-stub-session-chain` | `stratum_4_pyi_type_candidate` | **`FAIL`** (3, `746`) | **`PASS`** (7, `3,032`, `19,534` tok) | **`PASS`** (6, `2,005`, `32,022` tok) |
