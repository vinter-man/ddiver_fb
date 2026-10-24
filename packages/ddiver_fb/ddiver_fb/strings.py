"""The ``fb`` overlay: ddiver's bot, speaking of Facebook groups, posts and authors.

ddiver-core's catalogs say "chat", "message" and "sender", and a few of their strings are about
Telegram itself (an export "contains no Telegram session"). This overlay replaces exactly those,
in all three languages, and adds the brain's own keys (``fb.*``): collector states, /status,
owner alerts and the owner's commands. Everything it does not name falls through to the core.

Each language must carry the same keys with the same placeholders; ``tests/brain/
test_strings.py`` keeps them in sync, and :func:`ddiver_core.i18n.register_overlay` refuses a
replacement that uses a placeholder the core's call site never passes.
"""

from __future__ import annotations

from typing import Final

from ddiver_core import i18n
from ddiver_core.i18n import en as core_en
from ddiver_core.i18n import ru as core_ru
from ddiver_core.i18n import uk as core_uk


def _help(base: str, intro: str) -> str:
    """The core's help with its first paragraph — the one about chats — replaced.

    The rule syntax that follows is the core's and stays the core's, except for the ``from:``
    paragraph (:func:`_from`): copying it here would let the two drift the first time the syntax
    gains a feature.
    """
    _, _, rest = base.partition("\n\n")
    return f"{intro}\n\n{rest}"


FROM_PARAGRAPH: Final = "<code>from:"


def _from(base: str, paragraph: str) -> str:
    """The core's text with its ``from:`` paragraph swapped for the Facebook one.

    The core's example watches ``@vasya``, and Facebook shows no username. Here a ``from:``
    rule names the author's id (the number every notification shows next to the name) or part
    of the name, and the paragraph says that ``from:@…`` finds nothing. Exactly one paragraph
    of the core's text must start with ``<code>from:``: if a new core moves it, this fails at
    import, before anybody is shown a Telegram example.
    """
    paragraphs = base.split("\n\n")
    found = [i for i, text in enumerate(paragraphs) if text.startswith(FROM_PARAGRAPH)]
    if len(found) != 1:
        msg = f"expected one from: paragraph in the core's text, found {len(found)}"
        raise ValueError(msg)
    paragraphs[found[0]] = paragraph
    return "\n\n".join(paragraphs)


