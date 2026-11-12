"""Obligations stay open across edits until they are resolved, reverted, or waived."""
from mipdelta.ledger import ObligationLedger, format_open_obligations
from mipdelta.session import DeltaSession

LIB = "def load(p):\n    return p, 1\n"
LIB_THREE = "def load(p):\n    return p, 1, 2\n"
APP = "from lib import load\n\ndef use():\n    a, b = load('x')\n    return a\n"
APP_FIXED = "from lib import load\n\ndef use():\n    a, b, c = load('x')\n    return a\n"


def _session(**extra):
    return DeltaSession.from_workspace({"lib.py": LIB, "app.py": APP, **extra})


def test_fixing_the_flagged_site_discharges_the_obligation():
    session = _session()
    opened = session.apply_write("lib.py", LIB_THREE)
    assert "app.py:4 return_contract_review" in opened.advisory_text
    assert opened.open_obligations == 1

    fixed = session.apply_write("app.py", APP_FIXED)
    assert fixed.advisory_text == ""
    assert [f.path for f in fixed.discharged_findings] == ["app.py"]
    assert fixed.open_obligations == 0
    assert session.finalize_advisory() == ""


def test_reverting_the_contract_change_discharges_the_obligation():
    session = _session()
    session.apply_write("lib.py", LIB_THREE)
    reverted = session.apply_write("lib.py", LIB)
    assert len(reverted.discharged_findings) == 1
    assert session.open_obligations() == ()


def test_stale_caller_written_after_the_contract_change_is_reported():
    session = DeltaSession.from_workspace({"lib.py": "def load(p):\n    return p\n", "app.py": "x = 1\n"})
    first = session.apply_write("lib.py", "def load(p, *, strict):\n    return p if strict else None\n")
    assert first.advisory_text == ""

    late = session.apply_write("app.py", "from lib import load\n\ndef use():\n    return load('x')\n")
    assert "app.py:4 required_keyword_missing" in late.advisory_text
    assert late.open_obligations == 1


def test_an_open_obligation_is_announced_once():
    session = _session(**{"other.py": "def helper():\n    return 1\n"})
    session.apply_write("lib.py", LIB_THREE)

    unrelated = session.apply_write("other.py", "def helper():\n    return 2\n")
    assert unrelated.advisory_text == ""
    assert unrelated.open_obligations == 1

    body_only = session.apply_write("lib.py", "def load(p):\n    value = p\n    return value, 1, 2\n")
    assert body_only.advisory_text == ""
    assert body_only.open_obligations == 1


def test_lines_added_above_a_site_do_not_reopen_it():
    session = _session()
    session.apply_write("lib.py", LIB_THREE)
    shifted = session.apply_write("app.py", "import os\nimport sys\n" + APP)
    assert shifted.advisory_text == ""
    assert shifted.discharged_findings == ()
    assert [item.finding.start_line for item in session.open_obligations()] == [6]


def test_identical_sites_in_one_scope_are_counted_separately():
    binding = "from lib import dump\n\nA = prop(dump)\nB = prop(dump)\nC = prop(dump)\n"
    session = DeltaSession.from_workspace(
        {"lib.py": "def dump(v):\n    return v\n", "table.py": binding},
        enable=["callback_binding_review"],
    )
    opened = session.apply_write("lib.py", "def dump(v, *, strict):\n    return v if strict else None\n")
    assert opened.open_obligations == 3

    one_fixed = session.apply_write(
        "table.py", binding.replace("B = prop(dump)", "B = prop(lambda v: dump(v, strict=True))")
    )
    assert one_fixed.advisory_text == ""
    assert len(one_fixed.discharged_findings) == 1
    assert one_fixed.open_obligations == 2


def test_waived_obligation_is_not_listed_or_announced_again():
    session = _session()
    session.apply_write("lib.py", LIB_THREE)
    (item,) = session.open_obligations()
    assert session.waive(item.id, "old callers are removed in the next change")
    assert not session.waive("ffffffff")
    assert session.open_obligations() == ()
    assert session.finalize_advisory() == ""

    later = session.apply_write("lib.py", "def load(p):\n    q = p\n    return q, 1, 2\n")
    assert later.advisory_text == ""
    assert later.open_obligations == 0


def test_finalize_advisory_states_the_total_and_summarizes_overflow():
    callers = {
        f"pkg/mod{index}.py": "from lib import load\n\ndef use():\n    a, b = load('x')\n    return a\n"
        for index in range(12)
    }
    session = DeltaSession.from_workspace({"lib.py": LIB, **callers})
    session.apply_write("lib.py", LIB_THREE)
    text = session.finalize_advisory(max_bytes=300)
    assert text.startswith("[mip-delta] 12 open: pkg/mod0.py:4 return_contract_review[static] load")
    assert len(text.encode("utf-8")) <= 300
    assert "(+" in text and "files)" in text


def test_ledger_round_trips_through_json_data():
    session = _session()
    session.apply_write("lib.py", LIB_THREE)
    (item,) = session.open_obligations()
    session.ledger.mark_reported_at_stop([item])

    restored = ObligationLedger.from_dict(session.ledger.to_dict())
    assert [(o.id, o.anchor, o.finding) for o in restored.open()] == [(item.id, "use", item.finding)]
    assert restored.unreported_at_stop() == ()
    # The same findings are neither new nor discharged for the restored ledger.
    update = restored.update([item.finding], lambda finding: "use")
    assert update.opened == () and update.discharged == ()
    assert format_open_obligations(update.open, show_ids=True).endswith(f"#{item.id}")
