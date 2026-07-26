"""Check the Discord layer constructs correctly, without connecting to a gateway.

Run on the host, where discord.py is installed::

    cd /home/ubuntu/melon-kits/kit-app
    MELONKIT_DISCORD_TOKEN=x ./.venv/bin/python deploy/verify_components.py

**Why this exists as a separate script.** Every other module in this directory is stdlib-only,
which is what lets `python -m unittest discover` run the whole suite with nothing installed --
and it is also why none of those tests can touch `bot.py`. So the entire Discord surface has no
unit coverage: modal field counts, custom_id lengths, DynamicItem templates, app-command
parameter types and the channel pickers Discord derives from them are all validated by
discord.py at construction or decoration time, and a mistake in any of them is a crash on a
button press or at startup rather than a test failure.

This closes that gap for everything that can be checked without a connection. It has caught,
for real: modals that would have silently discarded a slow typist's answers, and a channel
picker that offered media channels for the reviewer queue -- where every post requires an
attachment, so no card could ever be posted.

Exits non-zero on the first failure so it can gate a deploy.
"""

import asyncio
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import bot as bot_mod
import card as card_mod
import config as config_mod
import store as store_mod

fails = []


def check(name, fn):
    try:
        fn()
        print("  ok   %s" % name)
    except Exception as exc:
        fails.append((name, exc))
        print("  FAIL %s -> %s: %s" % (name, type(exc).__name__, exc))


print("discord.py", discord.__version__)
print("\n-- modals construct, and none of them can time out mid-typing --")


def modal_check(cls, *args):
    def inner():
        m = cls(None, *args)
        assert m.timeout is None, "timeout is %r, a slow typist loses their answers" % m.timeout
        n = len(list(m.walk_children()))
        assert n <= 5, "%d components, Discord allows 5" % n
        return m
    return inner


check("RequestModal", modal_check(bot_mod.RequestModal))
check("FundingModal", modal_check(bot_mod.FundingModal))
check("CoordsModal", modal_check(bot_mod.CoordsModal, 1))
check("DeclineModal", modal_check(bot_mod.DeclineModal, 1))


def field_count():
    m = bot_mod.FundingModal(None)
    labels = [c.label for c in m.walk_children()]
    assert len(labels) == 4, labels
    print("       funding fields: %s" % labels)


check("FundingModal has the four asked-for fields", field_count)

print("\n-- the panel has both buttons, and the original custom_id is untouched --")


def panel():
    v = bot_mod.PanelView()
    ids = [c.custom_id for c in v.children]
    assert bot_mod.PANEL_CUSTOM_ID in ids, ids
    assert bot_mod.PANEL_FUNDING_CUSTOM_ID in ids, ids
    assert len(ids) == 2, ids
    for cid in ids:
        assert len(cid) <= 100, "%s is %d chars, Discord caps custom_id at 100" % (cid, len(cid))
    print("       %s" % ids)


check("PanelView", panel)


def panel_embed_renders():
    g = {"cooldown_days": 21, "response_time": "24-48 hours", "rescued_count": "15.1k",
         "rescued_as_of": "7/20/25"}
    e = bot_mod.panel_embed(g)
    assert len(e.description) <= 4096
    assert any("Rescue kit" in f.value for f in e.fields), "the choice is not explained"
    total = len(e.title or "") + len(e.description or "") + sum(
        len(f.name) + len(f.value) for f in e.fields)
    assert total <= 6000, total
    print("       embed %d chars total" % total)


check("panel_embed", panel_embed_renders)

print("\n-- the purge sweeper is wired up and refuses to run without a grace period --")


