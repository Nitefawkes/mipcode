"""Unit tests for the Solidity `sibling_guard_asymmetry` detector."""
from __future__ import annotations

import json

from mipdelta import analyze_solidity_delta, audit_solidity_workspace, cli
from mipdelta.delta import format_advisory, rank_findings
from mipdelta.review import _group_findings


def _sga(files: dict[str, str]):
    return [f for f in audit_solidity_workspace(files) if f.detector == "sibling_guard_asymmetry"]


# ---------------------------------------------------------------------------
# Positive detection tests
# ---------------------------------------------------------------------------


def test_detects_missing_nonreentrant_in_consensus_group():
    src = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

contract Vault {
    IERC20 public token;
    mapping(address => uint256) public balances;

    modifier nonReentrant() { _; }

    function deposit(uint256 amount) external nonReentrant {
        balances[msg.sender] += amount;
        token.transferFrom(msg.sender, address(this), amount);
    }

    function withdraw(uint256 amount) external nonReentrant {
        balances[msg.sender] -= amount;
        token.transfer(msg.sender, amount);
    }

    function flashExit(uint256 amount) external {
        balances[msg.sender] -= amount;
        token.transfer(msg.sender, amount);
    }
}
"""
    hits = _sga({"contracts/Vault.sol": src})
    assert [(h.symbol, h.detector) for h in hits] == [
        ("Vault.flashExit", "sibling_guard_asymmetry")
    ]
    assert "nonReentrant" in hits[0].evidence[0]
    assert "balances" in hits[0].evidence[0] or "token" in hits[0].evidence[0]


def test_detects_missing_nonreentrant_on_matched_opposite_pair():
    src = """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

contract TokenWrapper {
    IERC20 public token;
    modifier lock() { _; }

    function wrap(uint256 amount, address to) external {
        token.transferFrom(msg.sender, address(this), amount);
        token.transfer(to, amount);
    }

    function unwrap(uint256 amount, address to) external lock {
        token.transferFrom(msg.sender, address(this), amount);
        token.transfer(to, amount);
    }
}
"""
    hits = _sga({"contracts/TokenWrapper.sol": src})
    assert [h.symbol for h in hits] == ["TokenWrapper.wrap"]
    assert "lock" in hits[0].evidence[0]


def test_detects_missing_when_not_paused_across_siblings():
    src = """
pragma solidity ^0.8.20;

contract StakingPool {
    uint256 public totalStaked;
    mapping(address => uint256) public stakes;

    modifier whenNotPaused() { _; }

    function stake(uint256 amt) external whenNotPaused {
        stakes[msg.sender] += amt;
        totalStaked += amt;
    }

    function unstake(uint256 amt) external whenNotPaused {
        stakes[msg.sender] -= amt;
        totalStaked -= amt;
    }

    function compound(uint256 amt) external {
        stakes[msg.sender] += amt;
        totalStaked += amt;
    }
}
"""
    hits = _sga({"contracts/StakingPool.sol": src})
    assert [h.symbol for h in hits] == ["StakingPool.compound"]
    assert "whenNotPaused" in hits[0].evidence[0]


def test_detects_missing_caller_auth_on_privileged_siblings_and_batch_variant():
    src = """
pragma solidity ^0.8.20;

contract TreasuryToken {
    uint256 public totalMinted;

    modifier onlyOwner() { _; }

    function mint(address to, uint256 amount) external onlyOwner {
        totalMinted += amount;
        _mint(to, amount);
    }

    function mintBatch(address[] calldata tos, uint256 amount) external {
        for (uint256 i = 0; i < tos.length; ++i) {
            totalMinted += amount;
            _mint(tos[i], amount);
        }
    }

    function _mint(address to, uint256 amount) internal {
        to;
        amount;
    }
}
"""
    hits = _sga({"contracts/TreasuryToken.sol": src})
    assert [h.symbol for h in hits] == ["TreasuryToken.mintBatch"]
    assert "onlyOwner" in hits[0].evidence[0]


def test_detects_missing_custom_state_gate_modifier_and_inline_view_helper_guard():
    src = """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract Locker {
    IERC20 public immutable TOKEN;
    bool public immutable UNLOCK_ACTIVE;
    uint256 private _staked;

    modifier unlockActive() {
        if (!UNLOCK_ACTIVE) revert("disabled");
        _;
    }

    function getAvailableBalance() public view returns (uint256) {
        return _staked;
    }

    function unlock(uint256 amt, address to) external unlockActive {
        if (amt > getAvailableBalance()) revert("insufficient");
        TOKEN.transfer(to, amt);
    }

    function withdrawRewards(uint256 amt, address to) external unlockActive {
        if (amt > getAvailableBalance()) revert("insufficient");
        TOKEN.transfer(to, amt);
    }

    function deployLiquidity(uint256 amt, address pool) external {
        TOKEN.approve(pool, amt);
    }
}
"""
    hits = _sga({"contracts/Locker.sol": src})
    assert len(hits) == 2
    assert {h.symbol for h in hits} == {"Locker.deployLiquidity"}
    evidence_joined = " | ".join(h.evidence[0] for h in hits)
    assert "unlockActive" in evidence_joined
    assert "getAvailableBalance()" in evidence_joined


def test_detects_missing_inline_deadline_check_when_sibling_accepts_deadline():
    src = """
