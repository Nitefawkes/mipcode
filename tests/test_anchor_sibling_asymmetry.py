"""Unit and CLI integration tests for Anchor #[derive(Accounts)] sibling constraint asymmetry."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mipdelta import audit_anchor_workspace, cli, format_advisory, parse_anchor_file


def test_lexer_lifetimes_nested_comments_raw_strings_and_cfg_test():
    src = r'''
use anchor_lang::prelude::*;

/* outer comment /* nested block comment /* even deeper */ still comment */ end */
pub fn helper_raw() -> &'static str {
    let ch: char = '\'';
    let bch: u8 = b'x';
    let _msg = r#"
        #[derive(Accounts)]
        pub struct FakeFromRawString<'info> {
            pub authority: UncheckedAccount<'info>,
        }
    "#;
    _msg
}

#[derive(Accounts)]
#[instruction(nonce: u64)]
pub struct RealCtx<'info, 'a> {
    pub authority: Signer<'info>,
    #[account(
        mut,
        has_one = authority @ ErrorCode::BadAuth,
        constraint = state.active && (nonce > 0) @ ErrorCode::Inactive,
    )]
    pub state: Box<Account<'info, State>>,
    #[account(seeds = [b"escrow", authority.key().as_ref()], bump)]
    pub escrow: Option<AccountLoader<'info, Escrow>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Accounts)]
    pub struct UnitTestCtx<'info> {
        pub authority: UncheckedAccount<'info>,
    }
}
'''
    facts = parse_anchor_file("programs/demo/src/lib.rs", src)
    assert [s.name for s in facts.structs] == ["RealCtx"]
    real = facts.structs[0]
    assert real.instruction_params == "nonce: u64"
    by_name = {f.name: f for f in real.fields}
    assert by_name["authority"].wrapper_kind == "Signer"
    assert by_name["authority"].is_signer is True
    assert by_name["state"].wrapper_kind == "Account"
    assert by_name["state"].inner_type == "State"
    assert by_name["state"].is_mut is True
    assert by_name["state"].has_ones == ("authority",)
    assert by_name["state"].constraints == ("state.active && (nonce > 0)",)
    assert by_name["escrow"].wrapper_kind == "AccountLoader"
    assert by_name["escrow"].inner_type == "Escrow"
    assert by_name["escrow"].is_optional is True
    assert by_name["escrow"].has_seeds is True
    assert by_name["escrow"].has_bump is True


def test_has_one_asymmetry_and_negative_controls():
    src = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct InitializePool<'info> {
    #[account(init, payer = authority, space = 128)]
    pub pool: Account<'info, Pool>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct UpdateFee<'info> {
    #[account(mut, has_one = authority)]
    pub pool: Account<'info, Pool>,
    pub authority: Signer<'info>,
}

#[derive(Accounts)]
pub struct PausePool<'info> {
    #[account(mut, has_one = authority)]
    pub pool: Account<'info, Pool>,
    pub authority: Signer<'info>,
}

#[derive(Accounts)]
pub struct SetRate<'info> {
    #[account(mut, has_one = authority)]
    pub pool: Account<'info, Pool>,
    pub authority: Signer<'info>,
}

// VULNERABLE: declares both `pool` and `authority` without `has_one = authority`
#[derive(Accounts)]
pub struct WithdrawTreasury<'info> {
    #[account(mut)]
    pub pool: Account<'info, Pool>,
    pub authority: Signer<'info>,
}

// SAFE CONTROL: verifies ownership inline via `require_keys_eq!` in handler
#[derive(Accounts)]
pub struct SweepRewards<'info> {
    #[account(mut)]
    pub pool: Account<'info, Pool>,
    pub authority: Signer<'info>,
}

pub fn sweep_rewards(ctx: Context<SweepRewards>) -> Result<()> {
    require_keys_eq!(ctx.accounts.pool.authority, ctx.accounts.authority.key());
    Ok(())
}

// SAFE CONTROL: does not accept `authority` field at all
#[derive(Accounts)]
pub struct PermissionlessPoke<'info> {
    #[account(mut)]
    pub pool: Account<'info, Pool>,
}
"""
    findings = audit_anchor_workspace({"programs/vault/src/lib.rs": src})
    got = [(f.detector, f.symbol, f.evidence[0]) for f in findings]
    assert len(got) == 1
    assert got[0][0] == "anchor_sibling_constraint_asymmetry"
    assert got[0][1] == "WithdrawTreasury"
    assert "has_one = authority" in got[0][2]


