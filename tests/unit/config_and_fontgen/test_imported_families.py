"""K1 rule 6 (amendment 2026-11-24, W1 font import): imported family names in validate()."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scribesense.config import settings
from scribesense.contracts.config import Configuration

GOOD = Configuration("My Font", 0.12, 1.5, 1.5, 1.2, 1.0)


@pytest.fixture(autouse=True)
def no_provider() -> Iterator[None]:
    settings.set_imported_families(None)
    yield
    settings.set_imported_families(None)  # module state: never leak a provider into other tests


def codes(config: Configuration) -> list[str]:
    return [i.code for i in settings.validate(config) if i.field == "base_family"]


def test_without_a_provider_an_imported_name_is_unknown() -> None:
    assert codes(GOOD) == ["UNKNOWN_FONT"]


def test_a_listed_imported_family_is_valid_and_others_are_not() -> None:
    settings.set_imported_families(lambda: ["My Font"])
    assert codes(GOOD) == []
    assert codes(Configuration("Other Font", 0.12, 1.5, 1.5, 1.2, 1.0)) == ["UNKNOWN_FONT"]


def test_bundled_names_still_validate_without_any_provider() -> None:
    for family in settings.SHORTLIST:
        assert codes(Configuration(family, 0.12, 1.5, 1.5, 1.2, 1.0)) == []


@pytest.mark.parametrize("name", ["", "noto sans", "NOTO SANS", " My Font", "My Font ", "My\nFont", "x" * 65, 5])
def test_names_rule_6_refuses(name: object) -> None:
    assert settings.imported_name_ok(name) is False


def test_a_refused_name_is_unknown_even_if_the_provider_lists_it() -> None:
    settings.set_imported_families(lambda: ["noto sans", "x" * 65])
    assert codes(Configuration("noto sans", 0.12, 1.5, 1.5, 1.2, 1.0)) == ["UNKNOWN_FONT"]


def test_a_failing_provider_refuses_instead_of_raising() -> None:
    def broken() -> list[str]:
        raise OSError("store unreadable")
    settings.set_imported_families(broken)
    assert codes(GOOD) == ["UNKNOWN_FONT"]