pragma solidity ^0.8.20;

contract SwapPool {
    uint256 public reserve;

    function swapExactIn(uint256 amt, uint256 deadline) external {
        require(block.timestamp <= deadline, "expired");
        reserve += amt;
    }

    function swapExactOut(uint256 amt, uint256 deadline) external {
        require(block.timestamp <= deadline, "expired");
        reserve -= amt;
    }

    function swapMulti(uint256 amt, uint256 deadline) external {
        require(amt > 0, "zero");
        reserve += amt;
        deadline;
    }

    function syncReserve(uint256 amt) external {
        // Does NOT accept deadline -> must NOT be flagged for missing deadline check
        reserve = amt;
    }
}
"""
    hits = _sga({"contracts/SwapPool.sol": src})
    assert [h.symbol for h in hits] == ["SwapPool.swapMulti"]
    assert "deadline" in hits[0].evidence[0]


def test_internal_helper_propagation_groups_siblings():
    src = """
pragma solidity ^0.8.20;

contract VaultWithHelpers {
    uint256 public totalShares;
    modifier whenNotPaused() { _; }

    function _record(uint256 delta) internal {
        totalShares += delta;
    }

    function depositA(uint256 x) external whenNotPaused { _record(x); }
    function depositB(uint256 x) external whenNotPaused { _record(x); }
    function depositC(uint256 x) external { _record(x); }
}
"""
    hits = _sga({"contracts/VaultWithHelpers.sol": src})
    assert [h.symbol for h in hits] == ["VaultWithHelpers.depositC"]


def test_solidity_delta_detects_newly_introduced_guard_asymmetry():
    before = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public total;
    modifier whenNotPaused() { _; }
    function deposit(uint256 a) external whenNotPaused { total += a; }
    function withdraw(uint256 a) external whenNotPaused { total -= a; }
}
"""
    after = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public total;
    modifier whenNotPaused() { _; }
    function deposit(uint256 a) external whenNotPaused { total += a; }
    function withdraw(uint256 a) external whenNotPaused { total -= a; }
    function fastWithdraw(uint256 a) external { total -= a; }
}
"""
    new_findings, summary = analyze_solidity_delta({"Vault.sol": before}, {"Vault.sol": after})
    assert [(f.detector, f.symbol) for f in new_findings] == [
        ("sibling_guard_asymmetry", "Vault.fastWithdraw")
    ]
    assert summary["new_findings"] == 1


# ---------------------------------------------------------------------------
# Negative / anti-noise tests (MUST produce 0 false positives)
# ---------------------------------------------------------------------------


def test_no_false_positive_when_all_siblings_symmetric():
    src = """
pragma solidity ^0.8.20;
interface IERC20 { function transfer(address, uint256) external returns (bool); }
contract SymmetricVault {
    IERC20 public token;
    uint256 public total;
    modifier nonReentrant() { _; }
    modifier whenNotPaused() { _; }
    function deposit(uint256 a) external nonReentrant whenNotPaused { total += a; token.transfer(msg.sender, a); }
    function withdraw(uint256 a) external nonReentrant whenNotPaused { total -= a; token.transfer(msg.sender, a); }
    function redeem(uint256 a) external nonReentrant whenNotPaused { total -= a; token.transfer(msg.sender, a); }
}
"""
    assert _sga({"contracts/SymmetricVault.sol": src}) == []


def test_inline_guards_and_allowance_helpers_satisfy_modifier_peers():
    src = """
