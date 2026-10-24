from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fbcollector.detectors import (
    FEED_URL,
    JOINS_URL,
    PANEL_NOTICE_REASON,
    DetectorInput,
    FeedPreloader,
    HeadsUpItem,
    PageFacts,
    StepKind,
    SurfaceMatch,
    age_seconds,
    age_unit,
    classify,
    decode_url,
    group_url,
    match_surface,
    panel_heads_up_items,
    panel_problem,
    panel_read_items,
    redact_url,
    scan_step,
    surface_url,
    url_targets,
    visible_text,
)
from fbcollector.driver.cdp import html_text
from fbcollector.model import DomFacts, GraphqlBody, GroupMeta, PageState, Step, Surface

GID = "1234567890"
GROUP = group_url(GID)
COOKIES = frozenset({"c_user", "xs", "datr"})
PRELOADER = '"feedStyle":"MOST_RECENT_GROUPS_FEED","orderby":["MOST_RECENT"]'


def panel_notices(text: str) -> tuple[str, ...]:
    """The words of the unread heads-up items of a panel, in panel order."""
    return tuple(item.text for item in panel_heads_up_items(text))


def document(
    *,
    user_id: str = "100001",
    lang: str | None = "en",
    body: str = "",
    extra_json: str = PRELOADER,
    head: str = "",
) -> bytes:
    lang_attr = f' lang="{lang}"' if lang else ""
    data = f'{{"USER_ID":"{user_id}","locale":"en_US",{extra_json}}}'
    return (
        f'<html id="facebook"{lang_attr}><head>{head}</head><body>'
        f'<script type="application/json" data-sjs>{data}</script>'
        f"<script>require('CaptchaModule');window.checkpointHelper=1;</script>"
        f"{body}</body></html>"
    ).encode()


def step(
    url: str = FEED_URL,
    *,
    status: int | None = 200,
    doc: bytes | None = None,
    graphql: tuple[GraphqlBody, ...] = (),
    dialogs: tuple[str, ...] = (),
    lang: str | None = "en",
    password: bool = False,
) -> Step:
    return Step(
        final_url=url,
        status=status,
        document=document() if doc is None else doc,
        graphql=graphql,
        dom=DomFacts(html_lang=lang, dialog_texts=dialogs, has_password_field=password),
    )


def inp(
    s: Step,
    *,
    surface: Surface = Surface.FEED,
    cookies: frozenset[str] = COOKIES,
    kind: StepKind = StepKind.NAVIGATE,
    group_slug: str | None = None,
    listings: bool = False,
    **facts: Any,
) -> DetectorInput:
    facts.setdefault("stories", 3)
    return DetectorInput(
        surface=surface,
        step=s,
        kind=kind,
        group_id=GID if surface is Surface.GROUP else None,
        group_slug=group_slug,
        listings=listings,
        cookies=cookies,
        facts=PageFacts(**facts),
    )


def body(data: Any, *, status: int | None = 200, prefix: str = "for (;;);") -> GraphqlBody:
    return GraphqlBody(friendly_name="Q", body=(prefix + json.dumps(data)).encode(), status=status)


def member(**kw: Any) -> GroupMeta:
    base: dict[str, Any] = {
        "id": GID,
        "join_state": "MEMBER",
        "subscribe_status": "IS_SUBSCRIBED",
        "sorting_setting": "CHRONOLOGICAL",
    }
    base.update(kw)
    return GroupMeta(**base)


def state_of(detector_input: DetectorInput, **kw: Any) -> PageState:
    return classify(detector_input, **kw).state


