# E1 key-row index experiment — deferred

**Status: DEFERRED / NOT PASSED.** This branch preserves an isolated bounded-selector prototype
that replaces spilled per-key row dictionaries with a revision-id-to-run-ordinal index. It is not
approved for the Phase 1 candidate.

The prototype keeps full Canonical rows in the existing per-key sorted run and stores only ordinal
and effective-availability scalars in `ContentKeyTree`. Short histories still use the configured
`key_history_buffer`. A selected revision is fetched by a checked root-to-leaf ordinal lookup.

The new node `test_spilled_key_index_seeks_rows_and_replays_multitime_selector` ran twice and is
deferred under the two-failure rule. The first attempt failed because the fixture compared a tuple
`supersedes` value with the sorted-run JSON-decoded list representation. After matching that wire
representation, the second attempt reached the read-count assertion but measured 3,085 opened pages
against the test's 2,774-page structural estimate; the call took 1.68 seconds (1.96 seconds total).
The expected parity checks before the read-count assertion passed in that second attempt. The node
must not be rerun or weakened in this experiment.

The production cutoff-parity node passed once with this prototype:

```text
uv run pytest -q --tb=short tests/infrastructure/pit/test_selector_v3.py -k 'test_iter_bounded_matches_select_across_the_four_cutoffs and not test_a_narrow_window_proves_only_the_batches_it_reads and not test_one_selector_proves_each_unit_once_across_slices and not test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
1 passed, 28 deselected in 3.88s
```

The earlier materialized-row control measured 0.82 seconds for its 257-row, 18-instant call; the
indexed run-backed attempt measured 1.68 seconds. That indicates higher read/CPU cost at this scale.
The accepted ordinal-reader substrate is separately frozen at `1102ddd`; this experiment does not
change its acceptance scope. Leaf decode can also use more Python memory than the encoded page byte
bound. No complete E1-CAP-1 or 32 MiB process-capacity claim follows from either result.
