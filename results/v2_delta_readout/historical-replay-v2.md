# Historical Delta Replay v2 (Incremental Session + Tightened Precision)

- Manifest SHA-256: `4f5716986f88de11cbe8cf5b9426a840d93a4d093054a5c67d9229b441583281`
- External evidence pins verified: `17` (v1 directory untouched: `True`)
- Candidate volume: **`53` (v1) -> `10` (v2)** (`-81.13%` chaff eliminated)
- Cold full-package wall median / p95: `388.03 ms` / `1118.80 ms`
- Warm incremental **per-patch** wall median / p95: **`26.24 ms` / `39.46 ms`**
- Warm incremental **per-file write** wall median / p95: **`8.32 ms` / `31.49 ms`**

## Per-Case Comparison (v1 vs v2)

| Case | Files (Changed) | v1 Cands | v2 Cands | Warmup ms | Custody ms | Warm Patch Wall ms | Warm Per-File Wall ms | Advisory Bytes | Witness Outcomes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| T10-A-live | 52 (4) | 3 | **0** | 332.3 | 1.61 | **35.89** | **8.97** | 0 | mimeaccept_negotiation=no_anchor_overlap, multipart_generated_header=known_gap_not_detected |
| T10-B-live | 52 (4) | 3 | **0** | 303.3 | 1.60 | **39.46** | **9.86** | 0 | mimeaccept_negotiation=no_anchor_overlap, multipart_generated_header=known_gap_not_detected |
| T11-A-live | 52 (4) | 6 | **0** | 300.9 | 1.69 | **22.97** | **5.74** | 0 | descriptor_dump_header_bindings=correct_abstention |
| T11-B-live | 52 (4) | 6 | **3** | 333.6 | 2.15 | **25.57** | **6.39** | 241 | descriptor_dump_header_bindings=actionable_warning_found |
| T35-E05-live | 8 (3) | 0 | **0** | 23.0 | 0.30 | **9.67** | **3.22** | 0 | parent_directory_umask_behavior=no_anchor_overlap |
| T35-E06-live | 8 (5) | 0 | **0** | 22.6 | 0.26 | **13.75** | **2.75** | 0 | parent_directory_umask_behavior=no_anchor_overlap |
| T44-definition-only-source-only | 129 (1) | 2 | **1** | 897.9 | 4.20 | **7.68** | **7.68** | 95 | observer_threshold_state_symmetry=off_predicate_candidate_not_scored |
| T45-definition-only-source-only | 136 (1) | 12 | **3** | 1024.3 | 5.39 | **26.90** | **26.90** | 215 | base_iri_forwarding=off_predicate_candidate_not_scored |
| T45-missed-seam-source-only | 136 (1) | 10 | **1** | 1124.9 | 5.07 | **31.49** | **31.49** | 85 | conjunctive_graph_parse_forwarding=abstained |
| T45-invariant-break-source-only | 136 (1) | 11 | **2** | 1102.0 | 6.11 | **27.41** | **27.41** | 161 | nullable_base_iri_truthiness=off_predicate_candidate_not_scored |

## Complete Per-Finding Adjudication Ledger (All Emitted v2 Candidates)

| Case | Path:Line | Detector | Symbol | Classification | Rationale |
|---|---|---|---|---|---|
| T11-B-live | `werkzeug/sansio/response.py:713` | `callback_binding_review` | `dump_header` | `true_positive_adjudicated_defect` | Direct dump_header callback passed to header_property at response.py:713-735 without strict=True wrapper (confirmed by P08 blind review). |
| T11-B-live | `werkzeug/sansio/response.py:720` | `callback_binding_review` | `dump_header` | `true_positive_adjudicated_defect` | Direct dump_header callback passed to header_property at response.py:713-735 without strict=True wrapper (confirmed by P08 blind review). |
| T11-B-live | `werkzeug/sansio/response.py:732` | `callback_binding_review` | `dump_header` | `true_positive_adjudicated_defect` | Direct dump_header callback passed to header_property at response.py:713-735 without strict=True wrapper (confirmed by P08 blind review). |
| T44-definition-only-source-only | `docutils/utils/__init__.py:181` | `state_field_review` | `Reporter.notify_observers` | `diagnostic_relevant_off_frozen_predicate` | Identifies parallel self._observer_minimum_levels written in attach/detach but unread in notify_observers (T44 root-cause target; off v1 frozen predicate). |
| T45-definition-only-source-only | `rdflib/graph.py:1527` | `parameter_sink_review` | `Graph.parse` | `diagnostic_relevant_unforwarded_parameter` | Newly added base_iri parameter is accepted in signature without being read or forwarded in method body. |
| T45-definition-only-source-only | `rdflib/graph.py:2439` | `parameter_sink_review` | `ConjunctiveGraph.parse` | `diagnostic_relevant_unforwarded_parameter` | Newly added base_iri parameter is accepted in signature without being read or forwarded in method body. |
| T45-definition-only-source-only | `rdflib/graph.py:2788` | `parameter_sink_review` | `Dataset.parse` | `diagnostic_relevant_unforwarded_parameter` | Newly added base_iri parameter is accepted in signature without being read or forwarded in method body. |
| T45-missed-seam-source-only | `rdflib/graph.py:2444` | `parameter_sink_review` | `ConjunctiveGraph.parse` | `diagnostic_relevant_unforwarded_parameter` | Newly added base_iri parameter is accepted in signature without being read or forwarded in method body. |
| T45-invariant-break-source-only | `rdflib/graph.py:1627` | `parameter_truthiness_review` | `Graph.parse` | `diagnostic_relevant_nullable_truthiness` | Newly added None-default parameter base_iri tested via bare `if base_iri:` which conflates empty string base IRI '' with None. |
| T45-invariant-break-source-only | `rdflib/graph.py:2500` | `parameter_truthiness_review` | `ConjunctiveGraph.parse` | `diagnostic_relevant_nullable_truthiness` | Newly added None-default parameter base_iri tested via bare `if base_iri:` which conflates empty string base IRI '' with None. |
