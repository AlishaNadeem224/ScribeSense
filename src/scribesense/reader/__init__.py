"""O2 — Fallback reader (M2.9) and text capture (M2.10).

`capture` provides the four ways to bring text into the reader. It is
deliberately GTK-free so that the CLI can call it, and so that the tests can
run without a display.
"""

from __future__ import annotations

from scribesense.reader.capture import (
    CaptureResult,
    from_clipboard,
    from_extension,
    from_selection,
    normalize,
)

__all__ = [
    "CaptureResult",
    "from_clipboard",
    "from_extension",
    "from_selection",
    "normalize",
]