def test_seeds_opposite_pair_and_equivalence_suppressions():
    src = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct MarginBorrow<'info> {
    #[account(mut)]
    pub margin_pool: AccountLoader<'info, MarginPool>,
    pub margin_account: AccountLoader<'info, MarginAccount>,
    pub loan_note_mint: InterfaceAccount<'info, Mint>,
    #[account(
        mut,
        seeds = [margin_account.key().as_ref(), loan_note_mint.key().as_ref()],
        bump,
    )]
    pub loan_account: InterfaceAccount<'info, TokenAccount>,
}

// VULNERABLE opposite: omits `seeds` and every other address/authority check on `loan_account`
#[derive(Accounts)]
pub struct MarginRepay<'info> {
    #[account(mut)]
    pub margin_pool: AccountLoader<'info, MarginPool>,
    pub margin_account: AccountLoader<'info, MarginAccount>,
    pub loan_note_mint: InterfaceAccount<'info, Mint>,
    #[account(mut)]
    pub loan_account: InterfaceAccount<'info, TokenAccount>,
}

// SAFE CONTROL 1: verified via `address = ...`
#[derive(Accounts)]
pub struct MarginBorrowV2<'info> {
    #[account(mut)]
    pub margin_pool: AccountLoader<'info, MarginPool>,
    pub margin_account: AccountLoader<'info, MarginAccount>,
    pub loan_note_mint: InterfaceAccount<'info, Mint>,
    #[account(mut, address = margin_account.load()?.loan_account)]
    pub loan_account: InterfaceAccount<'info, TokenAccount>,
}

// SAFE CONTROL 2: verified via handler `find_program_address`
#[derive(Accounts)]
pub struct MarginRepayV2<'info> {
    #[account(mut)]
    pub margin_pool: AccountLoader<'info, MarginPool>,
    pub margin_account: AccountLoader<'info, MarginAccount>,
    pub loan_note_mint: InterfaceAccount<'info, Mint>,
    #[account(mut)]
    pub loan_account: InterfaceAccount<'info, TokenAccount>,
}