EN: Final[dict[str, str]] = {
    # -- the core's wording, for groups and posts --------------------------------------------
    "notif.chat": "👥 Group",
    "notif.sender": "👤 Author",
    "notif.link": "🔗 Post",
    "notif.rule_scope.chat": "group rule",
    "notif.rule_scope.global": "rule for all groups",
    "notif.sender.unknown": "Unknown author",
    "notif.sender.by_id": "Author {id}",
    "notif.sender.anonymous": "Anonymous participant",
    "notif.btn.ignore": "🚫 Ignore author",
    "notif.forwarded": "↪️ shared post",
    "notif.private": "🔒 Private group — the link opens for members only",
    "notif.late": "⏳ Posted {ago}, appeared in the group only now",
    "digest.more_chats": "… and {n} more groups ({m} matches)",
    "bot.gate.card": "👤 User {user_id} wants access to ddiver-fb.",
    "bot.btn.all_chats": "🌐 All groups",
    "bot.btn.refresh_chats": "🔄 Refresh groups",
    "bot.cmd.export": "Save your groups and rules to a file",
    "bot.cmd.import": "Load groups and rules from a file",
    "bot.export.caption": (
        "📦 Your configuration: {chats} groups, {rules} rules, {ignored} ignored.\n"
        "Keep the file somewhere else — /import restores it.\n"
        "It contains no Facebook login, and never will."
    ),
    "bot.import.prompt": (
        "<b>Load a configuration</b>\n\n"
        "Send the file /export gave you. A small one can be pasted as text.\n\n"
        "Importing <b>adds</b> rather than replaces: nothing you already have is removed, "
        "and duplicates are skipped.\n\n"
        "Groups are matched by their Facebook id, so a group comes back only where the "
        "collector's account is a member of it."
    ),
    "bot.import.over_limit": (
        "⚠️ {n} rules did not fit. The caps are {chat} per group and {total} in total."
    ),
    "bot.import.chats_over_limit": "⚠️ Only the first {n} groups in the file were read.",
    "bot.import.ignored_over_limit": "⚠️ {n} ignored authors did not fit. The cap is {limit}.",
    "bot.import.missing_chats": "⚠️ These groups were not found: {chats}",
    "bot.menu.chats": "👥 Groups",
    "bot.menu.shared": "🗂 Shared groups",
    "bot.chats.title": "Groups you are monitoring ({n}). Tapping toggles: ✅ on, ☐ off.",
    "bot.chats.empty": (
        "No groups yet: the collector has not reported the account's groups. Tap 🔄 Refresh groups."
    ),
    "bot.chats.none_shared": "The owner has not shared any groups with you yet.",
    "bot.chats.search_prompt": "Send part of the group's name.",
    "bot.chats.lost_access": (
        "⚠️ The Facebook account can no longer read {chat}; monitoring is paused for it."
    ),
    "bot.chats.regained_access": "✅ The Facebook account can read {chat} again.",
    "bot.rules.pick_chat": "Which group are these rules for?",
    "bot.rules.title_global": "Rules for all groups",
    "bot.rules.limit_chat": "You already have {limit} rules for this group.",
    "bot.ignored.title": "Ignored authors",
    "bot.ignored.added_global": "🚫 Ignoring {sender} in every group.",
    "bot.ignored.removed": "Author is no longer ignored.",
    "bot.ignored.all_chats_btn": "🚫 In all groups too",
    "bot.notif.drop_scope_chat": "this group only: {chat}",
    "bot.test.prompt": (
        "<b>Rule check</b>\n\n"
        "Send any text and I will run it through your rules exactly as if it were a real "
        "post, then show the verdict for every group you watch.\n\n"
        "This checks a text, it does not search history: nothing is looked up and no time "
        "window applies. The author and your ignore list are not considered either — text "
        "only.\n\n"
        "You can do it in one go:\n"
        "<code>/test flat for rent in Varna, 400 EUR</code>\n\n"
        "What comes back, per group:\n"
        "  ✅ matched — which rule, and the fragment it hit\n"
        "  🚫 dropped by one of your exclusions\n"
        "  — no match"
    ),
    "bot.test.no_chats": (
        "⚠️ You are not watching any group yet, so no rule can notify you. Below is "
        "what your rules for all groups would have done."
    ),
    "bot.test.global_scope": "rules for all groups",
    "bot.users.title": (
        "👥 Users\n\n"
        "Anyone who sends /start to the bot lands here as “waiting” and gets nothing "
        "until you decide. An allowed user picks groups only from the ones you shared, "
        "and keeps their own rules, ignore list, language and timezone.\n"
    ),
    "bot.shared.hint": (
        "🗂 Shared groups\n\n"
        "Only the collector's Facebook account reads the groups, so other users do not see "
        "all of them — only the ones you open here.\n\n"
        "🗂 — open: a user can add it to their own list\n"
        "🔒 — yours only\n\n"
        "Tapping toggles it. Closing a group removes it from everyone else's "
        "subscriptions; yours are untouched."
    ),
    "bot.shared.unshared_notice": (
        "ℹ️ The owner stopped sharing {chat}; it was removed from your groups."
    ),
    "bot.help": _help(
        _from(
            core_en.STRINGS["bot.help"],
            "<code>from:100012345678901</code> — watch the author\n"
            "  ✅ anything they post; by the id next to their name in a notification, or by part of the name\n"
            "  ❌ a mention of them in somebody else's post",
        ),
        "I watch the Facebook groups you pick and message you when a new post matches your "
        "rules. A post reaches you a few minutes after it appears; /status shows the current "
        "delay.",
    ),
    # -- from: rules watch a post's author: an id or part of a name, never a username ---------
    "bot.rules.prompt": _from(
        core_en.STRINGS["bot.rules.prompt"],
        "<code>from:100012345678901</code> — watch the author, not the text\n"
        "  ✅ anything they post, even a photo with no caption\n"
        "  ❌ somebody else's post that mentions them\n"
        "  The number is the author's Facebook id, shown next to their name in every notification; it stays when they change their name.\n"
        "  Also <code>from:Olena Kovalenko</code> (part of the name, case-insensitive).\n"
        "  Facebook shows no @usernames, so <code>from:@…</code> finds nothing here.",
    ),
    "rule.err.no_phone": (
        "Facebook does not show an author's phone number, so a phone rule would match nothing. "
        "Use the author's id (the number next to their name in a notification) or part of their name."
    ),
    "rule.err.bad_username": (
        "Facebook shows no @usernames, so a from:@… rule would match nothing. "
        "Use the author's id (the number next to their name in a notification) or part of their name."
    ),
    "rule.err.bad_sender_id": "That number is too large to be a Facebook id.",
    "rule.err.name_too_short": (
        "A name is matched as a fragment, so it needs at least {limit} characters — otherwise it fires on half the group."
    ),
    "bot.test.sender_rules": "(plus {n} from: rules — those match on who wrote a post, so a text test cannot answer for them)",
    # -- collector states and pacing ---------------------------------------------------------
    "fb.state.ok": "✅ reading",
    "fb.state.warmup": "🌱 warming up after a login — not reading yet",
    "fb.state.quiet": "🌙 quiet hours",
    "fb.state.break": "☕ on a break",
    "fb.state.not_home": "🏠 not at home — not reading",
    "fb.state.blocked": "⛔ stopped by a Facebook warning",
    "fb.state.logged_out": "🚪 logged out of Facebook",
    "fb.state.unknown": "❓ backing off after a page it did not recognise",
    "fb.state.crash_loop": "🔁 restarting too often — stopped",
    "fb.state.parser_broken": "🧩 cannot read Facebook's pages — stopped",
    "fb.state.feed_not_chrono": "📰 the Groups feed is not newest-first — reading groups one by one",
    "fb.state.needs_login": "🔑 needs a login",
    "fb.state.paused": "⏸ paused",
    "fb.state.offline": "📴 offline",
    "fb.profile.cautious": "cautious",
    "fb.profile.normal": "normal",
    "fb.dur.min": "{n} min",
    "fb.dur.hours": "{n} h",
    "fb.dur.days": "{n} d",
    "fb.budget.navigation": "page loads",
    "fb.budget.scroll": "scrolls",
    # -- /status -----------------------------------------------------------------------------
    "fb.status.title": "📊 Status",
    "fb.status.collector": "Collector: {state}",
    "fb.status.collector_owner": "Collector {id}: {state}",
    "fb.status.never": "Collector: none has reported yet, so nothing is being read.",
    "fb.status.report": "Last report {ago} · pace: {profile}",
    "fb.status.detail": "Details: {detail}",
    "fb.status.paused": "⏸ Paused by you — /resume continues.",
    "fb.status.paused_user": "⏸ The owner has paused reading.",
    "fb.status.pace_cap": "Pace capped at {profile} (/pace)",
    "fb.status.block": "⛔ Block #{id}: {reason} — until {until}",
    "fb.status.block_final": "⛔ Block #{id}: {reason} — until re-planned on the collector host",
    "fb.status.budget": "Budget left: {items}",
    "fb.status.budget_item": "{kind} {remaining} of {cap} per {window}",
    "fb.status.others": "Without the lease (told to stop): {ids}",
    "fb.status.groups": "Groups: {monitored} read · {known} known · {unavailable} unreadable",
    "fb.status.lost": "🚫 Unreadable: {groups}",
    "fb.status.unverified": "❔ Missing from the last sync: {groups}",
    "fb.status.not_followed": "👁 Not followed, slow backstop only: {groups}",
    "fb.status.sort_ignored": "↕️ Ignore newest-first sorting: {groups}",
    "fb.status.over_cap": "⚠️ Over the {cap}-group limit, not read: {groups}",
    "fb.status.delay": "Delay: up to {feed} for new posts",
    "fb.status.delay_backstop": "Up to {backstop} in: {groups}",
    "fb.status.delay_feed_off": "Delay: up to {backstop} — groups are read one by one",
    "fb.status.delay_stopped": "Delay: nothing is being read right now",
    "fb.status.delay_shoulder": "Up to {shoulder} in the hour after and the 1.5 h before quiet hours",
    "fb.status.delay_idle": "Delay: nothing is read right now ({state}); reading resumes at {time}",
    "fb.status.delay_idle_soon": "Delay: nothing is read right now ({state}); reading resumes by itself",
    "fb.status.your_over_cap": "⚠️ Not read (over the {cap}-group limit): {groups}",
    "fb.status.items": (
        "Since start: {stored} new posts · {duplicates} repeats · {not_monitored} unwatched · "
        "{rejected} rejected"
    ),
    "fb.status.delivery": (
        "Notifications: {delivered} sent · {digested} in digests · {failed} failed · {queue} queued"
    ),
    "fb.status.uptime": "Bot up {uptime}",
    "fb.status.owner_help": "/pause · /resume · /pace · /lease · /listings · /ai",
    "fb.status.your_groups": "Your groups: {n}",
    # -- owner alerts ------------------------------------------------------------------------
    "fb.alert.state": "⚠️ Collector {id}: {state}",
    "fb.alert.blocked": (
        "⛔ Facebook warning: collector {id} stopped (block #{block}: {reason}). It stays "
        "stopped until {until}; after that /resume asks for one careful check."
    ),
    "fb.alert.blocked_final": (
        "⛔ Facebook warning: collector {id} stopped (block #{block}: {reason}). This is the "
        "last rung: it stays stopped until you re-plan on the collector host."
    ),
    "fb.alert.offline": "📴 Collector {id} has not reported for {silent}. Nothing is being read.",
    "fb.alert.back": "✅ Collector {id} is reading again.",
    "fb.alert.block_closed": (
        "✅ Block #{block} lifted after a clean check. The collector reads cautiously for a while."
    ),
    "fb.alert.group_lost": (
        "🚫 The account can no longer read {group}. It stays paused until a group sync lists "
        "the account as a member again."
    ),
    "fb.alert.not_followed": (
        "👁 The account does not follow {group} (or snoozed it), so it only gets the slow "
        "backstop. Follow it by hand in the collector's browser (fbcollector login)."
    ),
    "fb.alert.sort_ignored": "↕️ {group} ignores newest-first sorting; it relies on the Groups feed.",
    "fb.alert.outbox_high": (
        "📦 Collector {id}: its outbox is over 80 % full — it cannot reach the bot."
    ),
    "fb.alert.event": "ℹ️ Collector {id}: {kind}",
    "fb.alert.silence": (
        "🔕 No new posts from {group} for {silent}, nights not counted (it usually posts at "
        "least every {gap}). "
        "The collector says it is reading — check the group by hand."
    ),
    "fb.alert.over_cap": (
        "⚠️ {n} watched groups are over the {cap}-group limit and are not read: {groups}"
    ),
    "fb.alert.contender": (
        "🔒 Collector {id} asked to read, but {holder} holds the lease, so {id} was told to "
        "stop. If {id} replaces {holder}: /lease {id}"
    ),
    "fb.alert.twin": (
        "🔒 A second state database reports as {id} (database {new}: a second machine, or a "
        "state that was recreated). Only {id} with database {old} reads; the new one was told "
        "to stop. If it replaces the old one: /lease {id}"
    ),
    "fb.alert.lease_moved": (
        "🔒 {old} has checked in and was told to stop. {id} gets the lease at its next check-in."
    ),
    "fb.alert.sync": "🔄 Groups synced from the account: {total}.",
    "fb.alert.sync_added": (
        "➕ New: {groups}. Open them under 🗂 Shared groups if others should see them."
    ),
    "fb.alert.sync_unverified": "❔ Missing from this sync (not confirmed gone): {groups}",
    "fb.alert.sync_left": "➖ The account left: {groups}",
    "fb.alert.sync_lost": "🚫 Unreadable: {groups}",
    "fb.alert.sync_back": "✅ Readable again: {groups}",
    # -- owner commands ----------------------------------------------------------------------
    "fb.pause.done": (
        "⏸ Paused. The collector stops at its next check-in, within a minute or two. "
        "/resume continues."
    ),
    "fb.pause.already": "⏸ Already paused. /resume continues.",
    "fb.resume.unpaused": "▶️ Unpaused. The collector continues at its next check-in.",
    "fb.resume.nothing": "Nothing to resume: not paused, and the collector reports no open block.",
    "fb.resume.card": (
        "⛔ Block #{id}: {reason}\n"
        "Opened {opened} · rung {rung}\n\n"
        "Resuming asks the collector for one careful look at the Groups feed. A clean look "
        "lifts the block; anything else starts the next, longer one."
    ),
    "fb.resume.button": "▶️ Resume block #{id}",
    "fb.resume.too_early": "Block #{id} runs until {until}. Ask again after that.",
    "fb.resume.final": (
        "Block #{id} is the last rung: only a re-plan on the collector host lifts it."
    ),
    "fb.resume.no_probe": (
        "Block #{id} is an advanced-protection lock. Resume it only after you have checked, "
        "in the collector's own browser, that Facebook lifted it: /resume offers a button for "
        "that."
    ),
    "fb.resume.lock_card": (
        "⛔ Block #{id}: {reason}\n"
        "Facebook's advanced-protection lock: the collector makes no probe until you confirm "
        "it is lifted.\n\n"
        "Check only in the collector's own browser (fbcollector login), never on your phone or "
        "main browser. Once Facebook shows the account unlocked, confirm below; the collector "
        "then takes one careful look."
    ),
    "fb.resume.lock_button": "🔓 Lock lifted — resume #{id}",
    "fb.resume.no_holder": (
        "The lease is moving between collectors, so nothing was sent. Try again once it has moved."
    ),
    "fb.resume.stale": "That block is no longer the open one; nothing was sent.",
    "fb.resume.sent": (
        "▶️ Sent. The collector takes one careful look within a few minutes, and you hear "
        "back either way."
    ),
    "fb.resume.pending": "Already sent for block #{id}; waiting for the collector.",
    "fb.pace.show": (
        "Pace cap: {profile}.\n"
        "/pace cautious — slow the collector down\n"
        "/pace normal — let it use its own profile"
    ),
    "fb.pace.set": "Pace cap is now: {profile}. The collector picks it up at its next check-in.",
    "fb.pace.none": "none — the collector's own profile",
    "fb.listings.show": (
        "Buy/sell groups read with the listings sort (CHRONOLOGICAL_LISTINGS) as well: {on}.\n"
        "Watched groups: {groups}\n"
        "/listings <group id> on|off — only where phase 0 showed listing posts missing"
    ),
    "fb.listings.none": "none",
    "fb.listings.set": (
        "Listings sort for {group}: {state}. The collector picks it up at its next check-in."
    ),
    "fb.listings.on": "on",
    "fb.listings.off": "off",
    "fb.listings.unknown": "No group with id {id} is known; /listings shows the ids.",
    "fb.ai.head": "🤖 AI rule · model {model} · shadow: verdicts are recorded, nothing is sent yet",
    "fb.ai.off": "🤖 The AI rule is off: the brain has no OPENROUTER_API_KEY.",
    "fb.ai.none": "No wishes yet.",
    "fb.ai.wish": (
        "#{n} {group}: «{wish}» — sends from {threshold}%\n"
        "   24 h: {judged} judged, {would} would be sent, {failed} failed, {queued} waiting · "
        "${cost}"
    ),
    "fb.ai.calls": "Model calls in 24 h: {calls} of {cap}",
    "fb.ai.matched_head": "Would have been sent (7 days):",
    "fb.ai.matched_none": "Nothing would have been sent in 7 days.",
    "fb.ai.matched": "• {percent}% #{n} {reason}",
    "fb.ai.usage": "/ai add <group id> <wish in your words> · /ai del <n> · group ids: /listings",
    "fb.ai.added": (
        "🤖 Wish #{n} for {group}: «{wish}». Every new post there is judged by the model; in "
        "shadow nothing is sent, /ai shows what would be."
    ),
    "fb.ai.deleted": "🤖 Wish #{n} deleted.",
    "fb.ai.not_found": "You have no wish #{n}; /ai lists them.",
    "fb.ai.unknown_group": "No watched group with id {id}; /listings shows the ids.",
    "fb.ai.bad_wish": "Write the wish in words, up to {n} characters.",
    "fb.ai.too_many": "At most {n} wishes: delete one first (/ai del <n>).",
    "fb.ai.failing": (
        "🤖 The AI rule cannot reach its model ({error}). Posts wait in its queue and are "
        "retried for six hours."
    ),
    "fb.ai.recovered": "🤖 The AI rule's model answers again.",
    "fb.ai.cap": (
        "🤖 The AI rule reached its cap of {cap} model calls a day; posts wait until the day's "
        "calls fall below it (AI_DAILY_CALLS)."
    ),
    "fb.lease.show": "Lease: {holder}.\nCollectors seen: {ids}\n/lease <id> moves it.",
    "fb.lease.nobody": "nobody yet",
    "fb.lease.moved": "🔒 Lease moved to {id}. {old} is told to stop at its next check-in.",
    "fb.lease.unknown": "No collector called {id} has checked in; nothing changed.",
    "fb.lease.same": "{id} already holds the lease.",
    "fb.lease.moving": (
        "Moving to {id}: waiting for {old} to check in and hear that it must stop. If {old} is "
        "switched off for good: /lease {id} now"
    ),
    "fb.lease.revoking": (
        "🔒 {old} is told to stop at its next check-in, and {id} gets the lease once {old} has "
        "heard it. If {old} is switched off for good: /lease {id} now"
    ),
    "fb.lease.cancelled": "Move cancelled: {id} keeps the lease.",
    "fb.lease.replacing": (
        "🔒 {id} with its current state database is told to stop at its next check-in; {id} "
        "with the newer one gets the lease once the old one has heard it. If the old one is "
        "switched off for good: /lease {id} now"
    ),
    "fb.lease.replaced": (
        "🔒 Lease moved to {id} with its newer state database. The old one is told to stop at "
        "its next check-in."
    ),
    "fb.refresh.no_holder": "No collector holds the lease right now; try again once one does.",
    "fb.refresh.sent": (
        "🔄 Asked the collector to re-read the account's groups. It starts within a few "
        "minutes, when its checks allow."
    ),
    "fb.refresh.pending": "🔄 Already asked; waiting for the collector.",
    "fb.refresh.limit": "At most {n} group refreshes a day; try again later.",
}

