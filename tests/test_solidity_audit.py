"""Unit and CLI tests for zero-dependency Solidity structural auditing (`--audit-solidity`)."""
from __future__ import annotations

import json

from mipdelta import analyze_solidity_delta, audit_solidity_workspace, cli


def test_try_catch_outbox_sink_detection_and_safe_controls():
    vulnerable = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IGateway {
    function send(address to, bytes calldata data) external returns (bytes32);
}

contract Bridge {
    struct Outbox {
        address gateway;
        bytes32 id;
    }
    event Failed(address indexed gateway);
    event OutboxDetails(bytes32 indexed sendId, Outbox[] outbox);

    function sendMessage(address[] memory gateways, address to, bytes calldata data) external returns (bytes32) {
        Outbox[] memory outbox = new Outbox[](gateways.length);
        for (uint256 i = 0; i < gateways.length; ++i) {
            try IGateway(gateways[i]).send(to, data) returns (bytes32 id) {
                outbox[i] = Outbox(gateways[i], id);
            } catch {
                emit Failed(gateways[i]);
            }
        }
        bytes32 sendId = keccak256(abi.encode(outbox));
        emit OutboxDetails(sendId, outbox);
        return sendId;
    }
}
"""
    findings = audit_solidity_workspace({"contracts/Bridge.sol": vulnerable})
    assert [(f.detector, f.symbol) for f in findings] == [
        ("try_catch_sink_review", "Bridge.sendMessage")
    ]

    safe_revert = vulnerable.replace("emit Failed(gateways[i]);", "revert();")
    assert audit_solidity_workspace({"contracts/Bridge.sol": safe_revert}) == []

    safe_populated = vulnerable.replace(
        "emit Failed(gateways[i]);",
        "outbox[i] = Outbox(gateways[i], bytes32(0));",
    )
    assert audit_solidity_workspace({"contracts/Bridge.sol": safe_populated}) == []


def test_dead_private_constant_and_upgradeable_gap_control():
    src = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Vault {
    uint256 private constant _VIRTUAL_SHARES = 1000;
    uint256 private constant _ACTIVE_SCALE = 1e18;
    uint256[49] private __gap;

    function preview(uint256 assets) external pure returns (uint256) {
        return assets * _ACTIVE_SCALE;
    }
}
"""
    findings = audit_solidity_workspace({"Vault.sol": src})
    assert [(f.detector, f.symbol) for f in findings] == [
        ("state_sink_review", "Vault._VIRTUAL_SHARES")
    ]


def test_unused_param_wrapper_and_override_modifier_drops():
    src = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Base {
    modifier nonReentrant() {
        _;
    }

    function withdraw(uint256 amount) public virtual nonReentrant {
        _burn(amount);
    }

    function _burn(uint256 amount) internal {
        uint256 x = amount;
        x;
    }

    function _swap(address tokenIn, uint256 amountIn, uint256 minOut, uint256 deadline) internal pure returns (uint256) {
        require(block.timestamp <= deadline, "expired");
        return amountIn + minOut + (tokenIn == address(0) ? 0 : 1);
    }
}

contract Derived is Base {
    function withdraw(uint256 amount) public override {
        _burn(amount);
    }

    function routerSwap(address tokenIn, uint256 amountIn, uint256 minOut, uint256 deadline, address unusedFeeTo)
        external
        pure
        returns (uint256)
    {
        return _swap(tokenIn, amountIn, 0, deadline);
    }
}
"""
    findings = audit_solidity_workspace({"Router.sol": src})
    pairs = {(f.detector, f.symbol) for f in findings}
    assert ("override_modifier_review", "Derived.withdraw") in pairs
    assert ("delegating_wrapper_review", "Derived.routerSwap") in pairs
    assert ("parameter_sink_review", "Derived.routerSwap") in pairs

    # Calling super.withdraw preserves base nonReentrant modifier execution -> no override_modifier_review
    safe_override = src.replace("function withdraw(uint256 amount) public override {\n        _burn(amount);", "function withdraw(uint256 amount) public override {\n        super.withdraw(amount);")
    safe_pairs = {(f.detector, f.symbol) for f in audit_solidity_workspace({"Router.sol": safe_override})}
    assert ("override_modifier_review", "Derived.withdraw") not in safe_pairs


    # Branch-forwarded parameter (`to` forwarded on else-branch, handled separately on if-branch) -> no warning
    branch_forwarded = """
pragma solidity ^0.8.20;
contract SwapRouter {
    function _pull(address from, address to, uint256 amount) internal pure returns (uint256) {
        return from == to ? amount : amount + 1;
    }
    function transfer(address from, address to, uint256 amount, bool unwrap) external view returns (uint256) {
        if (unwrap) {
            return _pull(from, address(this), amount);
        }
        return _pull(from, to, amount);
    }
}
"""
    assert audit_solidity_workspace({"SwapRouter.sol": branch_forwarded}) == []


def test_solidity_delta_and_cli(tmp_path, capsys):
    v1 = """
pragma solidity ^0.8.20;
contract Vault {
    function deposit(uint256 x) external pure returns (uint256) {
        return x + 1;
    }
}
"""
    v2 = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 private constant _VIRTUAL_SHARES = 1000;
    function deposit(uint256 x) external pure returns (uint256) {
        require(x > 0, "zero");
        return x + 1;
    }
}
"""
    before_p = tmp_path / "Vault_v1.sol"
    after_p = tmp_path / "Vault_v2.sol"
    before_p.write_text(v1, encoding="utf-8")
    after_p.write_text(v2, encoding="utf-8")

    findings, meta = analyze_solidity_delta({"Vault.sol": v1}, {"Vault.sol": v2})
    assert [(f.detector, f.symbol) for f in findings] == [("state_sink_review", "Vault._VIRTUAL_SHARES")]
    assert meta["added_state_vars"] == ["Vault._VIRTUAL_SHARES"]
    assert meta["added_guards"] == {"Vault.deposit": ["x"]}

    capsys.readouterr()
    rc_audit = cli.main_cli(["--audit-solidity", "--root", str(after_p), "--fail-on-findings", "--format", "github"])
    assert rc_audit == 1
    gh = capsys.readouterr().out.strip()
    assert gh.startswith("::warning file=Vault_v2.sol,")
    assert "state_sink_review" in gh

    rc_pair = cli.main_cli(["--before", str(before_p), "--after", str(after_p), "--json"])
    assert rc_pair == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "solidity-delta"
    assert len(out["findings"]) == 1
    assert out["findings"][0]["symbol"] == "Vault._VIRTUAL_SHARES"
