"""Create the two channels the kit app needs, with the right permission overwrites.

Idempotent: run it twice and it reuses what already exists. Prints a JSON object with the
two channel ids on stdout so a deploy script can feed them straight into melonkit.json.

    python setup_channels.py --config melonkit.json

Requires Manage Channels, which the bot does **not** need at runtime -- only to create these.
Once setup is done the permission can be removed again.

Why the overwrites are shaped the way they are:

- `#kit-requests` is public but **read-only in the channel itself**: `@everyone` keeps
  view_channel and read_message_history, loses send_messages, and keeps
  send_messages_in_threads so an applicant can still talk inside their own ticket thread.
  Applicants also lose thread *creation*, so the only threads are the ones the bot makes.
- A deny on `@everyone` applies to the bot too. Discord resolves base role permissions, then
  the `@everyone` channel overwrite, then role overwrites, then member overwrites -- a
  guild-level role permission does not survive a channel-level deny. So the bot and the
  reviewer role get explicit allows rather than relying on their role permissions.
- `#kit-queue` denies view_channel to `@everyone` outright. In a forum, `send_messages` is
  what governs creating a post, so staff roles need it to comment and the bot needs it to
  open the post at all.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as config_mod   # noqa: E402

REQUESTS_NAME = "kit-requests"
QUEUE_NAME = "kit-queue"

REQUESTS_TOPIC = ("Ask for a barebones diamond kit. Press the button on the pinned panel - "
                  "each request opens a private thread just for you.")
QUEUE_TOPIC = ("Staff only. One post per kit request: the reviewer card, the decision, and "
               "the delivery claim. Filter by tag.")

# Lifecycle tags. Names must match QUEUE_TAGS in bot.py (matched case-insensitively).
TAGS = [
    ("awaiting review", "\U0001F552"),
    ("approved", "✅"),
    ("declined", "\U0001F6AB"),
    ("claimed", "\U0001F91D"),
    ("delivered", "\U0001F348"),
    ("closed", "\U0001F5C3"),
]


async def sync_existing(channel, topic, overwrites, label):
    """Re-sync an existing channel, treating topic and overwrites as separate operations.

    They need **different** permissions, and bundling them means the one you cannot do blocks
    the one you can. Editing permission overwrites requires **Manage Roles** (shown as "Manage
    Permissions" on a channel) -- Manage Channels is not enough, even though creating a channel
    *with* overwrites is allowed. So a rerun that only wants to add a forum tag would 403 on
    the overwrite sync and never reach the tag.
    """
    try:
        await channel.edit(topic=topic, reason="Melon Kits: sync topic")
        sys.stderr.write("  reused #%s (topic synced)\n" % label)
    except discord.Forbidden:
        sys.stderr.write("  reused #%s (topic unchanged: needs Manage Channels)\n" % label)
    try:
        await channel.edit(overwrites=overwrites, reason="Melon Kits: sync overwrites")
        sys.stderr.write("    overwrites synced\n")
    except discord.Forbidden:
        sys.stderr.write("    overwrites NOT synced: editing them needs Manage Roles "
                         "(\"Manage Permissions\"), which is separate from Manage Channels. "
                         "Existing overwrites are left as they are.\n")


def find_channel(guild, name, cls):
    for ch in guild.channels:
        if ch.name == name and isinstance(ch, cls):
            return ch
    return None


async def run(cfg, token):
    guild_id = int(cfg["discord"]["guild_id"])
    reviewer_id = int(cfg["discord"]["reviewer_role_id"] or 0)
    runner_id = int(cfg["discord"]["runner_role_id"] or 0)

    client = discord.Client(intents=discord.Intents.default())
    out = {}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
            me = guild.me or await guild.fetch_member(client.user.id)
            everyone = guild.default_role
            reviewer = guild.get_role(reviewer_id) if reviewer_id else None
            runner = guild.get_role(runner_id) if runner_id else None

            sys.stderr.write("guild: %s\n" % guild.name)
            sys.stderr.write("  reviewer role: %s\n" % (reviewer.name if reviewer else "NONE"))
            sys.stderr.write("  delivery role: %s\n" % (runner.name if runner else "NONE"))

            # Discord split pinning out of Manage Messages into its own `pin_messages`
            # permission, so a bot with manage_messages still gets 403/50013 on a pin. It is
            # only added to the overwrite when the bot actually holds it guild-wide: an
            # overwrite may not grant a permission the granter lacks, so including it
            # unconditionally would fail the whole channel create/edit call.
            can_pin = bool(me.guild_permissions.pin_messages)
            extra = {"pin_messages": True} if can_pin else {}
            if not can_pin:
                sys.stderr.write("  note: bot lacks pin_messages, so the panel cannot be "
                                 "pinned. Harmless -- nobody else can post in the channel, "
                                 "so the panel stays the newest message.\n")

            # ---------------------------------------------------------- requests
            ch = find_channel(guild, REQUESTS_NAME, discord.TextChannel)
            ow = {
                everyone: discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True,
                    send_messages=False,
                    create_public_threads=False, create_private_threads=False,
                    # Kept True so an applicant can reply inside their own ticket thread.
                    send_messages_in_threads=True),
                me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True,
                    attach_files=True, read_message_history=True,
                    create_private_threads=True, send_messages_in_threads=True,
                    manage_threads=True, manage_messages=True, **extra),
            }
            if reviewer:
                ow[reviewer] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    send_messages_in_threads=True, manage_threads=True)
            if ch is None:
                ch = await guild.create_text_channel(
                    REQUESTS_NAME, topic=REQUESTS_TOPIC, overwrites=ow,
                    reason="Melon Kits: public request channel")
                sys.stderr.write("  created #%s\n" % REQUESTS_NAME)
            else:
                await sync_existing(ch, REQUESTS_TOPIC, ow, REQUESTS_NAME)
            out["panel_channel_id"] = ch.id

            # ------------------------------------------------------------- queue
            forum = find_channel(guild, QUEUE_NAME, discord.ForumChannel)
            qow = {
                everyone: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True,
                    attach_files=True, read_message_history=True,
                    create_public_threads=True, send_messages_in_threads=True,
                    manage_threads=True, manage_messages=True),
            }
            for role in (reviewer, runner):
                if role:
                    qow[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True,
                        send_messages_in_threads=True, attach_files=True, embed_links=True)
            if forum is None:
                forum = await guild.create_forum(
                    QUEUE_NAME, topic=QUEUE_TOPIC, overwrites=qow,
                    available_tags=[discord.ForumTag(name=n, emoji=e) for n, e in TAGS],
                    reason="Melon Kits: staff review + dispatch queue")
                sys.stderr.write("  created #%s with %d tags\n" % (QUEUE_NAME, len(TAGS)))
            else:
                await sync_existing(forum, QUEUE_TOPIC, qow, QUEUE_NAME)
                have = {t.name.casefold() for t in forum.available_tags}
                for name, emoji in TAGS:
                    if name.casefold() not in have:
                        await forum.create_tag(name=name, emoji=emoji)
                        sys.stderr.write("  added tag %s\n" % name)
                sys.stderr.write("  reused #%s\n" % QUEUE_NAME)
            out["queue_channel_id"] = forum.id

            # ------------------------------------------------------- verification
            sys.stderr.write("\neffective permissions:\n")
            for label, channel, needs in (
                ("#" + REQUESTS_NAME, ch,
                 ("send_messages", "create_private_threads", "manage_threads",
                  "embed_links", "attach_files")),
                ("#" + QUEUE_NAME, forum,
                 ("send_messages", "create_public_threads", "manage_threads",
                  "embed_links", "attach_files")),
            ):
                perms = channel.permissions_for(me)
                missing = [n for n in needs if not getattr(perms, n)]
                sys.stderr.write("  bot in %-14s %s\n" % (
                    label, "OK" if not missing else "MISSING " + ", ".join(missing)))
            if reviewer:
                p = forum.permissions_for(reviewer)
                sys.stderr.write("  reviewer can see #%s: %s\n"
                                 % (QUEUE_NAME, p.view_channel))
            p = forum.permissions_for(everyone)
            sys.stderr.write("  @everyone can see #%s: %s  (must be False)\n"
                             % (QUEUE_NAME, p.view_channel))
        except Exception as exc:
            sys.stderr.write("FAILED: %s: %s\n" % (type(exc).__name__, exc))
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            await client.close()

    await client.start(token)
    print(json.dumps(out))
    return 0 if "error" not in out and len(out) == 2 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    cfg = config_mod.load_config(args.config)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.stderr.write("no token in %s\n" % cfg["discord"]["token_env"])
        return 2
    if not int(cfg["discord"]["guild_id"] or 0):
        sys.stderr.write("discord.guild_id must be set before creating channels\n")
        return 2
    return asyncio.run(run(cfg, token))


if __name__ == "__main__":
    sys.exit(main())
