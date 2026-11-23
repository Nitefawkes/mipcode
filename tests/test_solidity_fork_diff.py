"""Unit and CLI integration tests for Solidity fork-vs-upstream invariant regression diffs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mipdelta import analyze_solidity_fork_diff, cli, format_advisory


def test_dropped_guards_and_benign_refactors():
    upstream = {
        "contracts/Vault.sol": """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
}

contract Vault {
    address public owner;
    address public poolManager;
    bool public paused;
    uint256 private _locked;
    uint256 public totalDeposits;
    mapping(address => uint256) public balances;
    IERC20 public token;

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }
    modifier onlyPoolManager() {
        if (msg.sender != poolManager) revert();
        _;
    }
    modifier whenNotPaused() {
        require(!paused, "paused");
        _;
    }
    modifier nonReentrant() {
        require(_locked == 0, "reent");
        _locked = 1;
        _;
        _locked = 0;
    }

    function beforeSwap(uint256 amount) external onlyPoolManager {
        balances[msg.sender] += amount;
    }

    function deposit(uint256 amount) external whenNotPaused {
        balances[msg.sender] += amount;
    }

    function withdraw(uint256 amount) external nonReentrant {
        balances[msg.sender] -= amount;
        token.transfer(msg.sender, amount);
    }

    // Refactored cleanly in fork into 2-hop internal helper -> should NOT FP
    function adminSweep(address to, uint256 amount) external onlyOwner {
        totalDeposits -= amount;
        token.transfer(to, amount);
    }

    // Narrowed visibility in fork (`external` -> `internal`) -> should NOT FP
    function helperDeposit(uint256 amount) external onlyOwner {
        balances[msg.sender] += amount;
    }

    // Reentrancy guard dropped in fork when external call removed -> should NOT FP
    function updateConfig(uint256 val) external nonReentrant {
        totalDeposits = val;
    }
}
"""
    }
    fork = {
        "src/CustomVault.sol": """
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
}

contract CustomVault {
    address public owner;
    address public poolManager;
    bool public paused;
    uint256 private _locked;
    uint256 public totalDeposits;
    mapping(address => uint256) public balances;
    IERC20 public token;

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }
    modifier onlyPoolManager() {
        if (msg.sender != poolManager) revert();
        _;
    }

    function _enforceAuth() internal view {
        _checkOwner();
    }
    function _checkOwner() internal view {
        require(msg.sender == owner, "not owner");
    }

    function beforeSwap(uint256 amount) external {
        balances[msg.sender] += amount;
    }

    function deposit(uint256 amount) external {
        balances[msg.sender] += amount;
    }

    function withdraw(uint256 amount) external {
        balances[msg.sender] -= amount;
        token.transfer(msg.sender, amount);
    }

    function adminSweep(address to, uint256 amount) external {
        _enforceAuth();
        totalDeposits -= amount;
        token.transfer(to, amount);
    }

    function helperDeposit(uint256 amount) internal {
        balances[msg.sender] += amount;
    }

    function updateConfig(uint256 val) external {
        totalDeposits = val;
    }
}
"""
    }

    findings, summary = analyze_solidity_fork_diff(upstream, fork)
    assert summary["matched_contracts"] == 1
    got = {(f.detector, f.symbol) for f in findings}
    assert ("fork_dropped_guard", "CustomVault.beforeSwap") in got
    assert ("fork_dropped_guard", "CustomVault.deposit") in got
    assert ("fork_dropped_guard", "CustomVault.withdraw") in got
    assert ("fork_dropped_guard", "CustomVault.adminSweep") not in got
    assert ("fork_dropped_guard", "CustomVault.helperDeposit") not in got
    assert ("fork_dropped_guard", "CustomVault.updateConfig") not in got


def test_visibility_widening_without_auth():
    upstream = {
        "contracts/Pool.sol": """
pragma solidity ^0.8.20;
contract Pool {
    uint256 public debt;
    address public admin;
    modifier onlyAdmin() { require(msg.sender == admin); _; }
    function _mintBad(uint256 amt) internal { debt += amt; }
    function _mintSafe(uint256 amt) internal { debt += amt; }
}
"""
    }
    fork = {
        "contracts/Pool.sol": """