pub fn margin_repay_v2(ctx: Context<MarginRepayV2>) -> Result<()> {
    let (expected, _) = Pubkey::find_program_address(&[ctx.accounts.margin_account.key().as_ref()], ctx.program_id);
    require_keys_eq!(ctx.accounts.loan_account.key(), expected);
    Ok(())
}
"""
    findings = audit_anchor_workspace({"programs/margin/src/instructions.rs": src})
    assert [(f.symbol, "seeds" in f.evidence[0]) for f in findings] == [("MarginRepay", True)]


def test_constraint_flag_predicate_and_program_signer_and_mut_checks():
    src = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct DepositVault<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut,
        has_one = authority,
        constraint = !margin.is_liquidating,
        constraint = config.load()?.vault_cfg() == Some(&vault_cfg.key()),
    )]
    pub config: AccountLoader<'info, Config>,
    #[account(mut)]
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
pub struct WithdrawVault<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut,
        has_one = authority,
        constraint = !margin.is_liquidating,
        constraint = config.load()?.vault_cfg() == Some(&vault_cfg.key()),
    )]
    pub config: AccountLoader<'info, Config>,
    #[account(mut)]
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
pub struct SwapVault<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut,
        has_one = authority,
        constraint = !margin.is_liquidating,
        constraint = config.load()?.vault_cfg() == Some(&vault_cfg.key()),
    )]
    pub config: AccountLoader<'info, Config>,
    #[account(mut)]
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
pub struct RebalanceVault<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut,
        has_one = authority,
        constraint = !margin.is_liquidating,
        constraint = config.load()?.vault_cfg() == Some(&vault_cfg.key()),
    )]
    pub config: AccountLoader<'info, Config>,
    #[account(mut)]
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

// VULNERABLE: misses `mut` on `margin` alongside `mut vault`, misses `has_one`, misses predicate on `config`,
// misses `!is_liquidating`, weakens `authority` to `UncheckedAccount`, weakens `token_program` to `AccountInfo`!
#[derive(Accounts)]
pub struct BadWithdrawFees<'info> {
    /// CHECK: omitted signer
    pub authority: UncheckedAccount<'info>,
    pub config: AccountLoader<'info, Config>,
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    /// CHECK: arbitrary program
    pub token_program: AccountInfo<'info>,
}

// SAFE EXEMPTION: LiquidateEnd is exempt from `not-liquidating`, and read-only view omits `mut` on `vault` too
#[derive(Accounts)]
pub struct LiquidateEnd<'info> {
    pub authority: Signer<'info>,
    #[account(mut, has_one = authority, constraint = config.load()?.vault_cfg() == Some(&vault_cfg.key()))]
    pub config: AccountLoader<'info, Config>,
    #[account(mut)]
    pub margin: Account<'info, MarginState>,
    pub vault_cfg: AccountLoader<'info, VaultConfig>,
    #[account(mut)]
    pub vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}
"""
    findings = audit_anchor_workspace({"programs/margin/src/treasury.rs": src})
    bad_msgs = [f.evidence[0] for f in findings if f.symbol == "BadWithdrawFees"]
    assert any("missing `mut` on `margin: MarginState`" in m or "missing `mut` on `config: Config`" in m for m in bad_msgs)
    assert any("not-liquidating" in m for m in bad_msgs)
    assert any("vault_cfg" in m for m in bad_msgs)
    assert any("missing `Signer` on `authority" in m for m in bad_msgs)
    assert any("unchecked `token_program: AccountInfo`" in m for m in bad_msgs)
    assert not any(f.symbol == "LiquidateEnd" for f in findings)


def test_program_crate_isolation_composite_v2_and_cli(tmp_path: Path):
    clean_prog = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct A<'info> { #[account(mut, has_one = owner)] pub st: Account<'info, St>, pub owner: Signer<'info> }
#[derive(Accounts)]
pub struct B<'info> { #[account(mut, has_one = owner)] pub st: Account<'info, St>, pub owner: Signer<'info> }
#[derive(Accounts)]
pub struct C<'info> { #[account(mut, has_one = owner)] pub st: Account<'info, St>, pub owner: Signer<'info> }
// Composite V2 struct embeds `A` -> must NOT be re-flagged
#[derive(Accounts)]
pub struct AV2<'info> { pub inner: A<'info>, pub extra: UncheckedAccount<'info> }
"""
    other_prog = """