pragma solidity ^0.8.20;
contract MixedGuardStyles {
    address public owner;
    bool public paused;
    uint256 public supply;

    modifier onlyOwner() {
        require(msg.sender == owner, "auth");
        _;
    }
    modifier whenNotPaused() {
        require(!paused, "paused");
        _;
    }

    function mintA(uint256 x) external onlyOwner whenNotPaused { supply += x; }
    function mintB(uint256 x) external onlyOwner whenNotPaused { supply += x; }

    function mintC(uint256 x) external {
        if (msg.sender != owner) revert("auth");
        if (paused) revert("paused");
        supply += x;
    }

    function mintD(uint256 x) external {
        _checkOwner();
        _requireNotPaused();
        supply += x;
    }

    function _checkOwner() internal view {
        require(msg.sender == owner, "auth");
    }
    function _requireNotPaused() internal view {
        require(!paused, "paused");
    }
}
"""
    assert _sga({"contracts/MixedGuardStyles.sol": src}) == []


def test_delegating_wrapper_inherits_guards_without_false_positive():
    src = """
pragma solidity ^0.8.20;
interface IERC20 { function transfer(address, uint256) external returns (bool); }
contract DelegatingVault {
    IERC20 public token;
    uint256 public total;
    modifier nonReentrant() { _; }
    modifier onlyOwner() { _; }

    function withdraw(uint256 amount, address to) public nonReentrant onlyOwner {
        total -= amount;
        token.transfer(to, amount);
    }

    function withdraw(uint256 amount) external {
        withdraw(amount, msg.sender);
    }
}
"""
    assert _sga({"contracts/DelegatingVault.sol": src}) == []


def test_suppresses_intentional_asymmetry_and_non_target_functions():
    src = """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
}

interface IVaultInterface {
    function deposit(uint256 a) external;
    function withdraw(uint256 a) external;
}

library MathHelper {
    function add(uint256 a, uint256 b) public pure returns (uint256) { return a + b; }
}

contract WellDesignedVault {
    IERC20 public token;
    mapping(address => uint256) public balances;
    mapping(address => bool) public pausedMinter;
    uint256 public feeBps;
    uint256 public cap;
    bool public paused;

    modifier initializer() { _; }
    modifier onlyOwner() { _; }
    modifier nonReentrant() { _; }
    modifier whenNotPaused() { _; }
    modifier whenPaused() { _; }
    modifier afterTimelock(uint256 ts) { require(block.timestamp >= ts); _; }

    constructor() {}
    receive() external payable {}
    fallback() external payable {}

    function initialize(uint256 _cap) external initializer {
        cap = _cap;
    }

    // Pure view / stub functions ignored
    function preview(uint256 a) external view returns (uint256) { return a + feeBps; }
    function disabledPath(uint256) external { revert("disabled"); }

    // User-scoped deposit/withdraw vs admin sweep: deposit/withdraw must NOT require onlyOwner
    function deposit(uint256 amt) external nonReentrant whenNotPaused {
        balances[msg.sender] += amt;
        token.transferFrom(msg.sender, address(this), amt);
    }

    function withdraw(uint256 amt) external nonReentrant whenNotPaused {
        balances[msg.sender] -= amt;
        token.transfer(msg.sender, amt);
    }

    // Emergency withdraw intentionally omits whenNotPaused
    function emergencyWithdraw(uint256 amt) external nonReentrant {
        balances[msg.sender] -= amt;
        token.transfer(msg.sender, amt);
    }

    // Admin-only config setters with no external calls must NOT require nonReentrant,
    // and pure config-setter onlyOwner consensus must NOT flag a timelocked acceptCap
    function setCap(uint256 c) external onlyOwner { cap = c; }
    function updateCap(uint256 c) external onlyOwner { cap = c; }
    function acceptCap(uint256 c, uint256 validAt) external afterTimelock(validAt) { cap = c; }

    // Pause controller intentionally uses whenPaused, not whenNotPaused
    function unpause() external onlyOwner whenPaused { paused = false; }
}
"""
    assert _sga({"contracts/WellDesignedVault.sol": src}) == []


def test_different_custom_only_roles_and_allowance_auth_are_equivalent():
    src = """
