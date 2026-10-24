"""Fail-closed page-state classifier: a step is OK only when a positive signature holds.

The dangerous mistake here is not a false alarm but a missed warning: reading on into a
checkpoint or an automation wall is what turns a warning into a lost account (sellee kept
polling for days because its detector did not recognise the wall). So nothing is OK by default.
A step is OK only when *all* of these hold (DESIGN, Detectors): logged-in markers (``c_user``
cookie and a non-zero ``USER_ID``), a final URL of the requested surface, an English page, no
dialog we do not recognise, and either a story or a known empty marker. Everything else is one
of the named states, evaluated in a fixed order — the first match wins:

``BLOCKED > LOGGED_OUT > UNKNOWN > FEED_NOT_CHRONO > GROUP_LOST > NOT_FOLLOWED > SORT_IGNORED > OK``

Signals are read from more than one place on purpose. The parser provides scoped facts
(``PageFacts``), but the safety-critical ones — the URL, HTTP status, GraphQL error codes,
``USER_ID``, the password field — are also scanned here straight from the raw step, so a broken
or renamed parser path can make a page UNKNOWN but never make a checkpoint look like a feed.

Phrase matching is scoped to what cannot be a post: dialog texts (isolated world) and the SSR
document's visible text outside ``<script>`` blocks, which on a Comet page is little more than
the group name. Short, common phrases ("security check", "unusual activity") count in the
visible text only when the page shows no stories, so a group *named* "Security check" does not
lock the collector out; in a dialog they always count. ``captcha`` and ``checkpoint`` are URL
signals only in the SSR document and the visible text: both words occur dozens of times in
every page's JavaScript module names. In an unread item of the notifications panel, "complete
the checkpoint" is a trouble word (``PANEL_TROUBLE``).

Adapted with thanks from carousell/sellee (Apache-2.0: path-segment checkpoint match, automation
phrases), viseshrp/fbn (MIT: fail-closed classifier), adbertram/cli-tools (MIT: logout by
``USER_ID "0"``) and MEOMcGill/fbscrape (MIT snapshot 7fca6ca: error 1675004, members-only
marker); code written here.
"""

from __future__ import annotations

import enum
import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

from fbcollector.frames import iter_graphql_frames
from fbcollector.model import GroupMeta, PageState, Step, Surface

FACEBOOK_ORIGIN: Final = "https://www.facebook.com"
FEED_URL: Final = f"{FACEBOOK_ORIGIN}/?filter=groups&sk=h_chr"
JOINS_URL: Final = f"{FACEBOOK_ORIGIN}/groups/joins/"
CHRONOLOGICAL: Final = "CHRONOLOGICAL"
CHRONOLOGICAL_LISTINGS: Final = "CHRONOLOGICAL_LISTINGS"
FEED_STYLE: Final = "MOST_RECENT_GROUPS_FEED"
FEED_ORDER: Final = "MOST_RECENT"
RATE_LIMIT_CODE: Final = 1675004
EXCERPT_MAX: Final = 300
JOIN_STATE_UNSTATED: Final = "join_state:unstated"
"""The one ``GROUP_LOST`` reason that is an absence: the page of a group confirmed by hand
does not say ``viewer_join_state`` at all."""
_DIGITS = re.compile(r"^[1-9][0-9]{0,18}$")


def group_url(group_id: str, *, listings: bool = False) -> str:
    """The backstop URL of a group; only numeric ids are ever opened."""
    if not _DIGITS.match(group_id):
        msg = "a group URL needs a numeric group id"
        raise ValueError(msg)
    sort = CHRONOLOGICAL_LISTINGS if listings else CHRONOLOGICAL
    return f"{FACEBOOK_ORIGIN}/groups/{group_id}/?sorting_setting={sort}"


def surface_url(surface: Surface, group_id: str | None = None, *, listings: bool = False) -> str:
    """The one URL of each allow-listed surface (DESIGN, Surfaces and URL allow-list)."""
    if surface is Surface.FEED:
        return FEED_URL
    if surface is Surface.JOINS:
        return JOINS_URL
    if group_id is None:
        msg = "a group surface needs a group id"
        raise ValueError(msg)
    return group_url(group_id, listings=listings)


# -- signals ----------------------------------------------------------------------------------

_BLOCK_URL: Final = (
    ("checkpoint", re.compile(r"(?:^|/)checkpoint(?=/|$|[?&#])", re.IGNORECASE)),
    ("two_step_verification", re.compile(r"two_step_verification", re.IGNORECASE)),
    ("auth_platform", re.compile(r"auth_platform", re.IGNORECASE)),
    ("recover", re.compile(r"/recover(?=/|$|[?&#.])", re.IGNORECASE)),
    ("login_device_based", re.compile(r"/login/device-based", re.IGNORECASE)),
    ("unsupported_browser", re.compile(r"/zero/unsupported_browser/", re.IGNORECASE)),
    ("captcha", re.compile(r"captcha", re.IGNORECASE)),
)
_ADVANCED_PROTECTION: Final = re.compile(r"advanced_protection", re.IGNORECASE)
_CONSENT_URL: Final = re.compile(r"privacy/consent", re.IGNORECASE)
_LOGIN_PATH: Final = re.compile(r"^/login(?:[./?]|$)", re.IGNORECASE)