use anchor_lang::prelude::*;
// Separate program crate with identical `St` name must NOT cross-pollinate with `prog_a`
#[derive(Accounts)]
pub struct Other<'info> { #[account(mut)] pub st: Account<'info, St>, pub owner: Signer<'info> }
"""
    test_prog = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct TestUnconstrained<'info> { #[account(mut)] pub st: Account<'info, St>, pub owner: Signer<'info> }
"""
    ws = {
        "programs/prog_a/src/lib.rs": clean_prog,
        "programs/prog_b/src/lib.rs": other_prog,
        "programs/prog_a/tests/integration_test.rs": test_prog,
    }
    assert audit_anchor_workspace(ws) == []

    # Now create an actual multi-finding workspace and test CLI `--audit-anchor --json` and `<= 250B` wire cap
    buggy_prog = tmp_path / "programs" / "demo" / "src" / "lib.rs"
    buggy_prog.parent.mkdir(parents=True)
    extra_clean = "\n".join(
        f"#[derive(Accounts)]\npub struct Good{i}<'info> {{ #[account(mut, has_one = owner)] pub st: Account<'info, St>, pub owner: Signer<'info> }}"
        for i in range(10)
    )
    buggy_src = (
        clean_prog
        + "\n"
        + extra_clean
        + "\n"
        + "\n".join(
            f"#[derive(Accounts)]\npub struct Bad{i}<'info> {{ #[account(mut)] pub st: Account<'info, St>, pub owner: Signer<'info> }}"
            for i in range(6)
        )
    )
    buggy_prog.write_text(buggy_src, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "mipdelta.cli",
            "--audit-anchor",
            "--root",
            str(tmp_path),
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
    assert data["mode"] == "audit-anchor"
    assert data["files_scanned"] == 1
    assert len(data["findings"]) == 6
    assert data["advisory_utf8_bytes"] <= 250
    assert data["advisory_text"].startswith("[mip-delta] ")
    assert format_advisory([], max_bytes=250) == ""
    assert cli  # keep cli referenced


def test_opposite_pair_predicate_multi_field_and_readonly_exemption():
    src = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct DepositToVaultBox<'info> {
    #[account(constraint = cfg.active_vault() == Some(&vault_cfg.key()))]
    pub cfg: AccountLoader<'info, VaultMaster>,
    #[account(constraint = vault_cfg.is_deposit_allowed(&mint.key())?)]
    pub vault_cfg: AccountLoader<'info, VaultSubCfg>,
    pub mint: InterfaceAccount<'info, Mint>,
}

// Opposite pair omits active_vault() (flagged), while direction-specific `is_deposit_allowed` vs
// `is_withdrawal_allowed` stays suppressed on opposite pairs (0 FPs)
#[derive(Accounts)]
pub struct WithdrawFromVaultBox<'info> {
    pub cfg: AccountLoader<'info, VaultMaster>,
    #[account(constraint = vault_cfg.is_withdrawal_allowed(&mint.key())?)]
    pub vault_cfg: AccountLoader<'info, VaultSubCfg>,
    pub mint: InterfaceAccount<'info, Mint>,
}

// Two fields of same custom state type `Header`: constraint on second field `secondary` satisfies predicate
#[derive(Accounts)]
pub struct LinkOne<'info> {
    #[account(mut, constraint = primary.load()?.code == token_cfg.key())]
    pub primary: AccountLoader<'info, Header>,
    pub token_cfg: AccountLoader<'info, CfgRef>,
}
#[derive(Accounts)]
pub struct LinkTwo<'info> {
    #[account(mut, constraint = primary.load()?.code == token_cfg.key())]
    pub primary: AccountLoader<'info, Header>,
    pub token_cfg: AccountLoader<'info, CfgRef>,
}
#[derive(Accounts)]
pub struct LinkThree<'info> {
    #[account(mut)]
    pub primary: AccountLoader<'info, Header>,
    pub token_cfg: AccountLoader<'info, CfgRef>,
    #[account(mut, constraint = secondary.load()?.code == token_cfg.key())]
    pub secondary: AccountLoader<'info, Header>,
}

// Read-only Calculate* action is exempt from Signer downgrade even when peer group mutates state
#[derive(Accounts)]
pub struct ActionA<'info> { #[account(mut)] pub st: Account<'info, Item>, pub holder: Signer<'info> }
#[derive(Accounts)]
pub struct ActionB<'info> { #[account(mut)] pub st: Account<'info, Item>, pub holder: Signer<'info> }
#[derive(Accounts)]
pub struct CalculateReward<'info> { #[account(mut)] pub st: Account<'info, Item>, pub holder: UncheckedAccount<'info> }
"""
    findings = audit_anchor_workspace({"programs/app/src/lib.rs": src})
    assert [(f.symbol, f.evidence[0].startswith("missing constraint `cfg.active_vault()")) for f in findings] == [
        ("WithdrawFromVaultBox", True)
    ]


def test_mechanical_polish_mock_crates_bitflags_and_option_key_normalization():
    prod_src = """
