import pytest

from theme_origins import (
    DEFAULT_THEME_ORIGINS,
    inject_theme_origins,
    insecure_origins,
    is_secure_origin,
    parse_theme_origins,
)


def test_default_is_the_dashboard_https_origin():
    assert parse_theme_origins(DEFAULT_THEME_ORIGINS) == [
        "https://dashboard.baldino.me"
    ]


def test_parses_comma_separated_and_trims_whitespace():
    raw = " https://a.example.com , https://b.example.com "
    assert parse_theme_origins(raw) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_strips_trailing_slash_so_it_matches_event_origin():
    # window.postMessage event.origin never has a trailing slash.
    assert parse_theme_origins("https://a.example.com/") == ["https://a.example.com"]


def test_empty_string_yields_no_origins():
    assert parse_theme_origins("") == []
    assert parse_theme_origins("  ,  ") == []


@pytest.mark.parametrize(
    "raw",
    [
        'https://a.example.com" onload="alert(1)',
        "javascript:alert(1)",
        "not-a-url",
        "https://a.example.com/path",
        "https://a.example.com two",
    ],
)
def test_rejects_malformed_or_unsafe_entries(raw):
    # Origins land in an HTML attribute, so anything that is not a bare
    # scheme://host[:port] is dropped rather than escaped.
    assert parse_theme_origins(raw) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Browsers always report event.origin lowercase.
        ("https://Dashboard.Baldino.me", "https://dashboard.baldino.me"),
        # Browsers omit default ports from an origin.
        ("https://dashboard.baldino.me:443", "https://dashboard.baldino.me"),
        # Both normalisations apply together, and the local-host check
        # downstream (is_secure_origin) depends on the host being lowercase.
        ("http://LOCALHOST:5173", "http://localhost:5173"),
        # A non-default port must be preserved, not stripped.
        ("https://example.com:8443", "https://example.com:8443"),
    ],
)
def test_normalizes_case_and_default_port(raw, expected):
    assert parse_theme_origins(raw) == [expected]


def test_keeps_valid_entries_alongside_rejected_ones():
    raw = "https://good.example.com,not-a-url,https://also-good.example.com:8443"
    assert parse_theme_origins(raw) == [
        "https://good.example.com",
        "https://also-good.example.com:8443",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "https://dashboard.baldino.me",
        "https://dashboard.baldino.me:8443",
        "http://localhost",
        "http://localhost:5173",
        "http://127.0.0.1:3042",
    ],
)
def test_secure_origins(origin):
    assert is_secure_origin(origin) is True


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.1.220:3042",
        "http://192.168.1.220:5173",
        "http://dashboard.baldino.me",
    ],
)
def test_insecure_origins_rejected(origin):
    # An https iframe inside an http parent is not a secure context at all:
    # RTCPeerConnection and getUserMedia both become unavailable.
    assert is_secure_origin(origin) is False


def test_insecure_origins_lists_only_the_bad_ones():
    origins = ["https://good.example.com", "http://192.168.1.220:3042"]
    assert insecure_origins(origins) == ["http://192.168.1.220:3042"]


def test_injects_origins_into_the_meta_tag():
    html = '<head><meta name="doorbell-theme-origins" content=""></head>'
    out = inject_theme_origins(html, ["https://a.example.com", "https://b.example.com"])
    assert (
        '<meta name="doorbell-theme-origins" '
        'content="https://a.example.com,https://b.example.com">' in out
    )


def test_injection_replaces_rather_than_appends():
    html = '<meta name="doorbell-theme-origins" content="https://stale.example.com">'
    out = inject_theme_origins(html, ["https://fresh.example.com"])
    assert "stale.example.com" not in out
    assert "fresh.example.com" in out


def test_injection_with_no_origins_leaves_an_empty_attribute():
    html = '<meta name="doorbell-theme-origins" content="https://a.example.com">'
    assert 'content=""' in inject_theme_origins(html, [])


def test_injection_is_a_noop_when_the_meta_tag_is_absent():
    html = "<head><title>x</title></head>"
    assert inject_theme_origins(html, ["https://a.example.com"]) == html
