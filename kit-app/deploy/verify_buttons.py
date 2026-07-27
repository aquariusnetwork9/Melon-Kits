"""Prove the queue card carries the right buttons at every stage of a ticket's life.

A component that Discord rejects takes the WHOLE card edit with it, so a malformed button does
not degrade -- it strips the card of every control it had. And a button that is simply never
added fails the other way: nothing errors, the card just quietly lacks it, which is how the
Claim button came to be missing without a single line in the log.

So this renders each lifecycle view onto a real forum post and reads the components back off
the message, which is the only way to see what Discord actually stored. It builds the views
through the same helper the handlers use (`_with_extras`), so a button dropped there shows up
here. It cleans up the post afterwards.

    python deploy/verify_buttons.py --config melonkit.json

Read-only with respect to the ledger apart from one throwaway ticket, which it deletes.
Run as the service user, never under sudo -- it opens the ledger. See DEPLOY.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as bot_mod          # noqa: E402
import config as config_mod    # noqa: E402
import store as store_mod      # noqa: E402


def log(msg):
    sys.stderr.write("  %s\n" % msg)


class _Shim(object):
    """Only what `_with_extras` reaches for: the ledger and the chat-row lookup."""

    def __init__(self, st):
        self.store = st

    _chat_rows = bot_mod.KitBot._chat_rows
    _with_extras = bot_mod.KitBot._with_extras


def labels(message):
    out = []
    for row in message.components:
        for child in getattr(row, "children", []):
            out.append(getattr(child, "label", "?"))
    return out


async def drive(cfg, token):
    st = store_mod.open_store(cfg["store"]["path"])
    client = discord.Client(intents=discord.Intents.default())
    results = {"ok": [], "fail": []}

    def check(name, passed, detail=""):
        (results["ok"] if passed else results["fail"]).append(name)
        log("%s %s%s" % ("PASS" if passed else "FAIL", name,
                         (" - %s" % detail) if detail else ""))

    @client.event
    async def on_ready():
        post = None
        tid = None
        try:
            gid = int(cfg["discord"]["home_guild_id"])
            queue = await client.fetch_channel(int(cfg["discord"]["queue_channel_id"]))
            shim = _Shim(st)

            # A real ticket row so `_with_extras` sees a thread id and behaves as it does live.
            tid = st.create_ticket(gid, 1, "ButtonProbe", None, "verification")
            st.set_ticket_thread(tid, 123456789012345678)
            kit_id = st.record_kit(gid, tid, 1, "ButtonProbe", None)

            def staged(view):
                return shim._with_extras(view, gid, tid)

            awaiting = discord.ui.View(timeout=None)
            awaiting.add_item(bot_mod.ApproveButton(tid))
            awaiting.add_item(bot_mod.DeclineButton(tid))
            awaiting.add_item(bot_mod.ViewConversationButton(tid))
            awaiting.add_item(bot_mod.JoinThreadButton(tid))

            approved = discord.ui.View(timeout=None)
            approved.add_item(bot_mod.ClaimButton(kit_id))

            claimed = discord.ui.View(timeout=None)
            claimed.add_item(bot_mod.DeliveredButton(kit_id))
            claimed.add_item(bot_mod.UnclaimButton(kit_id))

            created = await queue.create_thread(
                name="verify-buttons", content="button verification",
                view=awaiting)
            post = created.thread
            msg = await post.fetch_message(post.id)
            got = labels(msg)
            check("awaiting review card",
                  {"Approve", "Decline", "Read conversation", "Join applicant thread"} <= set(got), str(got))

            partial = post.get_partial_message(post.id)

            await partial.edit(content="approved", view=staged(approved))
            got = labels(await post.fetch_message(post.id))
            check("approved card keeps Claim + Join",
                  {"Claim this delivery", "Read conversation", "Join applicant thread"} <= set(got), str(got))

            await partial.edit(content="claimed", view=staged(claimed))
            got = labels(await post.fetch_message(post.id))
            check("claimed card has Delivered + Hand back + Join",
                  {"Mark delivered", "Hand back", "Read conversation", "Join applicant thread"} <= set(got),
                  str(got))

            back = discord.ui.View(timeout=None)
            back.add_item(bot_mod.ClaimButton(kit_id))
            await partial.edit(content="unclaimed", view=staged(back))
            got = labels(await post.fetch_message(post.id))
            check("handed-back card returns to Claim + Join",
                  {"Claim this delivery", "Join applicant thread"} <= set(got), str(got))

            await partial.edit(content="delivered", view=staged(None))
            got = labels(await post.fetch_message(post.id))
            check("finished card still offers Read + Join",
                  {"Read conversation", "Join applicant thread"} <= set(got), str(got))

            # `_post_queue_card` no longer hand-maintains its own copy of the trailing
            # buttons; it calls `_with_extras` like every later edit does. That substitution
            # is only correct if both inputs are already persisted when the card is posted,
            # so this reproduces that exact state: thread id written, chat lines recorded.
            fresh = st.create_ticket(gid, 2, "OrderProbe", None, "ordering")
            st.set_ticket_thread(fresh, 123456789012345679)
            st.record_shown_chats(fresh, [{"ts": "2026-01-01", "chat": "hello",
                                           "flagged": False}])
            at_post = shim._with_extras(None, gid, fresh)
            got = [type(i).__name__ for i in (at_post.children if at_post else [])]
            check("card-post state yields chat + both doors",
                  {"ChatHistoryButton", "ViewConversationButton",
                   "JoinThreadButton"} == set(got), str(got))
            st._db.execute("DELETE FROM shown_chats WHERE ticket_id=?", (fresh,))
            st._db.execute("DELETE FROM tickets WHERE id=?", (fresh,))

            # A ticket with no thread must not carry a door to nowhere.
            # Straight to SQL: set_ticket_thread takes an id, not the absence of one.
            st._db.execute("UPDATE tickets SET thread_id=NULL WHERE id=?", (tid,))
            got_view = shim._with_extras(None, gid, tid)
            check("no thread -> no thread buttons",
                  got_view is None or not any(
                      isinstance(i, (bot_mod.JoinThreadButton,
                                     bot_mod.ViewConversationButton))
                      for i in got_view.children))
        except Exception as exc:  # noqa: BLE001
            check("run completed", False, "%s: %s" % (type(exc).__name__, exc))
        finally:
            if post is not None:
                try:
                    await post.delete()
                    log("cleaned up the verification post")
                except discord.HTTPException:
                    log("WARNING could not delete post %d" % post.id)
            if tid is not None:
                st._db.execute("DELETE FROM kits WHERE ticket_id=?", (tid,))
                st._db.execute("DELETE FROM tickets WHERE id=?", (tid,))
            st.close()
            await client.close()

    await client.start(token)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="melonkit.json")
    args = ap.parse_args()
    cfg = config_mod.load_config(args.config)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.exit("no token in %s" % cfg["discord"]["token_env"])
    results = asyncio.run(drive(cfg, token))
    print("\n%d passed, %d failed" % (len(results["ok"]), len(results["fail"])))
    if results["fail"]:
        print("failed: %s" % ", ".join(results["fail"]))
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
