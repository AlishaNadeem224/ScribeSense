"""M2.5 — Preview widget (K7 rule 2, A5 [V]).

Shows the fixed sample paragraph in the font `build_preview()` generated for the
configuration being edited.

How the font reaches the widget. `build_preview()` only writes files into a fresh
``temp_dir``; it does not load them anywhere (K7 `PreviewFonts`). The files are loaded
PRIVATELY: a new ``PangoCairo.FontMap`` (never the default one) gets each file through
``add_font_file()`` (Pango >= 1.56), and only the preview label uses that map
(``Gtk.Widget.set_font_map``). Nothing goes through fontconfig files, ``fonts_dir()``
or ``fc-cache``, so the preview cannot affect any other window or app.

Line spacing. The generated font carries the reading line height in ``hhea.lineGap``
(K7 D5), but a ``GtkLabel`` (Pango) does not add a font's line gap between the lines of
a layout, so the gap alone has no visible effect. The label therefore sets the line
height explicitly: ``reading_line`` x the label's resolved font size x ``text_scale``
(`sample_attributes`). That is the same unitless multiplier the browser adapter writes
as CSS ``line-height`` (M4.6), so the preview shows what reading text will look like.

Ownership. The widget owns the `PreviewFonts` it shows and deletes its ``temp_dir``
(K7: "the caller deletes temp_dir when the preview closes or is replaced"):
- when a newer preview replaces it (after the label has switched to the new font map),
- when a preview fails (`show_error`),
- when the widget is unmapped: the window is hidden (hide-on-close, A1), the page is
  left, or the widget is removed. Mapping it again rebuilds the last request,
- immediately, for a result that arrives after a newer request (stale),
- on `PreviewWidget.shutdown()` (app quit) and, as a safety net, at interpreter exit:
  this also closes a result that was built but never reached the main loop.

Threading (K7 rule 1: font generation never blocks the UI). Builds run on one worker
thread (`LatestOnlyBuilder`); only the latest request is built, and results come back
to the GTK main thread through ``GLib.idle_add``.
"""

from __future__ import annotations

import atexit
import shutil
import threading
import weakref
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import GLib, Gtk, Pango, PangoCairo  # noqa: E402

from scribesense.contracts.errors import ErrorCode, ScribeSenseError  # noqa: E402
from scribesense.contracts.fonts import PreviewFonts  # noqa: E402
from scribesense.log import log  # noqa: E402

SAMPLE_TEXT = (
    "Reading is easier when letters have room to breathe. Some readers need "
    "more space between words, others need more space between lines, and the "
    "right amount differs from person to person. ScribeSense lets each reader "
    "choose, then applies that choice everywhere they read."
)

#: A build job: called on the worker thread, returns the preview fonts or raises.
BuildJob = Callable[[], PreviewFonts]


# --------------------------------------------------------------------------- #
# Helpers without widgets (unit-tested headless)
# --------------------------------------------------------------------------- #


def close_preview(preview: PreviewFonts | None) -> None:
    """Delete a preview's ``temp_dir`` (K7 ownership rule). Never raises.

    Uses the generator's ``close()`` when the object has one (M1.8), else removes the
    directory itself, so a plain K7 `PreviewFonts` is cleaned up too.
    """
    if preview is None:
        return
    try:
        close = getattr(preview, "close", None)
        removed = close() if callable(close) else _rmtree(preview.temp_dir)
    except Exception:  # cleanup must never break the UI
        removed = False
    if removed is False:
        log(ErrorCode.PREVIEW_UNAVAILABLE, "ui", reason="W_PREVIEW_TEMP_LEAK")


def _rmtree(path: str) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return True


def private_font_map(files: tuple[str, ...] | list[str]) -> PangoCairo.FontMap:
    """A NEW font map holding only ``files`` plus the system fonts. Never the default map.

    Raises ``ScribeSenseError(PREVIEW_UNAVAILABLE)`` if the installed Pango cannot load
    files privately (< 1.56) or a file does not load; the caller then shows a message
    (M2.5 failure rule) instead of silently rendering a fallback font.
    """
    font_map = PangoCairo.FontMap.new()
    if not hasattr(font_map, "add_font_file"):
        raise ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE)
    if not files:
        raise ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE)
    for path in files:
        try:
            ok = font_map.add_font_file(path)
        except GLib.Error as exc:
            raise ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE) from exc
        if not ok:
            raise ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE)
    return font_map


def sample_attributes(
    context: Pango.Context,
    family: str,
    text_scale: float,
    reading_line: float | None,
) -> Pango.AttrList:
    """Pango attributes for the sample: the preview family, ``text_scale`` and, when
    ``reading_line`` is given, an absolute line height of ``reading_line`` x the
    context's font size x ``text_scale``.

    ``context`` is the label's Pango context (its font description is the label's
    resolved CSS font). The line height is in Pango layout units (device pixels x
    ``Pango.SCALE``), which is what Pango compares it against when laying out lines.
    """
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_family_new(family))
    attrs.insert(Pango.attr_scale_new(text_scale))
    if reading_line is not None:
        size = font_size_units(context)
        if size > 0:
            attrs.insert(
                Pango.attr_line_height_new_absolute(round(reading_line * size * text_scale))
            )
    return attrs


