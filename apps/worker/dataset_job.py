"""Thin worker job for the v3 Dataset build (ADR-0101 D2; ADR-0044 jobs, ADR-0095 host).

This module only shapes the job; it does not know how a Dataset is built. A deployment's trusted
runtime factory (``python -m apps.worker.serve --factory ...``) opens the v3 pipeline, wraps it in
a port (``infrastructure.dataset.job_port.PipelineDatasetJobPort``) and composes::

    JobRunner(bus, consumer=..., topic=..., handlers=dataset_job_handlers(port), results=...)

Nothing in the repository registers this job automatically (``serve`` and ``apps.worker`` never
import it); this module depends only on ``apps``, ``core`` and the standard library.

Idempotency key = ``selection_id``. ``dataset_build_job`` asks the port for the request's
``selection_id`` and submits ``{"selection_id": ..., "request": ...}``: the request carries the
pinned point-in-time spec, so the ``selection_id`` is a function of the request and the job's
content identity (``JobSpec.job_id``) is fixed by it. The handler re-derives the ``selection_id``
before building and refuses a job whose key does not match its request, and refuses a result for
another selection. A re-delivered job is absorbed by ``JobRunner``'s stored outcome; a re-run after
a crash inside the handler replays the selection's committed chunks and manifest in the v3 build,
so a deployment may declare ``DATASET_BUILD_JOB`` in ``idempotent=`` (that declaration is the
deployment's decision, never made here).

``JobRunner`` journals ``"<type>: <message>"`` of a failed attempt. A port failure's text may
carry deployment details (paths, DSNs), so the handler re-raises every port failure as
``DatasetJobFailed`` naming only the exception type.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

from apps.worker.jobs import JobSpec

__all__ = [
    "DATASET_BUILD_JOB",
    "DatasetBuildPort",
    "DatasetJobFailed",
    "DatasetJobRejected",
    "dataset_build_handler",
    "dataset_build_job",
    "dataset_job_handlers",
]

#: The handler name of the v3 Dataset build job.
DATASET_BUILD_JOB: Final = "dataset.build"

_PARAMS: Final = frozenset({"selection_id", "request"})
_SELECTION_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{0,255}$")


class DatasetBuildPort(Protocol):
    """What the job needs from the deployment: JSON request in, JSON summary out."""

    def selection_id(self, request: Mapping[str, Any]) -> str: ...

    def build(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class DatasetJobRejected(ValueError):
    """The job's parameters are malformed or its key does not match its request."""


class DatasetJobFailed(RuntimeError):
    """The port failed; the message names only the exception type (never its text)."""


def _selection_id(value: object, where: str) -> str:
    if not isinstance(value, str) or _SELECTION_ID.match(value) is None:
        raise DatasetJobRejected(f"{where} is not a selection id")
    return value


def _port_call[T](what: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except Exception as exc:  # never journal a port failure's text: it may carry a credential
        raise DatasetJobFailed(f"dataset {what} failed ({type(exc).__name__})") from None


def dataset_build_job(port: DatasetBuildPort, request: Mapping[str, Any]) -> JobSpec:
    """The job of ``request``: its ``selection_id`` (the idempotency key) and the request."""
    if not isinstance(request, Mapping):
        raise DatasetJobRejected("request must be a mapping")
    document = dict(request)
    selection_id = _selection_id(
        _port_call("selection", lambda: port.selection_id(document)), "the port's answer"
    )
    return JobSpec(DATASET_BUILD_JOB, {"selection_id": selection_id, "request": document})


def dataset_build_handler(port: DatasetBuildPort) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """The ``JobRunner`` handler: check the key, build through the port, check the result."""

    def handle(params: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(params, Mapping) or set(params) != _PARAMS:
            raise DatasetJobRejected(f"job params must be exactly {sorted(_PARAMS)}")
        expected = _selection_id(params["selection_id"], "selection_id")
        request = params["request"]
        if not isinstance(request, Mapping):
            raise DatasetJobRejected("request must be a mapping")
        derived = _port_call("selection", lambda: port.selection_id(request))
        if derived != expected:
            raise DatasetJobRejected("selection_id does not match the job's request")
        summary = _port_call("build", lambda: port.build(request))
        if not isinstance(summary, Mapping) or summary.get("selection_id") != expected:
            raise DatasetJobRejected("the build summary is not of the job's selection")
        return dict(summary)

    return handle


def dataset_job_handlers(
    port: DatasetBuildPort,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """``{DATASET_BUILD_JOB: handler}`` for a deployment to merge into its ``JobRunner``."""
    return {DATASET_BUILD_JOB: dataset_build_handler(port)}
