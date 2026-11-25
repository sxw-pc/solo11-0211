"""Compatibility entry point for the proofreading/defect-evidence workstation.

The canonical intake and task entries live in :mod:`bindery_gather.binding`
(as required by the bindery hand-off); this module re-exports them under the
workstation vocabulary.
"""
from .binding import (  # noqa: F401
    MAX_READINGS_PER_SEGMENT,
    MAX_SEGMENTS_PER_INTAKE,
    add_reading,
    add_ruler,
    calibration,
    calibrate,
    capture_reading,
    create_order,
    create_task,
    defect_reading,
    defect_report,
    intake,
    open_proof_task,
    open_task,
    order_report,
    proof_report,
    proof_status,
    proof_task_report,
    reading_report,
    reconcile,
    reconcile_defect,
    record_reading,
    record_ruler,
    resolve_segment,
    review_task,
    segment_report,
    task_report,
    triage,
    withdraw_task,
    complete_task,
)
from .imposition import facing_pair, half_page, spread_pages  # noqa: F401

# Vocabulary aliases that callers/tests may use.
record = record_reading
read = record_reading
ruler = record_ruler
calibrate_segment = record_ruler
open_order = open_task
create_proof_order = open_task
create_proof_task = open_task
proof_order_report = order_report
status = proof_report
report = proof_report

__all__ = [name for name in globals() if not name.startswith("_")]