_STRONG_PHRASES: Final = (
    ("suspect_automated", re.compile(r"suspect\w* automated behavio(?:u)?r", re.IGNORECASE)),
    ("temporarily_blocked", re.compile(r"temporarily blocked", re.IGNORECASE)),
    ("cant_use_feature", re.compile(r"can'?t use this feature", re.IGNORECASE)),
    ("suspended_account", re.compile(r"suspended your account", re.IGNORECASE)),
    (
        "account_disabled",
        re.compile(
            r"account (?:has been|was|is) (?:disabled|restricted|suspended|locked)", re.IGNORECASE
        ),
    ),
    (
        "no_other_users",
        re.compile(r"ensure that no other users or tools have access", re.IGNORECASE),
    ),
)
_WEAK_PHRASES: Final = (
    ("going_too_fast", re.compile(r"going too fast", re.IGNORECASE)),
    ("confirm_identity", re.compile(r"confirm your identity", re.IGNORECASE)),
    ("unusual_activity", re.compile(r"unusual activity", re.IGNORECASE)),
    ("security_check", re.compile(r"security check", re.IGNORECASE)),
)
_RATE_LIMIT_TEXT: Final = re.compile(r"rate limit exceeded", re.IGNORECASE)
_MEMBERS_ONLY: Final = re.compile(r"only members can see", re.IGNORECASE)
_PANEL_HEAD: Final = re.compile(r"\ANotifications All Unread\b")
_PANEL_FOOT: Final = re.compile(r"\bSee previous notifications\Z")
_UNREAD: Final = re.compile(r"\bUnread\b")
_ITEM_AGE: Final = re.compile(r"\b\d{1,3}[mhdwy]\b")
"""An age ("1h", "5d"). It ends a notification unless what follows goes on with the notice
(``_starts_item``): a number of the notice's own ("3 times in 24h.") does not."""
_ITEM_START: Final = re.compile(r"\s*(?:\Z|(?:Unread|Earlier|New|See previous notifications)\b)")
_GOES_ON: Final = frozenset(".,;:!?()[]{}…—–-")
"""Marks that go on with the sentence after a number ("24h.", "2h (Chrome)", "2h — twice")."""
_TITLES: Final = re.compile(r"\A(?:\s*(?:New|Earlier|See all)\b)*")
_SENTENCE_END: Final = re.compile(r"[.!?…\"'”»)]\s*\Z")
_LOOSE_AGE: Final = re.compile(r"\b(?:Just now|Yesterday)\b")
"""An age that is no number: the split does not know it, so its item runs on into the next;
a notice for the owner ends at it."""
_LOOSE_AGES: Final[Mapping[str, str | None]] = {"Just now": None, "Yesterday": "1d"}
_AGE_UNIT: Final = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "y": 365 * 86400}
PANEL_TROUBLE: Final = re.compile(
    r"suspicious (?:activity|login|log ?in|sign[- ]?in|attempt|behaviou?r|device|location)"
    r"|(?:account|profile)(?: \w+){0,2} (?:is |may be |might be |could be )?at risk"
    r"|at risk of (?:being )?(?:disabled|suspended|restricted|locked)|secure your account"
    r"|(?:account|profile|\byou(?:'re|'ve| are| have| were)?)\b[^.!?]{0,40}?"
    r"(?:restrict|(?<!b)locked|suspen|disabled)"
    r"|(?:restrict(?:ed|ions?)?|(?<!b)locked|suspended|disabled) (?:on |to |from )?your"
    r"|\bblocked (?:you\b|your (?!login))"
    r"|(?:you(?:'re|'ve| are| were| have)|account (?:is|was|has))"
    r" (?:been )?(?:temporarily )?blocked"
    r"|temporarily (?:blocked|restricted|limited|locked|suspended|unable)"
    r"|(?:you can'?t|unable to) (?:post|comment|send|join|use|go live)"
    r"|(?:we(?:'ve| have)?|temporarily|facebook has|meta has) limited"
    r"|limited your (?:account|profile)"
    r"|(?:account|profile) (?:has been|was|is|is being) limited"
    r"|removed your|your (?:post|comment|photo|video|reel|story|account|profile) (?:was|has been)"
    r" removed|community standards|account status"
    r"|security (?:check|alert|issue|warning|reasons?|concern|risk)|account security"
    r"|your (?:account'?s? )?security|verify (?:your|it'?s you|that it'?s you|you\b)"
    r"|(?:account|identity|id|age|photo|selfie|video) verification"
    r"|verification (?:is )?(?:pending|required|needed|in progress)"
    r"|unusual (?:activity|login|log ?in|sign[- ]?in|attempt|behaviou?r|device|location)"
    r"|confirm (?:it'?s you|it was you|this was you|that was you|your)"
    r"|(?:account|profile) (?:is |was |has been |placed )?(?:under|pending) review"
    r"|your (?:facebook )?(?:account|profile|post|comment) (?:was |has been |is |may be )?flagged"
    r"|(?:complete|pass|go through) (?:the |a |this )?(?:security )?checkpoint"
    r"|checkpoint to continue"
    r"|your (?:facebook )?(?:account|profile|password)(?: \w+){0,3} (?:hacked|compromised)"
    r"|(?:hacked|compromised) (?:into )?your"
    r"|(?:(?:may|might|could) have(?: had| gained)?|someone(?: else)?(?: has| had| gained)?)"
    r" access(?:ed)? (?:to )?your (?:facebook )?account"
    r"|(?:change|reset) your password to continue"
    r"|(?:need|have|must) to (?:change|reset) your password|must (?:change|reset) your password"
    r"|we(?:'ve| have)? reset your password|your password (?:was|has been) reset"
    r"|create a new password",
    re.IGNORECASE,
)
"""Words of a notice that the account is in trouble (a restriction, a lock, a removal, a check, a
compromise, a password Facebook changed or reset, or makes the owner change) in an unread item
of the notifications panel: such a panel is not recognised, so the page is UNKNOWN. Every word
is tied to the account or to the owner ("your account was hacked", "you're restricted", "your
account has been locked", "complete the checkpoint", "account verification", "unusual login",
"security issue"): an unread item of a friend or a group quotes a post ("a security deposit",
"temporarily closed", "a disabled parking spot", "the car got locked", "a kitchen hack"), stays
unread on this account nobody reads, and a bare word there would stop every read the panel opens
on, 24 h and then 72 h each (the final review of 2026-10-01). A security notice is here, not in
``PANEL_HEADS_UP``: it does not say which security matter (a "security check" is a wall).
"blocked" is trouble said of the owner ("you're blocked"), not in "blocked a login attempt",
which is Facebook guarding the account. "Keep your account safe" and "protect your account" are
not here: a plain login heads-up may say them. The text is read with Facebook's typographic
apostrophe made plain."""
PANEL_HEADS_UP: Final = re.compile(
    r"new login|log(?:ged)? ?in(?:to)? (?:to )?your account|tried to log|login alert|password"
    r"|two[- ]factor|\b2fa\b|login code|login attempt|you(?:'ve| have| were)(?: been)? logged out"
    r"|(?:e-?mail|phone(?: number)?|mobile number|contact (?:info|point))\b[^.!?]{0,40}?"
    r"\b(?:changed|added|removed|updated)",
    re.IGNORECASE,
)
"""Words of a notice about a login, a password or the account's email or phone in an unread
item of the notifications panel: a heads-up for the owner (``panel_heads_up_items``), not a
sign that Facebook acts against the account, so the panel is still recognised. An item that
also has a word of ``PANEL_TROUBLE`` ("suspicious login") is trouble, and so is one that
looks cut short whose next read item has one (``panel_problem``)."""
PANEL_NOTICE_REASON: Final = "dialog:panel_account_notice"
"""Beside ``signature:dialogs_clear``: the notifications panel was whole, and an unread item
in it says the account is in trouble (``PANEL_TROUBLE``)."""
PANEL_NOTICE_MAX: Final = 200


