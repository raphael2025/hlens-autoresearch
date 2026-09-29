# E1 bounded normalizer proof staging: deferred test record

This experiment snapshot preserves an 8-revision single-key capacity test that reached its
two-attempt limit. It is not an acceptance result and is not part of the candidate test set.

## Exact node and attempts

Node: `tests/infrastructure/pit/test_selector_bounded_proofs.py::test_bounded_proofs_spill_long_single_key_without_proof_collection`

Attempt 1 command:

```text
uv run pytest -q tests/infrastructure/pit/test_selector_bounded_proofs.py::test_bounded_proofs_spill_long_single_key_without_proof_collection
```

Raw result:

```text
F                                                                        [100%]
______ test_bounded_proofs_spill_long_single_key_without_proof_collection ______
>       assert builder.root.record_count == 8
E       AssertionError: assert 1 == 8
E        +  where 1 = RunRef(..., record_count=1, leaf_count=1, depth=1).record_count
1 failed in 2.43s
```

Fixture diagnosis: identical repeated responses were idempotent, so this run contained one
revision, not eight. The fixture was changed to use distinct price payloads for the same trade ID;
no assertion or production rule was changed.

Attempt 2 used the same exact command. Raw result:

```text
F                                                                        [100%]
______ test_bounded_proofs_spill_long_single_key_without_proof_collection ______
>       assert builder.max_buffered_rows == _PARAMS.row_batch_rows
E       assert 0 == 1
1 failed in 7.64s
```

Before this observer assertion, the corrected fixture's actual run-root assertions passed:
`record_count == 8`, `leaf_count == 8`, and `depth >= 3`. The observer sampled
`len(_rows)` after `RunSetBuilder.add()` returned; capacity 1 flushes synchronously inside
`add()`, so it observed zero after each flush. This measures the wrong instant and does not prove
or disprove the peak buffer bound. Per the two-attempt rule, this node is deferred and must not be
rerun or have its assertion weakened/replaced under the same node.

The two separate staging/failure nodes in the candidate file passed `2 passed in 8.05s`; the
four-cutoff selector parity node passed `1 passed in 3.94s`; two public Normalizer restricted-proof
nodes passed `2 passed in 1.49s`. These are separate nodes and are not changed by this deferral.
