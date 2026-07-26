"""Exercise the Discord operations a real ticket performs, without a button press.

A button press cannot be automated (it needs a user account), so this proves the parts a
press would fail on: creating a **private** thread and adding a member, building a card from
live 2b2t data, and opening a **forum post** with tags, an embed and a file attachment -- all
under the real channel overwrites. If those work here they will work from the handler, which
is thin wrapping around exactly these calls.

    python deploy/smoke_ticket.py --config melonkit.json --name <MCNAME> --user <DISCORD_ID>
    python deploy/smoke_ticket.py --config melonkit.json --cleanup <TICKET_ID>

What it deliberately does NOT test: the modal, and the Approve/Decline/Claim buttons. Those
need a human. The buttons it posts are the real ones, with the real template custom_ids, so
the already-running service handles them -- press Approve on the post this creates and the
rest of the lifecycle runs for real.

Run it as the service user, never under sudo: it opens the ledger, and a root-owned ledger
leaves the service able to read but not write it. See DEPLOY.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as bot_mod          # noqa: E402
import card as card_mod        # noqa: E402
import config as config_mod    # noqa: E402
import identity                # noqa: E402
import screening               # noqa: E402
import store as store_mod      # noqa: E402
import vc as vc_mod            # noqa: E402


def log(msg):
    sys.stderr.write("  %s\n" % msg)


async def drive(cfg, token, mc_name, user_id, cleanup_id, kind=store_mod.KIND_RESCUE):
    st = store_mod.open_store(cfg["store"]["path"])
    client = discord.Client(intents=discord.Intents.default())
    result = {"ok": False}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(cfg["discord"]["home_guild_id"]))
            if cleanup_id:
                await do_cleanup(client, guild, st, cleanup_id)
                result["ok"] = True
                return

            g = st.get_guild_config(guild.id)
            if not g.get("panel_channel_id"):
                log("guild %s has not been set up -- run /setup in Discord first" % guild.id)
                return
            panel = guild.get_channel(int(g["panel_channel_id"]))
            queue = guild.get_channel(int(g["queue_channel_id"]))
            log("panel: #%s (%s)" % (panel.name, type(panel).__name__))
            log("queue: #%s (%s)" % (queue.name, type(queue).__name__))

            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            log("requester: %s" % member)

            # 1. identity -- also off the event loop, as the handler does it
            resolved = await asyncio.get_running_loop().run_in_executor(
                None, lambda: identity.resolve(mc_name, cfg))
            uuid, canonical = resolved["uuid"], resolved["name"]
            log("resolved %s -> %s" % (canonical, uuid))

            # 2. ledger row
            ticket_id = st.create_ticket(guild.id, member.id, canonical, uuid,
                                         "smoke test", request_type=kind)
            log("ticket #%d created in the ledger (%s)"
                % (ticket_id, store_mod.KIND_LABEL[kind]))

            # 3. THE RISKY ONE: a private thread, with the requester added
            thread = await panel.create_thread(
                name="kit-%d-%s" % (ticket_id, canonical[:20]),
                type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=10080)
            await thread.add_user(member)
            st.set_ticket_thread(ticket_id, thread.id)
            log("PRIVATE thread created: #%s (%s) and requester added"
                % (thread.name, thread.type))
            # The REAL receipt, wording and all, plus the guides embed a rescue gets. A
            # placeholder here would have made this the one thing the smoke test could not
            # actually show: what the applicant reads.
            await thread.send(
                "Thanks %s - this is request **#%d**, for **%s**, on `%s`.\n\n"
                "A reviewer will look at it when someone's around. Help here is voluntary, so "
                "there's no queue position to give you, and it isn't guaranteed. You don't need "
                "to do anything else - if it's approved, whoever is delivering will message you "
                "in this thread to sort out where to meet."
                % (member.mention, ticket_id, store_mod.KIND_LABEL[kind], canonical),
                embed=bot_mod.guides_embed() if kind == store_mod.KIND_RESCUE else None)

            # 4. the card, from live 2b2t data -- and deliberately through
            #    run_in_executor, exactly as the handler does. Calling gather directly on
            #    this thread is what let the store's cross-thread ProgrammingError reach
            #    production with a green smoke test, so the executor is load-bearing here
            #    rather than incidental.
            lex = screening.Lexicon.load(cfg["screening"]["lexicon_path"])
            loop = asyncio.get_running_loop()
            # request_type and ticket_id both matter: without them this builds a LOOKUP card --
            # no request clock, cooldown scoped to any grant -- which is not what a reviewer
            # will actually be looking at.
            built = await loop.run_in_executor(None, lambda: card_mod.gather(
                guild.id, canonical, uuid, member.id, cfg, vc_mod.Client(cfg), st, lex,
                request_type=kind, ticket_id=ticket_id))
            rec = card_mod.recommend(built)
            log("card built: tracked=%s deaths=%d chats=%d coords_redacted=%d errors=%d"
                % (built["tracked"], len(built["deaths"]), len(built["chat_lines"]),
                   built["coords_redacted"], len(built["errors"])))
            log("recommendation: %s (%d rule(s)) -- %s"
                % (rec["call"].upper(), len(rec["rules"]),
                   card_mod.chat_coverage_phrase(built)))
            st.record_shown_chats(ticket_id, built["chat_lines"])

            # 5. THE OTHER RISKY ONE: forum post with tags, embed and an attachment
            view = discord.ui.View(timeout=None)
            view.add_item(bot_mod.ApproveButton(ticket_id))
            view.add_item(bot_mod.DeclineButton(ticket_id))
            # The pager replaced the .txt attachment. Posting a file here would test a path the
            # handler no longer takes and skip the one it does.
            if built["chat_lines"]:
                view.add_item(bot_mod.ChatHistoryButton(ticket_id))
            tags = []
            if isinstance(queue, discord.ForumChannel):
                tags = (bot_mod._state_tags(queue, store_mod.KIND_LABEL[kind])
                        + bot_mod._state_tags(queue, "awaiting review"))
            created = await queue.create_thread(
                name="#%d %s - %s" % (ticket_id, canonical, store_mod.KIND_LABEL[kind]),
                content="Ticket #%d - **%s** requested by %s\nApplicant thread: <#%d>"
                        % (ticket_id, store_mod.KIND_LABEL[kind], member.mention, thread.id),
                embed=bot_mod._embed_for(built, cfg), view=view,
                applied_tags=tags,
                allowed_mentions=discord.AllowedMentions(users=True))
            st.set_queue_thread(ticket_id, created.thread.id)
            log("FORUM post created: %s  tags=%s  chat button=%s"
                % (created.thread.name, [t.name for t in tags], bool(built["chat_lines"])))

            # 6. verify the applicant genuinely cannot see the queue
            everyone = guild.default_role
            log("@everyone can view the queue: %s (must be False)"
                % queue.permissions_for(everyone).view_channel)
            log("requester can view the queue: %s (must be False unless they are staff)"
                % queue.permissions_for(member).view_channel)

            result.update(ok=True, ticket=ticket_id, thread=thread.id,
                          post=created.thread.id)
        except Exception as exc:
            import traceback
            log("FAILED: %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc(file=sys.stderr)
        finally:
            st.close()
            await client.close()

    await client.start(token)
    return result


async def do_cleanup(client, guild, st, ticket_id):
    row = st.get_ticket(ticket_id)
    if row is None:
        log("no ticket #%d" % ticket_id)
        return
    for label, cid in (("applicant thread", row["thread_id"]),
                       ("queue post", row["queue_thread_id"])):
        if not cid:
            continue
        try:
            ch = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
            await ch.delete()
            log("deleted %s %s" % (label, cid))
        except discord.HTTPException as exc:
            log("could not delete %s %s: %s" % (label, cid, exc))
    st._db.execute("DELETE FROM shown_chats WHERE ticket_id=?", (ticket_id,))
    st._db.execute("DELETE FROM kits WHERE ticket_id=?", (ticket_id,))
    st._db.execute("DELETE FROM decisions WHERE ticket_id=?", (ticket_id,))
    st._db.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
    log("ledger rows for ticket #%d removed" % ticket_id)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--name", help="Minecraft username to build the card from")
    ap.add_argument("--user", type=int, help="Discord user id to treat as the requester")
    ap.add_argument("--kind", default=store_mod.KIND_RESCUE, choices=list(store_mod.KINDS),
                    help="which sort of request to file (default rescue)")
    ap.add_argument("--cleanup", type=int, default=0, metavar="TICKET_ID",
                    help="delete a smoke ticket's thread, post and ledger rows")
    args = ap.parse_args(argv)

    cfg = config_mod.load_config(args.config)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.stderr.write("no token in %s\n" % cfg["discord"]["token_env"])
        return 2
    if not int(cfg["discord"]["home_guild_id"] or 0):
        sys.stderr.write("set discord.home_guild_id to the guild to smoke-test\n")
        return 2
    if not args.cleanup and not (args.name and args.user):
        sys.stderr.write("need --name and --user (or --cleanup TICKET_ID)\n")
        return 2

    out = asyncio.run(drive(cfg, token, args.name, args.user, args.cleanup, args.kind))
    if out.get("ok"):
        if out.get("ticket"):
            print("ticket=%s thread=%s post=%s" % (out["ticket"], out["thread"], out["post"]))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