def purge_wiring():
    for name in ("purge_finished_tickets", "_purge_loop", "_delete_thread"):
        assert callable(getattr(bot_mod.KitBot, name, None)), "KitBot.%s is missing" % name
    src = inspect.getsource(bot_mod.KitBot.purge_finished_tickets)
    # The two rules that make deleting BOTH sides of a ticket safe. Asserted on the source
    # because there is no way to exercise this without a gateway, and a silent regression here
    # deletes the only surviving record of a ticket.
    assert "thread_purge_hours" in src, "the sweeper does not read the grace period"
    assert "hours <= 0" in src, "the sweeper has no off switch"
    assert "tickets_to_purge" in src, "the sweeper does not use the guarded query"
    ts = inspect.getsource(bot_mod.KitBot._post_transcript)
    assert "mark_transcribed" in ts, "a posted transcript is not recorded, so nothing can purge"
    assert ts.rstrip().endswith("return True"), "_post_transcript no longer reports success"
    # The task must be held, or asyncio's weak reference lets it vanish at an arbitrary GC.
    assert "_purge_task" in inspect.getsource(bot_mod.KitBot.setup_hook)
    print("       purge_finished_tickets, _purge_loop, _delete_thread all present")


def purge_config():
    cfg = config_mod.load_config()
    assert cfg["policy"]["thread_purge_hours"] == 24, cfg["policy"]["thread_purge_hours"]
    name = config_mod.env_name("policy.thread_purge_hours")
    try:
        config_mod.load_config(env={name: "-1"})
    except config_mod.ConfigError:
        pass
    else:
        raise AssertionError("thread_purge_hours=-1 was accepted")
    # 0 must be allowed: it is the off switch for an irreversible feature.
    off = config_mod.load_config(env={name: "0"})
    assert off["policy"]["thread_purge_hours"] == 0
    print("       default 24h, 0 disables, negatives refused")


check("purge sweeper wiring", purge_wiring)
check("thread_purge_hours", purge_config)

print("\n-- an applicant hears about the LONGER of the two clocks --")


def block(days):
    return {"blocked": bool(days), "days_left": days}


CLOCK_CASES = [
    # (kit days, request days, which the applicant should be told about)
    (0, 0, None),
    (14, 0, "kit"),
    (0, 166, "request"),
    # The trap this exists for: 21-day kit cooldown reported over a 166-day request clock sends
    # somebody away for a fortnight to be refused again by a rule nobody mentioned.
    (14, 166, "request"),
    # ...and the reverse, which is real: the kit cooldown also matches on the MC account, so a
    # second Discord account asking for a recently-helped account trips it alone.
    (21, 0, "kit"),
    (200, 166, "kit"),
    (166, 166, "request"),
]


def clocks():
    for kit, req, want in CLOCK_CASES:
        got = bot_mod.longer_block(block(kit), block(req))
        assert got == want, "kit=%s request=%s -> %s, wanted %s" % (kit, req, got, want)
    # A blocked clock with a missing count must not read as "not blocked".
    assert bot_mod.longer_block({"blocked": True}, {"blocked": False}) == "kit"
    print("       %d cases" % len(CLOCK_CASES))


check("longer_block picks the longer wait", clocks)

print("\n-- the card's colour agrees with its heading --")


def call_colours():
    # Every call the recommender can return needs a colour, or a Blocked card silently renders
    # in the fallback and the stripe stops meaning anything.
    for call in (card_mod.CALL_APPROVE, card_mod.CALL_LOOK, card_mod.CALL_DENY):
        assert call in bot_mod._CALL_COLOUR, call
        discord.Colour.from_str(bot_mod._CALL_COLOUR[call])          # raises if malformed
    assert len(set(bot_mod._CALL_COLOUR.values())) == 3, "two calls share a colour"
    print("       %s" % bot_mod._CALL_COLOUR)


check("a colour per call", call_colours)

print("\n-- the guides embed links all four channels --")

ALL_GUIDES = (bot_mod.GUIDES_CATEGORY_ID, bot_mod.GUIDE_ESCAPE_WITH_KIT_ID,
              bot_mod.GUIDE_ESCAPE_NO_KIT_ID, bot_mod.GUIDE_CLIENT_SETUP_ID)