pragma solidity ^0.8.20;
contract RoleRouter {
    mapping(address => bool) public sources;
    mapping(address => bool) public admins;
    uint256 public flow;

    modifier onlyAdmin() { require(admins[msg.sender]); _; }
    modifier onlySource() { if (!sources[msg.sender]) revert("src"); _; }

    function pushA(uint256 x) external onlyAdmin { flow += x; }
    function pushB(uint256 x) external onlyAdmin { flow += x; }
    function pushFromSource(uint256 x) external onlySource { flow += x; }
    function pushWithAllowance(address owner, uint256 x) external {
        _spendAllowance(owner, msg.sender, x);
        flow += x;
    }
    function _spendAllowance(address owner, address spender, uint256 x) internal pure {
        owner; spender; x;
    }
}
"""
    assert _sga({"contracts/RoleRouter.sol": src}) == []


# ---------------------------------------------------------------------------
# Ranking, 250 B advisory budget, Stage-2 review grouping, and CLI integration
# ---------------------------------------------------------------------------


def test_ranking_demotes_tests_and_mocks_and_advisory_stays_under_250_bytes():
    vulnerable = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public total;
    modifier whenNotPaused() { _; }
    function op1(uint256 a) external whenNotPaused { total += a; }
    function op2(uint256 a) external whenNotPaused { total -= a; }
    function unguarded(uint256 a) external { total += a; }
}
"""
    files = {
        "test/Vault.t.sol": vulnerable,
        "contracts/mocks/VaultMock.sol": vulnerable,
        "script/DeployVault.s.sol": vulnerable,
        "contracts/TestVault.sol": vulnerable,
        "contracts/Vault.sol": vulnerable,
    }
    ranked = rank_findings(_sga(files))
    assert len(ranked) == 5
    # Production contract MUST rank first ahead of mocks, scripts, and tests
    assert ranked[0].path == "contracts/Vault.sol"

    # Single-finding advisory fits cleanly under 250 B with actionable evidence
    single_adv = format_advisory([ranked[0]], max_bytes=250)
    assert len(single_adv.encode("utf-8")) <= 250
    assert "sibling_guard_asymmetry" in single_adv
    assert "Vault.unguarded" in single_adv

    # 12 distinct production sites stay strictly <= 250 B with clean (+N) rollup and no mid-token clipping
    many_files = {f"contracts/module_{i:02d}/CoreVault{i:02d}.sol": vulnerable for i in range(12)}
    many_ranked = rank_findings(_sga(many_files))
    assert len(many_ranked) == 12
    many_adv = format_advisory(many_ranked, max_bytes=250)
    assert len(many_adv.encode("utf-8")) <= 250
    assert "(+" in many_adv
    assert not many_adv.endswith("...")

    # Stage-2 review grouping deduplicates and preserves detector metadata cleanly
    grouped, total = _group_findings(many_ranked, lambda f: f.symbol)
    assert total == 12
    assert len(grouped) == 12
    assert all(g["detector"] == "sibling_guard_asymmetry" for g in grouped)
    assert all("whenNotPaused" in g["evidence"] for g in grouped)