@dataclass(frozen=True, slots=True)
class HeadsUpItem:
    """One unread login or password notice of a notifications panel (``panel_heads_up_items``)."""

    text: str
    """Its words, without "Unread" and without its age, whitespace collapsed, at most
    ``PANEL_NOTICE_MAX`` characters."""
    age: str | None
    """The age that ends it ("6d"; "Yesterday" reads "1d"), or ``None`` for "Just now" or no
    age shown."""


def age_seconds(age: str | None) -> int:
    """How long ago a panel item came, by its age: "5d" is five days; ``None`` ("Just now",
    or no age shown) is now."""
    return 0 if age is None else int(age[:-1]) * _AGE_UNIT[age[-1]]


def age_unit(age: str | None) -> int:
    """The step of an age: Facebook shows whole units, so "5d" is anything from five days up
    to six, and an item's arrival read from it is that far off at most. ``None``: a minute."""
    return 60 if age is None else _AGE_UNIT[age[-1]]


def panel_problem(text: str) -> str | None:
    """Why a dialog text is not Facebook's notifications panel in a state the reading may
    pass, or ``None`` when it is.

    The panel ("Notifications All Unread New See all …") sat in the Groups feed's DOM on the
    first read after a new notification, on 2026-09-30, with no click: block #1. It is known
    only whole, up to its footer "See previous notifications" (a text the driver cut at
    ``MAX_DIALOG_TEXT`` hides what follows), and only while no unread item says the account
    is in trouble (``PANEL_TROUBLE``); an unread login or password notice
    (``PANEL_HEADS_UP``) is told to the owner (``panel_heads_up_items``) and passes. An item
    ends with its age ("1h", "5d") unless what follows goes on with the notice
    (``_panel_items``), and is unread when it says "Unread"; read items are not looked at,
    since a notice the owner has read stays in the list for weeks. An item whose age is
    missing runs on into the next, which is then looked at with it. A login notice may still
    end early at a number that reads as an age and is followed by a capital ("in the last 24h
    From Chrome"): when an unread heads-up item does not end its sentence, the one read item
    after it is looked at with it (fail closed). The phrase check reads the whole panel as
    any dialog, besides.
    """
    items = _panel_items(text)
    if isinstance(items, str):
        return items
    words = [item for item, _ in items]
    for n, item in enumerate(words):
        if not _UNREAD.search(item):
            continue
        found = PANEL_TROUBLE.search(item)
        if found is None and PANEL_HEADS_UP.search(item):
            found = next(filter(None, map(PANEL_TROUBLE.search, _run_on(words, n))), None)
        if found:
            return f"the notifications panel with an unread account notice ({found.group(0)!r})"
    return None


