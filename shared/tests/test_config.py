"""Settings that are themselves a security boundary must be hard to mis-set.

``OPSPILOT_ALLOWED_WRITE_PREFIXES`` decides where a template may write on a
remote host. A silent misconfiguration there is not a typo — it is a widened
boundary — so every entry is normalised and the shapes that would widen past
what the operator wrote are refused at load time, not at the first write.
"""

from __future__ import annotations

import pytest

from shared.config import Settings, _normalise_write_prefix


# ==========================================================================
# Normalising a single prefix
# ==========================================================================
def test_a_prefix_is_normalised_to_a_directory_form() -> None:
    assert _normalise_write_prefix("/opt/20kpay") == "/opt/20kpay/"
    assert _normalise_write_prefix("  /opt/20kpay/  ") == "/opt/20kpay/"
    assert _normalise_write_prefix("/opt/20kpay//api///") == "/opt/20kpay/api/"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/",
        "//",
        "relative/path",
        "opt/opspilot/apps",
        "/opt/../etc",
        "/opt/../../etc/passwd",
        "/opt/app\x00",
        "/opt/app\n",
    ],
)
def test_a_prefix_that_would_widen_past_what_was_written_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        _normalise_write_prefix(raw)


# ==========================================================================
# The setting itself
# ==========================================================================
def test_an_empty_setting_means_the_product_defaults() -> None:
    assert Settings(allowed_write_prefixes="").write_prefixes() == ()
    assert Settings().write_prefixes() == ()


def test_entries_are_split_trimmed_and_normalised() -> None:
    settings = Settings(allowed_write_prefixes=" /opt/20kpay , /www/python/ , , ")
    assert settings.write_prefixes() == ("/opt/20kpay/", "/www/python/")


def test_one_bad_entry_refuses_the_whole_setting() -> None:
    """Never "the good ones apply and the bad one is quietly dropped"."""
    with pytest.raises(ValueError):
        Settings(allowed_write_prefixes="/opt/20kpay/, /")