def font_size_units(context: Pango.Context) -> int:
    """The context's font size in Pango layout units (device pixels x ``Pango.SCALE``).

    A point size is converted with the context's resolution (GTK sets it from the
    display; 96 dpi when unset). Returns 0 when the context has no usable size.
    """
    desc = context.get_font_description()
    if desc is None or desc.get_size() <= 0:
        return 0
    if desc.get_size_is_absolute():
        return desc.get_size()
    dpi = PangoCairo.context_get_resolution(context)
    if dpi <= 0:
        dpi = 96.0
    return round(desc.get_size() * dpi / 72.0)


def error_text(exc: BaseException) -> str:
    """User message for a failed preview: an error code at most, never raw exception text."""
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None)
    code_value = getattr(code, "value", code)
    if isinstance(code_value, str) and code_value:
        return f"Preview unavailable ({code_value}). You can still apply."
    return "Preview unavailable. You can still apply."


class LatestOnlyBuilder:
    """Runs build jobs on ONE background thread; only the newest request matters.

    `submit()` (main thread) replaces any job still waiting. The worker builds the
    waiting job and posts ``deliver(generation, result, error)`` through ``post``
    (``GLib.idle_add`` by default), so ``deliver`` runs on the main thread. A result
    whose generation is no longer `current` is closed right here, on the main thread,
    by `accept` — the caller never sees it.

    A result is "in transit" from the moment the worker posts it until `accept` runs.
    `shutdown()` (app quit, when the main loop may never run the posted callback again)
    closes every result still in transit and makes the worker close any later result
    itself, so no ``temp_dir`` is left behind.
    """

    def __init__(
        self,
        deliver: Callable[[PreviewFonts | None, BaseException | None, Any], None],
        post: Callable[..., Any] = GLib.idle_add,
    ) -> None:
        self._deliver = deliver
        self._post = post
        self._lock = threading.Lock()
        self._pending: tuple[int, BuildJob, Any] | None = None
        self._running = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._in_transit: dict[int, PreviewFonts] = {}  # posted, `accept` not yet run
        self.current = 0  # generation of the newest request; read on the main thread

    def submit(self, job: BuildJob, context: Any = None) -> int:
        """Queue ``job`` as the newest request. Returns its generation.

        After `shutdown()` nothing is built any more (the generation is still returned).
        """
        self.current += 1
        with self._lock:
            if self._closed:
                return self.current
            self._pending = (self.current, job, context)
            if self._running:
                return self.current
            self._running = True
            self._thread = threading.Thread(
                target=self._work, name="scribesense-preview", daemon=True
            )
            thread = self._thread
        thread.start()
        return self.current

    def cancel(self) -> None:
        """Make every queued or running job stale (its result will be closed)."""
        self.current += 1
        with self._lock:
            self._pending = None

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop for good: drop queued work, close results the main loop has not taken.

        Waits up to ``timeout`` seconds for a build that is still running, so its result
        is closed too (by the worker itself, since `_closed` is set). Never raises.
        """
        self.cancel()
        with self._lock:
            self._closed = True
            thread = self._thread
            in_transit, self._in_transit = list(self._in_transit.values()), {}
        for result in in_transit:
            close_preview(result)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _work(self) -> None:
        while True:
            with self._lock:
                job = self._pending
                self._pending = None
                if job is None:
                    self._running = False
                    return
            generation, build, context = job
            result: PreviewFonts | None = None
            error: BaseException | None = None
            try:
                result = build()
            except Exception as exc:  # delivered to the UI as a message
                error = exc
            with self._lock:
                closed = self._closed
                if not closed and result is not None:
                    self._in_transit[generation] = result
            if closed:  # shut down while building: nobody will take this result
                close_preview(result)
                continue
            self._post(self.accept, generation, result, error, context)

    def accept(
        self,
        generation: int,
        result: PreviewFonts | None,
        error: BaseException | None,
        context: Any,
    ) -> bool:
        """Main thread: hand a fresh result to ``deliver``; close a stale one."""
        with self._lock:
            taken = self._in_transit.pop(generation, None) is not None
        if result is not None and not taken:
            return False  # already closed by `shutdown()`
        if generation != self.current:
            close_preview(result)
        else:
            self._deliver(result, error, context)
        return False  # GLib.SOURCE_REMOVE: run once


# --------------------------------------------------------------------------- #
# The widget
# --------------------------------------------------------------------------- #


class PreviewWidget(Gtk.Box):
    """The sample paragraph, rendered in the generated preview font."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        self.label = Gtk.Label()
        self.label.set_wrap(True)
        self.label.set_max_width_chars(60)
        self.label.set_xalign(0.0)
        self.label.set_selectable(False)
        self.label.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Font preview sample text"]
        )
        self.append(self.label)

        self.status = Gtk.Label()
        self.status.set_wrap(True)
        self.status.set_xalign(0.0)
        self.status.add_css_class("dim-label")
        self.status.set_visible(False)
        self.append(self.status)

        self._current: PreviewFonts | None = None
        self._font_map: PangoCairo.FontMap | None = None
        self._last_request: tuple[BuildJob, tuple[float, float | None]] | None = None
        self._builder = LatestOnlyBuilder(self._on_result)

        self.label.set_text(SAMPLE_TEXT)
        # Map/unmap, not realize/unrealize: a hide-on-close window (A1) stays realized
        # while hidden, so only unmap tells us nobody can see the preview any more.
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("unrealize", self._on_unmap)
        _LIVE.add(self)

    # --- requests ------------------------------------------------------------

    def request(
        self, build: BuildJob, text_scale: float = 1.0, reading_line: float | None = None
    ) -> None:
        """Build a new preview off the main thread; the newest request wins.

        ``text_scale`` and ``reading_line`` (K1) are how the label draws the sample;
        ``reading_line`` None keeps the default line height. While the widget is not
        mapped the request is only remembered (and started on map), so nothing is
        generated for a window that is hidden or never shown.
        """
        self._last_request = (build, (text_scale, reading_line))
        if self.get_mapped():
            self._show_status("Updating preview…")
            self._builder.submit(build, (text_scale, reading_line))

    def show_message(self, message: str) -> None:
        """Show ``message`` instead of a preview (e.g. Original selected). Drops any
        pending request and releases the current preview."""
        self._last_request = None
        self._builder.cancel()
        self._release()
        self.label.set_text(SAMPLE_TEXT)
        self._show_status(message)

    def show_error(self, message: str) -> None:
        """A failed preview: keep the sample in the normal font, show ``message``."""
        self._release()
        self.label.set_text(SAMPLE_TEXT)
        self._show_status(message)

    def release(self) -> None:
        """Stop pending work and delete the current preview's files."""
        self._builder.cancel()
        self._release()

    def shutdown(self) -> None:
        """App quit: release the preview and stop the worker for good, closing a result
        that was built but not yet shown. The widget shows no previews afterwards."""
        self._last_request = None
        self._builder.shutdown()
        self._release()
        _LIVE.discard(self)

    # --- internals -----------------------------------------------------------

    def _on_result(
        self, result: PreviewFonts | None, error: BaseException | None, context: Any
    ) -> None:
        if not self.get_mapped():
            close_preview(result)
            return
        if error is not None or result is None:
            exc = error or ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE)
            log(ErrorCode.PREVIEW_UNAVAILABLE, "ui", reason=_reason(exc))
            self.show_error(error_text(exc))
            return
        try:
            text_scale, reading_line = context
            self._show(result, float(text_scale), reading_line)
        except Exception as exc:
            close_preview(result)
            log(ErrorCode.PREVIEW_UNAVAILABLE, "ui", reason=_reason(exc))
            self.show_error(error_text(exc))

    def _show(
        self, preview: PreviewFonts, text_scale: float, reading_line: float | None = None
    ) -> None:
        font_map = private_font_map(preview.files)
        # Switch the label to the new map first, then delete the old files.
        self.label.set_font_map(font_map)
        self.label.set_attributes(
            sample_attributes(
                self.label.get_pango_context(),
                preview.reading_family,
                text_scale,
                None if reading_line is None else float(reading_line),
            )
        )
        self.label.set_text(SAMPLE_TEXT)
        old, self._current, self._font_map = self._current, preview, font_map
        close_preview(old)
        self.status.set_visible(False)

    def _release(self) -> None:
        if self._current is None and self._font_map is None:
            return
        self.label.set_attributes(None)
        # Back to the normal (default) map before deleting the files. Passing None
        # trips a GTK assertion; the default map itself is only read, never changed.
        self.label.set_font_map(PangoCairo.FontMap.get_default())
        old, self._current, self._font_map = self._current, None, None
        close_preview(old)

    def _show_status(self, message: str) -> None:
        self.status.set_text(message)
        self.status.set_visible(True)

    def _on_map(self, *_args: object) -> None:
        if self._last_request is not None and self._current is None:
            build, context = self._last_request
            self._show_status("Updating preview…")
            self._builder.submit(build, context)

    def _on_unmap(self, *_args: object) -> None:
        self.release()

    def _close_files_at_exit(self) -> None:
        """Interpreter exit: delete files only. GTK may already be gone, so the label
        is not touched (the process is ending; its font map dies with it)."""
        self._builder.shutdown(timeout=1.0)
        current, self._current = self._current, None
        close_preview(current)


#: Widgets whose files `_close_all_at_exit` deletes if the app quit without
#: `PreviewWidget.shutdown()` (e.g. the main loop stopped with the window still mapped).
_LIVE: weakref.WeakSet[PreviewWidget] = weakref.WeakSet()


@atexit.register
def _close_all_at_exit() -> None:
    for widget in list(_LIVE):
        try:
            widget._close_files_at_exit()
        except Exception:  # exit cleanup must never raise
            pass


def _reason(exc: BaseException) -> str:
    """An identifier-shaped reason for the log (M4.15): an error code, never text."""
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None)
    value = getattr(code, "value", code)
    return value if isinstance(value, str) and value.isidentifier() else "unknown"
