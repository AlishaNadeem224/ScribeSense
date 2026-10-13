"""Test for M3.7 Doctor (acceptance tests)."""

from __future__ import annotations
import sys
from unittest.mock import patch
from scribesense.contracts.cli import ExitCode
from scribesense.doctor import run_checks, run_doctor


def test_doctor_runs_cleanly_when_all_tools_present() -> None:
	checks = run_checks()
	# At minimum python and fonttools must be present in the active virtualenv
	assert any(c.name == "python" and c.present for c in checks)
	assert any(c.name == "fonttools" and c.present for c in checks)

def test_missing_required_returns_failed_exit_code() -> None:
	with patch("scribesense.doctor.shutil.which", return_value=None):
		code = run_doctor()
		assert code == ExitCode.FAILED

def test_all_required_present_returns_success() -> None:
	with patch("scribesense.doctor.shutil.which", return_value="/usr/bin/mock"), \
	     patch("scribesense.doctor.importlib.util.find_spec", return_value=True):
		code = run_doctor()
		assert code == ExitCode.OK
