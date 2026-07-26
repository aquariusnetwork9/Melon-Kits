"""The Discord app: panel -> ticket -> review -> dispatch.

Run with ``python bot.py --config melonkit.json``. Requires discord.py 2.4+ (for
``DynamicItem``); everything else in this directory is standard library only and importable
without it, which is what lets the logic be tested without a Discord connection.

Three implementation notes that are decisions, not incidentals:

- **Pre-checks run before the modal opens, not after it is submitted.** Making somebody fill
  in a form and only then telling them they were never eligible is the one avoidable bad
  experience in this flow. The checks are local SQLite, so they comfortably fit inside
  Discord's 3-second initial-response deadline.
- **Every ``custom_id`` is static or template-derived**, so buttons keep working across
  restarts and redeploys forever. A panel pinned once should still work in a year without
  anybody re-posting it.
- **Components V2 is not used.** Its message flag is irreversible once sent, and the panel is
  the one message here intended to live indefinitely and stay editable.

Chat text reaches Discord only as the attached, already-redacted log. It never reaches a log
record: diagnostics carry ticket ids, user ids and counts.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
import traceback
from typing import Any, Dict, Optional

import discord
from discord import app_commands

import card as card_mod
import config as config_mod
import identity
import screening
import store as store_mod
import vc as vc_mod

LOG = logging.getLogger("melonkit")

PANEL_CUSTOM_ID = "melonkit:request:v1"

PANEL_TITLE = "\U0001F348 Melon Men - Kit Requests"
PANEL_BODY = (
    "We hand out barebones diamond kits to players just getting started on 2b2t, and to "
    "anyone who's just lost everything.\n"
    "\n"
    "- A kit is never guaranteed.\n"
    "- We can't promise a waiting time.\n"
    "- We check a few things first. Requests usually get declined for one of these:\n"
    "  - the account looks like an alt of someone we've already helped\n"
    "  - public chat history we aren't willing to overlook\n"
    "  - you've had a kit from us in the last {cooldown} days\n"
    "\n"
    "Helping is voluntary and best-effort. Press the button below and we'll take a look."
)


# --------------------------------------------------------------------------- helpers

def is_reviewer(member: Any, cfg: Dict[str, Any]) -> bool:
    role_id = int(cfg["discord"]["reviewer_role_id"] or 0)
    if not role_id:
        return bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False))
    return any(r.id == role_id for r in getattr(member, "roles", []))


def may_claim(member: Any, cfg: Dict[str, Any]) -> bool:
    role_id = int(cfg["discord"]["runner_role_id"] or 0)
    if not role_id:
        return True
    return any(r.id == role_id for r in getattr(member, "roles", []))


# Dispatch state, expressed as forum tags when the dispatch channel is a forum. Tag names
# are matched case-insensitively against whatever the channel already has, so an operator can
# rename or restyle them without touching code -- a missing tag is skipped rather than being
# an error, because a forum with no tags configured must still work.
DISPATCH_TAGS = ("unclaimed", "claimed", "delivered")


def _state_tags(forum: "discord.ForumChannel", state: str) -> list:
    want = state.casefold()
    return [t for t in forum.available_tags if t.name.casefold() == want]


async def _set_dispatch_state(channel: Any, state: str) -> None:
    """Move a forum post's tags to `state`. No-op for a text-channel dispatch."""
    if not isinstance(channel, discord.Thread):
        return
    parent = channel.parent
    if not isinstance(parent, discord.ForumChannel):
        return
    tags = _state_tags(parent, state)
    if not tags:
        return
    try:
        await channel.edit(applied_tags=tags)
    except discord.HTTPException as exc:
        # Cosmetic. A dispatch whose tag is stale is still a dispatch, and failing the
        # claim over it would be worse than a wrong label.
        LOG.warning("could not set dispatch tag state=%s thread=%s status=%s",
                    state, channel.id, getattr(exc, "status", "?"))