class TestOk:
    def test_a_chronological_feed_with_stories(self) -> None:
        verdict = classify(inp(step()))
        assert verdict.ok
        assert verdict.signature is not None and verdict.signature.holds
        assert verdict.reasons == ()

    def test_a_group_page(self) -> None:
        assert classify(inp(step(GROUP), surface=Surface.GROUP, group=member())).ok

    def test_a_vanity_slug_counts_as_the_group(self) -> None:
        url = "https://www.facebook.com/groups/rentals.sofia/?sorting_setting=CHRONOLOGICAL"
        s = step(url)
        assert classify(inp(s, surface=Surface.GROUP, group_slug="rentals.sofia")).ok
        assert state_of(inp(s, surface=Surface.GROUP)) is PageState.UNKNOWN

    def test_the_joined_groups_page(self) -> None:
        assert classify(inp(step(JOINS_URL), surface=Surface.JOINS, stories=12)).ok

    def test_empty_but_known_empty(self) -> None:
        assert classify(inp(step(), stories=0, known_empty=True)).ok
        no_posts = step(doc=document(body="<div>No posts yet</div>"))
        assert classify(inp(no_posts, stories=0)).ok

    def test_a_scroll_step_needs_no_status_and_uses_read_facts(self) -> None:
        s = Step(final_url=FEED_URL, status=None, dom=DomFacts(html_lang="en"))
        verdict = classify(
            inp(
                s,
                kind=StepKind.SCROLL,
                user_id="100001",
                feed_preloader=FeedPreloader("MOST_RECENT_GROUPS_FEED", ("MOST_RECENT",)),
            )
        )
        assert verdict.ok

    def test_a_benign_dialog_can_be_recognised(self) -> None:
        s = step(dialogs=("Turn on notifications?",))
        assert state_of(inp(s)) is PageState.UNKNOWN
        benign = [re.compile(r"^Turn on notifications\?$")]
        assert state_of(inp(s), benign_dialogs=benign) is PageState.OK
        assert classify(inp(step(dialogs=("  ",)))).ok  # an empty layer is not a dialog

    # The panel as the live Groups feed held it on 2026-09-30 (names replaced).
    LIVE = (
        "Notifications All Unread New See all Unread You have a new friend suggestion: Иван"
        " Петров . 1h Earlier Unread You have a new friend suggestion: Ana Ivanova . 5d Unread"
        " Maria Georgieva commented on her post. 5d Unread We noticed a new login from a"
        " device or location you don't usually use. Please review. 5d Petar Popov made a post."
        " 5d See previous notifications"
    )
    # The panel that opened block #2 on 2026-10-01 (names replaced).
    LIVE_2 = (
        "Notifications All Unread New See all Unread Ana Petrova , Maria Ivanova and 11 other"
        " friends responded to events happening soon. 3m Unread You have a new friend"
        " suggestion: Иван Петров . 15h Earlier Unread You have a new friend suggestion: Ana"
        " Ivanova . 5d Unread Maria Georgieva commented on her post. 5d Unread We noticed a new"
        " login from a device or location you don't usually use. Please review. 6d See"
        " previous notifications"
    )
    LOGIN = "We noticed a new login from a device or location you don't usually use. Please review."
    # The same once the owner has read the login notice.
    PANEL = LIVE.replace("Unread We noticed", "We noticed")

    def test_facebooks_notifications_panel_is_known(self) -> None:
        assert classify(inp(step(dialogs=(self.PANEL,)))).ok
        assert classify(inp(step(dialogs=(self.PANEL, "")))).ok
        assert classify(
            inp(step(GROUP, dialogs=(self.PANEL,)), surface=Surface.GROUP)
        ).state is not (PageState.UNKNOWN)

    def test_the_panel_as_the_driver_reads_it(self) -> None:
        markup = (
            '<div role="dialog"><div><h1><span>Notifications</span></h1></div>'
            '<div role="tablist"><a role="tab">All</a><a role="tab">Unread</a></div>'
            "<div><span>New</span><a>See all</a></div>"
            '<div role="row"><span>Unread</span><a>Ana Ivanova commented on your post.'
            "</a><span>&nbsp;1h</span></div>"
            "<style>.x{color:red}</style>"
            '<div><a role="button">See previous notifications</a></div></div>'
        )
        text = html_text(markup)
        assert classify(inp(step(dialogs=(text,)))).ok, text

    @pytest.mark.parametrize(
        "text",
        [
            "Something new is here " + PANEL,  # the header must lead
            "notifications all unread" + PANEL.removeprefix("Notifications All Unread"),  # case
            PANEL.replace("All Unread", "All Unreadable"),  # the whole word
            "Notifications",
            PANEL.removesuffix(" See previous notifications"),  # its footer must end it
            PANEL[:200],  # cut short, as MAX_DIALOG_TEXT would
            PANEL + " Your account is at risk",  # nothing after the footer
            "\u200bNotifications All Unread New See all See previous notifications",
        ],
    )
    def test_only_the_whole_notifications_panel_is_known(self, text: str) -> None:
        verdict = classify(inp(step(dialogs=(text,))))
        assert verdict.state is PageState.UNKNOWN
        assert "signature:dialogs_clear" in verdict.reasons
        assert PANEL_NOTICE_REASON not in verdict.reasons  # no notice made it unrecognised

    @staticmethod
    def _unread(notice: str) -> str:
        return f"Notifications All Unread New See all Unread {notice} 1h See previous notifications"

    @pytest.mark.parametrize(
        "notice",
        [
            "We detected suspicious activity on your account.",
            "Your account is at risk. Secure your account.",
            "You're temporarily restricted from posting in groups.",
            "You can't post or comment for 3 days.",
            "Your account has restrictions. See Account Status.",
            "We limited how often you can post, comment or do other things.",
            "We removed your comment because it goes against our Community Standards.",
            "Please verify your email address.",
            "We noticed a suspicious login. Secure your account.",  # a login, and trouble
            "We noticed a suspicious login attempt.",
            "Someone tried to log in to your account. Confirm it's you.",
            "Your password was changed. If this wasn't you, secure your account.",
            "We noticed an unusual login from a new place.",
            "Review your account security.",  # "security" alone stays trouble
            "Your account was locked for your protection.",
            "Your account is suspended.",
            # A compromise or a forced change says the account is in trouble (review).
            "Your account was hacked. Reset your password now.",
            "Your account may be compromised. Change your password.",
            "Someone may have accessed your account. Change your password.",
            "Someone may have logged in to your account. Change your password to continue.",
            "You need to reset your password before you can log in to your account.",
            # Facebook's typographic apostrophe, beside a login word (review).
            "Someone tried to log into your account. Confirm it\u2019s you.",
            "We noticed a new login. You can\u2019t post until you review it.",
            "You're blocked from posting in groups.",
            # Facebook's forced password reset.
            "We've reset your password to keep your account safe.",
            "We reset your password to protect your account. Create a new one to log in.",
            "Your password was reset. Log in with your new password.",
            "Your password was reset.",
            "Your password has been reset by Facebook.",
            "Create a new password to get back into your account.",
            # Trouble that used to pass in silence.
            "Your account has been flagged for review.",
            "We've limited how often you can post, comment or do other things.",
            "We\u2019ve limited how often you can post.",  # Facebook's apostrophe
            "We have limited some features on your account.",
            "Complete account verification to keep using Facebook.",
            "Your identity verification is pending.",
            "Someone may have accessed your Facebook account.",
            "Someone else may have access to your account.",
            "Confirm this was you.",
            "Confirm that was you.",
            "Complete the checkpoint to continue.",
            "For the next 7d you can't post in groups.",  # an age-like number inside
            # Wordings that passed in silence (the review of the fix).
            "Your account has been limited.",
            "Your account is limited for 24 hours.",
            "Facebook has limited your account.",
            "Your post was removed.",
            "Confirm it was you.",
            "Your account is under review.",
            "Your account is pending review.",
            "We think your account may have been compromised.",
            "Someone hacked into your account.",
            "You have been blocked from sending messages.",
            "We noticed unusual activity from your account.",
        ],
    )
    def test_an_unread_trouble_notice_in_the_panel_stops_the_reading(self, notice: str) -> None:
        unread = self._unread(notice)
        verdict = classify(inp(step(dialogs=(unread,))))
        assert verdict.state in {PageState.UNKNOWN, PageState.BLOCKED}
        if verdict.state is PageState.UNKNOWN:
            assert verdict.reasons == ("signature:dialogs_clear", PANEL_NOTICE_REASON)
        assert panel_notices(unread) == ()  # trouble is not a heads-up: it stops
        read = unread.replace(f"Unread {notice}", notice)
        verdict = classify(inp(step(dialogs=(read,))))
        assert verdict.state is PageState.BLOCKED or verdict.ok  # a phrase still counts

    @pytest.mark.parametrize(
        "notice",
        [
            LOGIN,
            "Someone tried to log in to your account.",
            "Someone tried to log into Facebook from a new device.",
            "Did you just log in to your account from Sofia?",
            "Login alert: Chrome on Mac.",
            "Your password was changed.",
            "Two-factor authentication is now on.",
            "You turned on 2FA.",
            "Your login code was sent.",
            "We blocked a login attempt from Chrome on Windows.",  # not "locked" (review)
            "Someone tried to log in to your account 3 times in 24h.",  # "24h" is no age
            "You reset your password.",
        ],
    )
    def test_an_unread_login_notice_in_the_panel_is_for_the_owner(self, notice: str) -> None:
        unread = self._unread(notice)
        verdict = classify(inp(step(dialogs=(unread,))))
        assert verdict.ok, verdict.reasons
        assert panel_problem(unread) is None
        assert panel_heads_up_items(unread) == (HeadsUpItem(notice, "1h"),)
        read = unread.replace(f"Unread {notice}", notice)  # read: nothing to tell
        assert classify(inp(step(dialogs=(read,)))).ok
        assert panel_notices(read) == ()

    def test_the_live_panels_pass_and_tell_the_login_notice(self) -> None:
        for live in (self.LIVE, self.LIVE_2):
            assert panel_problem(live) is None
            assert classify(inp(step(dialogs=(live,)))).ok
            assert panel_notices(live) == (self.LOGIN,)
            age = "5d" if live is self.LIVE else "6d"
            assert panel_heads_up_items(live) == (HeadsUpItem(self.LOGIN, age),)
            group = inp(step(GROUP, dialogs=(live,)), surface=Surface.GROUP, group=member())
            assert classify(group).ok
        assert panel_notices(self.PANEL) == ()

    def test_panel_notices_in_order_without_unread_or_age(self) -> None:
        panel = (
            "Notifications All Unread New See all Unread Your   password was changed. 2h"
            " Earlier Unread Ana liked your post. 3d Unread Login alert: Chrome. 1w"
            " See previous notifications"
        )
        assert panel_notices(panel) == ("Your password was changed.", "Login alert: Chrome.")
        long = panel.replace("Chrome.", "Chrome " + "x" * 300)
        assert len(panel_notices(long)[1]) == 200
        assert panel_notices(long)[1].endswith("…")
        # An item without its age runs on into the next, as in panel_problem; each "Unread"
        # in it still starts its own notice, so the friend's words are not told (review).
        assert panel_notices(panel.replace("liked your post. 3d", "liked your post.")) == (
            "Your password was changed.",
            "Login alert: Chrome.",
        )
        # A panel that does not pass tells nothing (its own block alert speaks).
        assert panel_notices(panel.removesuffix(" See previous notifications")) == ()
        assert panel_notices("Your password was changed. 2h") == ()
        trouble = panel.replace("Ana liked your post.", "Your account is restricted.")
        assert panel_notices(trouble) == ()

    @pytest.mark.parametrize(
        "notice",
        [
            (
                "Someone tried to log in to your account 3 times in the last 24h. We temporarily"
                " locked your account."
            ),
            "We noticed a new login. For 7d you can't post.",
        ],
    )
    def test_trouble_after_an_age_in_a_login_notice_stops(self, notice: str) -> None:
        """A number in a login notice that reads as an age ("24h") splits the item: the
        trouble after it is the login notice's own (review)."""
        unread = self._unread(notice)
        assert panel_problem(unread) is not None
        verdict = classify(inp(step(dialogs=(unread,))))
        assert verdict.state in {PageState.UNKNOWN, PageState.BLOCKED}
        assert panel_notices(unread) == ()

    def test_a_cut_login_notice_runs_on_into_one_read_item(self) -> None:
        """A login notice cut at a number that reads as an age, before a capital, has the
        read item after it looked at with it; nothing further (the review of the fix)."""
        head, foot = "Notifications All Unread New See all", "See previous notifications"
        cut = (
            f"{head} Unread Someone logged in to your account in the last 24h From Chrome we"
            f" locked your account. 3w Ana liked your post. 4w {foot}"
        )
        assert panel_problem(cut) == (
            "the notifications panel with an unread account notice ('locked your')"
        )
        # Ended with its sentence: the read item after it is not its own.
        whole = cut.replace("in the last 24h From", "today. 24h From")
        assert panel_problem(whole) is None
        # Only the one item: the trouble in the read item after it is not looked at.
        further = cut.replace("we locked your account.", "on Mac.").replace(
            "Ana liked your post.", "Your account was restricted."
        )
        assert panel_problem(further) is None
        # An unread item after it is looked at as itself, not as the run-on.
        unread = cut.replace("From Chrome we locked", "Unread Ana liked")
        assert panel_problem(unread) is None

    def test_old_read_items_below_an_unread_login_notice_are_not_looked_at(self) -> None:
        """The owner's old login notice stays unread; the friends' and groups' items below it,
        read, quote posts ("security deposit", "a kitchen hack"): they do not stop the reading
        (the review of the fix)."""
        older = (
            " Petar Popov posted in Flats Sofia: 2-room flat, security deposit one month. 1w"
            " Ana Ivanova posted in Moms Sofia: a kitchen hack for you. 2w"
            " See previous notifications"
        )
        panel = self.LIVE_2.replace(" See previous notifications", older)
        assert panel_problem(panel) is None
        assert classify(inp(step(dialogs=(panel,)))).ok
        assert panel_notices(panel) == (self.LOGIN,)

    @pytest.mark.parametrize(
        "after",
        [
            '山田太郎 posted in Home Security Cameras: "which one"',  # no case
            "محمد posted in Home Security Cameras.",
            "eMAG posted: security cameras -30%.",  # a page named in mixed case
            "🔥 Hot deals: security cameras.",
            "\u201cFlats\u201d has a security notice for you.",
        ],
    )
    def test_an_item_that_starts_without_a_capital_starts_after_an_age(self, after: str) -> None:
        """A name in a script without case, a page in mixed case, an emoji or a quote after an
        age starts an item: it is not run into the unread item before it (the review of the
        fix)."""
        head, foot = "Notifications All Unread New See all", "See previous notifications"
        panel = f"{head} Unread Ana liked your post. 2h {after} 5d {foot}"
        assert panel_problem(panel) is None
        assert classify(inp(step(dialogs=(panel,)))).ok
        login = f"{head} Unread {self.LOGIN} 6d {after} 5d {foot}"
        assert panel_heads_up_items(login) == (HeadsUpItem(self.LOGIN, "6d"),)

    @pytest.mark.parametrize(
        "notice",
        [
            'Ana Ivanova posted in Life Hacks: "best kitchen hack ever"',
            "Ana Ivanova invited you to Hackathon Sofia 2026.",
            'Ana posted: "No compromise on quality."',
            'Ana posted in ML Models: "new checkpoint released"',
            "Ana posted in Travel BG: queue at the Kapitan Andreevo checkpoint.",
            "Ana flagged your post in Group X.",
            "Ana posted in Cars: this listing was flagged as a scam.",
            "Ana posted in Flats: an unusual loft for rent.",
            "Ana posted in Jobs: document verification clerk wanted.",
            "You can now use a passkey to access your account.",
            "Ana posted in Roads BG: the pass is blocked by snow.",
            "Ana: my account was hacked, don't accept requests.",
        ],
    )
    def test_trouble_words_in_a_friends_unread_item_pass(self, notice: str) -> None:
        """The trouble words added since block #2 are tied to the account: a friend's or a
        group's unread item quoting a post with them passes (the review of the fix)."""
        unread = self._unread(notice)
        assert panel_problem(unread) is None
        assert classify(inp(step(dialogs=(unread,)))).ok
        assert panel_heads_up_items(unread) == ()

    @pytest.mark.parametrize(
        "notice",
        [
            "The email on your account was changed to x@mail.ru.",
            "A new phone number was added to your account.",
            "Your mobile number was removed from your account.",
            "You've been logged out of Facebook on Chrome.",
        ],
    )
    def test_a_changed_email_or_phone_is_for_the_owner(self, notice: str) -> None:
        """A changed email or phone, a classic sign of a takeover, is told (the review of the
        fix); the owner may have made it, so it does not stop."""
        unread = self._unread(notice)
        assert classify(inp(step(dialogs=(unread,)))).ok
        assert panel_heads_up_items(unread) == (HeadsUpItem(notice, "1h"),)

    def test_read_login_notices_of_the_panel(self) -> None:
        """The read heads-up items, with their ages: the sign that a told notice was read."""
        assert panel_read_items(self.PANEL) == (HeadsUpItem(self.LOGIN, "5d"),)
        assert panel_read_items(self.LIVE) == ()  # unread there
        head, foot = "Notifications All Unread New See all", "See previous notifications"
        panel = (
            f"{head} {self.LOGIN} 2h Earlier Unread {self.LOGIN} 1w Your password was changed."
            f" 2w Ana liked it. Yesterday Login alert: Chrome. 3w {foot}"
        )
        assert panel_read_items(panel) == (
            HeadsUpItem(self.LOGIN, "2h"),
            HeadsUpItem("Your password was changed.", "2w"),
        )
        assert panel_heads_up_items(panel) == (HeadsUpItem(self.LOGIN, "1w"),)
        trouble = panel.replace(f"Unread {self.LOGIN}", "Unread Your account is restricted.")
        assert panel_read_items(trouble) == ()  # a panel that does not pass gives nothing

    def test_a_fresh_item_without_a_numeric_age_is_not_told_with_the_notice(self) -> None:
        """ "Just now" is no age to the split: the friend item runs into the login notice,
        and only the login notice is told, under its usual key (review)."""
        just_now = self.LIVE_2.replace(" 3m Unread", " Just now Unread")
        assert panel_problem(just_now) is None
        assert panel_notices(just_now) == (self.LOGIN,)
        own = self._unread(self.LOGIN).replace(" 1h ", " Just now Earlier Unread Ana liked it. 5d ")
        assert panel_notices(own) == (self.LOGIN,)
        assert panel_heads_up_items(own) == (HeadsUpItem(self.LOGIN, None),)
        # A read login item run into an unread friend's item is not told as the friend's.
        merged = self._unread("Ana liked it. Just now Earlier Login alert: Chrome.")
        assert panel_problem(merged) is None
        assert panel_notices(merged) == ()

    def test_alike_notices_are_items_of_their_own_with_their_ages(self) -> None:
        """Facebook's login notice is one generic text: a second login is a second item, and
        each item's age dates it."""
        panel = (
            f"Notifications All Unread New See all Unread {self.LOGIN} 2m Earlier Unread"
            f" Your password was changed. 1w Unread {self.LOGIN} 6d See previous notifications"
        )
        assert panel_heads_up_items(panel) == (
            HeadsUpItem(self.LOGIN, "2m"),
            HeadsUpItem("Your password was changed.", "1w"),
            HeadsUpItem(self.LOGIN, "6d"),
        )
        assert panel_heads_up_items(self.PANEL) == ()
        yesterday = panel.replace(" 2m Earlier", " Yesterday Earlier")
        assert panel_heads_up_items(yesterday)[0] == HeadsUpItem(self.LOGIN, "1d")
        # An unread notice whose age is missing runs on into the next item: it shows no age,
        # the next item's age is that item's.
        missing = self._unread(self.LOGIN).replace(" 1h ", " Unread Ana liked it. 5d ")
        assert panel_heads_up_items(missing) == (HeadsUpItem(self.LOGIN, None),)

    def test_an_age_ends_an_item_only_where_a_new_item_starts(self) -> None:
        """An age-like number followed by a lowercase word or a punctuation mark is the
        notice's own: it neither cuts the notice nor dates it."""
        login = "Someone tried to log in to your account 3 times in 24h."
        panel = self._unread(login).replace(" 1h ", " 5d ")
        assert panel_heads_up_items(panel) == (HeadsUpItem(login, "5d"),)
        # What follows an age that starts an item: a capital (a name), a digit, "Unread", a
        # section title, the footer.
        head, foot = "Notifications All Unread New See all", "See previous notifications"
        split = (
            f"{head} Unread Login alert: Chrome. 2h 3 friends liked your post. 1d Ana liked it."
            f" 3d Unread Your password was changed. 4d Earlier Unread Login alert: Edge. 1w {foot}"
        )
        assert panel_heads_up_items(split) == (
            HeadsUpItem("Login alert: Chrome.", "2h"),
            HeadsUpItem("Your password was changed.", "4d"),
            HeadsUpItem("Login alert: Edge.", "1w"),
        )
        # A lowercase word or a punctuation mark after it: the same notice runs on.
        for rest in ("from Chrome", ", then", "— twice", "(Chrome)"):
            merged = f"{head} Unread Login alert: 3 tries in 2h {rest}. 5d {foot}"
            assert panel_heads_up_items(merged) == (
                HeadsUpItem(f"Login alert: 3 tries in 2h {rest}.", "5d"),
            )
        # Finding 6: the trouble after "7d" is the unread item's own, not a read item's.
        stop = f"{head} Unread For the next 7d you can't post in groups. 1h {foot}"
        assert panel_problem(stop) == (
            'the notifications panel with an unread account notice ("you can\'t post")'
        )

    def test_age_seconds_and_units(self) -> None:
        assert [age_seconds(a) for a in ("2m", "15h", "6d", "1w", "1y", None)] == [
            120,
            15 * 3600,
            6 * 86400,
            7 * 86400,
            365 * 86400,
            0,
        ]
        assert [age_unit(a) for a in ("2m", "15h", "6d", "1w", "1y", None)] == [
            60,
            3600,
            86400,
            7 * 86400,
            365 * 86400,
            60,
        ]

    @pytest.mark.parametrize(
        "notice",
        [
            'Petar Popov posted in Flats Sofia: "2-room flat, security deposit one month".',
            "Ana posted in Roads BG: Hemus is temporarily closed.",
            "Ana posted in Cars: disabled parking spot.",
            "Ana posted in Rides: car got locked, keys inside.",
            "Ana posted in Rides: please verify the price.",
            "Ana posted: suspicious car near the school.",
            "Ana posted: new restrictions at the border.",
            "Ana posted in Jobs: security guard wanted.",
            "Ana posted in Rides: I can't post the ride yet.",
            "Ana started a fundraiser for disabled children.",
            "Ana posted in Flats: the flat is at risk of mould, act fast.",
        ],
    )
    def test_a_friends_or_groups_words_do_not_stop_the_reading(self, notice: str) -> None:
        """An unread item quoting a post: the trouble words are tied to the account, since
        each false stop costs 24 h, then 72 h (the final review of 2026-10-01)."""
        assert panel_problem(self._unread(notice)) is None
        assert classify(inp(step(dialogs=(self._unread(notice),)))).ok

    @pytest.mark.parametrize(
        "notice",
        [
            "Your account has been locked.",
            "We've temporarily locked your account.",
            "Your account has been disabled.",
            "We disabled your account.",
            "Your account has been suspended.",
            "We suspended your account.",
            "You're temporarily restricted from posting in groups.",
            "Restrictions on your account: you can't post for 3 days.",
            "We restricted your account.",
            "Your account may be at risk.",
            "Your account is at risk of being disabled.",
            "We detected suspicious activity on your account.",
            "For security reasons, please verify your identity.",
            "We noticed a security issue with your account.",
            "Review your account security.",
            "You're temporarily unable to post.",
            "Please verify it's you.",
        ],
    )
    def test_the_account_tied_trouble_words_still_stop(self, notice: str) -> None:
        verdict = classify(inp(step(dialogs=(self._unread(notice),))))
        assert verdict.state in {PageState.UNKNOWN, PageState.BLOCKED}, notice

    @pytest.mark.parametrize(
        "notice",
        [
            "Get Meta Verified: a verified badge and more protection for your account.",
            "Subscribe to Meta Verified today.",
            "Ana Ivanova is now Meta Verified.",
        ],
    )
    def test_a_meta_verified_promotion_passes(self, notice: str) -> None:
        """ "verif(y|ication)" is trouble, a paid badge's promotion is not."""
        unread = self._unread(notice)
        assert panel_problem(unread) is None
        assert classify(inp(step(dialogs=(unread,)))).ok
        assert panel_heads_up_items(unread) == ()

    def test_a_read_item_after_an_unread_one_is_not_looked_at(self) -> None:
        panel = (
            "Notifications All Unread New Unread Ana liked your comment. 2h"
            " Your account was restricted. 3w See previous notifications"
        )
        assert panel_problem(panel) is None
        without_age = panel.replace("comment. 2h", "comment.")  # the item runs on
        assert panel_problem(without_age) is not None

    def test_the_known_panel_does_not_excuse_another_dialog(self) -> None:
        verdict = classify(inp(step(dialogs=(self.PANEL, "Something new is here"))))
        assert verdict.state is PageState.UNKNOWN

    @pytest.mark.parametrize(
        "notice",
        [
            "We noticed unusual activity on your account.",
            "Your account has been restricted.",
            "Please confirm your identity.",
        ],
    )
    def test_a_warning_listed_in_the_panel_still_blocks(self, notice: str) -> None:
        panel = f"Notifications All Unread New {notice} 1h See previous notifications"
        assert state_of(inp(step(dialogs=(panel,)))) is PageState.BLOCKED

    def test_a_dialog_the_driver_did_not_read_is_not_clear(self) -> None:
        s = step(dialogs=("", "", "", "", ""))
        assert classify(inp(s)).ok
        more = replace(s, dom=replace(s.dom, dialog_count=6))
        verdict = classify(inp(more))
        assert "signature:dialogs_clear" in verdict.reasons
        assert classify(inp(replace(s, dom=replace(s.dom, dialog_count=5)))).ok

    def test_members_only_quoted_in_the_panel_is_not_a_lost_group(self) -> None:
        panel = (
            "Notifications All Unread New Ana posted in Flats: only members can see who's in"
            " it. 1h See previous notifications"
        )
        verdict = classify(inp(step(GROUP, dialogs=(panel,)), surface=Surface.GROUP))
        assert verdict.state is not PageState.GROUP_LOST


