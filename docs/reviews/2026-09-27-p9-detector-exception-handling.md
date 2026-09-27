# P9 detector exception handling review

Date: 2026-09-27
Scope: `research/synthetic_lab/gate_calibration.py`
Outcome: the detector-exception behavior listed as an open item in
`2026-09-25-framework-debug-backlog.md` is already implemented on the coordination
branch; no production-code change was needed.

## Verified behavior

- `run_gate_calibration` records an exception from `detect` as an
  `INCONCLUSIVE` `RunRecord` with no gates and the bounded exception text in
  `detector_error`, then continues with the next profile / sample.
- `run_multi_instrument_calibration` does the same for an exception from
  `detect_instruments`; unaffected books and profiles continue to be processed.
- In optional G5 mode, `_run_g5` records an exception from `detect_sealed` as
  an `INCONCLUSIVE` G5 result with no gates, marks the claimed evaluation as
  consumed without a result, and continues the calibration.
- `PROPAGATED_ERRORS` (`ValueError`, `TypeError`, `MemoryError`) deliberately
  propagate as caller/configuration/resource failures instead of becoming
  evidence. A mismatched Profile report and other harness misconfiguration
  also remain errors.
- Error counts and conservative pass-rate bounds are included in the relevant
  arm evidence, so an errored sample cannot be counted as a pass or silently
  disappear from the denominator.

## Code locations

- `_errored`: creates the single-instrument `INCONCLUSIVE` record.
- `run_gate_calibration`: catches detector exceptions and continues the loop.
- `_run_g5`: isolates the sealed-window detector failure after consuming the
  one-shot evaluation.
- `_book_record` and `run_multi_instrument_calibration`: record multi-instrument
  detector failures and continue unrelated calibration work.
- `_evidence` / `_multi_evidence`: preserve error counts and report pass-rate
  bounds.

No gate, Profile, threshold, frozen contract, or test was changed. The existing
backlog entry is historical and should be marked resolved when that backlog is
next reconciled; this scoped review does not edit the backlog itself.
