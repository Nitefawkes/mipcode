import json
from pathlib import Path
from textwrap import dedent

def text(value):
    return dedent(value).lstrip("\n")

def case(ticket, files, reference, bad, oracle, anchors):
    return dict(ticket=ticket, files={p:text(s) for p,s in files.items()},
                reference={p:text(s) for p,s in reference.items()},
                incomplete={p:text(s) for p,s in bad.items()},
                oracle=text(oracle), anchors=anchors)

cases={}
calendar='def duration(start, end):\n    return end - start\n'
clock='def duration(start, end):\n    return max(0, end - start)\n'
booking='from span import duration as elapsed\n\ndef booking_hours(start, end):\n    return elapsed(start, end)\n'
statement='from span import duration as width\n\ndef amount_due(start, end):\n    return width(start, end) * 8\n'
cases['OS01']=case(
 "Overnight bookings should bill the elapsed time across midnight. Make duration require an explicit day length so callers choose their clock. Both booking quotes and invoices use 24-hour days. Keep same-day behavior.",
 {'span.py':calendar,'bookings.py':booking,'statements.py':statement,'diagnostics.py':"def duration(first, last):\n    return last - first\n"},
 {'span.py':'def duration(start, end, *, day):\n    return (end - start) % day if end < start else end - start\n',
  'bookings.py':booking.replace('elapsed(start, end)','elapsed(start, end, day=24)'),
  'statements.py':statement.replace('width(start, end)','width(start, end, day=24)')},
 {'span.py':'def duration(start, end, *, day):\n    return (end - start) % day if end < start else end - start\n',
  'bookings.py':booking.replace('elapsed(start, end)','elapsed(start, end, day=24)')},
 """
 from bookings import booking_hours
 from statements import amount_due
 from span import duration
 import inspect
 assert booking_hours(22, 2) == 4
 assert amount_due(22, 2) == 32
 assert amount_due(2, 6) == 32
 assert inspect.signature(duration).parameters['day'].default is inspect.Parameter.empty
 """,
 {'span.py':[1,2], 'bookings.py':[4], 'statements.py':[4]})
lookup='def resolve_label(labels, code):\n    return labels[code]\n'
lookup_after='class UnknownLabel(LookupError):\n    pass\n\ndef resolve_label(labels, code):\n    try:\n        return labels[code]\n    except KeyError as exc:\n        raise UnknownLabel(code) from exc\n'
receipt='from lookup import resolve_label as label_for\n\ndef print_receipt(labels, code):\n    try:\n        return label_for(labels, code)\n    except KeyError:\n        return "(unlisted)"\n'
feed='from lookup import resolve_label as render\n\ndef summarize(labels, code):\n    try:\n        return {"label": render(labels, code)}\n    except KeyError:\n        return {"label": None}\n'
def adapt(source):
    return source.replace(' as label_for',' as label_for, UnknownLabel').replace(' as render',' as render, UnknownLabel').replace('except KeyError:', 'except UnknownLabel:')
cases['OS02']=case(
 "A missing label should raise a domain-specific UnknownLabel from resolve_label. Preserve the printable receipt fallback and the feed summary fallback. A lookup that succeeds should keep the same result.",
 {'lookup.py':lookup,'checkout.py':receipt,'feed.py':feed,'legacy.py':'def resolve_label(registry, key):\n    return registry.get(key)\n'},
 {'lookup.py':lookup_after,'checkout.py':adapt(receipt),'feed.py':adapt(feed)},
 {'lookup.py':lookup_after,'checkout.py':adapt(receipt)},
 """
 from checkout import print_receipt
 from feed import summarize
 import lookup
 assert print_receipt({}, "x") == "(unlisted)"
 assert summarize({}, "x") == {"label": None}
 assert summarize({"x":"Red"}, "x") == {"label":"Red"}
 try:
     lookup.resolve_label({}, "x")
 except getattr(lookup, "UnknownLabel", AssertionError):
     pass
 else:
     raise AssertionError("domain exception required")
 """,
 {'lookup.py':[1,2], 'checkout.py':[5,6,7], 'feed.py':[5,6,7]})
core='def measure(values):\n    return sum(values), len(values)\n'
api='from stats import measure as collect\n\ndef public_summary(values):\n    total, count = collect(values)\n    return {"total": total, "count": count}\n'
audit='from stats import measure as sample\n\ndef export_row(values):\n    total, count = sample(values)\n    return [total, count]\n'
core_after='def measure(values):\n    return sum(values), len(values), max(values, default=None)\n'
cases['OS03']=case(
 "Extend measure to return the maximum as a third item. Keep the public summary response at exactly its old two fields. Include the maximum as the third export column. For empty values the maximum is None.",
 {'stats.py':core,'api.py':api,'rows.py':audit,'metrics.py':'def measure(value):\n    return float(value)\n'},
 {'stats.py':core_after,'api.py':api.replace('total, count =','total, count, _ ='),
  'rows.py':audit.replace('total, count','total, count, maximum')},
 {'stats.py':core_after,'api.py':api.replace('total, count =','total, count, maximum =').replace('"count": count','"count": count, "maximum": maximum'),
  'rows.py':audit.replace('total, count','total, count, maximum')},
 """
 from stats import measure
 from api import public_summary
 from rows import export_row
 assert measure([3, 8]) == (11, 2, 8)
 assert public_summary([3, 8]) == {"total":11, "count":2}
 assert export_row([3, 8]) == [11, 2, 8]
 assert export_row([]) == [0, 0, None]
 """,
 {'stats.py':[1,2], 'api.py':[4,5], 'rows.py':[4,5]})
cases['OS04']=case(
 "When extracting nonempty tags, skip entries that contain whitespace only. Keep surrounding whitespace on nonempty entries and preserve input order.",
 {'tags.py':'def nonempty_tags(tags):\n    return [tag for tag in tags if tag]\n',
  'notes.py':'def normalize_tags(tags):\n    return sorted(tags)\n'},
 {'tags.py':'def nonempty_tags(tags):\n    return [tag for tag in tags if tag.strip()]\n'},
 {'tags.py':'def nonempty_tags(tags):\n    return [tag.strip() for tag in tags if tag.strip()]\n'},
 """
 from tags import nonempty_tags
 assert nonempty_tags(["", "  ", " a ", "b", "\t", "a"]) == [" a ", "b", "a"]
 """, {'tags.py':[1,2]})
# Unrelated modules provide a small, uniform distractor set across all arms.
for case_data in cases.values():
    for i in range(24):
        case_data['files'][f'helpers/aux_{i:02}.py']=f'def utility_{i:02}(value):\n    return value\n'
print(json.dumps(cases, indent=2))