class TestBlocked:
    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("https://www.facebook.com/checkpoint/1501092823525282/", "url:checkpoint"),
            ("https://www.facebook.com/checkpoint/?next", "url:checkpoint"),
            (
                (
                    "https://www.facebook.com/login.php?next=https%3A%2F%2Fwww.facebook.com"
                    "%2Fcheckpoint%2F828281030927956%2F"
                ),
                "url:checkpoint",
            ),
            (
                (
                    "https://www.facebook.com/login.php?next=https%253A%252F%252Fwww.facebook.com"
                    "%252Fcheckpoint%252F601051028565049%252F"
                ),
                "url:checkpoint",
            ),
            (
                "https://www.facebook.com/two_step_verification/authentication/",
                "url:two_step_verification",
            ),
            ("https://www.facebook.com/auth_platform/afad/?apc=1", "url:auth_platform"),
            ("https://www.facebook.com/recover/initiate/", "url:recover"),
            (
                "https://www.facebook.com/login/device-based/regular/login/",
                "url:login_device_based",
            ),
            ("https://www.facebook.com/zero/unsupported_browser/", "url:unsupported_browser"),
            ("https://www.facebook.com/captcha/tfbimage/", "url:captcha"),
        ],
    )
    def test_urls(self, url: str, reason: str) -> None:
        verdict = classify(inp(step(url)))
        assert verdict.state is PageState.BLOCKED
        assert reason in verdict.reasons

    def test_advanced_protection_is_flagged(self) -> None:
        verdict = classify(inp(step("https://www.facebook.com/checkpoint/advanced_protection/")))
        assert verdict.state is PageState.BLOCKED
        assert verdict.advanced_protection

    @pytest.mark.parametrize(
        "text",
        [
            "We suspect automated behavior on your account",
            "We suspect automated behaviour on your account",
            "You're Temporarily Blocked",
            "It looks like you were misusing this feature by going too fast.",
            "Please confirm your identity",
            "You can’t use this feature right now",
            "We suspended your account",
            "Your account has been disabled",
            "Your account has been restricted",
            "We noticed unusual activity",
            "ensure that no other users or tools have access to your account",
            "Security check",
        ],
    )
    def test_phrases_in_a_dialog(self, text: str) -> None:
        assert state_of(inp(step(dialogs=(text,)))) is PageState.BLOCKED

    def test_a_strong_phrase_in_the_page_text(self) -> None:
        s = step(doc=document(body="<h2>We suspect automated behavior on your account</h2>"))
        assert state_of(inp(s)) is PageState.BLOCKED

    def test_a_group_named_like_a_weak_phrase_is_not_a_wall(self) -> None:
        s = step(GROUP, doc=document(body="<h1>Security check volunteers</h1>"))
        assert state_of(inp(s, surface=Surface.GROUP, group=member())) is PageState.OK
        empty = inp(s, surface=Surface.GROUP, group=member(), stories=0, known_empty=True)
        assert state_of(empty) is PageState.BLOCKED

    def test_script_words_are_not_signals(self) -> None:
        # every real page names "Captcha…" and "checkpoint…" modules in its scripts
        assert classify(inp(step())).ok

    def test_password_fields(self) -> None:
        assert state_of(inp(step(password=True))) is PageState.BLOCKED
        form = step(doc=document(body='<form><input type="password" name="pass"></form>'))
        assert state_of(inp(form)) is PageState.BLOCKED
        scripted = step(doc=document(head="<script>var x = '<input name=\"pass\">';</script>"))
        assert classify(inp(scripted)).ok

    @pytest.mark.parametrize(
        "payload",
        [
            {"errors": [{"code": 1675004, "severity": "CRITICAL", "message": "x"}]},
            {"error": 1675004, "errorSummary": "Rate limit"},
            {"errors": [{"message": "Rate limit exceeded", "code": 1}]},
            {"error": 1, "errorSummary": "Rate limit exceeded"},
        ],
    )
    def test_graphql_throttle(self, payload: dict[str, Any]) -> None:
        verdict = classify(inp(step(graphql=(body(payload),))))
        assert verdict.state is PageState.BLOCKED
        assert any(r.startswith("graphql:") for r in verdict.reasons)

    def test_a_throttle_code_in_an_undecodable_body(self) -> None:
        raw = GraphqlBody(friendly_name=None, body=b'for (;;);{"error":1675004,"trunc')
        assert state_of(inp(step(graphql=(raw,)))) is PageState.BLOCKED

    def test_parser_facts_can_report_the_throttle(self) -> None:
        assert state_of(inp(step(), rate_limited=True)) is PageState.BLOCKED
        assert state_of(inp(step(), error_codes=frozenset({1675004}))) is PageState.BLOCKED

    def test_http_429(self) -> None:
        assert state_of(inp(step(status=429))) is PageState.BLOCKED
        throttled = GraphqlBody(friendly_name="Q", body=b"{}", status=429)
        assert state_of(inp(step(graphql=(throttled,)))) is PageState.BLOCKED

    def test_blocked_wins_over_everything(self) -> None:
        s = step(
            "https://www.facebook.com/checkpoint/1/",
            doc=document(user_id="0", lang="bg"),
            lang="bg",
        )
        assert state_of(inp(s, cookies=frozenset(), stories=0)) is PageState.BLOCKED