pragma solidity ^0.8.20;
contract Pool {
    uint256 public debt;
    address public admin;
    modifier onlyAdmin() { require(msg.sender == admin); _; }
    function _mintBad(uint256 amt) external { debt += amt; }
    function _mintSafe(uint256 amt) external onlyAdmin { debt += amt; }
}
"""
    }
    findings, _ = analyze_solidity_fork_diff(upstream, fork)
    assert [(f.detector, f.symbol) for f in findings] == [("fork_dropped_guard", "Pool._mintBad")]
    assert "widened internal -> external from upstream without caller auth" in findings[0].evidence[0]


def test_dropped_sync_and_sync_order_shift_across_renamed_libraries():
    upstream = {
        "contracts/protocol/libraries/logic/PoolLogic.sol": """
pragma solidity ^0.8.20;

library ReserveLogic {
    struct ReserveData {
        uint256 accruedToTreasury;
        uint256 borrowIndex;
    }
    function updateInterestRates(ReserveData storage reserve, uint256 added, uint256 taken) internal {}
    function updateState(ReserveData storage reserve) internal {}
}

interface IERC20 {
    function safeTransfer(address to, uint256 amount) external;
}

library PoolLogic {
    using ReserveLogic for ReserveLogic.ReserveData;

    function executeMintToTreasury(
        mapping(address => ReserveLogic.ReserveData) storage reservesData,
        address asset,
        address treasury,
        IERC20 aToken
    ) external {
        ReserveLogic.ReserveData storage reserve = reservesData[asset];
        uint256 amount = reserve.accruedToTreasury;
        reserve.accruedToTreasury = 0;
        ReserveLogic.updateInterestRates(reserve, 0, amount);
        aToken.safeTransfer(treasury, amount);
    }

    function executeLiquidate(
        mapping(address => ReserveLogic.ReserveData) storage reservesData,
        address asset,
        address liquidator,
        IERC20 debtToken
    ) external {
        ReserveLogic.ReserveData storage reserve = reservesData[asset];
        reserve.updateState();
        ReserveLogic.updateInterestRates(reserve, 100, 0);
        debtToken.safeTransfer(liquidator, 100);
    }

    // Benign: only renamed `ReserveLogic` -> `ZeroReserveLogic` in fork
    function executeRepay(mapping(address => ReserveLogic.ReserveData) storage reservesData, address asset) external {
        ReserveLogic.ReserveData storage reserve = reservesData[asset];
        reserve.updateInterestRates(10, 0);
    }
}
"""
    }
    fork = {
        "src/core/ZeroPoolLogic.sol": """
pragma solidity ^0.8.20;

library ZeroReserveLogic {
    struct ReserveData {
        uint256 accruedToTreasury;
        uint256 borrowIndex;
    }
    function updateInterestRates(ReserveData storage reserve, uint256 added, uint256 taken) internal {}
    function updateState(ReserveData storage reserve) internal {}
}

interface IERC20 {
    function safeTransfer(address to, uint256 amount) external;
}

library ZeroPoolLogic {
    using ZeroReserveLogic for ZeroReserveLogic.ReserveData;

    // BUG 1: omitted updateInterestRates completely
    function executeMintToTreasury(
        mapping(address => ZeroReserveLogic.ReserveData) storage reservesData,
        address asset,
        address treasury,
        IERC20 aToken
    ) external {
        ZeroReserveLogic.ReserveData storage reserve = reservesData[asset];
        uint256 amount = reserve.accruedToTreasury;
        reserve.accruedToTreasury = 0;
        aToken.safeTransfer(treasury, amount);
    }

    // BUG 2: moved updateInterestRates AFTER safeTransfer
    function executeLiquidate(
        mapping(address => ZeroReserveLogic.ReserveData) storage reservesData,
        address asset,
        address liquidator,
        IERC20 debtToken
    ) external {
        ZeroReserveLogic.ReserveData storage reserve = reservesData[asset];
        reserve.updateState();
        debtToken.safeTransfer(liquidator, 100);
        ZeroReserveLogic.updateInterestRates(reserve, 100, 0);
    }

    // Safe: renamed prefix only
    function executeRepay(mapping(address => ZeroReserveLogic.ReserveData) storage reservesData, address asset) external {
        ZeroReserveLogic.ReserveData storage reserve = reservesData[asset];
        ZeroReserveLogic.updateInterestRates(reserve, 10, 0);
    }
}
"""
    }

    findings, summary = analyze_solidity_fork_diff(upstream, fork)
    assert summary["matched_contracts"] >= 1
    got = {(f.detector, f.symbol) for f in findings}
    assert ("fork_dropped_sync", "ZeroPoolLogic.executeMintToTreasury") in got
    assert ("fork_sync_order_shift", "ZeroPoolLogic.executeLiquidate") in got
    assert ("fork_dropped_sync", "ZeroPoolLogic.executeRepay") not in got


def test_removed_sync_module_suppressed_and_callee_sync_deduped():
    upstream = {
        "contracts/Pool.sol": """
