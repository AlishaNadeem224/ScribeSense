"""O1 — Font patcher, variants, validation, cache, generator API (M1.4–M1.8).

`patcher` is M1.4, the font patcher. `naming` is M1.5's deterministic names and content
key; `variants` is M1.5's reading/UI variant builder, which is the one module that puts
the other two together. `validate` is M1.6, which checks a built font really carries the
spacing it was asked for before M1.7 may install it. `cache` is M1.7: the only module
that writes into the user's font directory, and the owner of everything under
``fonts_dir()``. `generator` is M1.8, K7's one entry point -- the only module that
imports all of the others, and the only one a caller outside O1 should need.

Nothing is imported here. Using the patcher must never drag in the install path;
`naming` -- which M1.7's cache lookup needs -- must stay reachable without building
anything; and `validate` reaches for ``pango-view`` and ``fc-match``, which nothing
else in the package should have to care about.
"""
