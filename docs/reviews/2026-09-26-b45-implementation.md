# B45 implementation note — CalibrationReport outward-rounded bounds (2026-09-26)

Status: **CODE_COMPLETE / DEBUG_PENDING**. Fixes the B45 blocker recorded in
`docs/reviews/2026-09-26-codex-full-code-review.md` (Codex review branch
`codex/full-code-review-2026-09-26`, `544860d`). Does not accept B45, does not complete Phase 9,
and does not touch B46 / B49 or any other module.

- Branch: `fix/b45-calibration-bounds`, based on `origin/wip/all-code-completion` @ `1cd3284`;
  not merged into `wip/all-code-completion`, `phase/1` or `main`.
- Scope: `research/synthetic_lab/calibration.py`, `research/synthetic_lab/README.md`,
  `tests/research/synthetic_lab/test_calibration.py`, this note.

## Change

`CalibrationReport.false_positive_rate_bounds` / `power_bounds` computed their endpoints as
`Decimal(n) / trials` in the ambient context (`ROUND_HALF_EVEN`), so a repeating ratio could put
the lower bound above (2/3) or the upper bound below (1/3) the exact rate. Both now go through one
helper that divides in a local `decimal.Context(prec=28)`: lower with `ROUND_FLOOR`, upper with
`ROUND_CEILING`. Per the Codex decision:

- Public API unchanged: same properties, `tuple[Decimal, Decimal]`; the `false_positive_rate` /
  `power` point fields and `calibrate` are unchanged.
- Independent of the ambient decimal context; no `intervals.PLACES` 6-place quantization.
- Ratios exactly representable in 28 significant digits (0, 1, integer ratios such as 1/4, 1/128)
  stay exact, so zero detector errors still give a point; a repeating ratio with zero errors gives
  the tightest 28-digit enclosure (one unit in the last digit wide).
- `GateCalibrationReport` intervals (`intervals.py`) are a separate path and are unaffected.

## Tests

`tests/research/synthetic_lab/test_calibration.py`, for both properties: endpoints compared with
`Fraction` exact ratios (enclosure plus tightness via `next_plus` / `next_minus`) over repeating
decimals (1/3, 2/3, 1/6, 3/7, 5/7, 1/997, 996/997), `trials=1`, zero errors, integer divisions and
1/128; identical results under a coarse (`prec=5`, `ROUND_HALF_UP`) and fine (`prec=60`) ambient
context; exact rates without errors are a point; an end-to-end `calibrate` run with 1/3 false
positives. The new cases fail on the pre-fix code (27 failed) and pass with the fix.

## Remaining

Codex re-review of the fix commit; final full gate on the final integration SHA after all lanes
(B45 / B46 / B49) are integrated.