def panel_heads_up_items(text: str) -> tuple[HeadsUpItem, ...]:
    """The unread login and password notices (``PANEL_HEADS_UP``) of a notifications panel
    that is recognised (``panel_problem``), one per item, in panel order, for the owner: the
    words after each "Unread" up to the item's age (or an age that is no number, "Just now"),
    and that age. Items are split as ``panel_problem`` splits them; an item that runs on into
    the next still starts a notice at each "Unread", so a friend's item is not told with a
    login notice, and such an earlier notice shows no age of its own. Two alike items are two
    notices: Facebook's login notice is one generic text, and a second login is a second item
    of it."""
    items = _panel_items(text)
    if isinstance(items, str) or panel_problem(text) is not None:
        return ()
    found: list[HeadsUpItem] = []
    for words, age in items:
        pieces = _UNREAD.split(words)[1:]  # each "Unread" starts a notice of its own
        for n, piece in enumerate(pieces):
            # the item's age is its last notice's; an earlier one shows none
            notice = _notice(piece, age if n == len(pieces) - 1 else None)
            if notice is not None:
                found.append(notice)
    return tuple(found)


def panel_read_items(text: str) -> tuple[HeadsUpItem, ...]:
    """The login and password notices of a recognised notifications panel that the owner has
    read, as ``panel_heads_up_items`` gives the unread ones (a section title before one is
    dropped): the sign that a notice told while unread was read since, so that a later alike
    notice cannot be taken for it (``reader._panel_notices``)."""
    items = _panel_items(text)
    if isinstance(items, str) or panel_problem(text) is not None:
        return ()
    found = (_notice(_TITLES.sub("", w), age) for w, age in items if not _UNREAD.search(w))
    return tuple(notice for notice in found if notice is not None)


def _notice(piece: str, age: str | None) -> HeadsUpItem | None:
    """One item's words as a notice for the owner, up to an age that is no number, or
    ``None`` when they are no heads-up."""
    loose = _LOOSE_AGE.search(piece)
    if loose is not None:
        piece, age = piece[: loose.start()], _LOOSE_AGES[loose.group(0)]
    if not PANEL_HEADS_UP.search(piece) or PANEL_TROUBLE.search(piece):
        return None
    notice = _SPACE.sub(" ", piece).strip()
    if len(notice) > PANEL_NOTICE_MAX:
        notice = notice[: PANEL_NOTICE_MAX - 1] + "…"
    return HeadsUpItem(notice, age)


def _run_on(items: Sequence[str], n: int) -> Iterable[str]:
    """The read item after item ``n`` when item ``n`` does not end its sentence (cut early at
    a number that reads as an age); nothing else. Older read items are friends' and groups'
    notifications that stay in the list for weeks: looking at all of them made every look at
    a panel with an old unread login notice a stop."""
    cut = n + 1 < len(items) and not _SENTENCE_END.search(items[n])
    if cut and not _UNREAD.search(items[n + 1]):
        yield items[n + 1]


def _panel_items(text: str) -> list[tuple[str, str | None]] | str:
    """The items of a whole notifications panel, each with the age that ends it (``None`` for
    the last, the footer), or why ``text`` is not one. Facebook's typographic apostrophe is
    made plain."""
    head = _PANEL_HEAD.match(text)
    if head is None:
        return "not the notifications panel"
    if not _PANEL_FOOT.search(text):
        return "the notifications panel without its footer (cut short, or changed)"
    body = _normalise(text[head.end() :])
    items: list[tuple[str, str | None]] = []
    start = 0
    for age in _ITEM_AGE.finditer(body):
        if _starts_item(body[age.end() :]):
            items.append((body[start : age.start()], age.group(0)))
            start = age.end()
    items.append((body[start:], None))
    return items


def _starts_item(rest: str) -> bool:
    """Whether the text after an age starts a new item, so that the age ends one. It does
    unless it goes on with the notice: a word all in lowercase or a punctuation mark
    (``_GOES_ON``), as in "3 times in 24h. 5d" and "For the next 7d you can't post", which
    hold numbers of their own, not their age. "Unread", a section title, the footer and the
    end always start one. So does anything else: a capital or a digit, a name in a script
    without case (山田太郎), a page named in mixed case (eMAG), an emoji, an opening quote. A
    name all in lowercase ("ana") is taken for the notice going on."""
    if _ITEM_START.match(rest):
        return True
    word = rest.split(maxsplit=1)[0]
    return not (word[0] in _GOES_ON or word.islower())


def _known_panel(text: str) -> bool:
    return _PANEL_HEAD.match(text) is not None and panel_problem(text) is None


def _panel_trouble(text: str) -> bool:
    """A whole notifications panel that is unrecognised for an unread notice of trouble."""
    return not isinstance(_panel_items(text), str) and panel_problem(text) is not None


