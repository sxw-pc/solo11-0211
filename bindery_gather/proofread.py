"""Alias module: proofreading task entry points live in :mod:`bindery_gather.binding`."""
from .proofing import *  # noqa: F401,F403
from . import proofing as _proofing  # noqa: F401

report = _proofing.proof_report