pragma solidity ^0.8.20;
library RemovedModule {
    function updateIsolatedDebtIfIsolated(uint256 x) internal {}
}
contract Pool {
    uint256 public state;
    function _updateRates() internal { state += 1; }
    function entry(uint256 amt) external {
        RemovedModule.updateIsolatedDebtIfIsolated(amt);
        _inner(amt);
    }
    function _inner(uint256 amt) internal {
        state += amt;
        _updateRates();
    }
}
"""
    }
    fork = {
        "contracts/Pool.sol": """
pragma solidity ^0.8.20;
contract Pool {
    uint256 public state;
    function _updateRates() internal { state += 1; }
    // RemovedModule stripped globally from fork; _inner already reports dropped _updateRates()
    function entry(uint256 amt) external {
        _inner(amt);
    }
    function _inner(uint256 amt) internal {
        state += amt;
    }
}
"""
    }
    findings, _ = analyze_solidity_fork_diff(upstream, fork)
    assert [(f.detector, f.symbol) for f in findings] == [("fork_dropped_sync", "Pool._inner")]


def test_rounding_polarity_shifts_and_safe_refactor():
    upstream = {
        "contracts/Vault.sol": """
pragma solidity ^0.8.20;
contract Vault {
    function convertToShares(uint256 assets) public pure returns (uint256) {
        return Math.mulDiv(assets, 1e18, 2e18, Math.Rounding.Down);
    }
    function previewWithdraw(uint256 assets) public pure returns (uint256) {
        return Math.mulDiv(assets, 2e18, 1e18, Math.Rounding.Up);
    }
    function withdraw(uint256 assets) external pure returns (uint256 shares) {
        shares = previewWithdraw(assets);
    }
    function mintShares(uint256 assets) external pure returns (uint256) {
        return Math.mulDiv(assets, 100, 99, Math.Rounding.Up);
    }
    function chargeFee(uint256 assets) external pure returns (uint256) {
        return Math.mulDiv(assets, 3, 1000, Math.Rounding.Up);
    }
}
"""
    }
    fork = {
        "contracts/CustomVault.sol": """
pragma solidity ^0.8.20;
contract CustomVault {
    // Safe refactor: Math.mulDiv(..., Rounding.Down) -> FixedPointMathLib.mulDivDown(...)
    function convertToShares(uint256 assets) public pure returns (uint256) {
        return FixedPointMathLib.mulDivDown(assets, 1e18, 2e18);
    }
    // Safe refactor: Math.mulDiv(..., Rounding.Up) -> FixedPointMathLib.mulDivUp(...)
    function previewWithdraw(uint256 assets) public pure returns (uint256) {
        return FixedPointMathLib.mulDivUp(assets, 2e18, 1e18);
    }
    // BUG 1: switched previewWithdraw (UP) -> convertToShares (DOWN)
    function withdraw(uint256 assets) external pure returns (uint256 shares) {
        shares = convertToShares(assets);
    }
    // BUG 2: flipped Rounding.Up -> Rounding.Floor
    function mintShares(uint256 assets) external pure returns (uint256) {
        return Math.mulDiv(assets, 100, 99, Math.Rounding.Floor);
    }
    // BUG 3: replaced explicit Rounding.Up with plain unrounded division
    function chargeFee(uint256 assets) external pure returns (uint256) {
        return (assets * 3) / 1000;
    }
}
"""
    }
    findings, _ = analyze_solidity_fork_diff(upstream, fork)
    got = {(f.detector, f.symbol) for f in findings}
    assert got == {
        ("fork_rounding_shift", "CustomVault.withdraw"),
        ("fork_rounding_shift", "CustomVault.mintShares"),
        ("fork_rounding_shift", "CustomVault.chargeFee"),
    }


def test_safety_param_weakened_and_negative_controls():
    upstream = {
        "contracts/Router.sol": """
pragma solidity ^0.8.20;
contract Router {
    function swapA(address pool, uint256 amountIn, uint256 minOut, uint256 userDeadline) external {
        ExactInputParams memory p = ExactInputParams({
            tokenIn: pool,
            amountIn: amountIn,
            amountOutMinimum: minOut,
            deadline: userDeadline
        });
        router.exactInput(p);
    }
    function swapB(address pool, uint256 amountIn, uint256 minAmountOut) external {
        swapCore(pool, amountIn, minAmountOut);
    }
    // Safe: upstream already passed 0 intentionally inside internal test/fallback helper
    function swapDust(address pool, uint256 amountIn) external {
        ExactInputParams memory p = ExactInputParams({
            tokenIn: pool,
            amountIn: amountIn,
            amountOutMinimum: 0,
            deadline: block.timestamp
        });
        router.exactInput(p);
    }
}
"""
    }
    fork = {
        "contracts/Router.sol": """
