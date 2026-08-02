"""Origin allowlist for the doorbell page's theming channel.

Kept separate from server2.py so it can be tested without importing aiohttp,
pychromecast, and gtts.
"""
import re

DEFAULT_THEME_ORIGINS = "https://dashboard.baldino.me"

# A bare origin: scheme://host[:port]. No path, no query, no whitespace, no
# quotes. Origins are interpolated into an HTML attribute, so anything that is
# not this shape is dropped rather than escaped.
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")

_META_RE = re.compile(r'(<meta name="doorbell-theme-origins" content=")[^"]*(">)')

# Hosts a browser treats as potentially trustworthy over plain http.
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def parse_theme_origins(raw):
    """Parse a comma-separated allowlist, dropping anything malformed.

    Candidates are lowercased and stripped of a default port (`:443` for
    https, `:80` for http) before validation. Browsers always report
    `event.origin` lowercase and without a default port, so an operator's
    mixed-case entry or explicit `:443`/`:80` would otherwise pass validation
    and then silently never match.
    """
    origins = []
    for entry in raw.split(","):
        candidate = entry.strip().rstrip("/").lower()
        if candidate.startswith("https://") and candidate.endswith(":443"):
            candidate = candidate[: -len(":443")]
        elif candidate.startswith("http://") and candidate.endswith(":80"):
            candidate = candidate[: -len(":80")]
        if candidate and _ORIGIN_RE.match(candidate):
            origins.append(candidate)
    return origins


def is_secure_origin(origin):
    """True if a parent frame on this origin can host a secure-context iframe.

    A document is only a secure context if its own origin is trustworthy *and*
    every ancestor is. An https doorbell page inside an http parent therefore
    loses RTCPeerConnection and getUserMedia entirely.
    """
    if origin.startswith("https://"):
        return True
    if origin.startswith("http://"):
        host = origin[len("http://") :].split(":")[0]
        return host in _LOCAL_HOSTS
    return False


def insecure_origins(origins):
    """Return the subset of origins that cannot host a secure-context iframe."""
    return [o for o in origins if not is_secure_origin(o)]


def inject_theme_origins(html, origins):
    """Replace the doorbell-theme-origins meta tag's content attribute."""
    joined = ",".join(origins)
    return _META_RE.sub(lambda m: m.group(1) + joined + m.group(2), html)
