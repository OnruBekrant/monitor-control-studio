"""ddc ayrıştırıcıları: ddcutil çıktısı ve EDID güvenilmeyen girdidir → bulanık (fuzz) testler."""
from conftest import make_edid
from hypothesis import given, settings
from hypothesis import strategies as st

from monitor_ddc import ddc


def test_parse_terse_types():
    out = ddc.parse_terse(
        "VCP 10 C 35 100\nVCP 14 CNC x00 x0b x00 x08\nVCP DF CNC x00 x00 x02 x02\n"
        "VCP 0F ERR\ngarbage line\nVCP 62 C 30 100\n")
    assert out == {0x10: (35, 100), 0x14: (8, None), 0xDF: (0x0202, None), 0x62: (30, 100)}


def test_parse_terse_rejects_malformed_numbers():
    assert ddc.parse_terse("VCP 10 C abc 100\nVCP 14 CNC xzz") == {}


@settings(max_examples=500)
@given(st.text())
def test_parse_terse_never_raises(text):
    res = ddc.parse_terse(text)
    assert all(0 <= k <= 0xFF for k in res)


@settings(max_examples=300)
@given(st.lists(st.tuples(st.integers(0, 255), st.sampled_from(["C", "CNC", "SNC", "T", "ERR", "X"]),
                          st.lists(st.text(alphabet="x0123456789abcdefg ", max_size=6), max_size=5))))
def test_parse_terse_structured_fuzz(rows):
    text = "\n".join(f"VCP {c:02X} {t} {' '.join(r)}" for c, t, r in rows)
    ddc.parse_terse(text)


def test_parse_edid_real_layout():
    info = ddc.parse_edid(make_edid(serial_str="ABC123"))
    assert info["mfg"] == "BOE" and info["model"] == "NV156FHM-T06"
    assert info["product_code"] == 0x2014 and info["bin_serial"] == 1 and info["serial"] == "ABC123"


def test_parse_edid_rejects_bad_header_and_short():
    assert ddc.parse_edid(b"") == {}
    assert ddc.parse_edid(b"\x00" * 128) == {}
    assert ddc.parse_edid(make_edid()[:100]) == {}


@settings(max_examples=500)
@given(st.binary(max_size=512))
def test_parse_edid_never_raises_on_random(data):
    ddc.parse_edid(data)


@settings(max_examples=500)
@given(st.binary(min_size=120, max_size=120))
def test_parse_edid_with_valid_header_fuzzed_body(body):
    info = ddc.parse_edid(bytes.fromhex("00ffffffffffff00") + body)
    assert len(info["model"]) <= 13 and len(info["serial"]) <= 13
    assert len(info["mfg"]) == 3


def test_display_key_stable():
    d = ddc.Display(bus=7, mfg="BOE", model="NV156FHM-T06", product_code=0x2014, bin_serial=1)
    assert d.key == "BOE:NV156FHM-T06:2014:1"
    d.bus = 3
    assert d.key == "BOE:NV156FHM-T06:2014:1", "bus numarası değişse de anahtar aynı kalmalı"
