"""M1.5 -- Deterministic naming and content identity (owner O1).

    content_key(config, source_sha, generator_version) -> str

One generated font set is identified by one 12-hex-character **content key**, and the
two family names are derived from it::

    AccessSans-3f9a1c2b7d10      the reading variant
    AccessSansUI-3f9a1c2b7d10    the UI variant

This module owns the key formula and the family names [K7, D A19]. It reads files only
to hash them and writes nothing.

What the key is for
-------------------
The key is the cache identity of a generated font set: M1.7 looks a build up by it and
M1.8 returns ``status="cached"`` on a hit. The correctness requirement is therefore
two-sided, and both sides are failures:

* **A false hit** -- two different sets of font-building inputs mapping to one key --
  would serve a user the wrong font with no symptom other than wrong spacing.
* **A false miss** -- one set of inputs mapping to two keys -- costs a rebuild and
  leaves a duplicate artifact installed.

The key is a 48-bit prefix of a SHA-256, so a false hit needs a collision in that
prefix. Research2 F.2 names BLAKE2b as the reproducibility literature's choice for
content addressing and SHA-256 as "an acceptable alternative"; the card's example
(``key = sha256(...)[:12]``) picks SHA-256, so that is what is used.

Why there is exactly one key for both variants
----------------------------------------------
Research2 F.2 proposes a *per-variant* key -- ``variant_kind`` hashed in, and only that
variant's line value included. K7's ``FontSet`` has a single ``key`` field shared by
``reading_family`` and ``ui_family``, and the M1.5 card's acceptance test requires that
changing ``ui_line`` **alone** produces a new key. Both point the same way: one key per
*set*, covering both line values. The card governs and the research's open item D-08 is
closed by K7 D5, which confirms the patcher does write vertical metrics -- so the two
artifacts genuinely differ in bytes and both line values are font-building inputs.

The cost of the frozen one-key shape is recorded rather than hidden: changing ``ui_line``
alone gives the *reading* artifact a new key, although none of its advances or line
metrics move. Its bytes do change with it -- the key is embedded in the family name, so
the artifact stays honestly self-identifying -- but the rebuild and the reinstall under
a new family name are both avoidable work, and anything pointing at the old reading
family has to be re-pointed. That is the safe direction of error -- redundant work, never
a wrong cache hit -- and a two-key scheme is not available without reopening K7.

Platform: stdlib ``hashlib`` and ``pathlib`` only. No clock, no path, no locale, no
environment, no randomness -- the inputs Research2 F.2 explicitly excludes. Identical on
Windows (development) and Fedora/Wayland (target).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from scribesense.config.settings import RANGES, Configuration, decimals_of, validate
from scribesense.contracts import fonts as k7
from scribesense.fontgen.patcher import LIGATURE_FEATURES

__all__ = [
    "FAMILY_PATTERN",
    "fc_pattern",
    "GENERATOR_VERSION",
    "KEYED_FIELDS",
    "KEY_LENGTH",
    "READING_FAMILY_PREFIX",
    "UI_FAMILY_PREFIX",
    "UNKEYED_FIELDS",
    "canonical",
    "content_key",
    "file_sha256",
    "is_content_key",
    "is_generated_family",
    "reading_family",
    "source_sha_for",
    "ui_family",
    "variant_filename",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

GENERATOR_VERSION = "1.0"
"""The generation algorithm's version, hashed into every key.

Research2 F.2: "an explicit constant that must be bumped by any change that can alter
output bytes (Mokhov et al., 2020)(Kopcho, 2026)". Concretely, bump it for **any**
change to M1.4's patch -- a different spacing formula, a different rounding, a different
set of tables edited, a different name-table layout -- and for a fontTools upgrade that
changes compiled output. Not bumping it when output changes is the one way this module
can produce a false cache hit; bumping it unnecessarily only costs one rebuild, so when
in doubt, bump.

It is deliberately *not* derived from anything automatic. A package version would move
for unrelated reasons, and a hash of the source files would change on a comment edit --
both trade a rare false hit for constant false misses.
"""

KEY_LENGTH = 12
"""Hex characters kept from the SHA-256 digest -- 48 bits, per the card's example."""

_KEY_RE = re.compile(r"^[0-9a-f]{%d}$" % KEY_LENGTH)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z._+-]{1,64}$")
_STYLE_RE = re.compile(r"^[0-9A-Za-z]{1,32}$")

READING_FAMILY_PREFIX = k7.READING_FAMILY_PREFIX.removesuffix("-")
"""Family-name prefix of the reading variant [K7, D A19]."""

