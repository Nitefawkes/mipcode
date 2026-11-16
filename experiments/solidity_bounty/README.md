# Offline Solidity & Web3 Contract-Delta Evaluation (`mip-delta --audit-solidity`)

Read-only evaluation harnesses testing [`mipdelta.solidity`](../../src/mipdelta/solidity.py) across two suites:

1. **Curated Bounty & Aqua Replay ([`run_offline_eval.py`](run_offline_eval.py)):**
   - **Cross-Chain Bridge Case (`ERC7786OpenBridge`):** Validates `try_catch_sink_review` on `sendMessage` pre-sized `Outbox[]` arrays left zero-initialized by non-reverting `catch` clauses before `keccak256(abi.encode(outbox))` / `emit OutboxDetails`.
   - **Blinded DeFi Suite:** Validates patch-delta and control precision on ERC-4626 vault (`PatchedVault4626` dead `_VIRTUAL_SHARES` / `_VIRTUAL_ASSETS` constants), synthetic lending pool borrow cap deltas, and clean escrow/staking controls.
   - **Admitted Aqua Campaign Archive (`input.zip`):** Scans production `.sol` contracts in memory across 4 archive components without extracting or modifying external state.
2. **Large Contest & Bounty Corpus ([`run_contest_corpus.py`](run_contest_corpus.py)):**
   - Scans 12 production Solidity targets (`oz-community-contracts`, `dhedge-v2`, `twyne-v1`, `veda-boring-vault`, `lombard-evm`, `strata-contracts`, `origin-dollar`, `termmax-v2`, plus 4 Aqua archive components) strictly read-only.

## Running

```bash
python experiments/solidity_bounty/run_offline_eval.py
python experiments/solidity_bounty/run_contest_corpus.py
```

Outputs are sanitized before writing to [`results/solidity_bounty/summary.json`](../../results/solidity_bounty/summary.json) and [`results/solidity_bounty/contest_corpus.json`](../../results/solidity_bounty/contest_corpus.json) (no local filesystem paths).