_EMPTY_MARKERS: Final = (re.compile(r"no posts yet", re.IGNORECASE),)

_USER_ID: Final = re.compile(rb'"USER_ID"\s*:\s*"([0-9]+)"')
_HTML_LANG: Final = re.compile(rb"<html\b[^>]*\blang\s*=\s*[\"']?([A-Za-z_-]+)", re.IGNORECASE)
_LOCALE: Final = re.compile(rb'"locale"\s*:\s*"([A-Za-z_]+)"')
_PASSWORD_INPUT: Final = re.compile(r"<input\b[^>]*\bname\s*=\s*[\"']?pass[\"'\s/>]", re.IGNORECASE)
_ERROR_CODE: Final = re.compile(rb'"(?:code|error)"\s*:\s*(1675004)\b')
_FEED_STYLE_RAW: Final = re.compile(rb'"feedStyle"\s*:\s*"([A-Z_]+)"')
_ORDERBY_RAW: Final = re.compile(rb'"orderby"\s*:\s*\[([^\]]*)\]')
_SORTING_RAW: Final = re.compile(rb'"sortingSetting"\s*:\s*"([A-Z_]+)"')
_NON_VISIBLE: Final = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG: Final = re.compile(r"<[^>]+>")
_SPACE: Final = re.compile(r"\s+")
_LONG_NUMBER: Final = re.compile(r"[0-9]{6,}")


class StepKind(enum.StrEnum):
    NAVIGATE = "navigate"
    SCROLL = "scroll"
    DWELL = "dwell"  # the last look just before park: late dialogs


@dataclass(frozen=True, slots=True)
class FeedPreloader:
    """What the Groups feed's SSR preloader says about the query it will run."""

    feed_style: str | None = None
    orderby: tuple[str, ...] = ()

    @property
    def chronological(self) -> bool:
        return self.feed_style == FEED_STYLE and FEED_ORDER in self.orderby


@dataclass(frozen=True, slots=True)
class PageFacts:
    """What the parser (or the read so far) knows about the page; every field is optional.

    Read-level facts carry over to scroll steps: the ``USER_ID`` and the preloaders come from
    the navigation's SSR, ``stories`` counts every story of the read so far.
    """

    user_id: str | None = None
    viewer_null: bool = False
    error_codes: frozenset[int] = frozenset()
    rate_limited: bool = False
    feed_preloader: FeedPreloader | None = None
    group: GroupMeta | None = None
    sorting_setting: str | None = None
    """The group preloader's ``sortingSetting`` for the requested group."""
    stories: int = 0
    known_empty: bool = False
    members_only: bool = False
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class DetectorInput:
    surface: Surface
    step: Step
    kind: StepKind = StepKind.NAVIGATE
    group_id: str | None = None
    group_slug: str | None = None
    listings: bool = False
    cookies: frozenset[str] = frozenset()
    """Names of the facebook.com cookies present (never their values)."""
    facts: PageFacts = field(default_factory=PageFacts)
    needs_member_proof: bool = False
    """The group is a member on the owner's word alone (``fbcollector.confirm``): its page
    must say ``viewer_join_state == MEMBER``, and saying nothing is ``GROUP_LOST`` too."""


@dataclass(frozen=True, slots=True)
class Signature:
    logged_in: bool
    surface: bool
    english: bool
    dialogs_clear: bool
    content: bool

    @property
    def holds(self) -> bool:
        return (
            self.logged_in and self.surface and self.english and self.dialogs_clear and self.content
        )

    def failing(self) -> tuple[str, ...]:
        names = ("logged_in", "surface", "english", "dialogs_clear", "content")
        return tuple(f"signature:{n}" for n in names if not getattr(self, n))


@dataclass(frozen=True, slots=True)
class Verdict:
    state: PageState
    reasons: tuple[str, ...] = ()
    """Machine-readable signal names (``url:checkpoint``, ``text:suspect_automated``, ...)."""
    signature: Signature | None = None
    advanced_protection: bool = False
    excerpt: str | None = None
    """Redacted: the signal names and the URL's path shape, never page or post text."""

    @property
    def ok(self) -> bool:
        return self.state is PageState.OK


@dataclass(frozen=True, slots=True)
class RawSignals:
    """What this module reads from the raw step itself, independently of the parser."""

    user_id: str | None = None
    html_lang: str | None = None
    locale: str | None = None
    error_codes: frozenset[int] = frozenset()
    rate_limit_text: bool = False
    viewer_null: bool = False
    graphql_statuses: frozenset[int] = frozenset()
    password_field: bool = False
    visible_text: str = ""
    feed_preloader: FeedPreloader | None = None
    sorting_setting: str | None = None


# -- URLs -------------------------------------------------------------------------------------


def decode_url(url: str, rounds: int = 4) -> str:
    """Percent-decode until stable (double-encoded ``next=`` values are common)."""
    current = url
    for _ in range(rounds):
        decoded = unquote(current.replace("+", " "))
        if decoded == current:
            break
        current = decoded
    return current