RU: Final[dict[str, str]] = {
    "notif.chat": "👥 Группа",
    "notif.sender": "👤 Автор",
    "notif.link": "🔗 Пост",
    "notif.rule_scope.chat": "правило группы",
    "notif.rule_scope.global": "правило для всех групп",
    "notif.sender.unknown": "Неизвестный автор",
    "notif.sender.by_id": "Автор {id}",
    "notif.sender.anonymous": "Анонимный участник",
    "notif.btn.ignore": "🚫 Игнорировать автора",
    "notif.forwarded": "↪️ репост",
    "notif.private": "🔒 Закрытая группа — ссылка откроется только участникам",
    "notif.late": "⏳ Опубликовано {ago}, появилось в группе только сейчас",
    "digest.more_chats": "… и ещё {n} групп ({m} совпадений)",
    "bot.gate.card": "👤 Пользователь {user_id} просит доступ к ddiver-fb.",
    "bot.btn.all_chats": "🌐 Все группы",
    "bot.btn.refresh_chats": "🔄 Обновить группы",
    "bot.cmd.export": "Выгрузить группы и правила в файл",
    "bot.cmd.import": "Загрузить группы и правила из файла",
    "bot.export.caption": (
        "📦 Ваша конфигурация: групп {chats}, правил {rules}, в игноре {ignored}.\n"
        "Храните файл где-нибудь ещё — восстановиться можно командой /import.\n"
        "Входа в Facebook здесь нет и быть не может."
    ),
    "bot.import.prompt": (
        "<b>Загрузка конфигурации</b>\n\n"
        "Пришлите файл, который дал /export. Маленький можно вставить текстом.\n\n"
        "Загрузка <b>добавляет</b>, а не заменяет: ничего из уже настроенного не пропадёт, "
        "дубликаты просто пропустятся.\n\n"
        "Группы ищутся по их id в Facebook, поэтому группа вернётся, только если аккаунт "
        "коллектора в ней состоит."
    ),
    "bot.import.over_limit": (
        "⚠️ Не поместилось правил: {n}. Лимиты — {chat} на группу и {total} всего."
    ),
    "bot.import.chats_over_limit": "⚠️ Прочитал только первые {n} групп из файла.",
    "bot.import.ignored_over_limit": "⚠️ Не поместилось игнорируемых авторов: {n}. Лимит — {limit}.",
    "bot.import.missing_chats": "⚠️ Не нашёл эти группы: {chats}",
    "bot.menu.chats": "👥 Группы",
    "bot.menu.shared": "🗂 Общие группы",
    "bot.chats.title": "Группы под наблюдением ({n}). Нажатие переключает: ✅ — слежу, ☐ — нет.",
    "bot.chats.empty": (
        "Групп пока нет: коллектор ещё не прислал список групп аккаунта. "
        "Нажмите 🔄 Обновить группы."
    ),
    "bot.chats.none_shared": "Владелец пока не поделился ни одной группой.",
    "bot.chats.search_prompt": "Пришлите часть названия группы.",
    "bot.chats.lost_access": (
        "⚠️ Аккаунт Facebook больше не может читать {chat}; наблюдение приостановлено."
    ),
    "bot.chats.regained_access": "✅ Аккаунт Facebook снова может читать {chat}.",
    "bot.rules.pick_chat": "Для какой группы правила?",
    "bot.rules.title_global": "Правила для всех групп",
    "bot.rules.limit_chat": "Для этой группы уже {limit} правил.",
    "bot.ignored.title": "Игнорируемые авторы",
    "bot.ignored.added_global": "🚫 Игнорирую {sender} во всех группах.",
    "bot.ignored.removed": "Автор больше не игнорируется.",
    "bot.ignored.all_chats_btn": "🚫 И во всех группах",
    "bot.notif.drop_scope_chat": "только для группы {chat}",
    "bot.test.prompt": (
        "<b>Проверка правил</b>\n\n"
        "Пришлите любой текст — я прогоню его через ваши правила ровно так же, как "
        "настоящий пост, и покажу результат по каждой отслеживаемой группе.\n\n"
        "Это проверка текста, а не поиск по истории: никуда не лезу и период времени "
        "ни при чём. Автор и игнор-лист тоже не учитываются — только текст.\n\n"
        "Можно сразу одной командой:\n"
        "<code>/test сдаётся квартира в Варне, 400 евро</code>\n\n"
        "Что будет в ответе по каждой группе:\n"
        "  ✅ сработало — какое правило и какой кусок текста\n"
        "  🚫 отсеяно вашим исключением\n"
        "  — совпадений нет"
    ),
    "bot.test.no_chats": (
        "⚠️ Вы пока не следите ни за одной группой, так что уведомлений не будет "
        "ни по одному правилу. Ниже — что сработало бы на правилах для всех групп."
    ),
    "bot.test.global_scope": "правила для всех групп",
    "bot.users.title": (
        "👥 Пользователи\n\n"
        "Каждый, кто напишет боту /start, попадает сюда как «ожидает» и до вашего "
        "решения не получает ничего. Допущенный пользователь выбирает группы только из "
        "открытых вами и ведёт свои правила, игнор-лист, язык и часовой пояс.\n"
    ),
    "bot.shared.hint": (
        "🗂 Общие группы\n\n"
        "Группы читает только аккаунт Facebook коллектора, поэтому другие пользователи "
        "видят не все группы, а лишь те, что вы открыли здесь.\n\n"
        "🗂 — открыта: пользователь может добавить её себе\n"
        "🔒 — только ваша\n\n"
        "Нажатие переключает. Когда закрываете группу, она исчезает из подписок всех "
        "остальных — на ваши подписки это не влияет."
    ),
    "bot.shared.unshared_notice": "ℹ️ Владелец закрыл доступ к {chat}; группа убрана из ваших.",
    "bot.help": _help(
        _from(
            core_ru.STRINGS["bot.help"],
            "<code>from:100012345678901</code> — следить за автором\n"
            "  ✅ любой его пост; по id рядом с именем в уведомлении или по куску имени\n"
            "  ❌ упоминание о нём в чужом посте",
        ),
        "Я слежу за выбранными группами Facebook и пишу вам, когда новый пост совпадает "
        "с вашими правилами. Пост приходит через несколько минут после появления; текущую "
        "задержку показывает /status.",
    ),
    "bot.rules.prompt": _from(
        core_ru.STRINGS["bot.rules.prompt"],
        "<code>from:100012345678901</code> — следить за автором, а не за текстом\n"
        "  ✅ любой его пост, хоть фото без подписи\n"
        "  ❌ чужой пост, где он упомянут\n"
        "  Число — id автора в Facebook, оно стоит рядом с именем в каждом уведомлении и не меняется, если автор сменит имя.\n"
        "  Ещё можно <code>from:Иван Петров</code> (кусок имени, без учёта регистра).\n"
        "  Юзернеймов (@…) Facebook не показывает, так что <code>from:@…</code> здесь не найдёт ничего.",
    ),
    "rule.err.no_phone": (
        "Facebook не показывает телефон автора, так что такое правило не поймает ничего. "
        "Используйте id автора (число рядом с именем в уведомлении) или кусок имени."
    ),
    "rule.err.bad_username": (
        "Юзернеймов (@…) Facebook не показывает, так что правило from:@… не поймает ничего. "
        "Используйте id автора (число рядом с именем в уведомлении) или кусок имени."
    ),
    "rule.err.bad_sender_id": "Это число слишком большое для id в Facebook.",
    "rule.err.name_too_short": (
        "Имя ищется как кусок строки, поэтому нужно хотя бы {limit} символа — иначе сработает на пол-группы."
    ),
    "bot.test.sender_rules": "(ещё {n} правил from: — они смотрят на автора поста, текстом их не проверить)",
    "fb.state.ok": "✅ читает",
    "fb.state.warmup": "🌱 разогрев после входа — пока не читает",
    "fb.state.quiet": "🌙 тихие часы",
    "fb.state.break": "☕ перерыв",
    "fb.state.not_home": "🏠 не дома — не читает",
    "fb.state.blocked": "⛔ остановлен предупреждением Facebook",
    "fb.state.logged_out": "🚪 вышел из Facebook",
    "fb.state.unknown": "❓ пауза после незнакомой страницы",
    "fb.state.crash_loop": "🔁 слишком часто перезапускается — остановлен",
    "fb.state.parser_broken": "🧩 не может разобрать страницы Facebook — остановлен",
    "fb.state.feed_not_chrono": "📰 лента групп идёт не по времени — читает группы по одной",
    "fb.state.needs_login": "🔑 нужен вход",
    "fb.state.paused": "⏸ на паузе",
    "fb.state.offline": "📴 не на связи",
    "fb.profile.cautious": "осторожный",
    "fb.profile.normal": "обычный",
    "fb.dur.min": "{n} мин",
    "fb.dur.hours": "{n} ч",
    "fb.dur.days": "{n} д",
    "fb.budget.navigation": "загрузок страниц",
    "fb.budget.scroll": "прокруток",
    "fb.status.title": "📊 Статус",
    "fb.status.collector": "Коллектор: {state}",
    "fb.status.collector_owner": "Коллектор {id}: {state}",
    "fb.status.never": "Коллектор: ещё ни один не отчитался, поэтому ничего не читается.",
    "fb.status.report": "Последний отчёт {ago} · темп: {profile}",
    "fb.status.detail": "Подробности: {detail}",
    "fb.status.paused": "⏸ Пауза от вас — /resume продолжит.",
    "fb.status.paused_user": "⏸ Владелец поставил чтение на паузу.",
    "fb.status.pace_cap": "Темп ограничен: {profile} (/pace)",
    "fb.status.block": "⛔ Блок #{id}: {reason} — до {until}",
    "fb.status.block_final": "⛔ Блок #{id}: {reason} — до перепланирования на машине коллектора",
    "fb.status.budget": "Остаток бюджета: {items}",
    "fb.status.budget_item": "{kind} {remaining} из {cap} за {window}",
    "fb.status.others": "Без аренды (им велено остановиться): {ids}",
    "fb.status.groups": "Группы: читается {monitored} · известно {known} · недоступно {unavailable}",
    "fb.status.lost": "🚫 Недоступны: {groups}",
    "fb.status.unverified": "❔ Пропали из последней синхронизации: {groups}",
    "fb.status.not_followed": "👁 Аккаунт не отслеживает, только медленный обход: {groups}",
    "fb.status.sort_ignored": "↕️ Игнорируют сортировку по времени: {groups}",
    "fb.status.over_cap": "⚠️ Сверх лимита в {cap} групп, не читаются: {groups}",
    "fb.status.delay": "Задержка: до {feed} для новых постов",
    "fb.status.delay_backstop": "До {backstop} в: {groups}",
    "fb.status.delay_feed_off": "Задержка: до {backstop} — группы читаются по одной",
    "fb.status.delay_stopped": "Задержка: сейчас ничего не читается",
    "fb.status.delay_shoulder": "До {shoulder} в первый час после тихих часов и за 1,5 ч до них",
    "fb.status.delay_idle": "Задержка: сейчас ничего не читается ({state}); чтение возобновится в {time}",
    "fb.status.delay_idle_soon": "Задержка: сейчас ничего не читается ({state}); чтение возобновится само",
    "fb.status.your_over_cap": "⚠️ Не читаются (сверх лимита в {cap} групп): {groups}",
    "fb.status.items": (
        "С запуска: новых постов {stored} · повторов {duplicates} · без наблюдения "
        "{not_monitored} · отклонено {rejected}"
    ),
    "fb.status.delivery": (
        "Уведомления: отправлено {delivered} · в дайджестах {digested} · ошибок {failed} · "
        "в очереди {queue}"
    ),
    "fb.status.uptime": "Бот работает {uptime}",
    "fb.status.owner_help": "/pause · /resume · /pace · /lease · /listings · /ai",
    "fb.status.your_groups": "Ваших групп: {n}",
    "fb.alert.state": "⚠️ Коллектор {id}: {state}",
    "fb.alert.blocked": (
        "⛔ Предупреждение Facebook: коллектор {id} остановлен (блок #{block}: {reason}). "
        "Он стоит до {until}; после этого /resume попросит одну осторожную проверку."
    ),
    "fb.alert.blocked_final": (
        "⛔ Предупреждение Facebook: коллектор {id} остановлен (блок #{block}: {reason}). "
        "Это последняя ступень: он стоит, пока вы не перепланируете всё на машине коллектора."
    ),
    "fb.alert.offline": "📴 Коллектор {id} не отчитывался уже {silent}. Ничего не читается.",
    "fb.alert.back": "✅ Коллектор {id} снова читает.",
    "fb.alert.block_closed": (
        "✅ Блок #{block} снят после чистой проверки. Коллектор какое-то время читает осторожно."
    ),
    "fb.alert.group_lost": (
        "🚫 Аккаунт больше не может читать {group}. Группа на паузе, пока синхронизация "
        "снова не покажет аккаунт её участником."
    ),
    "fb.alert.not_followed": (
        "👁 Аккаунт не отслеживает {group} (или отложил её), поэтому она получает только "
        "медленный обход. Подпишитесь вручную в браузере коллектора (fbcollector login)."
    ),
    "fb.alert.sort_ignored": "↕️ {group} игнорирует сортировку по времени; для неё остаётся лента групп.",
    "fb.alert.outbox_high": (
        "📦 Коллектор {id}: очередь отправки заполнена больше чем на 80 % — он не может "
        "достучаться до бота."
    ),
    "fb.alert.event": "ℹ️ Коллектор {id}: {kind}",
    "fb.alert.silence": (
        "🔕 В {group} нет новых постов уже {silent}, не считая ночи (обычно не реже чем раз "
        "в {gap}). "
        "Коллектор говорит, что читает, — проверьте группу вручную."
    ),
    "fb.alert.over_cap": "⚠️ Групп сверх лимита в {cap}: {n}, они не читаются: {groups}",
    "fb.alert.contender": (
        "🔒 Коллектор {id} просится читать, но аренда у {holder}, поэтому {id} велено "
        "остановиться. Если {id} заменяет {holder}: /lease {id}"
    ),
    "fb.alert.twin": (
        "🔒 Вторая база состояния выходит на связь как {id} (база {new}: вторая машина или "
        "заново созданное состояние). Читает только {id} с базой {old}; новой велено "
        "остановиться. Если она заменяет старую: /lease {id}"
    ),
    "fb.alert.lease_moved": (
        "🔒 {old} вышел на связь, и ему велено остановиться. {id} получит аренду при "
        "следующем опросе."
    ),
    "fb.alert.sync": "🔄 Группы аккаунта синхронизированы: {total}.",
    "fb.alert.sync_added": (
        "➕ Новые: {groups}. Откройте их в 🗂 Общих группах, если другим тоже нужно их видеть."
    ),
    "fb.alert.sync_unverified": "❔ Пропали из этой синхронизации (уход не подтверждён): {groups}",
    "fb.alert.sync_left": "➖ Аккаунт вышел: {groups}",
    "fb.alert.sync_lost": "🚫 Недоступны: {groups}",
    "fb.alert.sync_back": "✅ Снова доступны: {groups}",
    "fb.pause.done": (
        "⏸ Пауза. Коллектор остановится при следующем опросе, в течение минуты-двух. "
        "/resume продолжит."
    ),
    "fb.pause.already": "⏸ Уже на паузе. /resume продолжит.",
    "fb.resume.unpaused": "▶️ Пауза снята. Коллектор продолжит при следующем опросе.",
    "fb.resume.nothing": "Нечего возобновлять: паузы нет, открытого блока коллектор не сообщает.",
    "fb.resume.card": (
        "⛔ Блок #{id}: {reason}\n"
        "Открыт {opened} · ступень {rung}\n\n"
        "Возобновление просит коллектор один раз осторожно посмотреть ленту групп. Чистый "
        "результат снимает блок; любой другой открывает следующий, более долгий."
    ),
    "fb.resume.button": "▶️ Возобновить блок #{id}",
    "fb.resume.too_early": "Блок #{id} действует до {until}. Попросите снова после этого.",
    "fb.resume.final": (
        "Блок #{id} — последняя ступень: его снимает только перепланирование на машине коллектора."
    ),
    "fb.resume.no_probe": (
        "Блок #{id} — блокировка расширенной защиты. Возобновляйте только после того, как в "
        "браузере коллектора убедились, что Facebook её снял: /resume предложит для этого кнопку."
    ),
    "fb.resume.lock_card": (
        "⛔ Блок #{id}: {reason}\n"
        "Блокировка расширенной защиты Facebook: коллектор не делает проверок, пока вы не "
        "подтвердите, что её сняли.\n\n"
        "Проверяйте только в браузере самого коллектора (fbcollector login), никогда в телефоне "
        "или основном браузере. Когда Facebook покажет, что аккаунт разблокирован, подтвердите "
        "ниже; коллектор один раз осторожно посмотрит."
    ),
    "fb.resume.lock_button": "🔓 Снята — возобновить #{id}",
    "fb.resume.no_holder": (
        "Аренда сейчас передаётся между коллекторами, поэтому ничего не отправлено. Попробуйте, "
        "когда передача завершится."
    ),
    "fb.resume.stale": "Этот блок уже не открыт; ничего не отправлено.",
    "fb.resume.sent": (
        "▶️ Отправлено. Коллектор осторожно посмотрит в течение нескольких минут, результат "
        "придёт в любом случае."
    ),
    "fb.resume.pending": "Для блока #{id} уже отправлено; ждём коллектор.",
    "fb.pace.show": (
        "Ограничение темпа: {profile}.\n"
        "/pace cautious — замедлить коллектор\n"
        "/pace normal — пусть работает по своему профилю"
    ),
    "fb.pace.set": "Ограничение темпа: {profile}. Коллектор узнает при следующем опросе.",
    "fb.pace.none": "нет — собственный профиль коллектора",
    "fb.listings.show": (
        "Группы купли-продажи, которые читаются ещё и в сортировке объявлений "
        "(CHRONOLOGICAL_LISTINGS): {on}.\n"
        "Отслеживаемые группы: {groups}\n"
        "/listings <id группы> on|off — только там, где фаза 0 показала пропуски объявлений"
    ),
    "fb.listings.none": "нет",
    "fb.listings.set": (
        "Сортировка объявлений для {group}: {state}. Коллектор узнает об этом при следующем опросе."
    ),
    "fb.listings.on": "вкл.",
    "fb.listings.off": "выкл.",
    "fb.listings.unknown": "Группа с id {id} неизвестна; id показывает /listings.",
    "fb.ai.head": "🤖 ИИ-правило · модель {model} · в тени: оценки пишутся, ничего не шлётся",
    "fb.ai.off": "🤖 ИИ-правило выключено: у мозга нет OPENROUTER_API_KEY.",
    "fb.ai.none": "Желаний пока нет.",
    "fb.ai.wish": (
        "#{n} {group}: «{wish}» — шлёт от {threshold}%\n"
        "   за сутки: оценено {judged}, отправилось бы {would}, сбоев {failed}, ждут {queued} · "
        "${cost}"
    ),
    "fb.ai.calls": "Запросов к модели за сутки: {calls} из {cap}",
    "fb.ai.matched_head": "Отправилось бы (7 дней):",
    "fb.ai.matched_none": "За 7 дней ничего бы не отправилось.",
    "fb.ai.matched": "• {percent}% #{n} {reason}",
    "fb.ai.usage": "/ai add <id группы> <желание своими словами> · /ai del <n> · id групп: /listings",
    "fb.ai.added": (
        "🤖 Желание #{n} для {group}: «{wish}». Каждый новый пост там оценит модель; в тени "
        "ничего не шлётся, /ai покажет, что отправилось бы."
    ),
    "fb.ai.deleted": "🤖 Желание #{n} удалено.",
    "fb.ai.not_found": "У вас нет желания #{n}; список — /ai.",
    "fb.ai.unknown_group": "Нет отслеживаемой группы с id {id}; id показывает /listings.",
    "fb.ai.bad_wish": "Напишите желание словами, до {n} символов.",
    "fb.ai.too_many": "Не больше {n} желаний: сначала удалите одно (/ai del <n>).",
    "fb.ai.failing": (
        "🤖 ИИ-правило не может достучаться до модели ({error}). Посты ждут в очереди, "
        "повторы — шесть часов."
    ),
    "fb.ai.recovered": "🤖 Модель ИИ-правила снова отвечает.",
    "fb.ai.cap": (
        "🤖 ИИ-правило упёрлось в дневной лимит {cap} запросов к модели; посты ждут, пока "
        "запросов за сутки не станет меньше (AI_DAILY_CALLS)."
    ),
    "fb.lease.show": "Аренда: {holder}.\nЗамеченные коллекторы: {ids}\n/lease <id> передаёт её.",
    "fb.lease.nobody": "пока ни у кого",
    "fb.lease.moved": "🔒 Аренда передана {id}. {old} остановится при следующем опросе.",
    "fb.lease.unknown": "Коллектор {id} ни разу не выходил на связь; ничего не изменено.",
    "fb.lease.same": "Аренда уже у {id}.",
    "fb.lease.moving": (
        "Передаётся {id}: ждём, пока {old} выйдет на связь и узнает, что должен остановиться. "
        "Если {old} выключен насовсем: /lease {id} now"
    ),
    "fb.lease.revoking": (
        "🔒 {old} велено остановиться при следующем опросе; {id} получит аренду, когда {old} "
        "это узнает. Если {old} выключен насовсем: /lease {id} now"
    ),
    "fb.lease.cancelled": "Передача отменена: аренда остаётся у {id}.",
    "fb.lease.replacing": (
        "🔒 {id} с текущей базой состояния велено остановиться при следующем опросе; {id} с "
        "более новой базой получит аренду, когда старый это узнает. Если старый выключен "
        "насовсем: /lease {id} now"
    ),
    "fb.lease.replaced": (
        "🔒 Аренда передана {id} с более новой базой состояния. Старый остановится при "
        "следующем опросе."
    ),
    "fb.refresh.no_holder": "Сейчас аренда ни у одного коллектора; попробуйте, когда она появится.",
    "fb.refresh.sent": (
        "🔄 Попросил коллектор перечитать группы аккаунта. Он начнёт через несколько минут, "
        "когда позволят проверки."
    ),
    "fb.refresh.pending": "🔄 Уже попросил; ждём коллектор.",
    "fb.refresh.limit": "Не больше {n} обновлений групп в сутки; попробуйте позже.",
}