use anchor_lang::prelude::*;

// Multi-line block constraint checking ACCOUNT_FROZEN + IS_PAUSED + FLAG_LIQUIDATING
#[derive(Accounts)]
pub struct FlashStart<'info> {
    #[account(
        mut,
        has_one = authority,
        constraint = {
            let acc = user_acc.load()?;
            !acc.get_flag(ACCOUNT_FROZEN)
                && !acc.get_flag(IS_PAUSED)
                && !acc.get_flag(FLAG_LIQUIDATING)
        } @Err::Bad,
        constraint = order.load()?.escrow == Some(escrow.key()),
    )]
    pub user_acc: AccountLoader<'info, UserAcc>,
    #[account(mut)]
    pub order: AccountLoader<'info, OrdAcc>,
    pub escrow: Option<InterfaceAccount<'info, TokenAccount>>,
    pub authority: Signer<'info>,
}

// Sibling uses helper `account_not_frozen_for_authority`, `paused`, `is_liquidating`, and `.as_ref().map(|a| a.key())` -> ZERO FPs
#[derive(Accounts)]
pub struct FlashEnd<'info> {
    #[account(
        mut,
        has_one = authority,
        constraint = {
            let a = user_acc.load()?;
            account_not_frozen_for_authority(&a, authority.key()) && !a.paused && !a.is_liquidating
        },
        constraint = order.load()?.escrow == escrow.as_ref().map(|a| a.key()),
    )]
    pub user_acc: AccountLoader<'info, UserAcc>,
    #[account(mut)]
    pub order: AccountLoader<'info, OrdAcc>,
    pub escrow: Option<InterfaceAccount<'info, TokenAccount>>,
    pub authority: Signer<'info>,
}

// Third sibling uses Option::Some / .map(|x| x.key()) + Key::key equivalent and `paused` -> ZERO FPs
#[derive(Accounts)]
pub struct FlashSettle<'info> {
    #[account(
        mut,
        has_one = authority,
        constraint = !user_acc.load()?.is_frozen && !user_acc.load()?.paused && !user_acc.load()?.liquidating,
        constraint = order.load()?.escrow == Option::Some(escrow.key()),
    )]
    pub user_acc: AccountLoader<'info, UserAcc>,
    #[account(mut)]
    pub order: AccountLoader<'info, OrdAcc>,
    pub escrow: Option<InterfaceAccount<'info, TokenAccount>>,
    pub authority: Signer<'info>,
}

// Fourth sibling uses `.map(Key::key)` and all flags EXCEPT freeze guard, AND omits `has_one = authority` -> flagged for BOTH
#[derive(Accounts)]
pub struct BadSibling<'info> {
    #[account(
        mut,
        constraint = !user_acc.load()?.paused && !user_acc.load()?.liquidating,
        constraint = order.load()?.escrow == escrow.as_ref().map(Key::key),
    )]
    pub user_acc: AccountLoader<'info, UserAcc>,
    #[account(mut)]
    pub order: AccountLoader<'info, OrdAcc>,
    pub escrow: Option<InterfaceAccount<'info, TokenAccount>>,
    pub authority: Signer<'info>,
}
"""
    mock_crate_src = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct MockA<'info> { #[account(mut, has_one = owner)] pub m: Account<'info, MockSt>, pub owner: Signer<'info> }
#[derive(Accounts)]
pub struct MockB<'info> { #[account(mut, has_one = owner)] pub m: Account<'info, MockSt>, pub owner: Signer<'info> }
#[derive(Accounts)]
pub struct MockBad<'info> { #[account(mut)] pub m: Account<'info, MockSt>, pub owner: Signer<'info> }
"""
    findings = audit_anchor_workspace(
        {
            "programs/marginfi/src/flash.rs": prod_src,
            "programs/drift-mocks/src/lib.rs": mock_crate_src,
            "programs/kamino_mocks/src/lib.rs": mock_crate_src,
        }
    )
    by_sym_ev = [(f.symbol, f.evidence[0]) for f in findings]
    assert all(sym == "BadSibling" for sym, _ in by_sym_ev), by_sym_ev
    assert any("has_one = authority" in ev for _, ev in by_sym_ev)
    assert any("not-frozen/active guard" in ev for _, ev in by_sym_ev)
    assert not any("escrow" in ev for _, ev in by_sym_ev), by_sym_ev
    assert not any("MockBad" in sym for sym, _ in by_sym_ev), by_sym_ev