def url_targets(url: str) -> list[str]:
    """The URL's own path+query, plus every nested URL found in its query (``next=``, …).

    Each is returned decoded, so a checkpoint hidden in ``login.php?next=%2Fcheckpoint%2F…``
    is matched against its own path.
    """
    targets: list[str] = []
    pending = [url]
    while pending and len(targets) < 8:
        parts = urlsplit(decode_url(pending.pop()))
        targets.append(parts.path + ("?" + parts.query if parts.query else ""))
        for values in parse_qs(parts.query, keep_blank_values=False).values():
            pending.extend(v for v in values if "/" in v)
    return targets


def redact_url(url: str) -> str:
    """Host and path shape only: long numbers (ids) masked, query values dropped."""
    parts = urlsplit(decode_url(url))
    keys = sorted(parse_qs(parts.query, keep_blank_values=True))
    path = _LONG_NUMBER.sub("<id>", parts.path)
    query = "&".join(f"{k}=…" for k in keys)
    return f"{parts.netloc}{path}" + (f"?{query}" if query else "")


class SurfaceMatch(enum.Enum):
    YES = "yes"
    FEED_UNSORTED = "feed_unsorted"  # the feed, but sk=h_chr was lost
    NO = "no"


def match_surface(
    url: str, surface: Surface, group_id: str | None = None, group_slug: str | None = None
) -> SurfaceMatch:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "www.facebook.com":
        return SurfaceMatch.NO
    query = parse_qs(parts.query)
    path = parts.path.rstrip("/") or "/"
    if surface is Surface.FEED:
        if path not in {"/", "/home.php"} or query.get("filter") != ["groups"]:
            return SurfaceMatch.NO
        return SurfaceMatch.YES if query.get("sk") == ["h_chr"] else SurfaceMatch.FEED_UNSORTED
    if surface is Surface.JOINS:
        return SurfaceMatch.YES if path == "/groups/joins" else SurfaceMatch.NO
    segments = path.strip("/").split("/")
    if len(segments) != 2 or segments[0] != "groups":
        return SurfaceMatch.NO
    names = {n for n in (group_id, group_slug) if n}
    return SurfaceMatch.YES if segments[1] in names else SurfaceMatch.NO


# -- raw scan ---------------------------------------------------------------------------------


def visible_text(document: bytes | None) -> str:
    """The SSR document's text outside scripts, styles and templates, whitespace-collapsed."""
    if not document:
        return ""
    text = document.decode("utf-8", errors="replace")
    text = _TAG.sub(" ", _NON_VISIBLE.sub(" ", text))
    return _SPACE.sub(" ", html.unescape(text)).strip()


def scan_step(step: Step) -> RawSignals:
    doc = step.document or b""
    codes: set[int] = set()
    viewer_null = False
    rate_text = False
    statuses: set[int] = set()
    for body in step.graphql:
        if body.status is not None:
            statuses.add(body.status)
        codes.update(int(m.group(1)) for m in _ERROR_CODE.finditer(body.body))
        found_codes, found_null, found_text = _scan_frames(body.body)
        codes |= found_codes
        viewer_null = viewer_null or found_null
        rate_text = rate_text or found_text
    codes.update(int(m.group(1)) for m in _ERROR_CODE.finditer(doc))
    visible = visible_text(doc)
    return RawSignals(
        user_id=_first(_USER_ID, doc),
        html_lang=_first(_HTML_LANG, doc),
        locale=_first(_LOCALE, doc),
        error_codes=frozenset(codes),
        rate_limit_text=rate_text,
        viewer_null=viewer_null,
        graphql_statuses=frozenset(statuses),
        password_field=bool(_PASSWORD_INPUT.search(_NON_VISIBLE.sub(" ", _text(doc)))),
        visible_text=visible,
        feed_preloader=_feed_preloader(doc),
        sorting_setting=_first(_SORTING_RAW, doc),
    )


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _first(pattern: re.Pattern[bytes], raw: bytes) -> str | None:
    match = pattern.search(raw)
    return match.group(1).decode("ascii", errors="replace") if match else None


def _feed_preloader(doc: bytes) -> FeedPreloader | None:
    style = _first(_FEED_STYLE_RAW, doc)
    order_match = _ORDERBY_RAW.search(doc)
    if style is None and order_match is None:
        return None
    orderby: tuple[str, ...] = ()
    if order_match:
        orderby = tuple(
            part.strip().strip('"')
            for part in order_match.group(1).decode("ascii", "replace").split(",")
            if part.strip()
        )
    return FeedPreloader(feed_style=style, orderby=orderby)


def _scan_frames(body: bytes) -> tuple[set[int], bool, bool]:
    codes: set[int] = set()
    viewer_null = False
    rate_text = False
    for frame in iter_graphql_frames(body):
        if not isinstance(frame, Mapping):
            continue
        codes.update(_frame_codes(frame))
        rate_text = rate_text or _frame_rate_text(frame)
        data = frame.get("data")
        if isinstance(data, Mapping) and "viewer" in data and data["viewer"] is None:
            viewer_null = True
    return codes, viewer_null, rate_text


def _frame_codes(frame: Mapping[str, Any]) -> set[int]:
    codes: set[int] = set()
    error = frame.get("error")
    if isinstance(error, int) and not isinstance(error, bool):
        codes.add(error)
    errors = frame.get("errors")
    if isinstance(errors, list):
        for item in errors:
            code = item.get("code") if isinstance(item, Mapping) else None
            if isinstance(code, int) and not isinstance(code, bool):
                codes.add(code)
    return codes