UK: Final[dict[str, str]] = {
    "notif.chat": "👥 Група",
    "notif.sender": "👤 Автор",
    "notif.link": "🔗 Допис",
    "notif.rule_scope.chat": "правило групи",
    "notif.rule_scope.global": "правило для всіх груп",
    "notif.sender.unknown": "Невідомий автор",
    "notif.sender.by_id": "Автор {id}",
    "notif.sender.anonymous": "Анонімний учасник",
    "notif.btn.ignore": "🚫 Ігнорувати автора",
    "notif.forwarded": "↪️ поширений допис",
    "notif.private": "🔒 Закрита група — посилання відкриється лише учасникам",
    "notif.late": "⏳ Опубліковано {ago}, з'явилося в групі лише зараз",
    "digest.more_chats": "… і ще {n} груп ({m} збігів)",
    "bot.gate.card": "👤 Користувач {user_id} просить доступ до ddiver-fb.",
    "bot.btn.all_chats": "🌐 Усі групи",
    "bot.btn.refresh_chats": "🔄 Оновити групи",
    "bot.cmd.export": "Вивантажити групи й правила у файл",
    "bot.cmd.import": "Завантажити групи й правила з файлу",
    "bot.export.caption": (
        "📦 Ваша конфігурація: груп {chats}, правил {rules}, в ігнорі {ignored}.\n"
        "Тримайте файл десь іще — відновитися можна командою /import.\n"
        "Входу у Facebook тут немає й бути не може."
    ),
    "bot.import.prompt": (
        "<b>Завантаження конфігурації</b>\n\n"
        "Надішліть файл, який дав /export. Маленький можна вставити текстом.\n\n"
        "Завантаження <b>додає</b>, а не заміняє: нічого з уже налаштованого не зникне, "
        "дублікати просто пропустяться.\n\n"
        "Групи шукаються за їхнім id у Facebook, тож група повернеться, лише якщо акаунт "
        "колектора в ній є."
    ),
    "bot.import.over_limit": (
        "⚠️ Не вмістилося правил: {n}. Ліміти — {chat} на групу і {total} загалом."
    ),
    "bot.import.chats_over_limit": "⚠️ Прочитав лише перші {n} груп із файлу.",
    "bot.import.ignored_over_limit": "⚠️ Не вмістилося ігнорованих авторів: {n}. Ліміт — {limit}.",
    "bot.import.missing_chats": "⚠️ Не знайшов ці групи: {chats}",
    "bot.menu.chats": "👥 Групи",
    "bot.menu.shared": "🗂 Спільні групи",
    "bot.chats.title": "Групи під наглядом ({n}). Дотик перемикає: ✅ — стежу, ☐ — ні.",
    "bot.chats.empty": (
        "Груп поки немає: колектор ще не надіслав список груп акаунта. Натисніть 🔄 Оновити групи."
    ),
    "bot.chats.none_shared": "Власник поки не поділився жодною групою.",
    "bot.chats.search_prompt": "Надішліть частину назви групи.",
    "bot.chats.lost_access": (
        "⚠️ Акаунт Facebook більше не може читати {chat}; нагляд призупинено."
    ),
    "bot.chats.regained_access": "✅ Акаунт Facebook знову може читати {chat}.",
    "bot.rules.pick_chat": "Для якої групи правила?",
    "bot.rules.title_global": "Правила для всіх груп",
    "bot.rules.limit_chat": "Для цієї групи вже {limit} правил.",
    "bot.ignored.title": "Ігноровані автори",
    "bot.ignored.added_global": "🚫 Ігнорую {sender} в усіх групах.",
    "bot.ignored.removed": "Автор більше не ігнорується.",
    "bot.ignored.all_chats_btn": "🚫 І в усіх групах",
    "bot.notif.drop_scope_chat": "лише для групи {chat}",
    "bot.test.prompt": (
        "<b>Перевірка правил</b>\n\n"
        "Надішліть будь-який текст — я прожену його через ваші правила так само, як "
        "справжній допис, і покажу результат по кожній відстежуваній групі.\n\n"
        "Це перевірка тексту, а не пошук в історії: нікуди не лізу й період часу ні до "
        "чого. Автор та ігнор-лист теж не враховуються — лише текст.\n\n"
        "Можна одразу однією командою:\n"
        "<code>/test здається квартира у Варні, 400 євро</code>\n\n"
        "Що буде у відповіді по кожній групі:\n"
        "  ✅ спрацювало — яке правило і який шматок тексту\n"
        "  🚫 відсіяно вашим винятком\n"
        "  — збігів немає"
    ),
    "bot.test.no_chats": (
        "⚠️ Ви поки не стежите за жодною групою, тож сповіщень не буде за жодним "
        "правилом. Нижче — що спрацювало б на правилах для всіх груп."
    ),
    "bot.test.global_scope": "правила для всіх груп",
    "bot.users.title": (
        "👥 Користувачі\n\n"
        "Кожен, хто напише боту /start, потрапляє сюди як «очікує» і до вашого "
        "рішення не отримує нічого. Допущений користувач обирає групи лише з "
        "відкритих вами й веде свої правила, ігнор-лист, мову та часовий пояс.\n"
    ),
    "bot.shared.hint": (
        "🗂 Спільні групи\n\n"
        "Групи читає лише акаунт Facebook колектора, тому інші користувачі бачать не всі "
        "групи, а тільки ті, що ви відкрили тут.\n\n"
        "🗂 — відкрита: користувач може додати її собі\n"
        "🔒 — лише ваша\n\n"
        "Дотик перемикає. Коли ви закриваєте групу, вона зникає з підписок усіх інших — "
        "на ваші підписки це не впливає."
    ),
    "bot.shared.unshared_notice": "ℹ️ Власник закрив доступ до {chat}; групу прибрано з ваших.",
    "bot.help": _help(
        _from(
            core_uk.STRINGS["bot.help"],
            "<code>from:100012345678901</code> — стежити за автором\n"
            "  ✅ будь-який його допис; за id поруч з ім'ям у сповіщенні або за частиною імені\n"
            "  ❌ згадка про нього в чужому дописі",
        ),
        "Я стежу за обраними групами Facebook і пишу вам, коли новий допис збігається "
        "з вашими правилами. Допис приходить за кілька хвилин після появи; поточну затримку "
        "показує /status.",
    ),
    "bot.rules.prompt": _from(
        core_uk.STRINGS["bot.rules.prompt"],
        "<code>from:100012345678901</code> — стежити за автором, а не за текстом\n"
        "  ✅ будь-який його допис, навіть фото без підпису\n"
        "  ❌ чужий допис, де його згадано\n"
        "  Число — id автора у Facebook, воно стоїть поруч з ім'ям у кожному сповіщенні й не змінюється, якщо автор змінить ім'я.\n"
        "  Ще можна <code>from:Олена Коваленко</code> (частина імені, без урахування регістру).\n"
        "  Юзернеймів (@…) Facebook не показує, тож <code>from:@…</code> тут не знайде нічого.",
    ),
    "rule.err.no_phone": (
        "Facebook не показує телефон автора, тож таке правило не впіймає нічого. "
        "Використайте id автора (число поруч з ім'ям у сповіщенні) або частину імені."
    ),
    "rule.err.bad_username": (
        "Юзернеймів (@…) Facebook не показує, тож правило from:@… не впіймає нічого. "
        "Використайте id автора (число поруч з ім'ям у сповіщенні) або частину імені."
    ),
    "rule.err.bad_sender_id": "Це число завелике для id у Facebook.",
    "rule.err.name_too_short": (
        "Ім'я шукається як частина рядка, тому потрібно щонайменше {limit} символи — інакше спрацює на пів групи."
    ),
    "bot.test.sender_rules": "(ще {n} правил from: — вони дивляться на автора допису, текстом їх не перевірити)",
    "fb.state.ok": "✅ читає",
    "fb.state.warmup": "🌱 розігрів після входу — поки не читає",
    "fb.state.quiet": "🌙 тихі години",
    "fb.state.break": "☕ перерва",
    "fb.state.not_home": "🏠 не вдома — не читає",
    "fb.state.blocked": "⛔ зупинений попередженням Facebook",
    "fb.state.logged_out": "🚪 вийшов із Facebook",
    "fb.state.unknown": "❓ пауза після незнайомої сторінки",
    "fb.state.crash_loop": "🔁 надто часто перезапускається — зупинений",
    "fb.state.parser_broken": "🧩 не може розібрати сторінки Facebook — зупинений",
    "fb.state.feed_not_chrono": "📰 стрічка груп іде не за часом — читає групи по одній",
    "fb.state.needs_login": "🔑 потрібен вхід",
    "fb.state.paused": "⏸ на паузі",
    "fb.state.offline": "📴 не на зв'язку",
    "fb.profile.cautious": "обережний",
    "fb.profile.normal": "звичайний",
    "fb.dur.min": "{n} хв",
    "fb.dur.hours": "{n} год",
    "fb.dur.days": "{n} д",
    "fb.budget.navigation": "завантажень сторінок",
    "fb.budget.scroll": "прокручувань",
    "fb.status.title": "📊 Статус",
    "fb.status.collector": "Колектор: {state}",
    "fb.status.collector_owner": "Колектор {id}: {state}",
    "fb.status.never": "Колектор: жоден ще не звітував, тож нічого не читається.",
    "fb.status.report": "Останній звіт {ago} · темп: {profile}",
    "fb.status.detail": "Подробиці: {detail}",
    "fb.status.paused": "⏸ Пауза від вас — /resume продовжить.",
    "fb.status.paused_user": "⏸ Власник поставив читання на паузу.",
    "fb.status.pace_cap": "Темп обмежено: {profile} (/pace)",
    "fb.status.block": "⛔ Блок #{id}: {reason} — до {until}",
    "fb.status.block_final": "⛔ Блок #{id}: {reason} — до перепланування на машині колектора",
    "fb.status.budget": "Залишок бюджету: {items}",
    "fb.status.budget_item": "{kind} {remaining} з {cap} за {window}",
    "fb.status.others": "Без оренди (їм сказано зупинитися): {ids}",
    "fb.status.groups": "Групи: читається {monitored} · відомо {known} · недоступно {unavailable}",
    "fb.status.lost": "🚫 Недоступні: {groups}",
    "fb.status.unverified": "❔ Зникли з останньої синхронізації: {groups}",
    "fb.status.not_followed": "👁 Акаунт не відстежує, лише повільний обхід: {groups}",
    "fb.status.sort_ignored": "↕️ Ігнорують сортування за часом: {groups}",
    "fb.status.over_cap": "⚠️ Понад ліміт у {cap} груп, не читаються: {groups}",
    "fb.status.delay": "Затримка: до {feed} для нових дописів",
    "fb.status.delay_backstop": "До {backstop} у: {groups}",
    "fb.status.delay_feed_off": "Затримка: до {backstop} — групи читаються по одній",
    "fb.status.delay_stopped": "Затримка: зараз нічого не читається",
    "fb.status.delay_shoulder": "До {shoulder} у першу годину після тихих годин і за 1,5 год до них",
    "fb.status.delay_idle": "Затримка: зараз нічого не читається ({state}); читання відновиться о {time}",
    "fb.status.delay_idle_soon": "Затримка: зараз нічого не читається ({state}); читання відновиться саме",
    "fb.status.your_over_cap": "⚠️ Не читаються (понад ліміт у {cap} груп): {groups}",
    "fb.status.items": (
        "Від запуску: нових дописів {stored} · повторів {duplicates} · без нагляду "
        "{not_monitored} · відхилено {rejected}"
    ),
    "fb.status.delivery": (
        "Сповіщення: надіслано {delivered} · у дайджестах {digested} · помилок {failed} · "
        "у черзі {queue}"
    ),
    "fb.status.uptime": "Бот працює {uptime}",
    "fb.status.owner_help": "/pause · /resume · /pace · /lease · /listings · /ai",
    "fb.status.your_groups": "Ваших груп: {n}",
    "fb.alert.state": "⚠️ Колектор {id}: {state}",
    "fb.alert.blocked": (
        "⛔ Попередження Facebook: колектор {id} зупинено (блок #{block}: {reason}). Він стоїть "
        "до {until}; після цього /resume попросить одну обережну перевірку."
    ),
    "fb.alert.blocked_final": (
        "⛔ Попередження Facebook: колектор {id} зупинено (блок #{block}: {reason}). Це "
        "остання сходинка: він стоїть, доки ви не переплануєте все на машині колектора."
    ),
    "fb.alert.offline": "📴 Колектор {id} не звітував уже {silent}. Нічого не читається.",
    "fb.alert.back": "✅ Колектор {id} знову читає.",
    "fb.alert.block_closed": (
        "✅ Блок #{block} знято після чистої перевірки. Колектор якийсь час читає обережно."
    ),
    "fb.alert.group_lost": (
        "🚫 Акаунт більше не може читати {group}. Група на паузі, доки синхронізація знову "
        "не покаже акаунт її учасником."
    ),
    "fb.alert.not_followed": (
        "👁 Акаунт не відстежує {group} (або відклав її), тож вона отримує лише повільний "
        "обхід. Підпишіться вручну в браузері колектора (fbcollector login)."
    ),
    "fb.alert.sort_ignored": "↕️ {group} ігнорує сортування за часом; для неї лишається стрічка груп.",
    "fb.alert.outbox_high": (
        "📦 Колектор {id}: черга відправлення заповнена більш ніж на 80 % — він не може "
        "достукатися до бота."
    ),
    "fb.alert.event": "ℹ️ Колектор {id}: {kind}",
    "fb.alert.silence": (
        "🔕 У {group} немає нових дописів уже {silent}, не рахуючи ночі (зазвичай не рідше "
        "ніж раз на {gap}). "
        "Колектор каже, що читає, — перевірте групу вручну."
    ),
    "fb.alert.over_cap": "⚠️ Груп понад ліміт у {cap}: {n}, вони не читаються: {groups}",
    "fb.alert.contender": (
        "🔒 Колектор {id} проситься читати, але оренда в {holder}, тож {id} сказано "
        "зупинитися. Якщо {id} заміняє {holder}: /lease {id}"
    ),
    "fb.alert.twin": (
        "🔒 Друга база стану виходить на зв'язок як {id} (база {new}: друга машина або "
        "заново створений стан). Читає лише {id} з базою {old}; новій сказано зупинитися. "
        "Якщо вона заміняє стару: /lease {id}"
    ),
    "fb.alert.lease_moved": (
        "🔒 {old} вийшов на зв'язок, і йому сказано зупинитися. {id} отримає оренду під час "
        "наступного опитування."
    ),
    "fb.alert.sync": "🔄 Групи акаунта синхронізовано: {total}.",
    "fb.alert.sync_added": (
        "➕ Нові: {groups}. Відкрийте їх у 🗂 Спільних групах, якщо іншим теж треба їх бачити."
    ),
    "fb.alert.sync_unverified": "❔ Зникли з цієї синхронізації (вихід не підтверджено): {groups}",
    "fb.alert.sync_left": "➖ Акаунт вийшов: {groups}",
    "fb.alert.sync_lost": "🚫 Недоступні: {groups}",
    "fb.alert.sync_back": "✅ Знову доступні: {groups}",
    "fb.pause.done": (
        "⏸ Пауза. Колектор зупиниться під час наступного опитування, протягом хвилини-двох. "
        "/resume продовжить."
    ),
    "fb.pause.already": "⏸ Уже на паузі. /resume продовжить.",
    "fb.resume.unpaused": "▶️ Паузу знято. Колектор продовжить під час наступного опитування.",
    "fb.resume.nothing": "Нічого відновлювати: паузи немає, відкритого блоку колектор не повідомляє.",
    "fb.resume.card": (
        "⛔ Блок #{id}: {reason}\n"
        "Відкрито {opened} · сходинка {rung}\n\n"
        "Відновлення просить колектор один раз обережно подивитися стрічку груп. Чистий "
        "результат знімає блок; будь-який інший відкриває наступний, довший."
    ),
    "fb.resume.button": "▶️ Відновити блок #{id}",
    "fb.resume.too_early": "Блок #{id} діє до {until}. Попросіть знову після цього.",
    "fb.resume.final": (
        "Блок #{id} — остання сходинка: його знімає лише перепланування на машині колектора."
    ),
    "fb.resume.no_probe": (
        "Блок #{id} — блокування розширеного захисту. Відновлюйте лише після того, як у "
        "браузері колектора переконалися, що Facebook його зняв: /resume запропонує для цього "
        "кнопку."
    ),
    "fb.resume.lock_card": (
        "⛔ Блок #{id}: {reason}\n"
        "Блокування розширеного захисту Facebook: колектор не робить перевірок, доки ви не "
        "підтвердите, що його знято.\n\n"
        "Перевіряйте лише в браузері самого колектора (fbcollector login), ніколи в телефоні чи "
        "основному браузері. Коли Facebook покаже, що акаунт розблоковано, підтвердьте нижче; "
        "колектор один раз обережно подивиться."
    ),
    "fb.resume.lock_button": "🔓 Знято — відновити #{id}",
    "fb.resume.no_holder": (
        "Оренда зараз передається між колекторами, тож нічого не надіслано. Спробуйте, коли "
        "передачу завершено."
    ),
    "fb.resume.stale": "Цей блок уже не відкритий; нічого не надіслано.",
    "fb.resume.sent": (
        "▶️ Надіслано. Колектор обережно подивиться протягом кількох хвилин, результат прийде "
        "в будь-якому разі."
    ),
    "fb.resume.pending": "Для блоку #{id} уже надіслано; чекаємо на колектор.",
    "fb.pace.show": (
        "Обмеження темпу: {profile}.\n"
        "/pace cautious — сповільнити колектор\n"
        "/pace normal — хай працює за своїм профілем"
    ),
    "fb.pace.set": "Обмеження темпу: {profile}. Колектор дізнається під час наступного опитування.",
    "fb.pace.none": "немає — власний профіль колектора",
    "fb.listings.show": (
        "Групи купівлі-продажу, які читаються ще й у сортуванні оголошень "
        "(CHRONOLOGICAL_LISTINGS): {on}.\n"
        "Відстежувані групи: {groups}\n"
        "/listings <id групи> on|off — лише там, де фаза 0 показала пропуски оголошень"
    ),
    "fb.listings.none": "немає",
    "fb.listings.set": (
        "Сортування оголошень для {group}: {state}. Колектор дізнається про це під час "
        "наступного опитування."
    ),
    "fb.listings.on": "увімк.",
    "fb.listings.off": "вимк.",
    "fb.listings.unknown": "Групу з id {id} не знайдено; id показує /listings.",
    "fb.ai.head": "🤖 ШІ-правило · модель {model} · у тіні: оцінки пишуться, нічого не надсилається",
    "fb.ai.off": "🤖 ШІ-правило вимкнено: мозок не має OPENROUTER_API_KEY.",
    "fb.ai.none": "Бажань поки немає.",
    "fb.ai.wish": (
        "#{n} {group}: «{wish}» — надсилає від {threshold}%\n"
        "   за добу: оцінено {judged}, надіслалося б {would}, збоїв {failed}, чекають {queued} · "
        "${cost}"
    ),
    "fb.ai.calls": "Запитів до моделі за добу: {calls} з {cap}",
    "fb.ai.matched_head": "Надіслалося б (7 днів):",
    "fb.ai.matched_none": "За 7 днів нічого б не надіслалося.",
    "fb.ai.matched": "• {percent}% #{n} {reason}",
    "fb.ai.usage": "/ai add <id групи> <бажання своїми словами> · /ai del <n> · id груп: /listings",
    "fb.ai.added": (
        "🤖 Бажання #{n} для {group}: «{wish}». Кожен новий допис там оцінить модель; у тіні "
        "нічого не надсилається, /ai покаже, що надіслалося б."
    ),
    "fb.ai.deleted": "🤖 Бажання #{n} видалено.",
    "fb.ai.not_found": "У вас немає бажання #{n}; список — /ai.",
    "fb.ai.unknown_group": "Немає відстежуваної групи з id {id}; id показує /listings.",
    "fb.ai.bad_wish": "Напишіть бажання словами, до {n} символів.",
    "fb.ai.too_many": "Не більше {n} бажань: спершу видаліть одне (/ai del <n>).",
    "fb.ai.failing": (
        "🤖 ШІ-правило не може дістатися до моделі ({error}). Дописи чекають у черзі, "
        "повтори — шість годин."
    ),
    "fb.ai.recovered": "🤖 Модель ШІ-правила знову відповідає.",
    "fb.ai.cap": (
        "🤖 ШІ-правило вперлося в денний ліміт {cap} запитів до моделі; дописи чекають, доки "
        "запитів за добу не стане менше (AI_DAILY_CALLS)."
    ),
    "fb.lease.show": "Оренда: {holder}.\nПомічені колектори: {ids}\n/lease <id> передає її.",
    "fb.lease.nobody": "поки ні в кого",
    "fb.lease.moved": "🔒 Оренду передано {id}. {old} зупиниться під час наступного опитування.",
    "fb.lease.unknown": "Колектор {id} жодного разу не виходив на зв'язок; нічого не змінено.",
    "fb.lease.same": "Оренда вже в {id}.",
    "fb.lease.moving": (
        "Передається {id}: чекаємо, поки {old} вийде на зв'язок і дізнається, що має "
        "зупинитися. Якщо {old} вимкнено назавжди: /lease {id} now"
    ),
    "fb.lease.revoking": (
        "🔒 {old} сказано зупинитися під час наступного опитування; {id} отримає оренду, щойно "
        "{old} про це дізнається. Якщо {old} вимкнено назавжди: /lease {id} now"
    ),
    "fb.lease.cancelled": "Передачу скасовано: оренда лишається в {id}.",
    "fb.lease.replacing": (
        "🔒 {id} з поточною базою стану сказано зупинитися під час наступного опитування; {id} "
        "з новішою базою отримає оренду, щойно старий про це дізнається. Якщо старий вимкнено "
        "назавжди: /lease {id} now"
    ),
    "fb.lease.replaced": (
        "🔒 Оренду передано {id} з новішою базою стану. Старий зупиниться під час наступного "
        "опитування."
    ),
    "fb.refresh.no_holder": "Зараз оренди немає в жодного колектора; спробуйте, коли вона з'явиться.",
    "fb.refresh.sent": (
        "🔄 Попросив колектор перечитати групи акаунта. Він почне за кілька хвилин, коли "
        "дозволять перевірки."
    ),
    "fb.refresh.pending": "🔄 Уже попросив; чекаємо на колектор.",
    "fb.refresh.limit": "Не більше {n} оновлень груп на добу; спробуйте пізніше.",
}

OVERLAYS: Final[dict[str, dict[str, str]]] = {"en": EN, "ru": RU, "uk": UK}


def install() -> None:
    """Lay the overlay over the core's catalogs. Idempotent: a later call wins key by key."""
    for lang, catalog in OVERLAYS.items():
        i18n.register_overlay(lang, catalog)