pragma solidity ^0.8.20;
contract Router {
    function swapA(address pool, uint256 amountIn, uint256 minOut, uint256 userDeadline) external {
        ExactInputParams memory p = ExactInputParams({
            tokenIn: pool,
            amountIn: amountIn,
            amountOutMinimum: 0,
            deadline: userDeadline
        });
        router.exactInput(p);
    }
    function swapB(address pool, uint256 amountIn, uint256 minAmountOut) external {
        swapCore(pool, amountIn, 0);
    }
    function swapDust(address pool, uint256 amountIn) external {
        ExactInputParams memory p = ExactInputParams({
            tokenIn: pool,
            amountIn: amountIn,
            amountOutMinimum: 0,
            deadline: block.timestamp
        });
        router.exactInput(p);
    }
}
"""
    }
    findings, _ = analyze_solidity_fork_diff(upstream, fork)
    assert {(f.detector, f.symbol) for f in findings} == {
        ("fork_safety_param_weakened", "Router.swapA"),
        ("fork_safety_param_weakened", "Router.swapB"),
    }


def test_path_filtering_wire_cap_and_cli(tmp_path: Path):
    # Generate 12+ functions dropping a guard so we stress-test the <= 250B wire cap
    fn_defs_u = "\n".join(f"    function fn{i}(uint256 x) external onlyOwner {{ val += x + {i}; }}" for i in range(12))
    fn_defs_f = "\n".join(f"    function fn{i}(uint256 x) external {{ val += x + {i}; }}" for i in range(12))
    u_src = f"""
pragma solidity ^0.8.20;
contract BigPool {{
    address public owner;
    uint256 public val;
    modifier onlyOwner() {{ require(msg.sender == owner); _; }}
{fn_defs_u}
}}
"""
    f_src = f"""
pragma solidity ^0.8.20;
contract BigPool {{
    address public owner;
    uint256 public val;
{fn_defs_f}
}}
"""
    u_dir = tmp_path / "upstream"
    f_dir = tmp_path / "fork"
    (u_dir / "contracts").mkdir(parents=True)
    (u_dir / "test").mkdir(parents=True)
    (u_dir / "lib" / "forge-std").mkdir(parents=True)
    (f_dir / "src").mkdir(parents=True)
    (f_dir / "test").mkdir(parents=True)
    (u_dir / "contracts" / "BigPool.sol").write_text(u_src, encoding="utf-8")
    (f_dir / "src" / "BigPool.sol").write_text(f_src, encoding="utf-8")
    # Non-production / vendored paths must be excluded in tree mode
    (u_dir / "test" / "Mock.t.sol").write_text(u_src, encoding="utf-8")
    (f_dir / "test" / "Mock.t.sol").write_text(f_src, encoding="utf-8")
    (u_dir / "lib" / "forge-std" / "Test.sol").write_text(u_src, encoding="utf-8")

    findings, summary = analyze_solidity_fork_diff(
        {
            "contracts/BigPool.sol": u_src,
            "test/Mock.t.sol": u_src,
            "lib/forge-std/Test.sol": u_src,
        },
        {
            "src/BigPool.sol": f_src,
            "test/Mock.t.sol": f_src,
        },
    )
    assert len(findings) == 12
    assert summary["upstream_files"] == 1
    assert summary["fork_files"] == 1
    advisory = format_advisory(findings, max_bytes=250)
    assert len(advisory.encode("utf-8")) <= 250
    assert advisory.startswith("[mip-delta] ")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "mipdelta.cli",
            "--fork-diff",
            "--upstream",
            str(u_dir),
            "--root",
            str(f_dir),
            "--json",
            "--fail-on-findings",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "solidity-fork-diff"
    assert data["advisory_utf8_bytes"] <= 250
    assert data["findings_count"] == 12

    # `--before <dir> --after <dir>` also delegates cleanly to fork-diff
    proc2 = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "mipdelta.cli",
            "--before",
            str(u_dir),
            "--after",
            str(f_dir),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr
    data2 = json.loads(proc2.stdout)
    assert data2["mode"] == "solidity-fork-diff"
    assert data2["findings_count"] == 12
    assert cli  # ensure cli module import exercised
