"""O1's own tests for `scribesense.config` and `scribesense.fontgen`.

Kept beside the code it tests, at the request of O1 (2026-11-14), rather than under the
repository's `tests/` tree where every other suite lives.

Two consequences a reader should know about, because both touch W2.1 -- the very card
these tests exist for:

* **These files ship inside the package.** `uv_build` packages everything under
  `src/scribesense/`, so an installed ScribeSense carries them into site-packages. That is
  harmless but it is not free, and an RPM reviewer will ask. If the answer is "they should
  not ship", the fix is a `[tool.uv.build-backend] module-root` exclude in `pyproject.toml`
  (O3 owns that file) or moving this package under `tests/`.
* **`pytest` does not collect them by default.** `pyproject.toml` sets
  `testpaths = ["tests"]`, so a bare `pytest` run skips this directory. Run them with::

      pytest src/scribesense/config_and_fontgen_tests

  Until `testpaths` includes this path (O3 owns `pyproject.toml`), CI does **not** run
  these tests. That is recorded in PLAN_v2 as a task for O3.

The import-rule checker (`tests/unit/test_import_rules.py`) does scan this directory,
because it walks every `*.py` under `src/`. Rule R4 applies here by an accidental prefix
match -- `"scribesense.config_and_fontgen_tests".startswith("scribesense.config")` is true --
which is harmless and, as it happens, correct: nothing here may import ui, store, adapters,
safety, controller, reader or gi.
"""