class TestLoggedOut:
    def test_login_redirect_without_the_cookie(self) -> None:
        s = step("https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2F")
        verdict = classify(inp(s, cookies=frozenset({"datr"})))
        assert verdict.state is PageState.LOGGED_OUT
        assert {"url:login", "cookie:no_c_user"} <= set(verdict.reasons)

    def test_user_id_zero_with_viewer_null(self) -> None:
        s = step(doc=document(user_id="0"), graphql=(body({"data": {"viewer": None}}),))
        verdict = classify(inp(s))
        assert verdict.state is PageState.LOGGED_OUT
        assert "graphql:viewer_null" in verdict.reasons

    def test_one_signal_alone_is_not_a_logout_but_not_ok_either(self) -> None:
        assert state_of(inp(step(doc=document(user_id="0")))) is PageState.UNKNOWN
        login = step("https://www.facebook.com/login.php")
        assert state_of(inp(login)) is PageState.UNKNOWN

    def test_the_documents_own_id_beats_a_stale_read_level_one(self) -> None:
        s = step(doc=document(user_id="0"))
        assert state_of(inp(s, user_id="100001", cookies=frozenset())) is PageState.LOGGED_OUT


class TestUnknown:
    @pytest.mark.parametrize(
        ("s", "reason"),
        [
            (step(status=502), "http:502"),
            (step(status=None), "http:none"),
            (step(status=404), "http:404"),
            (step("https://www.facebook.com/privacy/consent/pay_or_consent/"), "url:consent"),
            (step(dialogs=("Something new is here",)), "signature:dialogs_clear"),
            (step(lang="bg", doc=document(lang="bg")), "signature:english"),
            (step("https://www.facebook.com/marketplace/"), "signature:surface"),
            (step("http://www.facebook.com/?filter=groups&sk=h_chr"), "signature:surface"),
            (step("https://m.facebook.com/?filter=groups&sk=h_chr"), "signature:surface"),
        ],
    )
    def test_the_signature_fails(self, s: Step, reason: str) -> None:
        verdict = classify(inp(s))
        assert verdict.state is PageState.UNKNOWN
        assert reason in verdict.reasons

    def test_an_empty_shell(self) -> None:
        verdict = classify(inp(step(), stories=0))
        assert verdict.state is PageState.UNKNOWN
        assert "signature:content" in verdict.reasons

    def test_no_cookie_or_no_user_id(self) -> None:
        assert state_of(inp(step(), cookies=frozenset({"xs"}))) is PageState.UNKNOWN
        bare = step(doc=b'<html lang="en"><body></body></html>')
        assert state_of(inp(bare)) is PageState.UNKNOWN

    def test_language_falls_back_to_the_locale(self) -> None:
        s = step(lang=None, doc=document(lang=None))
        assert classify(inp(s)).ok
        assert state_of(inp(s, locale="bg_BG")) is PageState.OK  # the page's own locale wins
        no_lang = Step(final_url=FEED_URL, status=200, document=b"<html></html>")
        assert state_of(inp(no_lang, user_id="1")) is PageState.UNKNOWN


