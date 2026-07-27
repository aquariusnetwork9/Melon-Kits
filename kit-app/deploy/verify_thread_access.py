"""Prove a claimed delivery really does put the runner in the applicant's thread.

Ticket threads are private and `invitable=False`. Reviewers see them through Manage Threads on
the parent channel; the delivery team deliberately does not have that, so being added on claim
is the only way a delivery-only member can talk to the applicant. That add is one API call in
`KitBot._admit_to_thread`, and if it silently fails the symptom is invisible from the outside:
the applicant is told who is coming, and that person never appears.

So this drives the **real helpers**: `_resolve_thread`, `_admit_to_thread` and
`_revoke_from_thread` are grafted onto a shim that supplies the two client lookups they use,
so the shipping code runs here including its internal `self.` hops -- not a re-implementation
of what it is supposed to do. It runs against the real panel channel under the real
overwrites, and it cleans up after itself.

    python deploy/verify_thread_access.py --config melonkit.json [--user <DISCORD_ID>]

With no --user it adds the guild owner, which proves the call succeeds under the bot's
permissions. To prove the *visibility* half as well, pass a test account that holds only the
delivery role and check whether the thread appears for it.

Run it as the service user, never under sudo -- it does not open the ledger, but keeping the
habit is what stops a root-owned ledger. See DEPLOY.md.
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


def log(msg):
    sys.stderr.write("  %s\n" % msg)


class _Shim(object):
    """The two client lookups the helpers use, and nothing else.

    Deliberately not a KitBot: constructing one opens the ledger, and this script has no
    business touching it. The helpers themselves are grafted on below rather than copied, so
    what runs here is the shipping code including its internal `self._resolve_thread` hop.
    """

    def __init__(self, client):
        self._client = client

    def get_channel(self, cid):
        return self._client.get_channel(cid)

    async def fetch_channel(self, cid):
        return await self._client.fetch_channel(cid)


_Shim._resolve_thread = bot_mod.KitBot._resolve_thread
_Shim._admit_to_thread = bot_mod.KitBot._admit_to_thread
_Shim._revoke_from_thread = bot_mod.KitBot._revoke_from_thread


async def drive(cfg, token, user_id):
    client = discord.Client(intents=discord.Intents.default())
    results = {"ok": [], "fail": []}

    def check(name, passed, detail=""):
        (results["ok"] if passed else results["fail"]).append(name)
        log("%s %s%s" % ("PASS" if passed else "FAIL", name,
                         (" - %s" % detail) if detail else ""))

    @client.event
    async def on_ready():
        thread = None
        try:
            gid = int(cfg["discord"]["home_guild_id"] or cfg["discord"].get("guild_id") or 0)
            guild = client.get_guild(gid) or await client.fetch_guild(gid)
            panel_id = int(cfg["discord"]["panel_channel_id"])
            channel = client.get_channel(panel_id) or await client.fetch_channel(panel_id)
            target_id = user_id or guild.owner_id
            log("guild=%s panel=#%s target_user=%s" % (guild.id, channel.name, target_id))

            # Same shape as a real ticket thread: private, uninvitable.
            thread = await channel.create_thread(
                name="verify-thread-access",
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=60)
            check("private thread created", True, "id=%d" % thread.id)

            shim = _Shim(client)
            ticket = {"id": 0, "thread_id": thread.id, "discord_user_id": 0}

            resolved = await shim._resolve_thread(ticket)
            check("_resolve_thread finds it", resolved is not None
                  and resolved.id == thread.id)

            member = discord.Object(id=int(target_id))
            admitted = await shim._admit_to_thread(ticket, member)
            check("_admit_to_thread returns True", admitted)

            ids = {m.id for m in await thread.fetch_members()}
            check("runner is a thread member", int(target_id) in ids,
                  "members=%s" % sorted(ids))

            # The guard that matters: the holder can be the applicant when no delivery role is
            # set, and evicting them from their own ticket would be the worst bug in the file.
            self_ticket = {"id": 0, "thread_id": thread.id,
                           "discord_user_id": int(target_id)}
            await shim._revoke_from_thread(self_ticket, int(target_id))
            ids = {m.id for m in await thread.fetch_members()}
            check("applicant is NOT removed by revoke", int(target_id) in ids)

            await shim._revoke_from_thread(ticket, int(target_id))
            ids = {m.id for m in await thread.fetch_members()}
            check("_revoke_from_thread removes a runner", int(target_id) not in ids,
                  "members=%s" % sorted(ids))

            # A thread the cache has never seen is the restart case: resolution must still
            # work. Evicting it reaches into discord.py internals, so a version that moved
            # them skips the check rather than reporting a failure that isn't ours.
            evicted = False
            try:
                client._connection._threads.pop(thread.id, None)
                evicted = client.get_channel(thread.id) is None
            except AttributeError:
                pass
            if evicted:
                refetched = await shim._resolve_thread(
                    {"id": 0, "thread_id": thread.id, "discord_user_id": 0})
                check("_resolve_thread survives a cold cache", refetched is not None,
                      "fetched over REST")
            else:
                log("SKIP cold-cache check - could not evict the thread from cache")
        except Exception as exc:  # noqa: BLE001 - report, never hang the client
            check("run completed", False, "%s: %s" % (type(exc).__name__, exc))
        finally:
            if thread is not None:
                try:
                    await thread.delete()
                    log("cleaned up the verification thread")
                except discord.HTTPException:
                    log("WARNING could not delete thread %d - remove it by hand" % thread.id)
            await client.close()

    await client.start(token)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="melonkit.json")
    ap.add_argument("--user", type=int, default=0,
                    help="Discord id to admit; defaults to the guild owner")
    args = ap.parse_args()

    cfg = config_mod.load_config(args.config)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.exit("no token in %s" % cfg["discord"]["token_env"])

    results = asyncio.run(drive(cfg, token, args.user))
    print("\n%d passed, %d failed" % (len(results["ok"]), len(results["fail"])))
    if results["fail"]:
        print("failed: %s" % ", ".join(results["fail"]))
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
