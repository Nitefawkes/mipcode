# Representative `pytest` `sys.monitoring` Runner Benchmark v2

- Python: `3.12.6 (tags/v3.12.6:a4a2d2b, Sep  6 2024, 20:11:23) [MSC v.1940 64 bit (AMD64)]`
- Real OSS `pytest` suites evaluated: **`3`** (`5` timed replicates + `1` warm-up per mode per suite)
- All suites deterministic test-outcome parity across all modes: **`True`**
- Child worker subprocess opt-in (`sitecustomize.py` multi-PID shard capture): **`True`** (`2` PIDs captured: `ChildReceiver.execute, ParentReceiver.run`)
- Median suite wall-clock overhead across packages:
  - `disable_k1` (`sys.monitoring.DISABLE` after 1 hit/site): **`4.39%`** (range `[-1.57, 8.03]`)
  - `disable_k8` (`sys.monitoring.DISABLE` after 8 hits/site): **`6.05%`** (range `[4.12, 10.79]`)
  - `local_unbounded` (package-local `CALL` without per-site `DISABLE`): **`61.67%`** (range `[15.68, 62.25]`)

## Per-Suite Breakdown

| Suite (`HEAD`) | Passed Tests | Baseline Wall (s) | `disable_k1` Wall (`Overhead %`) | `disable_k8` Wall (`Overhead %`) | `unbounded` Wall (`Overhead %`) | `k1` / `k8` / `unb` Target Callbacks | `k1` / `k8` / `unb` Unique Edges (`Poly Sites`) | Parity |
|---|---:|---:|---:|---:|---:|---|---|---|
| `tomlkit` (`d7def7db`) | 544 | `4.173s` | `4.508s` (**`+8.03%`**) | `4.345s` (**`+4.12%`**) | `6.746s` (**`+61.67%`**) | `3,284` / `12,781` / `1,099,049` | `3,283` (`0`) / `3,376` (`62`) / `3,468` (`89`) | `True` |
| `packaging` (`ae4946b3`) | 9443 | `14.162s` | `14.783s` (**`+4.39%`**) | `15.690s` (**`+10.79%`**) | `22.978s` (**`+62.25%`**) | `5,014` / `17,394` / `4,261,235` | `5,007` (`0`) / `5,052` (`4`) / `5,081` (`9`) | `True` |
| `click` (`2247b35e`) | 2155 | `9.784s` | `9.631s` (**`-1.57%`**) | `10.377s` (**`+6.05%`**) | `11.319s` (**`+15.68%`**) | `7,555` / `21,205` / `695,795` | `7,119` (`6`) / `7,312` (`88`) / `8,217` (`206`) | `True` |