class TestFeedNotChrono:
    def test_the_sort_parameter_was_lost(self) -> None:
        verdict = classify(inp(step("https://www.facebook.com/?filter=groups")))
        assert verdict.state is PageState.FEED_NOT_CHRONO
        assert verdict.reasons == ("url:sk_lost",)

    def test_the_preloader_is_missing_or_ranked(self) -> None:
        missing = step(doc=document(extra_json='"x":1'))
        assert classify(inp(missing)).reasons == ("preloader:missing",)
        ranked = step(doc=document(extra_json='"feedStyle":"DEFAULT","orderby":["TOP_STORIES"]'))
        assert classify(inp(ranked)).state is PageState.FEED_NOT_CHRONO

    def test_the_parser_can_supply_the_preloader(self) -> None:
        missing = step(doc=document(extra_json='"x":1'))
        chrono = FeedPreloader("MOST_RECENT_GROUPS_FEED", ("MOST_RECENT",))
        assert classify(inp(missing, feed_preloader=chrono)).ok
        assert chrono.chronological
        assert not FeedPreloader("MOST_RECENT_GROUPS_FEED", ()).chronological


class TestGroupStates:
    def g(self, s: Step | None = None, **facts: Any) -> DetectorInput:
        return inp(s or step(GROUP), surface=Surface.GROUP, **facts)

    def test_members_only(self) -> None:
        text = "Only members can see who's in the group and what they post."
        visible = step(GROUP, doc=document(body=f"<span>{text}</span>"))
        assert state_of(self.g(visible, stories=0)) is PageState.GROUP_LOST
        dialog = step(GROUP, dialogs=(text,))
        assert state_of(self.g(dialog, stories=0)) is PageState.GROUP_LOST
        assert state_of(self.g(members_only=True, stories=0)) is PageState.GROUP_LOST

    def test_not_a_member_or_gone(self) -> None:
        verdict = classify(self.g(group=member(join_state="CAN_JOIN")))
        assert verdict.state is PageState.GROUP_LOST
        assert "join_state:CAN_JOIN" in verdict.reasons
        assert state_of(self.g(step(GROUP, status=404), stories=0)) is PageState.GROUP_LOST
        assert state_of(self.g(step(GROUP, status=403), stories=0)) is PageState.GROUP_LOST

    def test_not_followed(self) -> None:
        verdict = classify(self.g(group=member(subscribe_status="UNSUBSCRIBED")))
        assert verdict.state is PageState.NOT_FOLLOWED

    def test_sort_ignored(self) -> None:
        assert state_of(self.g(sorting_setting="RECENT_ACTIVITY")) is PageState.SORT_IGNORED
        assert state_of(self.g(group=member(sorting_setting="TOP_POSTS"))) is PageState.SORT_IGNORED
        raw = step(GROUP, doc=document(extra_json='"sortingSetting":"RECENT_ACTIVITY"'))
        assert state_of(self.g(raw)) is PageState.SORT_IGNORED

    def test_listings_expect_their_own_sort(self) -> None:
        listing = step(group_url(GID, listings=True))
        facts: dict[str, Any] = {"sorting_setting": "CHRONOLOGICAL_LISTINGS"}
        assert state_of(self.g(listing, listings=True, **facts)) is PageState.OK
        assert state_of(self.g(listing, **facts)) is PageState.SORT_IGNORED

    def test_unknown_group_meta_is_not_guessed(self) -> None:
        assert classify(self.g()).ok


