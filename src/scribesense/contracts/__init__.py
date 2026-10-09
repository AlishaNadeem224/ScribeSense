"""Frozen shared contracts K1–K11 (ARCHITECTURE.md §5). Do NOT edit without owner approval.

    config.py     K1  Configuration, Range, RANGES, SHORTLIST, Issue        (O1)
    presets.py    K2  WordSpacing, PresetValues, Preset, ORIGINAL           (O1)
    adapter.py    K3  Target, State, SnapshotItem, PlannedWrite, Adapter    (O4)
    journal.py    K4  JournalItem, LocationRecord, ActiveConfig, Journal    (O4)
    states.py     K5  TxState, TRANSITIONS                                  (O4)
    coverage.py   K6  CoverageStatus, CoverageEntry                         (O2)
    fonts.py      K7  FontSet, FontBuildResult, FontGenerator               (O1)
    paths.py      K8  paths + run_cmd                                       (O3)
    errors.py     K8  ErrorCode, ScribeSenseError                           (O3)
    store.py      K9  *** DRAFT — O3 sign-off required ***                  (O3)
    cli.py        K10 ExitCode, COMMANDS                                    (O2)
    messages.py   K11 extension/IPC messages and limits                     (O4 + O2)

Contracts import only the standard library and each other.
"""