def guides_renders():
    e = bot_mod.guides_embed()
    # Three guides plus the category line.
    assert len(e.fields) == 4, [f.name for f in e.fields]
    for cid in ALL_GUIDES:
        assert any("<#%d>" % cid in f.value for f in e.fields), "%d is not linked" % cid
    total = len(e.title or "") + len(e.description or "") + sum(
        len(f.name) + len(f.value) for f in e.fields)
    assert total <= 6000, total
    for f in e.fields:
        assert len(f.value) <= 1024, (f.name, len(f.value))
    assert len(e.description) <= 4096
    print("       embed %d chars, %d fields" % (total, len(e.fields)))


def guides_needs_no_guild():
    # Takes no guild on purpose: the bot does not have to be in the server, or be able to see
    # those channels, for a reader's <#id> to resolve. Regression guard on that decision.
    import inspect
    params = list(inspect.signature(bot_mod.guides_embed).parameters)
    assert params == [], "guides_embed grew a %r parameter -- see its docstring" % params


def guides_ids_distinct():
    assert len(set(ALL_GUIDES)) == 4, "two guide constants hold the same id"
    for cid in ALL_GUIDES:
        # Discord snowflakes are 17-19 digits; a truncated paste would silently link nothing.
        assert 10 ** 16 < cid < 10 ** 19, cid


def guides_warn_survives():
    # The die-with-your-kit warning is the one line here with a consequence attached.
    e = bot_mod.guides_embed()
    assert "won't get another" in e.description, e.description


check("guides_embed renders", guides_renders)
check("guides_embed takes no guild", guides_needs_no_guild)
check("guide ids are distinct snowflakes", guides_ids_distinct)
check("the kit warning is still in it", guides_warn_survives)

print("\n-- every dynamic button round-trips its own custom_id --")


async def dynamic_roundtrip():
    cases = [
        (bot_mod.ChatHistoryButton, (42,), "melonkit:chat:42"),
        (bot_mod.ChatPageButton, (42, 3, "Next"), "melonkit:chatpage:42:3"),
        (bot_mod.ChatFileButton, (42,), "melonkit:chatfile:42"),
        (bot_mod.CoordsButton, (42,), "melonkit:coords:42"),
        (bot_mod.ApproveButton, (42,), "melonkit:approve:42"),
        (bot_mod.ClaimButton, (7,), "melonkit:claim:7"),
    ]
    for cls, args, expect in cases:
        item = cls(*args)
        cid = item.custom_id
        assert cid == expect, "%s built %r, expected %r" % (cls.__name__, cid, expect)
        assert len(cid) <= 100, cid
        m = cls.__discord_ui_compiled_template__.fullmatch(cid)
        assert m is not None, "%s cannot parse its own custom_id %r" % (cls.__name__, cid)
        rebuilt = await cls.from_custom_id(None, item.item, m)
        assert rebuilt.ticket_id if hasattr(rebuilt, "ticket_id") else True
        print("       ok %-20s %s" % (cls.__name__, cid))

    # And the templates must not collide: chat vs chatpage vs chatfile share a prefix.
    for cls, _, _ in cases:
        for other, _, cid in cases:
            if cls is other:
                continue
            if cls.__discord_ui_compiled_template__.fullmatch(cid):
                raise AssertionError("%s also matches %s's id %s"
                                     % (cls.__name__, other.__name__, cid))
    print("       no template collisions")


check("DynamicItem round-trip", lambda: asyncio.get_event_loop().run_until_complete(
    dynamic_roundtrip()))

print("\n-- the pager view never contains a non-dynamic child --")


def pager_view_shape():
    """A plain Button here would, on the forced 15-minute ephemeral timeout, unregister the
    dynamic templates process-wide and silently break paging for everyone."""
    for prev, nxt in ((False, True), (True, True), (True, False)):
        v = discord.ui.View(timeout=None)
        if prev:
            v.add_item(bot_mod.ChatPageButton(42, 0, "Previous"))
        if nxt:
            v.add_item(bot_mod.ChatPageButton(42, 2, "Next"))
        v.add_item(bot_mod.ChatFileButton(42))
        for child in v.children:
            assert isinstance(child, discord.ui.DynamicItem), type(child).__name__
        assert len(v.children) <= 25