def test_impl_method_isolation_and_single_crate_src_scope_parity():
    state_bank_rs = """
use anchor_lang::prelude::*;

pub struct Bank {
    pub is_frozen: bool,
}

impl Bank {
    pub fn load(&self) -> Result<()> {
        require!(!self.is_frozen, Err::Frozen);
        Ok(())
    }

    pub fn helper_static() -> Result<()> {
        let disabled = false;
        require!(!disabled, Err::Disabled);
        Ok(())
    }
}
"""
    utils_rs = """
use anchor_lang::prelude::*;

pub fn assert_not_paused(acc: &VaultState) -> Result<()> {
    require!(!acc.paused, Err::Paused);
    Ok(())
}
"""
    instr_bank_rs = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct BankOpA<'info> {
    #[account(mut, has_one = authority)]
    pub bank: AccountLoader<'info, Bank>,
    pub authority: Signer<'info>,
}
pub fn bank_op_a(ctx: Context<BankOpA>) -> Result<()> {
    ctx.accounts.bank.load()?;
    Ok(())
}

#[derive(Accounts)]
pub struct BankOpB<'info> {
    #[account(mut, has_one = authority)]
    pub bank: AccountLoader<'info, Bank>,
    pub authority: Signer<'info>,
}
pub fn bank_op_b(ctx: Context<BankOpB>) -> Result<()> {
    ctx.accounts.bank.load()?;
    Bank::helper_static()?;
    Ok(())
}

#[derive(Accounts)]
pub struct BankOpC<'info> {
    #[account(mut, has_one = authority)]
    pub bank: AccountLoader<'info, Bank>,
    pub authority: Signer<'info>,
}
pub fn bank_op_c(ctx: Context<BankOpC>) -> Result<()> {
    ctx.accounts.bank.load()?;
    Ok(())
}

#[derive(Accounts)]
pub struct BankAdminConfig<'info> {
    #[account(mut, has_one = authority)]
    pub bank: AccountLoader<'info, Bank>,
    pub authority: Signer<'info>,
}
pub fn bank_admin_config(_ctx: Context<BankAdminConfig>) -> Result<()> {
    Ok(())
}
"""
    instr_vault_rs = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct OpOne<'info> {
    #[account(mut, has_one = authority)]
    pub vault: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}
pub fn op_one(ctx: Context<OpOne>) -> Result<()> {
    utils::assert_not_paused(&ctx.accounts.vault)?;
    Ok(())
}

#[derive(Accounts)]
pub struct OpTwo<'info> {
    #[account(mut, has_one = authority)]
    pub vault: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}
pub fn op_two(ctx: Context<OpTwo>) -> Result<()> {
    utils::assert_not_paused(&ctx.accounts.vault)?;
    Ok(())
}

#[derive(Accounts)]
pub struct OpThree<'info> {
    #[account(mut, has_one = authority)]
    pub vault: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}
pub fn op_three(ctx: Context<OpThree>) -> Result<()> {
    utils::assert_not_paused(&ctx.accounts.vault)?;
    Ok(())
}

#[derive(Accounts)]
pub struct OpFour<'info> {
    #[account(mut, has_one = authority)]
    pub vault: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}
pub fn op_four(_ctx: Context<OpFour>) -> Result<()> {
    Ok(())
}
"""
    rel_files = {
        "state/bank.rs": state_bank_rs,
        "utils.rs": utils_rs,
        "instructions/bank.rs": instr_bank_rs,
        "instructions/vault.rs": instr_vault_rs,
    }

    layouts = [
        {f"programs/demo/src/{k}": v for k, v in rel_files.items()},
        {f"demo/src/{k}": v for k, v in rel_files.items()},
        {f"src/{k}": v for k, v in rel_files.items()},
        rel_files,
    ]
    results = [
        [(f.symbol, f.evidence[0]) for f in audit_anchor_workspace(ws)]
        for ws in layouts
    ]
    expected = results[0]
    assert len(expected) == 1, expected
    assert expected[0][0] == "OpFour", expected
    assert "not-paused guard" in expected[0][1], expected
    assert not any("BankAdminConfig" in sym for sym, _ in expected), expected
    assert not any("not-frozen/active guard" in ev for _, ev in expected), expected
    for idx, res in enumerate(results[1:], start=1):
        assert res == expected, f"layout {idx} diverged: {res} != {expected}"

    # Sibling crates directly under `<crate>/src/` (e.g. `--root programs/`) stay isolated
    crate_a = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct AlphaA<'info> { #[account(mut, has_one = authority)] pub cfg: Account<'info, SharedCfg>, pub authority: Signer<'info> }