class TestRedaction:
    def test_the_excerpt_names_signals_and_masks_ids(self) -> None:
        url = "https://www.facebook.com/checkpoint/1501092823525282/?next=secret-value&x=1"
        verdict = classify(inp(step(url, dialogs=("We suspect automated behavior; post text",))))
        assert verdict.excerpt is not None
        assert "1501092823525282" not in verdict.excerpt
        assert "secret-value" not in verdict.excerpt
        assert "post text" not in verdict.excerpt
        assert "<id>" in verdict.excerpt
        assert "text:suspect_automated" in verdict.excerpt

    def test_redact_url(self) -> None:
        assert redact_url("https://www.facebook.com/groups/123456789/?a=1&b=") == (
            "www.facebook.com/groups/<id>/?a=…&b=…"
        )


class TestUrls:
    def test_surface_urls(self) -> None:
        assert surface_url(Surface.FEED) == "https://www.facebook.com/?filter=groups&sk=h_chr"
        assert surface_url(Surface.JOINS) == "https://www.facebook.com/groups/joins/"
        assert surface_url(Surface.GROUP, GID) == (
            f"https://www.facebook.com/groups/{GID}/?sorting_setting=CHRONOLOGICAL"
        )
        assert surface_url(Surface.GROUP, GID, listings=True).endswith("CHRONOLOGICAL_LISTINGS")
        with pytest.raises(ValueError, match="group id"):
            surface_url(Surface.GROUP)
        with pytest.raises(ValueError, match="numeric"):
            group_url("rentals.sofia")
        with pytest.raises(ValueError, match="numeric"):
            group_url("0123")

    def test_decoding_and_nested_targets(self) -> None:
        assert decode_url("a%252Fb") == "a/b"
        targets = url_targets(
            "https://www.facebook.com/login.php?next=https%3A%2F%2Fwww.facebook.com%2Fcheckpoint%2F1%2F"
        )
        assert targets[0].startswith("/login.php?next=")
        assert "/checkpoint/1/" in targets

    def test_match_surface(self) -> None:
        home = "https://www.facebook.com/home.php?filter=groups&sk=h_chr"
        assert match_surface(home, Surface.FEED) is SurfaceMatch.YES
        assert match_surface(JOINS_URL, Surface.JOINS) is SurfaceMatch.YES
        assert match_surface(FEED_URL, Surface.JOINS) is SurfaceMatch.NO
        assert match_surface(GROUP, Surface.GROUP, "999") is SurfaceMatch.NO
        deep = f"https://www.facebook.com/groups/{GID}/posts/1/"
        assert match_surface(deep, Surface.GROUP, GID) is SurfaceMatch.NO