check("pager view children are all dynamic", pager_view_shape)

print("\n-- the setup tags fit Discord's limits --")


def tags():
    assert len(bot_mod.SETUP_TAGS) <= 20, len(bot_mod.SETUP_TAGS)
    for name, emoji in bot_mod.SETUP_TAGS:
        assert len(name) <= 20, "forum tag %r is %d chars, cap is 20" % (name, len(name))
    lifecycle = {n.casefold() for n in bot_mod.QUEUE_TAGS}
    kinds = {store_mod.KIND_LABEL[k].casefold() for k in store_mod.KINDS}
    assert not (lifecycle & kinds), "a request-type tag is also a lifecycle tag: %s" % (
        lifecycle & kinds)
    have = {n.casefold() for n, _ in bot_mod.SETUP_TAGS}
    assert kinds <= have, "missing type tags: %s" % (kinds - have)
    print("       %d tags, %s are type tags and none is a lifecycle tag"
          % (len(bot_mod.SETUP_TAGS), sorted(kinds)))


check("SETUP_TAGS", tags)

print("\n-- forum post titles stay inside the 100-char limit --")


def titles():
    for kind in store_mod.KINDS:
        title = "#%d %s - %s" % (999999, "A" * 16, store_mod.KIND_LABEL[kind])
        assert len(title) <= 100, "%d chars: %s" % (len(title), title)
    print("       worst case %d chars" % len(title))


check("queue post title", titles)



# ============================ the /setup signature ============================
#
# Discord derives the admin's role and channel pickers from these annotations, so the
# annotation IS the UI. discord.py resolves them at decoration time -- a bad one is a
# startup crash.

cfg = config_mod.load_config(None, env={"MELONKIT_DISCORD_TOKEN": "x",
                                        "MELONKIT_VC_USER_AGENT": "t (contact: a@b.c)"})
app = bot_mod.KitBot(cfg)
bot_mod.register_commands(app)


cmd = app.tree.get_command("setup")
if cmd is None:
    print("FAIL: /setup did not register")
    sys.exit(1)

print("/setup registered, %d options (Discord allows 25)\n" % len(cmd.parameters))
for p in cmd.parameters:
    kinds = getattr(p, "channel_types", None) or []
    print("  %-17s type=%-9s required=%-5s %s" % (
        p.name, p.type.name, p.required,
        "channels: " + ", ".join(sorted(k.name for k in kinds)) if kinds else ""))

by_name = {p.name: p for p in cmd.parameters}


def expect(name, required, kinds=None):
    p = by_name.get(name)
    if p is None:
        fails.append("%s is missing" % name)
        return
    if p.required != required:
        fails.append("%s required=%s, expected %s" % (name, p.required, required))
    if kinds is not None:
        got = {k.name for k in (getattr(p, "channel_types", None) or [])}
        if got != set(kinds):
            fails.append("%s channel_types=%s, expected %s" % (name, sorted(got),
                                                               sorted(kinds)))


# Only the reviewer role stays mandatory: the one-argument install must keep working.
expect("reviewer_role", True)
expect("delivery_role", False)
expect("category", False, ["category"])
expect("requests_channel", False, ["text", "news"])
# discord.py widens these: TextChannel also matches an announcement channel and ForumChannel
# also matches a media channel. Both are handled in /setup -- media is refused outright because
# it requires an attachment per post, announcement is warned about because it has no private
# threads. What matters here is that a voice or stage channel is never offered.
expect("queue_channel", False, ["forum", "text", "news", "media"])
expect("archive_channel", False, ["text", "news"])

if len(cmd.parameters) > 25:
    fails.append("%d options exceeds Discord's 25" % len(cmd.parameters))

