"""Digest: контрольное значение из примера RFC 2069 §2.4."""

import re

from qa_tool.drivers.camera.digest import digest_header, parse_challenge, uses_qop

CHALLENGE = 'Digest realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093"'


def test_parse_challenge():
    assert parse_challenge('Basic realm="x"')["scheme"] == "basic"
    parsed = parse_challenge(CHALLENGE + ', qop="auth,auth-int", opaque="abc"')
    assert parsed["scheme"] == "digest"
    assert parsed["realm"] == "testrealm@host.com"
    assert parsed["opaque"] == "abc"
    assert uses_qop(parsed)


def test_rfc2069_response_without_qop():
    header = digest_header(
        "Mufasa", "CircleOfLife", parse_challenge(CHALLENGE), "GET", "/dir/index.html", 0
    )
    assert 'response="1949323746fe6a43ef61f9606e7febea"' in header


def test_qop_adds_counter_and_opaque():
    challenge = parse_challenge(CHALLENGE + ', qop="auth", opaque="abc"')
    header = digest_header("u", "p", challenge, "POST", "/a", 3)
    assert re.search(r"nc=00000003", header)
    assert 'opaque="abc"' in header
    assert "cnonce=" in header