def panel_embed(cfg: Dict[str, Any]) -> discord.Embed:
    """The pinned panel. Shared by /panel and --post-panel so the copy cannot drift."""
    return discord.Embed(
        title=PANEL_TITLE,
        description=PANEL_BODY.format(cooldown=cfg["policy"]["cooldown_days"]),
        colour=discord.Colour.from_str("#2E6B3F"))


def _embed_for(card: Dict[str, Any], cfg: Dict[str, Any]) -> discord.Embed:
    colour = discord.Colour.from_str("#2E6B3F")
    if card["cooldown"]["blocked"] or card["flags"]:
        colour = discord.Colour.from_str("#8A6D1F")
    embed = discord.Embed(
        title="Kit request - %s" % card["mc_name"],
        description=card_mod.headline(card),
        colour=colour)
    for section in card_mod.sections(card):
        # Discord caps a field value at 1024 characters.
        value = section["value"]
        if len(value) > 1024:
            value = value[:1000].rsplit("\n", 1)[0] + "\n... (truncated)"
        embed.add_field(name=section["name"], value=value or "-", inline=False)
    embed.set_footer(text="UUID %s" % (card["mc_uuid"] or "unresolved"))
    return embed


# --------------------------------------------------------------------------- views

class RequestModal(discord.ui.Modal, title="Request a kit"):
    mc_name = discord.ui.TextInput(
        label="Your Minecraft username",
        placeholder="exactly as it appears in game",
        min_length=3, max_length=16, required=True)
    note = discord.ui.TextInput(
        label="Anything we should know? (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. just got killed on the highway and lost everything",
        max_length=500, required=False)

    def __init__(self, app: "KitBot") -> None:
        super().__init__(timeout=600)
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # The five API calls take a few seconds, well past the 3s deadline, so acknowledge
        # first and keep working behind the 15-minute follow-up token.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.app.handle_request(interaction, str(self.mc_name), str(self.note or ""))

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception) -> None:      # pragma: no cover - Discord runtime
        LOG.error("request modal failed user=%s err=%s", interaction.user.id,
                  type(error).__name__)
        await _safe_followup(interaction,
                             "Something broke on our side. Nothing was recorded - please "
                             "try again in a minute.")


class PanelView(discord.ui.View):
    """The pinned panel. Static custom_id, never times out, survives restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Request a kit", emoji="\U0001F348",
                       style=discord.ButtonStyle.success, custom_id=PANEL_CUSTOM_ID)
    async def request(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_panel_click(interaction)


class ApproveButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"melonkit:approve:(?P<ticket>\d+)"):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success,
            custom_id="melonkit:approve:%d" % ticket_id))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "ApproveButton":
        return cls(int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_decision(interaction, self.ticket_id, approve=True)


class DeclineButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"melonkit:decline:(?P<ticket>\d+)"):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.danger,
            custom_id="melonkit:decline:%d" % ticket_id))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "DeclineButton":
        return cls(int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can decide a request.", ephemeral=True)
            return
        await interaction.response.send_modal(DeclineModal(app, self.ticket_id))


class DeclineModal(discord.ui.Modal, title="Decline this request"):
    reason = discord.ui.TextInput(
        label="Reason (required)",
        style=discord.TextStyle.paragraph,
        placeholder="Written for whoever reads the ledger in a year - "
                    "\"known alt of <name>\" beats \"no\".",
        min_length=3, max_length=400, required=True)

    def __init__(self, app: "KitBot", ticket_id: int) -> None:
        super().__init__(timeout=600)
        self.app = app
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.app.handle_decision(interaction, self.ticket_id, approve=False,
                                       reason=str(self.reason))


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"melonkit:claim:(?P<kit>\d+)"):
    def __init__(self, kit_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Claim this delivery", style=discord.ButtonStyle.primary,
            custom_id="melonkit:claim:%d" % kit_id))
        self.kit_id = kit_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "ClaimButton":
        return cls(int(match["kit"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_claim(interaction, self.kit_id)


class DeliveredButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"melonkit:delivered:(?P<kit>\d+)"):
    def __init__(self, kit_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Mark delivered", style=discord.ButtonStyle.secondary,
            custom_id="melonkit:delivered:%d" % kit_id))
        self.kit_id = kit_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "DeliveredButton":
        return cls(int(match["kit"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_delivered(interaction, self.kit_id)


async def _safe_followup(interaction: discord.Interaction, text: str) -> None:
    """Reply once, whichever half of the interaction lifecycle we are in."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:                    # pragma: no cover - Discord runtime
        LOG.warning("could not deliver a reply to user=%s", interaction.user.id)


