# E1 Canonical revision ID iterator API audit

> Follow-up status (2026-09-30): the test-helper and assertion defects noted below were repaired in a separate test-only change, accepted in [revision ID iterator test hardening](2026-09-30-e1-revision-ids-test-hardening.md). The original audit result remains tied to candidate `dd3a965`.

**Result: ACCEPT WITH LIMITATIONS for the API boundary only.** This is not an E1-CAP-1 or Phase 1 acceptance.

## Scope and findings

The read-only audit examined `CanonicalUnitNormalized` and `CanonicalNormalizer.iter_revision_ids()` in the integration candidate `codex/e1-phase1-progress@dd3a965`. The result object retains scalar counters and identity/timing fields; it does not retain an O(N) revision ID collection. `iter_revision_ids()` pins a view, re-proves the unit, and yields IDs in Raw-position order one bounded microbatch at a time. The position index is disk-backed and is closed in a `finally` block.

A direct probe verified:

```text
ordered_ids=3; tampered_count=REJECTED; early_close=all position indexes closed
empty_unit_ids=0
```

The valid focused pytest command was:

```text
uv run pytest -q tests/infrastructure/canonical/test_normalizer.py::test_a_rest_unit_the_store_has_not_finished_is_refused_until_it_has tests/infrastructure/canonical/test_normalizer.py::test_a_rest_unit_lacking_positions_another_page_delivered_is_normalized
3 passed in 4.32s
```

## Limitations and failed invocation

One broader precise selection returned `2 failed, 2 passed in 3.52s`. The two failing nodes, `test_an_empty_rest_page_is_a_unit_without_rows` and `test_a_unit_is_proven_and_written_in_bounded_windows`, fail before the iterator assertion because their test setup calls `c.normalizer(h)` without the required keyword-only `clock` argument. The nodes were not edited or rerun during this audit. This is one observed test-helper failure, not a product behavior failure and not yet a two-round deferred test.

The repository also lacks dedicated pytest cases for a tampered result summary and early-close index cleanup; those behaviors were checked by the separate direct probe. Full process working-set costs and the main-matching 32 MiB measurement remain open.