#[derive(Accounts)]
pub struct AlphaB<'info> { #[account(mut, has_one = authority)] pub cfg: Account<'info, SharedCfg>, pub authority: Signer<'info> }
"""
    crate_b = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct BetaOnly<'info> { #[account(mut)] pub cfg: Account<'info, SharedCfg>, pub authority: Signer<'info> }
"""
    assert audit_anchor_workspace({"demo_a/src/lib.rs": crate_a, "demo_b/src/lib.rs": crate_b}) == []


def test_ambiguous_multi_file_free_fn_does_not_leak_while_local_resolves():
    # Two files define `fn process()`, so a third file calling `process()` must NOT pick one arbitrarily
    helper_a = """
pub fn process(frozen: bool) { require!(!frozen); }
"""
    helper_b = """
pub fn process(_unused: bool) { }
"""
    caller_c = """
use anchor_lang::prelude::*;
#[derive(Accounts)]
pub struct CallA<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn c_a(ctx: Context<CallA>) { process(true); }
#[derive(Accounts)]
pub struct CallB<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn c_b(ctx: Context<CallB>) { process(true); }
#[derive(Accounts)]
pub struct CallC<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn c_c(ctx: Context<CallC>) { process(true); }
#[derive(Accounts)]
pub struct CallNone<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn c_none(_ctx: Context<CallNone>) { }
"""
    findings_ambig = audit_anchor_workspace(
        {
            "programs/app/src/h_a.rs": helper_a,
            "programs/app/src/h_b.rs": helper_b,
            "programs/app/src/caller.rs": caller_c,
        }
    )
    assert findings_ambig == [], findings_ambig

    # Same-file `fn process()` MUST still resolve locally even when another file also defines `fn process()`
    local_mod = """
use anchor_lang::prelude::*;
fn process(paused: bool) { require!(!paused); }
#[derive(Accounts)]
pub struct LocalA<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn l_a(_ctx: Context<LocalA>) { process(false); }
#[derive(Accounts)]
pub struct LocalB<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn l_b(_ctx: Context<LocalB>) { process(false); }
#[derive(Accounts)]
pub struct LocalC<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn l_c(_ctx: Context<LocalC>) { process(false); }
#[derive(Accounts)]
pub struct LocalBad<'info> { #[account(mut, has_one = authority)] pub acc: Account<'info, Acc>, pub authority: Signer<'info> }
pub fn l_bad(_ctx: Context<LocalBad>) { }
"""
    findings_local = audit_anchor_workspace(
        {
            "programs/app/src/local.rs": local_mod,
            "programs/app/src/other.rs": helper_b,
        }
    )
    assert len(findings_local) == 1, findings_local
    assert findings_local[0].symbol == "LocalBad"
    assert "not-paused guard" in findings_local[0].evidence[0]

