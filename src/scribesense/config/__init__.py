"""O1 — Configuration vocabulary, presets, font eligibility rules (M1.1–M1.3).

`settings` is M1.1, the canonical configuration vocabulary (contract K1). Presets
(K2) belong in a sibling module and are deliberately not re-exported from here, so
that importing the vocabulary never drags in preset defaults. `bundled` finds the
bundled base fonts (K2 `base_font_files`); `rules` is M1.3's eligibility check.
"""