def test_cli_audit_solidity_json_emits_sibling_guard_asymmetry(tmp_path, capsys):
    sol = tmp_path / "contracts" / "Vault.sol"
    sol.parent.mkdir(parents=True)
    sol.write_text(
        """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public total;
    modifier onlyOwner() { _; }
    function setA(uint256 x) external onlyOwner { total = x; }
    function setB(uint256 x) external onlyOwner { total = x + 1; }
    function setC(uint256 x) external { total = x + 2; }
}
""",
        encoding="utf-8",
    )
    rc = cli.main_cli(["--root", str(tmp_path), "--audit-solidity", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "audit-solidity"
    assert payload["advisory_utf8_bytes"] <= 250
    dets = [(f["detector"], f["symbol"]) for f in payload["findings"]]
    assert ("sibling_guard_asymmetry", "Vault.setC") in dets


# ---------------------------------------------------------------------------
# v2 Upgrade A & B: Library storage-struct grouping & state-sync steps
# ---------------------------------------------------------------------------


def test_v2_library_storage_struct_detects_omitted_state_sync_calls():
    src = """
pragma solidity ^0.8.20;

struct PoolState {
    uint256 totalAssets;
    uint256 accruedIndex;
}

library AccountingLib {
    function syncAccounting(PoolState storage self) internal {
        self.accruedIndex += 1;
    }

    function sync(PoolState storage self, uint256 delta) internal {
        self.accruedIndex += delta;
    }
}

library PoolOps {
    using AccountingLib for PoolState;

    function executeSupply(PoolState storage state, uint256 amt) external {
        state.syncAccounting();
        AccountingLib.sync(state, amt);
        state.totalAssets += amt;
    }

    function executeWithdraw(PoolState storage state, uint256 amt) external {
        state.syncAccounting();
        AccountingLib.sync(state, amt);
        state.totalAssets -= amt;
    }

    function executeBorrow(PoolState storage state, uint256 amt) external {
        state.syncAccounting();
        AccountingLib.sync(state, amt);
        state.totalAssets -= amt;
    }

    function fastWithdraw(PoolState storage state, uint256 amt) external {
        state.totalAssets -= amt;
    }
}
"""
    hits = _sga({"contracts/PoolOps.sol": src})
    assert len(hits) == 2
    assert {h.symbol for h in hits} == {"PoolOps.fastWithdraw"}
    ev_all = " | ".join(h.evidence[0] for h in hits)
    assert "PoolState.syncAccounting()" in ev_all
    assert "AccountingLib.sync()" in ev_all
    assert "PoolState" in ev_all

    # Advisory stays strictly <= 250 B and review grouping deduplicates unique line numbers
    adv = format_advisory(hits, max_bytes=250)
    assert len(adv.encode("utf-8")) <= 250
    grouped, total = _group_findings(hits, lambda f: f.symbol)
    assert total == 2
    assert len(grouped) == 1
    assert grouped[0]["lines"] == [hits[0].start_line]


def test_v2_library_mapping_storage_alias_and_contract_internal_sync_step():
    src = """
pragma solidity ^0.8.20;

struct ReserveData {
    uint256 liquidity;
    uint256 rate;
}

library ReserveLib {
    function refreshRates(ReserveData storage self) internal {
        self.rate += 1;
    }
}

library LendingLogic {
    using ReserveLib for ReserveData;

    function executeSupply(mapping(address => ReserveData) storage reserves, address asset, uint256 amt) external {
        ReserveData storage reserve = reserves[asset];
        reserve.refreshRates();
        reserve.liquidity += amt;
    }

    function executeWithdraw(mapping(address => ReserveData) storage reserves, address asset, uint256 amt) external {
        ReserveData storage reserve = reserves[asset];
        reserve.refreshRates();
        reserve.liquidity -= amt;
    }

    function executeTreasurySweep(mapping(address => ReserveData) storage reserves, address asset, uint256 amt) external {
        ReserveData storage reserve = reserves[asset];
        reserve.liquidity -= amt;
    }
}

contract DebtVault {
    uint256 public totalDebt;
    uint256 public lastAccrual;

    function _accrueInterest() internal {
        lastAccrual = block.timestamp;
        totalDebt += 1;
    }

    function borrow(uint256 amt) external {
        _accrueInterest();
        totalDebt += amt;
    }

    function repay(uint256 amt) external {
        _accrueInterest();
        totalDebt -= amt;
    }

    function forgiveDebt(uint256 amt) external {
        totalDebt -= amt;
    }
}
"""
    hits = _sga({"contracts/LendingLogic.sol": src})
    symbols = {h.symbol for h in hits}
    assert "LendingLogic.executeTreasurySweep" in symbols
    assert "DebtVault.forgiveDebt" in symbols
    ev_by_sym = {h.symbol: h.evidence[0] for h in hits}
    assert "ReserveData.refreshRates()" in ev_by_sym["LendingLogic.executeTreasurySweep"]
    assert "_accrueInterest()" in ev_by_sym["DebtVault.forgiveDebt"]


def test_v2_library_math_and_conditional_or_minority_sync_produce_zero_findings():
    src = """
pragma solidity ^0.8.20;

struct PoolState {
    uint256 totalAssets;
    uint256 index;
}

library Math {
    function max(uint256 a, uint256 b) internal pure returns (uint256) { return a > b ? a : b; }
    function min(uint256 a, uint256 b) internal pure returns (uint256) { return a < b ? a : b; }
    function mulDiv(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) { return (x * y) / d; }
}

library SafeCast {
    function toUint128(uint256 x) internal pure returns (uint128) { require(x <= type(uint128).max); return uint128(x); }
    function toUint96(uint256 x) internal pure returns (uint96) { require(x <= type(uint96).max); return uint96(x); }
}

library BitMaps {
    struct BitMap { mapping(uint256 => uint256) _data; }
    function get(BitMap storage bitmap, uint256 idx) internal view returns (bool) {
        return bitmap._data[idx] != 0;
    }
    function set(BitMap storage bitmap, uint256 idx) internal {
        bitmap._data[idx] = 1;
    }
    function unset(BitMap storage bitmap, uint256 idx) internal {
        bitmap._data[idx] = 0;
    }
}

library ConditionalAndMinorityOps {
    function syncAccounting(PoolState storage self) internal {
        self.index += 1;
    }

    // Conditional sync in branches must NOT qualify as an unconditional invariant
    function opA(PoolState storage s, uint256 amt, bool flag) external {
        if (flag) {
            syncAccounting(s);
        }
        s.totalAssets += amt;
    }

    function opB(PoolState storage s, uint256 amt, bool flag) external {
        if (flag) {
            syncAccounting(s);
        }
        s.totalAssets -= amt;
    }

    // Only 1 of 3 calls unconditionally (< 2/3) -> must NOT fire
    function opC(PoolState storage s, uint256 amt) external {
        syncAccounting(s);
        s.totalAssets += amt;
    }
}
"""
    assert _sga({"contracts/Libs.sol": src}) == []


# ---------------------------------------------------------------------------
# v2 Upgrade C: Versioned-flow sibling pairing across separate storage
# ---------------------------------------------------------------------------


def test_v2_versioned_flow_pairs_v1_v2_even_on_separate_storage():
    src = """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amt) external returns (bool);
}

contract LiquidityLocker {
    bool public unlocked;
    IERC20 public token;
    mapping(address => uint256) public v1Positions;
    mapping(address => uint256) public v2Positions;

    modifier whenUnlocked() {
        if (!unlocked) revert("locked");
        _;
    }

    modifier nonReentrant() { _; }

    function provideLiquidity(uint256 amt) external whenUnlocked nonReentrant {
        v1Positions[msg.sender] += amt;
        token.transfer(msg.sender, amt);
    }

    function withdrawLiquidity(uint256 amt) external whenUnlocked nonReentrant {
        v1Positions[msg.sender] -= amt;
        token.transfer(msg.sender, amt);
    }

    // Separate V2 storage: misses custom lifecycle modifier `whenUnlocked` present on V1
    function provideLiquidityV2(uint256 amt) external nonReentrant {
        v2Positions[msg.sender] += amt;
        token.transfer(msg.sender, amt);
    }

    function withdrawLiquidityV2(uint256 amt) external nonReentrant {
        v2Positions[msg.sender] -= amt;
        token.transfer(msg.sender, amt);
    }
}
"""
    hits = _sga({"contracts/LiquidityLocker.sol": src})
    assert [(h.symbol, h.detector) for h in hits] == [
        ("LiquidityLocker.provideLiquidityV2", "sibling_guard_asymmetry"),
        ("LiquidityLocker.withdrawLiquidityV2", "sibling_guard_asymmetry"),
    ]
    assert all("whenUnlocked" in h.evidence[0] for h in hits)


def test_v2_versioned_flow_suppresses_delegating_inline_equivalent_and_create2():
    src = """
pragma solidity ^0.8.20;

contract SafeVersioned {
    bool public unlocked;
    uint256 public v1Total;
    uint256 public v2Total;

    modifier whenUnlocked() {
        if (!unlocked) revert("locked");
        _;
    }

    function deposit(uint256 amt) public whenUnlocked {
        v1Total += amt;
    }

    // Delegates unconditionally to V1 -> inherits whenUnlocked -> 0 findings
    function depositV2(uint256 amt) external {
        deposit(amt);
    }

    function withdraw(uint256 amt) external whenUnlocked {
        v1Total -= amt;
    }

    // Enforces the exact same state flag inline -> 0 findings
    function withdrawV2(uint256 amt) external {
        if (!unlocked) revert("locked");
        v2Total -= amt;
    }

    // EVM CREATE / CREATE2 / CREATE3 opcodes are NOT V1/V2 versioned siblings
    function deployCreate(bytes memory code) external whenUnlocked {
        v1Total += code.length;
    }

    function deployCreate2(bytes memory code) external {
        v2Total += code.length;
    }
}
"""
    assert _sga({"contracts/SafeVersioned.sol": src}) == []