UI_FAMILY_PREFIX = k7.UI_FAMILY_PREFIX.removesuffix("-")
"""Family-name prefix of the UI variant [K7, D A19]."""

FAMILY_PATTERN = re.compile(
    rf"^(?P<prefix>{UI_FAMILY_PREFIX}|{READING_FAMILY_PREFIX})"
    rf"-(?P<key>[0-9a-f]{{{KEY_LENGTH}}})$"
)
"""Matches exactly the family names this module generates.

``AccessSansUI`` is tried first: ``AccessSans`` is a prefix of it, so the alternation
order decides which branch a UI name matches, and the pattern is anchored at both ends
anyway. The point of the regex is the *reverse* direction -- `is_generated_family` lets
M1.6 and M1.7 tell a generated family from a system one by name alone, and the named
groups let `fontgen.variants.variant_of` recover which variant and which key a built
artifact belongs to without restating the scheme.
"""

KEYED_FIELDS: tuple[str, ...] = (
    "letter_em",
    "word_scale",
    "reading_line",
    "ui_line",
)
"""`Configuration` fields that change the generated bytes, in hashed order.

* ``letter_em`` -- changes every Latin advance in ``hmtx`` (M1.4 ``_apply_spacing``).
* ``word_scale`` -- changes the space glyph's advance (same).
* ``reading_line`` -- becomes the reading artifact's ``hhea.lineGap`` /
  ``OS/2.sTypoLineGap`` (M1.4 ``_apply_line_metrics``, K7 D5).
* ``ui_line`` -- the same, for the UI artifact.

``base_family`` is **not** listed because it is already in the key through the source
hash, which is the stronger statement: it hashes the bytes the family selects rather
than the label (Research2 F.3, "Yes, via the source-file hash"). Hashing the label too
would mean renaming a shortlist entry invalidated every cached font without a single
byte changing.
"""

UNKEYED_FIELDS: tuple[str, ...] = ("text_scale", "base_family")
"""`Configuration` fields deliberately absent from the key.

``text_scale`` is an application-level multiplier -- ``effective_size = base_size *
text_scale`` -- applied by the rendering layer. No byte of a font file depends on it,
and M1.4 does not read it: `patch` takes ``letter_em``, ``word_scale`` and a single
``line``, and nothing else from the configuration. Including it would fork the cache on
a pure display preference, which K1, K7 and Research2 F.3/F.6 all forbid; Research2
calls the "``text_scale`` changed -> same key" case "the single most important
determinism test in the suite".

``base_family`` is covered by the source hash, as `KEYED_FIELDS` explains. It is listed
here so that the two tuples together account for all six fields of `Configuration` --
a test asserts exactly that, so a seventh field cannot be added to K1 without someone
deciding which side of this line it falls on.
"""

_DOMAIN = "scribesense/font-content-key/1"
"""Domain-separation tag, hashed first.

Fixes what this digest *is*, so a key can never coincide with some other digest of the
same bytes computed elsewhere in the pipeline. The trailing ``/1`` versions the payload
*layout*; changing the layout without changing it would be a false-hit bug, so treat the
two as one decision and bump `GENERATOR_VERSION` at the same time.
"""


# --------------------------------------------------------------------------- #
# Canonical values
# --------------------------------------------------------------------------- #


def canonical(field: str, value: float) -> str:
    """The fixed-decimal spelling of one configuration value, for hashing.

    The decimal count comes from ``RANGES[field].step`` via `decimals_of`, so the
    canonical text can never drift out of sync with the grid the value was rounded onto
    (``letter_em`` 0.02 -> 2 places, ``reading_line`` 0.1 -> 1 place).

    This is what makes the card's "1.5 and 1.50 -> same key" hold, and it holds for a
    stronger reason than string formatting: both literals *are* the same float, and the
    fixed-decimal form means no value can reach the hash through ``repr`` -- where
    ``1.50`` and ``1.5000000000000002`` would differ while being the same setting.

    ``+ 0.0`` normalises ``-0.0`` to ``0.0`` (K1 A3, Research2 B.4). `round_to_steps`
    already does this, so it is belt and braces for a value that reached here another
    way; without it ``letter_em`` could hash as ``"-0.00"``.

    >>> canonical("reading_line", 1.5), canonical("word_scale", 1.5)
    ('1.5', '1.50')
    """
    bounds = RANGES[field]
    return f"{float(value) + 0.0:.{decimals_of(bounds)}f}"


# --------------------------------------------------------------------------- #
# Source identity
# --------------------------------------------------------------------------- #