# ----------------------------------------------------------------------------- bot

class KitBot(discord.Client):
    def __init__(self, cfg: Dict[str, Any], post_panel_to: int = 0) -> None:
        # No privileged intents are needed for the request flow itself. MESSAGE_CONTENT is
        # only required by the optional Discord-history lookup, so it is not demanded here:
        # a bot that cannot start without a privileged intent it barely uses is a bot that
        # does not start.
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        # One-shot deploy mode: post the panel to this channel, then exit. Lets a deployment
        # finish without a human having to run /panel in a client.
        self._post_panel_to = int(post_panel_to or 0)
        self.tree = app_commands.CommandTree(self)
        self.store = store_mod.open_store(cfg["store"]["path"])
        self.vc = vc_mod.Client(cfg, LOG)
        self.lex = screening.Lexicon({})
        path = cfg["screening"]["lexicon_path"]
        if path:
            try:
                self.lex = screening.Lexicon.load(path)
                LOG.info("lexicon loaded terms=%d categories=%d",
                         self.lex.term_count, len(self.lex.categories))
            except (OSError, ValueError) as exc:
                LOG.error("lexicon at %s could not be loaded (%s); continuing with none - "
                          "chat will still be listed, just not counted", path, exc)

    async def setup_hook(self) -> None:
        self.add_view(PanelView())
        for item in (ApproveButton, DeclineButton, ClaimButton, DeliveredButton):
            self.add_dynamic_items(item)
        register_commands(self)
        guild_id = int(self.cfg["discord"]["guild_id"] or 0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("commands synced to guild=%d", guild_id)
        else:
            await self.tree.sync()
            LOG.info("commands synced globally (set discord.guild_id for instant updates)")

    async def on_ready(self) -> None:
        LOG.info("connected as %s (%s)", self.user, getattr(self.user, "id", "?"))
        if self._post_panel_to:
            await self._deploy_panel()

    async def _deploy_panel(self) -> None:
        channel = self.get_channel(self._post_panel_to)
        try:
            if channel is None:
                LOG.error("panel channel %d not visible to the bot -- check it can View "
                          "Channels there", self._post_panel_to)
                return
            if isinstance(channel, discord.ForumChannel):
                LOG.error("panel channel %d is a forum. A forum cannot hold a standalone "
                          "message, and forum posts cannot be private -- which would make "
                          "every applicant's reviewer card readable by everyone. Use a text "
                          "channel.", self._post_panel_to)
                return
            msg = await channel.send(embed=panel_embed(self.cfg), view=PanelView())
            LOG.info("panel posted channel=%d message=%d", channel.id, msg.id)
            try:
                await msg.pin()
                LOG.info("panel pinned")
            except discord.HTTPException as exc:
                LOG.warning("panel posted but not pinned (needs Manage Messages): status=%s",
                            getattr(exc, "status", "?"))
        finally:
            await self.close()

    # ------------------------------------------------------------- panel -> modal

    async def handle_panel_click(self, interaction: discord.Interaction) -> None:
        """Pre-check, THEN show the form. Never the other way round."""
        user_id = interaction.user.id
        pol = self.cfg["policy"]

        open_n = self.store.open_ticket_count(user_id)
        if open_n >= int(pol["max_open_tickets_per_user"]):
            existing = self.store.open_ticket_for(user_id)
            where = ""
            if existing and existing["thread_id"]:
                where = " Here: <#%d>" % int(existing["thread_id"])
            await interaction.response.send_message(
                "You already have a request open, so there's nothing more to do for now.%s"
                % where, ephemeral=True)
            return

        cd = self.store.cooldown(pol["cooldown_days"], discord_user_id=user_id)
        if cd["blocked"]:
            await interaction.response.send_message(
                "You had a kit from us recently, so you're inside the %d-day cooldown - "
                "about %d day(s) to go. This is to spread kits around rather than anything "
                "against you." % (pol["cooldown_days"], cd["days_left"]), ephemeral=True)
            return

        await interaction.response.send_modal(RequestModal(self))

    # ---------------------------------------------------------------- the ticket

    async def handle_request(self, interaction: discord.Interaction,
                             mc_name: str, note: str) -> None:
        user = interaction.user
        mc_name = mc_name.strip()

        if not identity.valid_name(mc_name):
            await interaction.followup.send(
                "`%s` isn't a valid Minecraft username (3-16 characters, letters, digits "
                "and underscore). Nothing was submitted - press the button again."
                % mc_name[:32], ephemeral=True)
            return

        loop = asyncio.get_running_loop()

        # Identity first: the UUID is what the ledger and any flag key on, so a rename
        # cannot detach them later.
        resolved: Optional[Dict[str, Optional[str]]] = None
        if self.cfg["identity"]["resolve_uuid"]:
            try:
                resolved = await loop.run_in_executor(
                    None, lambda: identity.resolve(mc_name, self.cfg, LOG))
            except identity.UnknownPlayer:
                await interaction.followup.send(
                    "There's no Minecraft account named `%s`. Check the spelling and press "
                    "the button again - nothing was submitted." % mc_name[:32],
                    ephemeral=True)
                return
            except identity.IdentityError as exc:
                LOG.warning("uuid resolution failed user=%s err=%s", user.id, exc)
                # Non-fatal: proceed on the name, and say so on the card.
                resolved = None

        mc_uuid = (resolved or {}).get("uuid")
        canonical = (resolved or {}).get("name") or mc_name

        ticket_id = self.store.create_ticket(user.id, canonical, mc_uuid, note or None)
        LOG.info("ticket opened ticket=%d user=%s uuid=%s", ticket_id, user.id,
                 mc_uuid or "unresolved")

        thread = await self._open_thread(interaction, ticket_id, canonical)
        if thread is not None:
            self.store.set_ticket_thread(ticket_id, thread.id)

        built = await loop.run_in_executor(None, lambda: card_mod.gather(
            canonical, mc_uuid, user.id, self.cfg, self.vc, self.store, self.lex, LOG))

        # Instrumentation: the lines shown, stored redacted. Impossible to backfill.
        try:
            self.store.record_shown_chats(ticket_id, built["chat_lines"])
        except Exception:
            LOG.error("could not record shown chats ticket=%d", ticket_id)

        target = thread or interaction.channel
        view = discord.ui.View(timeout=None)
        view.add_item(ApproveButton(ticket_id))
        view.add_item(DeclineButton(ticket_id))

        files = []
        if built["chat_lines"]:
            files.append(discord.File(
                io.BytesIO(card_mod.chat_dump(built).encode("utf-8")),
                filename="chat-%s-ticket%d.txt" % (canonical, ticket_id)))

        role_id = int(self.cfg["discord"]["reviewer_role_id"] or 0)
        mention = "<@&%d>" % role_id if role_id else ""
        try:
            await target.send(
                content="%s Ticket #%d - requested by %s" % (mention, ticket_id, user.mention),
                embed=_embed_for(built, self.cfg), view=view, files=files,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        except discord.HTTPException as exc:
            LOG.error("could not post reviewer card ticket=%d status=%s",
                      ticket_id, getattr(exc, "status", "?"))
            await interaction.followup.send(
                "Your request was recorded as #%d, but I couldn't post the review card. "
                "A reviewer will need to look at it manually." % ticket_id, ephemeral=True)
            return

        where = "<#%d>" % thread.id if thread is not None else "this channel"
        await interaction.followup.send(
            "Request #%d is in. A reviewer will pick it up in %s when someone's around. "
            "Help is voluntary, so there's no queue position to give you - but you don't "
            "need to do anything else." % (ticket_id, where), ephemeral=True)

    async def _open_thread(self, interaction: discord.Interaction, ticket_id: int,
                           mc_name: str) -> Optional[discord.Thread]:
        channel = interaction.channel
        panel_id = int(self.cfg["discord"]["panel_channel_id"] or 0)
        if panel_id:
            found = self.get_channel(panel_id)
            if isinstance(found, discord.TextChannel):
                channel = found
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            thread = await channel.create_thread(
                name="kit-%d-%s" % (ticket_id, mc_name[:20]),
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=10080)
            await thread.add_user(interaction.user)
            return thread
        except discord.HTTPException as exc:
            # Most likely the ~1000 active-thread guild ceiling, or missing permissions.
            # Falling back to the channel keeps the request alive rather than losing it.
            LOG.error("thread creation failed ticket=%d status=%s", ticket_id,
                      getattr(exc, "status", "?"))
            return None

    # -------------------------------------------------------------- the decision

    async def handle_decision(self, interaction: discord.Interaction, ticket_id: int,
                              approve: bool, reason: Optional[str] = None) -> None:
        if not is_reviewer(interaction.user, self.cfg):
            await _safe_followup(interaction, "Only reviewers can decide a request.")
            return

        ticket = self.store.get_ticket(ticket_id)
        if ticket is None:
            await _safe_followup(interaction, "Ticket #%d no longer exists." % ticket_id)
            return
        if ticket["status"] != store_mod.STATUS_OPEN:
            await _safe_followup(
                interaction,
                "Ticket #%d was already **%s** - two reviewers probably opened it at once."
                % (ticket_id, ticket["status"]))
            return

        outcome = store_mod.STATUS_APPROVED if approve else store_mod.STATUS_DECLINED
        self.store.record_decision(ticket_id, interaction.user.id, outcome, reason)
        LOG.info("ticket decided ticket=%d outcome=%s reviewer=%s",
                 ticket_id, outcome, interaction.user.id)

        if not approve:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Ticket #%d declined. Reason recorded." % ticket_id, ephemeral=True)
            await self._notify_thread(
                ticket, "Ticket #%d was **declined**.\n> %s" % (ticket_id, reason or ""))
            return

        kit_id = self.store.record_kit(ticket_id, int(ticket["discord_user_id"]),
                                       str(ticket["mc_name"]), ticket["mc_uuid"])
        posted = await self._post_dispatch(ticket, kit_id)
        await _safe_followup(
            interaction,
            "Ticket #%d approved and dispatch #%d %s." % (
                ticket_id, kit_id,
                "posted" if posted else "recorded (couldn't reach the dispatch channel)"))
        await self._notify_thread(
            ticket,
            "Ticket #%d is **approved**. Someone will claim the delivery and get in touch. "
            "Times aren't guaranteed - everyone here is a volunteer." % ticket_id)

    async def _notify_thread(self, ticket: Any, text: str) -> None:
        thread_id = ticket["thread_id"]
        if not thread_id:
            return
        channel = self.get_channel(int(thread_id))
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.HTTPException:
            LOG.warning("could not post to thread=%s", thread_id)

    # -------------------------------------------------------------- the dispatch

    async def _post_dispatch(self, ticket: Any, kit_id: int) -> bool:
        channel_id = int(self.cfg["discord"]["dispatch_channel_id"] or 0)
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None:
            LOG.error("dispatch channel %s unavailable, kit=%d recorded anyway",
                      channel_id, kit_id)
            return False
        embed = discord.Embed(
            title="Kit dispatch #%d" % kit_id,
            description="**%s** - approved from ticket #%d.\nUnclaimed."
                        % (ticket["mc_name"], ticket["id"]),
            colour=discord.Colour.from_str("#8CBF5E"))
        embed.set_footer(text="UUID %s" % (ticket["mc_uuid"] or "unresolved"))
        view = discord.ui.View(timeout=None)
        view.add_item(ClaimButton(kit_id))
        try:
            if isinstance(channel, discord.ForumChannel):
                # A forum is the better home for this: each dispatch is a post, and the tags
                # below make the queue filterable instead of scrollable. Note you cannot
                # `send` to a forum at all -- a post IS a thread, so this is create_thread.
                await channel.create_thread(
                    name="kit #%d - %s" % (kit_id, ticket["mc_name"]),
                    embed=embed, view=view,
                    applied_tags=_state_tags(channel, "unclaimed"))
            else:
                await channel.send(embed=embed, view=view)
            return True
        except discord.HTTPException as exc:
            LOG.error("dispatch post failed kit=%d status=%s", kit_id,
                      getattr(exc, "status", "?"))
            return False

    async def handle_claim(self, interaction: discord.Interaction, kit_id: int) -> None:
        if not may_claim(interaction.user, self.cfg):
            await interaction.response.send_message(
                "You're not on the delivery team for this.", ephemeral=True)
            return
        kit = self.store.get_kit(kit_id)
        if kit is None:
            await interaction.response.send_message(
                "Dispatch #%d no longer exists." % kit_id, ephemeral=True)
            return

        # Conditional UPDATE, so two people pressing at once is resolved by the database
        # rather than by whichever callback happened to run second.
        if not self.store.claim_kit(kit_id, interaction.user.id):
            holder = self.store.get_kit(kit_id)
            who = "<@%d>" % int(holder["claimed_by"]) if holder and holder["claimed_by"] \
                else "someone else"
            await interaction.response.send_message(
                "%s claimed this one first." % who, ephemeral=True)
            return

        LOG.info("dispatch claimed kit=%d by=%s", kit_id, interaction.user.id)
        embed = discord.Embed(
            title="Kit dispatch #%d" % kit_id,
            description="**%s** - claimed by %s."
                        % (kit["mc_name"], interaction.user.mention),
            colour=discord.Colour.from_str("#2E6B3F"))
        view = discord.ui.View(timeout=None)
        view.add_item(DeliveredButton(kit_id))
        # The interaction response has to land first -- it is on a 3-second clock. Retagging
        # is a separate call and is allowed to be slow or to fail.
        await interaction.response.edit_message(embed=embed, view=view)
        await _set_dispatch_state(interaction.channel, "claimed")

    async def handle_delivered(self, interaction: discord.Interaction,
                               kit_id: int) -> None:
        kit = self.store.get_kit(kit_id)
        if kit is None:
            await interaction.response.send_message(
                "Dispatch #%d no longer exists." % kit_id, ephemeral=True)
            return
        claimer = kit["claimed_by"]
        if claimer and int(claimer) != interaction.user.id \
                and not is_reviewer(interaction.user, self.cfg):
            await interaction.response.send_message(
                "<@%d> claimed this one - they or a reviewer can close it." % int(claimer),
                ephemeral=True)
            return
        if not self.store.mark_delivered(kit_id):
            await interaction.response.send_message(
                "Dispatch #%d was already marked delivered." % kit_id, ephemeral=True)
            return
        LOG.info("dispatch delivered kit=%d by=%s", kit_id, interaction.user.id)
        embed = discord.Embed(
            title="Kit dispatch #%d - delivered" % kit_id,
            description="**%s** - delivered by %s. \U0001F348"
                        % (kit["mc_name"], interaction.user.mention),
            colour=discord.Colour.from_str("#4A4A4A"))
        await interaction.response.edit_message(embed=embed, view=None)
        await _set_dispatch_state(interaction.channel, "delivered")
        # Closing the post keeps the forum's default view to live work without deleting the
        # record. Archived rather than locked, so it can be reopened if a delivery falls
        # through after being marked done.
        if isinstance(interaction.channel, discord.Thread) and \
                isinstance(interaction.channel.parent, discord.ForumChannel):
            try:
                await interaction.channel.edit(archived=True)
            except discord.HTTPException:
                LOG.warning("could not archive delivered dispatch thread=%s",
                            interaction.channel.id)


# ------------------------------------------------------------------------ commands

def register_commands(app: KitBot) -> None:
    tree = app.tree

    @tree.command(name="panel", description="Post the kit-request panel in this channel.")
    async def panel(interaction: discord.Interaction) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can post the panel.", ephemeral=True)
            return
        await interaction.channel.send(embed=panel_embed(app.cfg), view=PanelView())
        await interaction.response.send_message(
            "Panel posted. Pin it - the button keeps working indefinitely, so it only needs "
            "posting once.", ephemeral=True)

    @tree.command(name="flag", description="Flag an account for future reviewers.")
    @app_commands.describe(mc_name="Minecraft username",
                           kind="What kind of flag",
                           note="Why - written for a reviewer who has never seen this name")
    @app_commands.choices(kind=[
        app_commands.Choice(name="known alt", value="alt"),
        app_commands.Choice(name="do not serve", value="deny"),
        app_commands.Choice(name="note only", value="note"),
    ])
    async def flag(interaction: discord.Interaction, mc_name: str,
                   kind: app_commands.Choice[str], note: str) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can set flags.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()
        uuid = None
        try:
            resolved = await loop.run_in_executor(
                None, lambda: identity.resolve(mc_name, app.cfg, LOG))
            uuid, mc_name = resolved["uuid"], resolved["name"] or mc_name
        except identity.IdentityError:
            # A flag on a name alone still beats losing the knowledge, but it follows the
            # name rather than the account, so say so plainly.
            pass
        flag_id = app.store.set_flag(kind.value, interaction.user.id, uuid, mc_name, note)
        await interaction.followup.send(
            "Flagged **%s** as `%s` (flag #%d).%s" % (
                mc_name, kind.value, flag_id,
                "" if uuid else " Couldn't resolve a UUID, so this flag is keyed on the "
                                "name and will follow the name if they rename."),
            ephemeral=True)

    @tree.command(name="unflag", description="Clear a flag by its id.")
    async def unflag(interaction: discord.Interaction, flag_id: int) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can clear flags.", ephemeral=True)
            return
        ok = app.store.clear_flag(flag_id)
        await interaction.response.send_message(
            "Flag #%d cleared." % flag_id if ok
            else "Flag #%d isn't set (or was already cleared)." % flag_id, ephemeral=True)

    @tree.command(name="lookup", description="Build a reviewer card without opening a ticket.")
    async def lookup(interaction: discord.Interaction, mc_name: str) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can run a lookup.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()
        uuid = None
        try:
            resolved = await loop.run_in_executor(
                None, lambda: identity.resolve(mc_name, app.cfg, LOG))
            uuid, mc_name = resolved["uuid"], resolved["name"] or mc_name
        except identity.UnknownPlayer:
            await interaction.followup.send(
                "No Minecraft account named `%s`." % mc_name[:32], ephemeral=True)
            return
        except identity.IdentityError:
            pass
        built = await loop.run_in_executor(None, lambda: card_mod.gather(
            mc_name, uuid, interaction.user.id, app.cfg, app.vc, app.store, app.lex, LOG))
        files = []
        if built["chat_lines"]:
            files.append(discord.File(
                io.BytesIO(card_mod.chat_dump(built).encode("utf-8")),
                filename="chat-%s.txt" % mc_name))
        await interaction.followup.send(embed=_embed_for(built, app.cfg), files=files,
                                        ephemeral=True)

    @tree.command(name="flagline",
                  description="Label chat lines a reviewer objected to (trains the lexicon).")
    @app_commands.describe(ticket="Ticket number",
                           lines="Line numbers from the attached chat log, e.g. 3,7,12")
    async def flagline(interaction: discord.Interaction, ticket: int, lines: str) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can label chat.", ephemeral=True)
            return
        positions = []
        for chunk in re.split(r"[,\s]+", lines.strip()):
            if chunk.isdigit():
                positions.append(int(chunk))
        if not positions:
            await interaction.response.send_message(
                "Give me line numbers from the attached log, e.g. `3,7,12`.", ephemeral=True)
            return
        done = sum(1 for p in positions if app.store.flag_chat(ticket, p))
        await interaction.response.send_message(
            "Labelled %d of %d line(s) on ticket #%d. This is the only labelled screening "
            "data that exists, so thank you - it can't be reconstructed later."
            % (done, len(positions), ticket), ephemeral=True)

    @tree.command(name="ledger", description="Kit history and flags for an account.")
    async def ledger(interaction: discord.Interaction, mc_name: str) -> None:
        if not is_reviewer(interaction.user, app.cfg):
            await interaction.response.send_message(
                "Only reviewers can read the ledger.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()
        uuid = None
        try:
            resolved = await loop.run_in_executor(
                None, lambda: identity.resolve(mc_name, app.cfg, LOG))
            uuid, mc_name = resolved["uuid"], resolved["name"] or mc_name
        except identity.IdentityError:
            pass

        text = ["**%s**%s" % (mc_name, "" if uuid else "  *(UUID unresolved - name only)*")]

        kits = app.store.kit_history(mc_uuid=uuid, limit=10) if uuid else []
        if kits:
            for k in kits:
                when = card_mod.ago(card_mod.parse_ts_epoch(k["created_at"]))
                state = ("delivered" if k["delivered_at"] else
                         "claimed" if k["claimed_by"] else "unclaimed")
                text.append("- kit #%d, %s, %s" % (k["id"], when, state))
        else:
            text.append("No kits on record for this account.")

        cd = app.store.cooldown(app.cfg["policy"]["cooldown_days"], mc_uuid=uuid)
        text.append("Cooldown: %s" % ("**%d day(s) left**" % cd["days_left"]
                                      if cd["blocked"] else "clear"))

        flags = app.store.flags_for(mc_uuid=uuid, mc_name=mc_name)
        text.append("Flags: %s" % (", ".join(
            "#%d %s" % (f["id"], f["kind"]) for f in flags) or "none"))

        await interaction.followup.send("\n".join(text), ephemeral=True)


# ---------------------------------------------------------------------------- main

def setup_logging(cfg: Dict[str, Any]) -> None:
    level = getattr(logging, str(cfg["ops"]["log_level"]).upper(), logging.INFO)
    handlers: list = [logging.StreamHandler(sys.stderr)]
    path = cfg["ops"]["log_path"]
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=level, handlers=handlers,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    logging.getLogger("discord").setLevel(max(level, logging.WARNING))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Melon Men kit-request bot")
    parser.add_argument("--config", help="path to melonkit.json")
    parser.add_argument("--print-config", action="store_true",
                        help="show the effective config (including env overrides) and exit")
    parser.add_argument("--post-panel", type=int, metavar="CHANNEL_ID", default=0,
                        help="connect, post and pin the request panel in this channel, then "
                             "exit. For deployment; /panel does the same thing from a client")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load_config(args.config)
    except config_mod.ConfigError as exc:
        sys.stderr.write("config error: %s\n" % exc)
        return 2

    if args.print_config:
        sys.stdout.write(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        return 0

    setup_logging(cfg)
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.stderr.write(
            "no bot token: set %s in the environment. It is deliberately not a config key, "
            "so it cannot end up in a file that gets committed.\n"
            % cfg["discord"]["token_env"])
        return 2

    app = KitBot(cfg, post_panel_to=args.post_panel)
    try:
        app.run(token, log_handler=None)
    except discord.LoginFailure:
        sys.stderr.write("Discord rejected the token in %s.\n"
                         % cfg["discord"]["token_env"])
        return 3
    except KeyboardInterrupt:                        # pragma: no cover
        pass
    finally:
        app.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
