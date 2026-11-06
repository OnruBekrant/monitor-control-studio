"""vcp: renk sıcaklığı matematiği (birim + özellik tabanlı testler)."""
from hypothesis import given
from hypothesis import strategies as st

from monitor_ddc import vcp


def test_reference_returns_neutral():
    assert vcp.gains_for_kelvin(6500, (50, 50, 50)) == (50, 50, 50)
    assert vcp.gains_for_kelvin(6500, (46, 52, 40)) == (46, 52, 40)


def test_warm_reduces_blue_cool_reduces_red():
    r, g, b = vcp.gains_for_kelvin(4000, (50, 50, 50))
    assert r == 50 and b < g < 50
    r, g, b = vcp.gains_for_kelvin(9300, (50, 50, 50))
    assert b == 50 and r < 50


def test_kelvin_to_rgb_monotonic_blue():
    blues = [vcp.kelvin_to_rgb(k)[2] for k in range(2000, 6600, 100)]
    assert blues == sorted(blues)


def test_tables_consistent():
    assert set(vcp.APPLY_ORDER) == set(vcp.FEATURES)
    assert vcp.USER_OWNED <= set(vcp.FEATURES)
    assert all(g in vcp.GROUPS for g in {f.group for f in vcp.FEATURES.values()})
    assert vcp.APPLY_ORDER[0] == vcp.PRESET, "ön ayar kazançları ezebilir; ilk yazılmalı"


@given(st.floats(min_value=-1e9, max_value=1e9, allow_nan=False),
       st.tuples(*[st.integers(0, 100)] * 3))
def test_gains_always_in_range(kelvin, neutral):
    out = vcp.gains_for_kelvin(kelvin, neutral)
    assert len(out) == 3 and all(0 <= x <= 100 for x in out)
