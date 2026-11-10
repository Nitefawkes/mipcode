"""Generate the fresh, constructed sibling-traversal fixtures."""
import json


def case(ticket, files, reference, incomplete, oracle, anchors):
    return dict(ticket=ticket, files=files, reference=reference,
                incomplete=incomplete, oracle=oracle, anchors=anchors)


units = "def count_crates(quantity, size):\n    return quantity // size\n"
quote = "from warehouse import count_crates as pack\n\ndef checkout_quote(quantity):\n    return pack(quantity, 6) * 4\n"
dispatch = "from warehouse import count_crates as allocate\n\ndef manifest_row(quantity):\n    return {'crates': allocate(quantity, 6)}\n"
new_units = "def count_crates(quantity, size, *, partial):\n    return (quantity + size - 1) // size if partial else quantity // size\n"
new_quote = quote.replace("pack(quantity, 6)", "pack(quantity, 6, partial=True)")
new_dispatch = dispatch.replace("allocate(quantity, 6)", "allocate(quantity, 6, partial=True)")

summary = "def summarize(samples):\n    return min(samples, default=None), max(samples, default=None)\n"
screen = "from samples import summarize as bounds\n\ndef dashboard_card(samples):\n    low, high = bounds(samples)\n    return {'low': low, 'high': high}\n"
archive = "from samples import summarize as reduce_batch\n\ndef record(samples):\n    low, high = reduce_batch(samples)\n    return [low, high]\n"
new_summary = summary.replace("max(samples, default=None)", "max(samples, default=None), len(samples)")
new_screen = screen.replace("low, high = ", "low, high, _ = ")
new_archive = archive.replace("low, high", "low, high, count")

parse = "def parse_rank(text):\n    return int(text)\n"
index = "from ranks import parse_rank as rank\n\ndef priority_index(text):\n    try:\n        return rank(text)\n    except ValueError:\n        return 0\n"
message = "from ranks import parse_rank as decode\n\ndef display_value(text):\n    try:\n        return str(decode(text))\n    except ValueError:\n        return '?'\n"
new_parse = "class InvalidRank(Exception):\n    pass\n\ndef parse_rank(text):\n    try:\n        return int(text)\n    except ValueError as exc:\n        raise InvalidRank(text) from exc\n"
def translate(source):
    return source.replace("from ranks import ", "from ranks import InvalidRank, ").replace("except ValueError:", "except InvalidRank:")


cases = {
    "SB01": case(
        "Update checkout_quote so a partial crate is billed as a full crate. Shipping manifests must report the same rounded-up crate count. Make the shared calculation require an explicit keyword named partial so callers choose their policy. Preserve exact multiples and zero.",
        {"warehouse.py": units, "sales.py": quote, "shipping.py": dispatch,
         "warehouse_old.py": units},
        {"warehouse.py": new_units, "sales.py": new_quote, "shipping.py": new_dispatch},
        {"warehouse.py": new_units, "sales.py": new_quote},
        "from sales import checkout_quote\nfrom shipping import manifest_row\nfrom warehouse import count_crates\nimport inspect\nassert checkout_quote(7) == 8\nassert manifest_row(7) == {'crates': 2}\nassert manifest_row(12) == {'crates': 2}\nassert checkout_quote(0) == 0\nassert inspect.signature(count_crates).parameters['partial'].default is inspect.Parameter.empty\n",
        {"sales.py": [4], "warehouse.py": [1, 2], "shipping.py": [4]},
    ),
    "SB02": case(
        "Keep dashboard_card at exactly its two existing fields. Extend the shared sample summary to carry the sample count as its third item and add that count to persisted batch records. Preserve the existing empty-input values.",
        {"samples.py": summary, "display.py": screen, "storage.py": archive,
         "debug.py": "def summarize(values):\n    return ','.join(map(str, values))\n"},
        {"samples.py": new_summary, "display.py": new_screen, "storage.py": new_archive},
        {"samples.py": new_summary, "display.py": new_screen,
         "storage.py": archive.replace("low, high = ", "low, high, _ = ")},
        "from display import dashboard_card\nfrom samples import summarize\nfrom storage import record\nassert dashboard_card([2, 8]) == {'low': 2, 'high': 8}\nassert summarize([2, 8]) == (2, 8, 2)\nassert record([2, 8]) == [2, 8, 2]\nassert record([]) == [None, None, 0]\n",
        {"display.py": [4, 5], "samples.py": [1, 2], "storage.py": [4, 5]},
    ),
    "SB03": case(
        "Have parse_rank raise InvalidRank for malformed inputs. Preserve the zero fallback for the priority index and the question-mark fallback in user-visible messages. Valid numbers should behave as before.",
        {"ranks.py": parse, "queueing.py": index, "labels.py": message},
        {"ranks.py": new_parse, "queueing.py": translate(index), "labels.py": translate(message)},
        {"ranks.py": new_parse, "queueing.py": translate(index)},
        "import ranks\nfrom queueing import priority_index\nfrom labels import display_value\nassert priority_index('bad') == 0\nassert display_value('bad') == '?'\nassert display_value('42') == '42'\ntry:\n    ranks.parse_rank('bad')\nexcept getattr(ranks, 'InvalidRank', AssertionError):\n    pass\nelse:\n    raise AssertionError('expected InvalidRank')\n",
        {"ranks.py": [1, 2], "queueing.py": [5, 6, 7], "labels.py": [5, 6, 7]},
    ),
    "SB04": case(
        "Update clamp_progress so percentages above 100 are capped at 100. Preserve the existing zero floor and fractional values inside the valid range.",
        {"progress.py": "def clamp_progress(percent):\n    return max(0, percent)\n",
         "debug.py": "def clamp_level(level):\n    return min(level, 1)\n"},
        {"progress.py": "def clamp_progress(percent):\n    return min(100, max(0, percent))\n"},
        {"progress.py": "def clamp_progress(percent):\n    return int(min(100, max(0, percent)))\n"},
        "from progress import clamp_progress\nassert clamp_progress(175) == 100\nassert clamp_progress(-2) == 0\nassert clamp_progress(14.5) == 14.5\n",
        {"progress.py": [1, 2]},
    ),
}
for item in cases.values():
    for n in range(24):
        item["files"][f"extras/utility_{n:02}.py"] = f"def utility_{n:02}(value):\n    return value\n"
print(json.dumps(cases, indent=2))
