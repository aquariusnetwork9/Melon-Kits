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
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import bot as bot_mod
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

print()
if fails:
    for f in fails:
        print("FAIL: %s" % f)
    sys.exit(1)
print("ADOPTION LOGIC OK")
