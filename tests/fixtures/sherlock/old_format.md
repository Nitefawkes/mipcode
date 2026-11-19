# Issue H-1: Vault withdraw ignores slippage parameter

Source: https://github.com/sherlock-audit/2024-08-x-judging/issues/66

## Summary
The vault drops the `minOut` argument.

## Vulnerability Detail
See [withdraw](https://github.com/sherlock-audit/2024-08-x/blob/db23bf67e45781b00ee6de5f6f23e621af16bd7e/src/Vault.sol#L50) for context.

## Impact
Users lose funds.

## Code Snippet
https://github.com/sherlock-audit/2024-08-x/blob/db23bf67e45781b00ee6de5f6f23e621af16bd7e/src/Vault.sol#L282
<https://github.com/sherlock-audit/2024-08-x/blob/db23bf67e45781b00ee6de5f6f23e621af16bd7e/src/lib/Math.sol#L10-L20>.

## Tool used
Manual Review

## Recommendation
Forward it, see https://github.com/sherlock-audit/2024-08-x/blob/main/test/Vault.t.sol#L5 for a test.

# Issue M-1: Missing zero check in setter

Source: https://github.com/sherlock-audit/2024-08-x-judging/issues/70

## Summary
Setter lacks a guard.

## Vulnerability Detail
The setter at [Config.sol](https://github.com/sherlock-audit/2024-08-x/blob/main/src/Config.sol#L7) accepts zero.

## Impact
Bricked config.

## Tool used
Manual Review

## Recommendation
Add a check.
