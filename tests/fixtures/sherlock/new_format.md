# Issue H-1: Fee never forwarded to the router

Source: https://github.com/sherlock-audit/2025-07-y-judging/issues/12

### Summary
Fee is dropped.

### Root Cause
In [Router.sol](https://github.com/sherlock-audit/2025-07-y/blob/abc123/contracts/Router.sol#L40-L55) the fee is not passed, and in `https://github.com/sherlock-audit/2025-07-y/blob/abc123/contracts/Pool.sol#L9`, the pool reads it.

### Internal Pre-conditions
None.

### External Pre-conditions
None.

### Attack Path
1. Call swap.

### Impact
Loss of fees.

### PoC
See https://github.com/sherlock-audit/2025-07-y/blob/main/test/Poc.t.sol#L1

### Mitigation
Fix at https://github.com/sherlock-audit/2025-07-y/blob/abc123/contracts/Fix.sol#L3

## Discussion

**someone** commented:
https://github.com/sherlock-audit/2025-07-y/blob/abc123/contracts/Discussion.sol#L1

# Issue M-1: Upstream bug in dependency

Source: https://github.com/sherlock-audit/2025-07-y-judging/issues/20

### Summary
Dependency issue.

### Root Cause
The bug is in [v3-core](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol#L100).

### Impact
Bad.

### Mitigation
Upgrade.
