"""O2 — Apply controller (M2.1) and K6 coverage mapping.

The controller is the only module in O2 that drives the transaction lifecycle.
It never writes to a setting itself: every write goes through an adapter, and
every adapter call is preceded by a journal record (K3 rule 1, K3 rule 2).

Public surface:
    ApplyController, ApplyReport, Problem
"""

from __future__ import annotations

from scribesense.controller.apply_flow import ApplyController, ApplyReport, Problem

__all__ = ["ApplyController", "ApplyReport", "Problem"]