# Descriptions are required by Discord and capped at 100 characters.
for p in cmd.parameters:
    if not p.description or p.description == "…":
        fails.append("%s has no description" % p.name)
    elif len(p.description) > 100:
        fails.append("%s description is %d chars, cap is 100" % (p.name, len(p.description)))

print("\ncommands registered: %s" % sorted(c.name for c in app.tree.get_commands()))

# The whole tree has to be serialisable or the sync at startup dies.
try:
    payload = [c.to_dict(app.tree) for c in app.tree.get_commands()]
    print("tree serialises: %d commands" % len(payload))
except Exception as exc:
    fails.append("tree.to_dict failed: %s: %s" % (type(exc).__name__, exc))

app.store.close()

print()
if fails:
    for f in fails:
        print("FAIL: %s" % f)
    sys.exit(1)
print("SETUP SIGNATURE OK")


# --- the adoption warnings, exercised with stubs -----------------------------
#
# _adoption_warnings lives in bot.py, the one module the stdlib test suite cannot import, so
# this is where it gets covered. It is pure logic over permissions_for().

class _Perms(object):
    def __init__(self, **kw):
        self._kw = kw

    def __getattr__(self, name):
        return self._kw.get(name, False)


class _Chan(object):
    def __init__(self, name, mine, everyone=None, news=False, guild=None):
        self.name = name
        self.mention = "#" + name
        self._mine = _Perms(**mine)
        self._everyone = _Perms(**(everyone or {}))
        self._news = news
        self.guild = guild or type("G", (), {"default_role": "everyone"})()

    def permissions_for(self, who):
        return self._everyone if who == "everyone" else self._mine

    def is_news(self):
        return self._news


ALL_OK = dict(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
              read_message_history=True, create_private_threads=True,
              send_messages_in_threads=True, manage_threads=True)

print("\n-- adoption warnings --")

quiet = bot_mod._adoption_warnings(_Chan("support", ALL_OK), "me", public=True)
print("  fully-permitted public channel -> %d warning(s)" % len(quiet))
if quiet:
    fails.append("a fully-permitted channel produced warnings: %s" % quiet)

chatty = bot_mod._adoption_warnings(
    _Chan("help", ALL_OK, everyone=dict(send_messages=True)), "me", public=True)
if not any("pushed up the channel" in w for w in chatty):
    fails.append("an everyone-can-post channel did not warn about the panel being buried")
else:
    print("  everyone-can-post -> warns the panel gets buried")

noprivate = bot_mod._adoption_warnings(
    _Chan("help", dict(ALL_OK, create_private_threads=False)), "me", public=True)
if not any("private thread" in w for w in noprivate):
    fails.append("a channel without create_private_threads did not warn")
else:
    print("  no create_private_threads -> warns applicants get no thread")

announce = bot_mod._adoption_warnings(_Chan("news", ALL_OK, news=True), "me", public=True)
if not any("announcement" in w for w in announce):
    fails.append("an announcement channel did not warn about private threads")
else:
    print("  announcement channel -> warns it cannot have private threads")

# A staff channel is not checked for thread permissions it never uses.
staff = bot_mod._adoption_warnings(
    _Chan("logs", dict(view_channel=True, send_messages=True, embed_links=True,
                       attach_files=True, read_message_history=True)), "me", public=False)
print("  staff channel without thread perms -> %d warning(s)" % len(staff))
if staff:
    fails.append("a staff channel was warned about thread permissions it does not use: %s"
                 % staff)

blind = bot_mod._adoption_warnings(_Chan("secret", {}), "me", public=False)
if not any("can't see it" in w for w in blind):
    fails.append("an invisible channel did not warn")
else:
    print("  invisible channel -> warns nothing will appear there")

print("  adoption logic ok")


# ======================== multiple reviewer / delivery roles ========================
#
# The gates live in bot.py, so this is where they get covered. The upgrade path is the part
# that matters: every deployment before multi-role has only the singular key stored, and
# reading the plural without falling back would lock every reviewer out of their own queue.