class TestRawScan:
    def test_scan_reads_the_safety_signals_itself(self) -> None:
        s = step(
            graphql=(
                body({"data": {"viewer": None}}),
                body({"errors": [{"code": 1675004}]}, prefix=""),
                GraphqlBody(friendly_name=None, body=b"[1,2]"),
            )
        )
        raw = scan_step(s)
        assert raw.user_id == "100001"
        assert raw.html_lang == "en"
        assert raw.locale == "en_US"
        assert raw.viewer_null
        assert 1675004 in raw.error_codes
        assert raw.feed_preloader == FeedPreloader("MOST_RECENT_GROUPS_FEED", ("MOST_RECENT",))

    def test_visible_text_drops_scripts(self) -> None:
        assert visible_text(None) == ""
        html_doc = b"<p>a &amp; b</p><script>captcha</script><style>x</style><p>c</p>"
        assert visible_text(html_doc) == "a & b c"


_SAFE_TEXT = st.text(alphabet=st.characters(categories=("L", "N", "Zs", "P")), min_size=1).filter(
    lambda t: t.strip() != ""
)


@settings(max_examples=200, deadline=None)
@given(dialog=_SAFE_TEXT)
def test_any_unrecognised_dialog_is_never_ok(dialog: str) -> None:
    assert not classify(inp(step(dialogs=(dialog,)))).ok


@settings(max_examples=200, deadline=None)
@given(
    path=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_/-.%", max_size=40),
    query=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_=&%", max_size=30),
)
def test_any_other_url_is_never_ok(path: str, query: str) -> None:
    url = f"https://www.facebook.com/{path.lstrip('/')}" + (f"?{query}" if query else "")
    verdict = classify(inp(step(url)))
    if verdict.ok:
        assert match_surface(url, Surface.FEED) is SurfaceMatch.YES