def file_sha256(path: str | os.PathLike[str]) -> str:
    """SHA-256 of a file's bytes, as 64 lowercase hex characters.

    Content, not metadata: the path, the modification time, the owner and the size are
    not part of it, so copying a bundled font to a second location cannot change a key
    (Research2 F.2, F.7). Read in chunks because a font may be up to M1.3's 32 MB budget.
    """
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_sha_for(files: Mapping[str, str | os.PathLike[str]]) -> str:
    """Fold a ``style -> file`` map into the single ``source_sha`` `content_key` takes.

    The card's example is "sha256 of every base-font style file". Every *style* file, not
    just Regular: a family that gains a Bold produces more artifacts from the same
    configuration, so the set of styles is itself a font-building input and belongs in
    the key. Style names are hashed alongside their digests for the same reason -- moving
    a file from Italic to BoldItalic changes what is built even though no byte moves.

    Styles are sorted, so the caller's mapping order cannot reach the digest (Research2
    F.7, "dict/set reordering -> identical key"). The payload is one
    ``style=<64 hex>`` line per style, a shape no style name can forge: names are
    restricted to alphanumerics, so neither ``=`` nor a newline can appear in one.

    Pass only the styles that will actually be built. An ineligible or absent optional
    style must be left out, or the key would promise an artifact that the build does not
    produce.
    """
    if not files:
        raise ValueError("source_sha_for() needs at least one style file")

    lines = []
    for style in sorted(files):
        if not _STYLE_RE.match(style):
            raise ValueError(f"style name must be alphanumeric, got {style!r}")
        lines.append(f"{style}={file_sha256(files[style])}")
    return sha256("\n".join(lines).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def content_key(
    config: Configuration, source_sha: str, generator_version: str
) -> str:
    """The 12-character content key for the font set built from ``config``.

    ``config``
        A resolved, validated K1 `Configuration` (the card's precondition, "Config
        rounded (M1.1)"). `KEYED_FIELDS` enter the key; `UNKEYED_FIELDS` do not.
    ``source_sha``
        64 lowercase hex characters identifying the base-font bytes -- normally
        `source_sha_for` over the styles about to be built.
    ``generator_version``
        The generation algorithm's version; `GENERATOR_VERSION` is this package's.

    The hashed payload, in order::

        scribesense/font-content-key/1
        generator=<generator_version>
        source=<source_sha>
        letter_em=0.12
        word_scale=1.50
        reading_line=1.5
        ui_line=1.2
        ligatures=liga

    Every term is named and newline-separated rather than concatenated, so no two
    different payloads can share a spelling -- plain concatenation lets ``1.2`` + ``1.5``
    and ``1.25`` + ``.1`` collide. The constrained inputs (hex digests, a token version,
    fixed-decimal numbers, alphanumeric feature tags) mean no value can contain a
    newline and forge a line of its own.

    ``ligatures`` is M1.4's `LIGATURE_FEATURES`, imported rather than restated. It is
    not a user setting -- it is a fixed policy of the patcher -- but it is a
    font-building input: the card names a "ligature setting" in the key, and if the
    policy is ever widened to ``dlig`` or narrowed to the ``latn`` script (K7 D4 keeps
    both open), every cached font becomes stale and must be rebuilt. Hashing it makes
    that automatic instead of a thing someone must remember.

    Raises ``ValueError`` -- never a silent key -- for an unvalidated or unrounded
    configuration, or a malformed ``source_sha`` or ``generator_version``. The choice
    mirrors M1.4 `patch`, which raises ``ValueError`` for caller errors and
    ``PatchUnsupported`` for font problems. It matters more here than there: an
    unrounded ``letter_em`` of 0.131 would key as ``"0.13"``, colliding with a genuine
    0.13 configuration whose artifacts differ -- the exact false hit this module exists
    to prevent. K1's ``NOT_ON_STEP`` is the "guard only" code for precisely this.

    >>> cfg = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
    >>> content_key(cfg, "00" * 32, "1.0")
    '0b82ba571f07'
    """
    errors = [issue for issue in validate(config) if issue.level == "error"]
    if errors:
        detail = ", ".join(f"{i.field}={i.code}" for i in errors)
        raise ValueError(f"content_key() requires a validated Configuration: {detail}")

    if not isinstance(source_sha, str) or not _SHA256_RE.match(source_sha):
        raise ValueError(
            f"source_sha must be 64 lowercase hex characters, got {source_sha!r}"
        )
    if not isinstance(generator_version, str) or not _VERSION_RE.match(
        generator_version
    ):
        raise ValueError(f"generator_version must be a short token, got "
                         f"{generator_version!r}")

    payload = "\n".join(
        [
            _DOMAIN,
            f"generator={generator_version}",
            f"source={source_sha}",
            *(f"{field}={canonical(field, getattr(config, field))}"
              for field in KEYED_FIELDS),
            f"ligatures={','.join(sorted(LIGATURE_FEATURES))}",
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:KEY_LENGTH]


# --------------------------------------------------------------------------- #
# Family names
# --------------------------------------------------------------------------- #


def is_content_key(key: object) -> bool:
    """Whether ``key`` is a well-formed content key: exactly 12 lowercase hex digits.

    Added for M1.7, which must refuse a malformed key *before* it reaches the
    filesystem -- a key becomes a directory name under ``fonts_dir()``, so ``"../.."``,
    an absolute path, a separator, a space or a control character have to be rejected,
    and Research2 H.3 requires a "fixed safe character set; bounded length". This
    predicate is that character set, and it is here rather than in the cache module so
    that there is one definition of what a key looks like: a second regex there could
    drift from `KEY_LENGTH` and start accepting names `content_key` can never produce.

    >>> is_content_key("3f9a1c2b7d10"), is_content_key("../../etc")
    (True, False)
    """
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def _checked(key: str) -> str:
    if not is_content_key(key):
        raise ValueError(
            f"key must be {KEY_LENGTH} lowercase hex characters, got {key!r}"
        )
    return key


def reading_family(key: str) -> str:
    """``AccessSans-<key>`` -- the reading variant's family name [K7].

    >>> reading_family("3f9a1c2b7d10")
    'AccessSans-3f9a1c2b7d10'
    """
    return f"{READING_FAMILY_PREFIX}-{_checked(key)}"


def ui_family(key: str) -> str:
    """``AccessSansUI-<key>`` -- the UI variant's family name [K7].

    >>> ui_family("3f9a1c2b7d10")
    'AccessSansUI-3f9a1c2b7d10'
    """
    return f"{UI_FAMILY_PREFIX}-{_checked(key)}"


def is_generated_family(name: object) -> bool:
    """Whether ``name`` is a family name this module produced.

    Two things depend on the answer. M1.6 must be able to assert that a built artifact
    does **not** carry the source family name -- Research2 E.7: "Never reuse the source
    family name for a modified font, because that would allow the generated font to
    shadow or be shadowed by the original" -- and M1.7 must be able to tell its own
    installed families from the system's when deciding what to remove.

    Both directions are safe by construction. No name without a 12-hex-digit suffix
    matches, so no real-world family can be mistaken for a generated one; K1's
    ``SHORTLIST`` is pinned against this by a test. And the suffix means a generated
    name cannot equal its source, whatever the source is called.

    >>> is_generated_family("AccessSans-3f9a1c2b7d10"), is_generated_family("Noto Sans")
    (True, False)
    """
    return isinstance(name, str) and bool(FAMILY_PATTERN.match(name))


def variant_filename(family: str, style: str, suffix: str = ".ttf") -> str:
    """The on-disk name of one artifact, e.g. ``AccessSans-<key>-Regular.ttf``.

    Deterministic and unique within a build: the family distinguishes reading from UI,
    the style distinguishes the four faces, so no two of the (at most) eight artifacts
    can land on one path. The name carries no timestamp, no process id and no temporary
    directory, so two builds of the same inputs produce the same file names as well as
    the same bytes.

    The suffix follows the source file, because M1.4 preserves the outline format it was
    given: a CFF source stays CFF and must stay ``.otf``.
    """
    if not is_generated_family(family):
        raise ValueError(f"not a generated family name: {family!r}")
    if not _STYLE_RE.match(style):
        raise ValueError(f"style name must be alphanumeric, got {style!r}")
    if suffix not in (".ttf", ".otf"):
        raise ValueError(f"unsupported font suffix: {suffix!r}")
    return f"{family}-{style}{suffix}"


def fc_pattern(family: str) -> str:
    """``family`` escaped for use as an ``fc-match`` pattern.

    In fontconfig's pattern syntax ``-`` separates the family from a size, so a bare
    ``AccessSansUI-3f9a1c2b7d10`` is parsed as family ``AccessSansUI`` at size ``3`` and
    fontconfig quietly substitutes another font. Every generated family has a ``-``, so
    every ``fc-match`` of one goes through here (``\\``, ``-``, ``:`` and ``,`` escaped).

    >>> fc_pattern("AccessSansUI-3f9a1c2b7d10")
    'AccessSansUI\\\\-3f9a1c2b7d10'
    """
    return re.sub(r"([\\\-:,])", r"\\\1", family)
