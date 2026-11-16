# Offline Solidity & Web3 Contract-Delta Evaluation (`mip-delta --audit-solidity`)

Read-only offline evaluation harness testing [`mipdelta.solidity`](../../src/mipdelta/solidity.py) across:

1. **Cross-Chain Bridge Case (`ERC7786OpenBridge`):** Validates `try_catch_sink_review` on the `sendMessage` pre-sized `Outbox[]` array left zero-initialized by non-reverting `catch` clauses before `keccak256(abi.encode(outbox))` / `emit OutboxDetails`.
2. **Blinded DeFi Suite:** Validates patch-delta and control precision on ERC-4626 vault (`PatchedVault4626` dead `_VIRTUAL_SHARES` / `_VIRTUAL_ASSETS` constants), synthetic lending pool borrow cap deltas, and clean escrow/staking controls.
3. **Admitted Aqua Campaign Archive (`input.zip`):** Scans production `.sol` contracts (excluding test/mock/lib harnesses) in memory without extracting or modifying external state.

## Running

```bash
python experiments/solidity_bounty/run_offline_eval.py
```

Outputs are sanitized before writing to [`results/solidity_bounty/summary.json`](../../results/solidity_bounty/summary.json) (no local filesystem paths).
