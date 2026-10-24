"""Which URLs the driver may open: checked inside ``navigate()``, before the browser is touched.

The scheduler only ever asks for the three surfaces of DESIGN (the Groups feed, a group page in
chronological order, the joined-groups list), but "the scheduler only asks for" is a convention;
this is the rule. A URL that is not *exactly* one of those shapes raises before any CDP message
is sent, so a bug upstream (a permalink, a profile, a marketplace item, a vanity slug) can never
turn into a Facebook request.

Tests and the self-test inject ``LocalOnly``, which admits nothing but ``http://127.0.0.1`` on
one port: a test that accidentally built a facebook.com URL fails loudly instead of reaching
Meta. ``about:blank`` is not a navigation target here; parking is the driver's own business.
"""

from __future__ import annotations

import re
from typing import Final, Protocol
from urllib.parse import urlsplit

from fbcollector.detectors import FEED_URL, JOINS_URL

_GROUP_URL: Final = re.compile(
    r"https://www\.facebook\.com/groups/[1-9][0-9]{0,18}/"
    r"\?sorting_setting=(?:CHRONOLOGICAL|CHRONOLOGICAL_LISTINGS)"
)
"""Matched with ``fullmatch``: ``$`` would also accept a trailing newline."""
LOOPBACK: Final = "127.0.0.1"


class UrlNotAllowedError(Exception):
    """``navigate()`` was asked for a URL outside the allow-list; nothing was sent."""


class UrlPolicy(Protocol):
    def allows(self, url: str) -> bool: ...


class FacebookSurfaces:
    """Production: exactly the three surfaces of DESIGN (Surfaces and URL allow-list)."""

    def allows(self, url: str) -> bool:
        return url in {FEED_URL, JOINS_URL} or _GROUP_URL.fullmatch(url) is not None


class LocalOnly:
    """Tests and the self-test: ``http://127.0.0.1:<port>/…`` and nothing else."""

    def __init__(self, port: int) -> None:
        if not 0 < port < 65536:
            msg = "a TCP port is 1..65535"
            raise ValueError(msg)
        self._port = port

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme == "http"
            and parts.hostname == LOOPBACK
            and port == self._port
            and parts.username is None
            and parts.password is None
        )


def require_allowed(policy: UrlPolicy, url: str) -> None:
    if not policy.allows(url):
        # The URL itself is not quoted: a wrong one may carry ids of people or posts.
        msg = f"navigation refused by the URL allow-list ({type(policy).__name__})"
        raise UrlNotAllowedError(msg)