def _frame_rate_text(frame: Mapping[str, Any]) -> bool:
    texts = [frame.get("errorSummary"), frame.get("errorDescription")]
    errors = frame.get("errors")
    if isinstance(errors, list):
        texts += [e.get("message") for e in errors if isinstance(e, Mapping)]
    return any(isinstance(t, str) and _RATE_LIMIT_TEXT.search(t) for t in texts)


# -- classification ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Merged:
    user_id: str | None
    viewer_null: bool
    error_codes: frozenset[int]
    feed_preloader: FeedPreloader | None
    group: GroupMeta | None
    sorting: str | None
    stories: int
    known_empty: bool
    members_only: bool
    english: bool


def _merge(inp: DetectorInput, raw: RawSignals) -> _Merged:
    facts = inp.facts
    lang = inp.step.dom.html_lang or raw.html_lang
    locale = raw.locale or facts.locale
    english = _is_english(lang) if lang else _is_english(locale)
    visible = raw.visible_text
    group = facts.group
    sorting = (
        facts.sorting_setting or (group.sorting_setting if group else None) or raw.sorting_setting
    )
    return _Merged(
        # The navigation's own document wins: a stale read-level id must not mask a logout.
        user_id=raw.user_id if raw.user_id is not None else facts.user_id,
        viewer_null=facts.viewer_null or raw.viewer_null,
        error_codes=facts.error_codes | raw.error_codes,
        feed_preloader=facts.feed_preloader or raw.feed_preloader,
        group=group,
        sorting=sorting,
        stories=facts.stories,
        known_empty=facts.known_empty or any(p.search(visible) for p in _EMPTY_MARKERS),
        members_only=facts.members_only
        or bool(_MEMBERS_ONLY.search(visible))
        # A notification may quote "only members can see" about another group.
        or any(
            _MEMBERS_ONLY.search(t) and not _PANEL_HEAD.match(t) for t in inp.step.dom.dialog_texts
        ),
        english=english,
    )


def _is_english(tag: str | None) -> bool:
    if not tag:
        return False
    low = tag.lower()
    return low == "en" or low.startswith(("en-", "en_"))


def _normalise(text: str) -> str:
    return text.replace("’", "'").replace(" ", " ")


def _phrase_hits(dialogs: Sequence[str], visible: str, *, include_weak_visible: bool) -> list[str]:
    hits: list[str] = []
    dialog_text = [_normalise(t) for t in dialogs]
    page_text = _normalise(visible)
    for name, pattern in _STRONG_PHRASES:
        if any(pattern.search(t) for t in dialog_text) or pattern.search(page_text):
            hits.append(f"text:{name}")
    for name, pattern in _WEAK_PHRASES:
        in_dialog = any(pattern.search(t) for t in dialog_text)
        if in_dialog or (include_weak_visible and pattern.search(page_text)):
            hits.append(f"text:{name}")
    return hits


def _block_signals(inp: DetectorInput, raw: RawSignals, merged: _Merged) -> list[str]:
    reasons: list[str] = []
    for target in url_targets(inp.step.final_url):
        for name, pattern in _BLOCK_URL:
            if pattern.search(target) and f"url:{name}" not in reasons:
                reasons.append(f"url:{name}")
    reasons += _phrase_hits(
        inp.step.dom.dialog_texts,
        raw.visible_text,
        include_weak_visible=merged.stories == 0,
    )
    if inp.step.dom.has_password_field or raw.password_field:
        reasons.append("dom:password_field")
    if RATE_LIMIT_CODE in merged.error_codes:
        reasons.append(f"graphql:{RATE_LIMIT_CODE}")
    if raw.rate_limit_text or inp.facts.rate_limited:
        reasons.append("graphql:rate_limit_text")
    if inp.step.status == 429 or 429 in raw.graphql_statuses:
        reasons.append("http:429")
    return reasons


def _logged_out(inp: DetectorInput, merged: _Merged) -> list[str]:
    login_redirect = any(
        _LOGIN_PATH.match(urlsplit(t).path) for t in url_targets(inp.step.final_url)[:1]
    )
    user_zero = merged.user_id == "0"
    if not (login_redirect or user_zero):
        return []
    no_cookie = "c_user" not in inp.cookies
    if not (no_cookie or merged.viewer_null):
        return []
    reasons = ["url:login"] if login_redirect else []
    reasons += ["user_id:0"] if user_zero else []
    reasons += ["cookie:no_c_user"] if no_cookie else []
    reasons += ["graphql:viewer_null"] if merged.viewer_null else []
    return reasons


def _group_lost_signals(inp: DetectorInput, merged: _Merged) -> list[str]:
    if inp.surface is not Surface.GROUP:
        return []
    reasons = []
    if inp.step.status in {403, 404}:
        reasons.append(f"http:{inp.step.status}")
    if merged.members_only:
        reasons.append("text:members_only")
    if merged.group is not None and merged.group.is_member is False:
        reasons.append(f"join_state:{merged.group.join_state}")
    elif inp.needs_member_proof and (merged.group is None or merged.group.is_member is None):
        reasons.append(JOIN_STATE_UNSTATED)
    return reasons