class _Role(object):
    def __init__(self, rid):
        self.id = rid


class _Member(object):
    def __init__(self, *role_ids, **kw):
        self.roles = [_Role(r) for r in role_ids]
        self.guild_permissions = _Perms(**kw)


print("\n-- staff role gates --")


def gate(label, ok):
    if ok:
        print("  ok   %s" % label)
    else:
        fails.append(label)
        print("  FAIL %s" % label)


REV, MOD, KIT, RANDOM = 11, 22, 33, 99

# The upgrade path: only the singular key exists, as every pre-multi-role guild has it.
legacy = {"reviewer_role_id": REV}
gate("a legacy single reviewer role still passes",
     bot_mod.is_reviewer(_Member(REV), legacy))
gate("a legacy config still refuses everyone else",
     not bot_mod.is_reviewer(_Member(RANDOM), legacy))
gate("role_ids reads the singular when there is no plural",
     bot_mod.role_ids(legacy, "reviewer_role_id") == [REV])

# Plural wins once written, and any one of the roles is enough.
multi = {"reviewer_role_id": REV, "reviewer_role_ids": [REV, MOD, KIT]}
for rid, name in ((REV, "first"), (MOD, "second"), (KIT, "third")):
    gate("the %s of three reviewer roles passes" % name,
         bot_mod.is_reviewer(_Member(rid), multi))
gate("someone holding none of the three is refused",
     not bot_mod.is_reviewer(_Member(RANDOM), multi))
gate("holding two of them is still fine",
     bot_mod.is_reviewer(_Member(REV, MOD), multi))

# No roles configured at all -> Manage Server reviews, which is the /setup bootstrap.
gate("with no reviewer role set, Manage Server reviews",
     bot_mod.is_reviewer(_Member(RANDOM, manage_guild=True), {}))
gate("with no reviewer role set, a random member does not",
     not bot_mod.is_reviewer(_Member(RANDOM), {}))

# Delivery.
runners = {"runner_role_ids": [MOD, KIT]}
gate("a delivery role can claim", bot_mod.may_claim(_Member(KIT), runners))
gate("a non-delivery member cannot claim", not bot_mod.may_claim(_Member(RANDOM), runners))
gate("a reviewer can always claim",
     bot_mod.may_claim(_Member(REV), dict(runners, reviewer_role_ids=[REV])))
gate("with no delivery role set, anyone can claim", bot_mod.may_claim(_Member(RANDOM), {}))

# The cap, and junk in the stored list.
over = {"reviewer_role_ids": list(range(1, 20))}
gate("the list is capped at %d" % bot_mod.MAX_STAFF_ROLES,
     len(bot_mod.role_ids(over, "reviewer_role_id")) == bot_mod.MAX_STAFF_ROLES)
messy = {"reviewer_role_ids": [REV, None, "22", 0, REV, "not-an-id"]}
gate("junk and duplicates are dropped, strings are kept",
     bot_mod.role_ids(messy, "reviewer_role_id") == [REV, 22])
gate("an empty plural falls back to the singular rather than locking everyone out",
     bot_mod.role_ids({"reviewer_role_ids": [], "reviewer_role_id": REV},
                      "reviewer_role_id") == [REV])

# The select is inside Discord's limits.
sel = bot_mod.StaffRoleSelect(None, "reviewer_role_id", "Reviewers", [REV, MOD])
gate("the role select allows up to %d" % bot_mod.MAX_STAFF_ROLES,
     sel.max_values == bot_mod.MAX_STAFF_ROLES and sel.max_values <= 25)
gate("the role select allows clearing to none", sel.min_values == 0)
gate("the role select pre-selects what is already configured",
     len(sel._underlying.default_values) == 2)

rcmd = app.tree.get_command("roles")
gate("/roles is registered", rcmd is not None)

print()
if fails:
    for f in fails:
        print("FAIL: %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
