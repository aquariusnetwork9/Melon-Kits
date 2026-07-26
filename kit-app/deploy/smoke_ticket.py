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
import io
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


async def drive(cfg, token, mc_name, user_id, cleanup_id):
    st = store_mod.open_store(cfg["store"]["path"])
    client = discord.Client(intents=discord.Intents.default())
    result = {"ok": False}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(cfg["discord"]["guild_id"]))
            if cleanup_id:
                await do_cleanup(client, guild, st, cleanup_id)
                result["ok"] = True
                return

            panel = guild.get_channel(int(cfg["discord"]["panel_channel_id"]))
            queue = guild.get_channel(int(cfg["discord"]["queue_channel_id"]))
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
            ticket_id = st.create_ticket(member.id, canonical, uuid, "smoke test")
            log("ticket #%d created in the ledger" % ticket_id)

            # 3. THE RISKY ONE: a private thread, with the requester added
            thread = await panel.create_thread(
                name="kit-%d-%s" % (ticket_id, canonical[:20]),
                type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=10080)
            await thread.add_user(member)
            st.set_ticket_thread(ticket_id, thread.id)
            log("PRIVATE thread created: #%s (%s) and requester added"
                % (thread.name, thread.type))
            await thread.send(
                "Smoke test for request **#%d** (`%s`). This is what an applicant sees - "
                "a receipt, and nothing from the reviewer card." % (ticket_id, canonical))

            # 4. the card, from live 2b2t data -- and deliberately through
            #    run_in_executor, exactly as the handler does. Calling gather directly on
            #    this thread is what let the store's cross-thread ProgrammingError reach
            #    production with a green smoke test, so the executor is load-bearing here
            #    rather than incidental.
            lex = screening.Lexicon.load(cfg["screening"]["lexicon_path"])
            loop = asyncio.get_running_loop()
            built = await loop.run_in_executor(None, lambda: card_mod.gather(
                canonical, uuid, member.id, cfg, vc_mod.Client(cfg), st, lex))
            log("card built: tracked=%s deaths=%d chats=%d coords_redacted=%d errors=%d"
                % (built["tracked"], len(built["deaths"]), len(built["chat_lines"]),
                   built["coords_redacted"], len(built["errors"])))
            st.record_shown_chats(ticket_id, built["chat_lines"])

            # 5. THE OTHER RISKY ONE: forum post with tags, embed and an attachment
            view = discord.ui.View(timeout=None)
            view.add_item(bot_mod.ApproveButton(ticket_id))
            view.add_item(bot_mod.DeclineButton(ticket_id))
            files = []
            if built["chat_lines"]:
                files.append(discord.File(
                    io.BytesIO(card_mod.chat_dump(built).encode("utf-8")),
                    filename="chat-%s-ticket%d.txt" % (canonical, ticket_id)))
            tags = bot_mod._state_tags(queue, "awaiting review") \
                if isinstance(queue, discord.ForumChannel) else []
            created = await queue.create_thread(
                name="#%d %s" % (ticket_id, canonical),
                content="Ticket #%d - smoke test, requested by %s\nApplicant thread: <#%d>"
                        % (ticket_id, member.mention, thread.id),
                embed=bot_mod._embed_for(built, cfg), view=view, files=files,
                applied_tags=tags,
                allowed_mentions=discord.AllowedMentions(users=True))
            st.set_queue_thread(ticket_id, created.thread.id)
            log("FORUM post created: %s  tags=%s  attachment=%s"
                % (created.thread.name, [t.name for t in tags], bool(files)))

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
    ap.add_argument("--cleanup", type=int, default=0, metavar="TICKET_ID",
                    help="delete a smoke ticket's thread, post and ledger rows")
    args = ap.parse_args(argv)

    cfg = config_mod.load_config(args.config)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.stderr.write("no token in %s\n" % cfg["discord"]["token_env"])
        return 2
    if not args.cleanup and not (args.name and args.user):
        sys.stderr.write("need --name and --user (or --cleanup TICKET_ID)\n")
        return 2

    out = asyncio.run(drive(cfg, token, args.name, args.user, args.cleanup))
    if out.get("ok"):
        if out.get("ticket"):
            print("ticket=%s thread=%s post=%s" % (out["ticket"], out["thread"], out["post"]))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