def classify(inp: DetectorInput, *, benign_dialogs: Iterable[re.Pattern[str]] = ()) -> Verdict:
    """The page state of one step (DESIGN table, first match wins)."""
    raw = scan_step(inp.step)
    merged = _merge(inp, raw)
    advanced = any(_ADVANCED_PROTECTION.search(t) for t in url_targets(inp.step.final_url))

    blocked = _block_signals(inp, raw, merged)
    if blocked or advanced:
        reasons = blocked or ["url:advanced_protection"]
        return _verdict(inp, PageState.BLOCKED, reasons, advanced=advanced)

    logged_out = _logged_out(inp, merged)
    if logged_out:
        return _verdict(inp, PageState.LOGGED_OUT, logged_out)

    lost = _group_lost_signals(inp, merged)
    match = match_surface(inp.step.final_url, inp.surface, inp.group_id, inp.group_slug)
    benign = tuple(benign_dialogs)
    signature = Signature(
        logged_in="c_user" in inp.cookies and merged.user_id not in {None, "0"},
        surface=match is not SurfaceMatch.NO,
        english=merged.english,
        # A page with more dialogs than the driver read texts of has one nobody looked at.
        dialogs_clear=(inp.step.dom.dialog_count or 0) <= len(inp.step.dom.dialog_texts)
        and all(_recognised(t, benign, inp.surface) for t in inp.step.dom.dialog_texts),
        # A lost group explains an empty page; FEED_UNSORTED is judged below, not here. Silence
        # about membership explains nothing: a page that shows nothing stays UNKNOWN (an empty
        # shell), whether or not the owner vouched for the group.
        content=merged.stories > 0
        or merged.known_empty
        or any(r != JOIN_STATE_UNSTATED for r in lost),
    )
    unknown = _unknown_reasons(inp, signature)
    if unknown:
        return _verdict(inp, PageState.UNKNOWN, unknown, signature=signature)

    later = _later_state(inp, merged, match, lost)
    if later is not None:
        state, reasons = later
        return _verdict(inp, state, reasons, signature=signature)
    return Verdict(state=PageState.OK, signature=signature)


def _recognised(text: str, benign: Sequence[re.Pattern[str]], surface: Surface) -> bool:
    """An empty layer, Facebook's notifications panel (``panel_problem``), a dialog the caller
    knows, or the members-only notice (GROUP_LOST)."""
    if not text.strip() or _known_panel(text) or any(p.search(text) for p in benign):
        return True
    return surface is Surface.GROUP and bool(_MEMBERS_ONLY.search(text))


def _unknown_reasons(inp: DetectorInput, signature: Signature) -> list[str]:
    reasons: list[str] = []
    status = inp.step.status
    if inp.kind is StepKind.NAVIGATE and status is None:
        reasons.append("http:none")
    if status is not None and status >= 500:
        reasons.append(f"http:{status}")
    if status in {403, 404} and inp.surface is not Surface.GROUP:
        reasons.append(f"http:{status}")
    if any(_CONSENT_URL.search(t) for t in url_targets(inp.step.final_url)):
        reasons.append("url:consent")
    reasons += signature.failing()
    if not signature.dialogs_clear and any(_panel_trouble(t) for t in inp.step.dom.dialog_texts):
        reasons.append(PANEL_NOTICE_REASON)  # the owner reads the block alert: say which dialog
    return reasons


def _later_state(
    inp: DetectorInput, merged: _Merged, match: SurfaceMatch, lost: list[str]
) -> tuple[PageState, list[str]] | None:
    if inp.surface is Surface.FEED:
        preloader = merged.feed_preloader
        if match is SurfaceMatch.FEED_UNSORTED:
            return PageState.FEED_NOT_CHRONO, ["url:sk_lost"]
        if preloader is None:
            return PageState.FEED_NOT_CHRONO, ["preloader:missing"]
        if not preloader.chronological:
            return PageState.FEED_NOT_CHRONO, [
                f"preloader:{preloader.feed_style}:{','.join(preloader.orderby)}"
            ]
        return None
    if inp.surface is not Surface.GROUP:
        return None
    if lost:
        return PageState.GROUP_LOST, lost
    group = merged.group
    if group is not None and group.is_followed is False:
        return PageState.NOT_FOLLOWED, [f"subscribe_status:{group.subscribe_status}"]
    expected = CHRONOLOGICAL_LISTINGS if inp.listings else CHRONOLOGICAL
    if merged.sorting is not None and merged.sorting != expected:
        return PageState.SORT_IGNORED, [f"sorting:{merged.sorting}"]
    return None


def _verdict(
    inp: DetectorInput,
    state: PageState,
    reasons: Sequence[str],
    *,
    signature: Signature | None = None,
    advanced: bool = False,
) -> Verdict:
    unique = tuple(dict.fromkeys(reasons))
    excerpt = f"{state.value} {' '.join(unique)} at {redact_url(inp.step.final_url)}"
    return Verdict(
        state=state,
        reasons=unique,
        signature=signature,
        advanced_protection=advanced,
        excerpt=excerpt[:EXCERPT_MAX],
    )
