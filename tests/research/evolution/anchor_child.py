"""A separate process sharing a ``ProposalAnchor`` (B49; TEST ONLY; run as ``python -m``).

``python -m tests.research.evolution.anchor_child '<json args>'`` prints one ``RESULT`` line.
Every mode opens its objects, writes ``args["ready"]`` and waits for ``args["start"]`` to exist,
so the parent can release all children at once against one anchor on disk:

- ``ledger``: open ``ProposalLedger(args["ledger"], anchor=args["anchor"])`` and record
  ``args["n"]`` distinct proposals; stops at the first refusal.
- ``publish``: ``ProposalAnchor(args["anchor"]).publish(chain[:k])`` for each ``k`` in
  ``args["prefixes"]`` (a stale-looking publisher: it never reloads anything itself).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from research.evolution import ProposalAnchor, ProposalLedger, ProposalLedgerInconsistent
from tests.research.evolution.test_replacement_proposals import _propose

RESULT_PREFIX = "RESULT "
_WAIT = 60.0  # seconds for the parent to release the children


def _wait_for(start: Path) -> None:
    deadline = time.monotonic() + _WAIT
    while not start.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{start} never appeared")
        time.sleep(0.001)


def _ledger(args: dict[str, Any]) -> dict[str, Any]:
    try:
        ledger = ProposalLedger(Path(args["ledger"]), anchor=Path(args["anchor"]))
    except ProposalLedgerInconsistent as exc:
        Path(args["ready"]).touch()
        return {"recorded": 0, "refused": f"open: {exc}"}
    Path(args["ready"]).touch()
    _wait_for(Path(args["start"]))
    recorded, refused = 0, None
    with ledger:
        for i in range(args["n"]):
            try:
                ledger.record(_propose(reason=f"{args['tag']} proposal {i}"))
            except ProposalLedgerInconsistent as exc:
                refused = str(exc)
                break
            recorded += 1
    return {"recorded": recorded, "refused": refused}


def _publish(args: dict[str, Any]) -> dict[str, Any]:
    anchor = ProposalAnchor(Path(args["anchor"]))
    chain: list[str] = args["chain"]
    Path(args["ready"]).touch()
    _wait_for(Path(args["start"]))
    published, refused = [], []
    for k in args["prefixes"]:
        try:
            anchor.publish(chain[:k])
        except ProposalLedgerInconsistent:
            refused.append(k)
        else:
            published.append(k)
    return {"published": published, "refused": refused}


def main() -> None:
    args = json.loads(sys.argv[1])
    result = {"ledger": _ledger, "publish": _publish}[args["mode"]](args)
    print(RESULT_PREFIX + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
