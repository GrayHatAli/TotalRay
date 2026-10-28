"""Parser hardening: values that make `sing-box check` reject a whole test batch."""
from __future__ import annotations

import base64
import json

import pytest

from totalray import parsers
from totalray.parsers import ParseError, parse_link, parse_many, parse_singbox_config

# 43-char base64url key, as seen in real subscriptions.
GOOD_KEY = "cSku9Lo_wLXixUZuWW2JZxcSUK-fGprJLKrCGUfoAgI"
UUID = "a52fcd33-b235-4b30-a4e8-165287302c66"


def _reality(pbk=GOOD_KEY, fp="chrome", extra=""):
    q = f"security=reality&encryption=none&pbk={pbk}&sni=apple.com&sid=ab&type=tcp"
    if fp is not None:
        q += f"&fp={fp}"
    return f"vless://{UUID}@host.example:443?{q}{extra}#remark"


def _tls_ws(fp):
    return (f"vless://{UUID}@host.example:443?security=tls&type=ws&host=h.example"
            f"&sni=h.example&path=/x&fp={fp}")


# ---------------------------------------------------------------- truncation
def test_link_cut_inside_the_address_or_credentials_is_rejected():
    link = f"vless://{UUID[:12]}\u2026"
    with pytest.raises(ParseError, match="truncated link"):
        parse_link(link)
    with pytest.raises(ParseError, match="truncated link"):
        parse_link(f"vless://{UUID}@host.exam\u2026")


def test_truncated_reality_key_is_rejected_by_key_validation():
    with pytest.raises(ParseError, match="public_key"):
        parse_link(_reality(pbk="SbVKOEMjK\u2026"))


def test_truncated_non_essential_tail_is_kept():
    """Regression guard: in the production database 239 of 562 links cut off in
    the query tail still passed a connectivity test, so a tail cut must not
    discard the config (an earlier draft rejected them all)."""
    link = (f"vless://{UUID}@host.example:443?security=tls&type=ws&sni=h.example"
            f"&host=h.example&path=%2Fabc%2Fdef\u2026")
    item = parse_link(link)
    assert item["outbound"]["server"] == "host.example"
    assert item["outbound"]["transport"]["type"] == "ws"


def test_ellipsis_in_the_remark_is_harmless():
    item = parse_link(f"vless://{UUID}@host.example:443?security=none&type=tcp#Germany\u2026")
    assert item["outbound"]["server"] == "host.example"


def test_truncated_links_are_counted_not_crashed_in_parse_many():
    items, bad, reasons = parse_many([
        _reality(),
        f"vless://{UUID[:12]}\u2026",           # address cut off
        _reality(pbk="abc\u2026"),               # key cut off
        _tls_ws("chro\u2026"),                   # fingerprint cut off, rest intact
    ])
    assert len(items) == 2 and bad == 2
    assert reasons["truncated link"]["count"] == 1
    assert reasons["reality public_key is not a valid 32-byte key"]["count"] == 1
    assert "utls" not in items[1]["outbound"]["tls"]


# --------------------------------------------------------------- fingerprint
@pytest.mark.parametrize("fp", ["unsafe", "randomizednoalpn", "bogus", ""])
def test_unknown_fingerprint_is_dropped_for_plain_tls(fp):
    ob = parse_link(_tls_ws(fp))["outbound"]
    assert "utls" not in ob["tls"]


@pytest.mark.parametrize("fp", ["unsafe", "bogus", None])
def test_unknown_or_missing_fingerprint_falls_back_to_chrome_for_reality(fp):
    ob = parse_link(_reality(fp=fp))["outbound"]
    assert ob["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}


def test_fingerprint_is_lowercased():
    ob = parse_link(_reality(fp="Chrome"))["outbound"]
    assert ob["tls"]["utls"]["fingerprint"] == "chrome"


# ----------------------------------------------------------- reality pubkey
def test_valid_reality_key_passes_through_unchanged():
    ob = parse_link(_reality())["outbound"]
    assert ob["tls"]["reality"] == {"enabled": True, "public_key": GOOD_KEY,
                                    "short_id": "ab"}


def test_standard_base64_key_is_converted_to_the_url_safe_form():
    raw = bytes([251, 255]) * 16  # encodes with both '+' and '/'
    std = base64.b64encode(raw).decode()
    assert "+" in std and "/" in std and std.endswith("=")
    want = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    link = _reality(pbk=std.replace("+", "%2B").replace("/", "%2F"))
    assert parse_link(link)["outbound"]["tls"]["reality"]["public_key"] == want


@pytest.mark.parametrize("pbk", [
    "PUj1la__ltNjv_ASE5n4HJY7GiRsN-0aYhkcAApCI",    # 41 chars (real, mangled)
    "PUj1la__ltNjv_ASE5n4HJY7GiRs\u2014N-0aYhkcAApCI",  # em dash inside
    "tooShort",
])
def test_invalid_reality_key_is_rejected(pbk):
    with pytest.raises(ParseError, match="public_key"):
        parse_link(_reality(pbk=pbk))


# ------------------------------------------------------------- JSON configs
def _json_sub(*outbounds):
    return json.dumps({"outbounds": list(outbounds)})


def _ob(tls, tag="t"):
    return {"type": "vless", "tag": tag, "server": "h.example", "server_port": 443,
            "uuid": UUID, "tls": tls}


def test_json_subscription_outbounds_are_sanitised_too():
    good = _ob({"enabled": True, "server_name": "a.example",
                "utls": {"enabled": True, "fingerprint": "firefox"}}, "good")
    unsafe = _ob({"enabled": True, "server_name": "b.example",
                  "utls": {"enabled": True, "fingerprint": "unsafe"}}, "unsafe")
    bad_key = _ob({"enabled": True, "utls": {"enabled": True, "fingerprint": "chrome"},
                   "reality": {"enabled": True, "public_key": "SbVKOEMjK\u2026"}}, "badkey")
    reality_no_utls = _ob({"enabled": True, "reality": {
        "enabled": True, "public_key": GOOD_KEY, "short_id": "ab"}}, "rnu")

    items, bad, reasons = parse_singbox_config(
        _json_sub(good, unsafe, bad_key, reality_no_utls))

    by_name = {i["name"]: i["outbound"] for i in items}
    assert set(by_name) == {"good", "unsafe", "rnu"}
    assert bad == 1 and "public_key" in next(iter(reasons))
    assert by_name["good"]["tls"]["utls"]["fingerprint"] == "firefox"
    assert "utls" not in by_name["unsafe"]["tls"]
    assert by_name["rnu"]["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}


def test_sanitising_does_not_mutate_its_input():
    original = _ob({"enabled": True, "utls": {"enabled": True, "fingerprint": "unsafe"}})
    snapshot = json.dumps(original, sort_keys=True)
    parsers.sanitize_outbound(original)
    assert json.dumps(original, sort_keys=True) == snapshot


# ---------------------------------------------------- dedupe-key stability
def test_valid_links_keep_the_exact_same_outbound_and_fingerprint():
    """A hardening pass must not re-key configs that were already fine,
    otherwise every existing row would be re-imported as a new candidate."""
    item = parse_link(_reality())
    assert item["outbound"] == {
        "type": "vless", "server": "host.example", "server_port": 443, "uuid": UUID,
        "tls": {"enabled": True, "server_name": "apple.com",
                "utls": {"enabled": True, "fingerprint": "chrome"},
                "reality": {"enabled": True, "public_key": GOOD_KEY, "short_id": "ab"}},
    }
