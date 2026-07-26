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

Chat text reaches Discord only through the reviewer-only pager, read back from the ledger rows
that were stored already-redacted. It never reaches a log record: diagnostics carry ticket ids,
user ids and counts. The same holds for a meeting point -- it goes to the runner who needs it,
and the journal records only that one was set.
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
import redact
import screening
import store as store_mod
import vc as vc_mod

LOG = logging.getLogger("melonkit")

PANEL_CUSTOM_ID = "melonkit:request:v1"
# The rescue button's id above never changes value: `find_existing_panel` matches on it, and
# that match is what lets an already-pinned panel be EDITED to gain this second button rather
# than deleted and re-posted, which would lose its pin, its place and anyone's bookmark.
PANEL_FUNDING_CUSTOM_ID = "melonkit:request:funding:v1"

PANEL_TITLE = "\U0001F198 Need Help on 2b2t?"
PANEL_BODY = (
    "Stuck in spawn? Bedtrapped? Base griefed? Need funding for a small project? "
    "**The Melon Men** may be able to help!\n"
    "\n"
    "⚠️ **If you die with your escape kit due to your own actions, you will NOT "
    "receive another.** Be careful and follow our advice.\n"
    "\n"
    "Submitting a ticket does not guarantee help, but we try to assist as many folks as we "
    "can. Most tickets are fulfilled within {response_time}.\n"
    "\n"
    "We do check a couple of things first: your **public** 2b2t history — when you were "
    "first seen, recent deaths, public chat — and whether we've already helped you in "
    "the last {cooldown} days. Nothing private, and nothing you haven't already said in "
    "open chat.\n"
    "\n"
    "⛑️ Players rescued: **{rescued_count}** as of {rescued_as_of}"
)

# Which button to press. Kept out of PANEL_BODY so a server rewording the panel cannot
# accidentally delete the only explanation of the difference between the two.
PANEL_CHOICE = (
    "**\U0001F198 Rescue kit** - you're stuck, trapped or just lost everything and need gear "
    "to get going again.\n"
    "**\U0001F9F1 Project funding** - you're building something and need materials for it.\n"
    "\n"
    "One request at a time either way. They're judged on different things, so pick the one "
    "that fits - a reviewer can't turn one into the other."
)


# --------------------------------------------------------------------------- helpers

def is_admin(member: Any) -> bool:
    """Who may run /setup. Manage Server, i.e. whoever could have invited the bot."""
    perms = getattr(member, "guild_permissions", None)
    return bool(getattr(perms, "manage_guild", False) or
                getattr(perms, "administrator", False))


def is_reviewer(member: Any, g: Dict[str, Any]) -> bool:
    """`g` is a merged per-guild config, not the file config -- roles differ per server."""
    role_id = int(g.get("reviewer_role_id") or 0)
    if not role_id:
        return is_admin(member)
    return any(r.id == role_id for r in getattr(member, "roles", []))


def may_claim(member: Any, g: Dict[str, Any]) -> bool:
    role_id = int(g.get("runner_role_id") or 0)
    if not role_id:
        return True
    if any(r.id == role_id for r in getattr(member, "roles", [])):
        return True
    # A reviewer can always pick up a delivery; requiring a second role to do the thing you
    # just approved is friction with no purpose.
    return is_reviewer(member, g)


# A ticket's whole lifecycle, expressed as forum tags on its queue post. Tag names are
# matched case-insensitively against whatever the channel already has, so they can be renamed
# or restyled without touching code -- a missing tag is skipped rather than being an error,
# because a forum with no tags configured must still work.
QUEUE_TAGS = ("awaiting review", "approved", "declined", "claimed", "delivered", "closed")


def _state_tags(forum: "discord.ForumChannel", state: str) -> list:
    want = state.casefold()
    return [t for t in forum.available_tags if t.name.casefold() == want]


async def _set_queue_state(channel: Any, state: str, *,
                           archive: bool = False) -> None:
    """Move a queue post's lifecycle tag to `state`, keeping any other tag it has.

    No-op when the queue is a plain text channel.

    `applied_tags` is replace-not-merge, so the obvious version of this -- edit with just the
    new state tag -- silently strips every other tag a post has. That was harmless while the
    lifecycle was the only thing tagged; now the request type is a tag too, and the first
    Approve would have deleted it. QUEUE_TAGS is what draws the line: anything in it is
    lifecycle and gets replaced, anything else is somebody's and gets kept.
    """
    if not isinstance(channel, discord.Thread):
        return
    if not isinstance(channel.parent, discord.ForumChannel):
        return
    tags = _state_tags(channel.parent, state)
    if tags:
        lifecycle = {n.casefold() for n in QUEUE_TAGS}
        keep = [t for t in channel.applied_tags
                if t.name.casefold() not in lifecycle]
        # A forum post takes at most 5 tags. The lifecycle tag goes first so that it is never
        # the one truncated: it is the one value this function is responsible for, and a
        # moderator's fifth topical label losing its slot is the right thing to sacrifice.
        tags = (tags + keep)[:5]
    try:
        if tags:
            await channel.edit(applied_tags=tags, archived=archive or False)
        elif archive:
            await channel.edit(archived=True)
    except discord.HTTPException as exc:
        # Cosmetic. A post whose tag is stale is still the record of the ticket, and failing
        # the decision over a label would be strictly worse than a wrong label.
        LOG.warning("could not set queue tag state=%s thread=%s status=%s",
                    state, channel.id, getattr(exc, "status", "?"))


def panel_embed(g: Dict[str, Any]) -> discord.Embed:
    """The pinned panel. `g` is a merged per-guild config, so a server that overrides its
    cooldown or its rescued counter gets its own copy. Shared by /setup, /panel and
    --post-panel so the wording cannot drift between them."""
    embed = discord.Embed(
        title=PANEL_TITLE,
        description=PANEL_BODY.format(
            cooldown=g["cooldown_days"],
            response_time=g["response_time"],
            rescued_count=g["rescued_count"],
            rescued_as_of=g["rescued_as_of"]),
        colour=discord.Colour.from_str("#2E6B3F"))
    embed.add_field(name="Which one do I want?", value=PANEL_CHOICE, inline=False)
    return embed


async def find_existing_panel(channel: Any) -> Optional[discord.Message]:
    """The panel message already in this channel, if there is one.

    Matched on the button's custom_id rather than on content, so the copy can change freely
    without orphaning the message. This is what lets the panel be *edited* in place: a pinned
    message that has to be deleted and re-posted every time the wording changes loses its pin
    and its position, and anyone who bookmarked it.
    """
    try:
        async for msg in channel.history(limit=100):
            if not msg.author.bot or msg.author.id != channel.guild.me.id:
                continue
            for row in msg.components:
                for item in getattr(row, "children", []) or []:
                    if getattr(item, "custom_id", None) == PANEL_CUSTOM_ID:
                        return msg
    except discord.HTTPException:
        return None
    return None


def _embed_for(card: Dict[str, Any], cfg: Dict[str, Any]) -> discord.Embed:
    colour = discord.Colour.from_str("#2E6B3F")
    if card["cooldown"]["blocked"] or card["flags"]:
        colour = discord.Colour.from_str("#8A6D1F")
    # "Lookup" when there is no request behind the card: /lookup builds one to vet a name, and
    # titling that "Rescue kit - Alice" would invent a request nobody made.
    what = (store_mod.KIND_LABEL[card_mod.kind_of(card)].capitalize()
            if card.get("request_type") else "Lookup")
    embed = discord.Embed(
        title="%s - %s" % (what, card["mc_name"]),
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

class RequestModal(discord.ui.Modal, title="Request a rescue kit"):
    """The rescue form. One free-text note, because the case is usually one sentence."""

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
        # No timeout. Discord puts no deadline on submitting a modal -- the dialog stays
        # open as long as the applicant keeps typing -- but discord.py's own view timeout
        # does, and when it expires the submission dispatches to nothing: "This
        # interaction failed", and everything they wrote is gone. Someone assembling a
        # materials list is checking their stock in game while the form sits open, so ten
        # minutes is a normal answer rather than an abandoned one.
        super().__init__(timeout=None)
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # The five API calls take a few seconds, well past the 3s deadline, so acknowledge
        # first and keep working behind the 15-minute follow-up token.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.app.handle_request(interaction, str(self.mc_name), str(self.note or ""),
                                      kind=store_mod.KIND_RESCUE)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception) -> None:      # pragma: no cover - Discord runtime
        LOG.error("request modal failed user=%s err=%s", interaction.user.id,
                  type(error).__name__)
        await _safe_followup(interaction,
                             "Something broke on our side. Nothing was recorded - please "
                             "try again in a minute.")


class FundingModal(discord.ui.Modal, title="Request project funding"):
    """The funding form.

    Separate from the rescue form rather than one form with a type selector, and the reason is
    a platform constraint rather than a preference: a modal is submitted whole, so it cannot
    change its own questions after someone picks a type inside it. Asking a builder "anything
    we should know?" and a rescue applicant "how big is this?" in the same box would make both
    answers unusable, so the choice happens on the panel, one button per form.

    Four fields against Discord's limit of five, leaving one spare.
    """

    mc_name = discord.ui.TextInput(
        label="Your Minecraft username",
        placeholder="exactly as it appears in game",
        min_length=3, max_length=16, required=True)
    project = discord.ui.TextInput(
        label="What's the project?",
        placeholder="e.g. rebuilding the nether hub after it got griefed",
        max_length=200, required=True)
    needs = discord.ui.TextInput(
        label="What do you need?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. 4 stacks of obsidian and a shulker of glass",
        max_length=500, required=True)
    scale = discord.ui.TextInput(
        label="Roughly how big is this?",
        placeholder="a weekend / a few weeks / ongoing",
        max_length=100, required=False)

    def __init__(self, app: "KitBot") -> None:
        # No timeout. Discord puts no deadline on submitting a modal -- the dialog stays
        # open as long as the applicant keeps typing -- but discord.py's own view timeout
        # does, and when it expires the submission dispatches to nothing: "This
        # interaction failed", and everything they wrote is gone. Someone assembling a
        # materials list is checking their stock in game while the form sits open, so ten
        # minutes is a normal answer rather than an abandoned one.
        super().__init__(timeout=None)
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.app.handle_request(
            interaction, str(self.mc_name), "",
            kind=store_mod.KIND_FUNDING,
            details={"project": str(self.project or ""),
                     "needs": str(self.needs or ""),
                     "scale": str(self.scale or "")})

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception) -> None:      # pragma: no cover - Discord runtime
        LOG.error("funding modal failed user=%s err=%s", interaction.user.id,
                  type(error).__name__)
        await _safe_followup(interaction,
                             "Something broke on our side. Nothing was recorded - please "
                             "try again in a minute.")


class PanelView(discord.ui.View):
    """The pinned panel. Static custom_ids, never times out, survives restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Request a rescue kit", emoji="\U0001F198",
                       style=discord.ButtonStyle.success, custom_id=PANEL_CUSTOM_ID)
    async def request(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_panel_click(interaction, store_mod.KIND_RESCUE)

    @discord.ui.button(label="Request project funding", emoji="\U0001F9F1",
                       style=discord.ButtonStyle.primary,
                       custom_id=PANEL_FUNDING_CUSTOM_ID)
    async def funding(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_panel_click(interaction, store_mod.KIND_FUNDING)


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
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
            await interaction.response.send_message(
                "Only reviewers can decide a request.", ephemeral=True)
            return
        await interaction.response.send_modal(DeclineModal(app, self.ticket_id))


def _normalise_dimension(text: str) -> Optional[str]:
    """Map whatever they typed onto overworld/nether/end, or keep it as written.

    Tolerant rather than validating: a runner can act on "the nether roof" perfectly well, and
    refusing a form because a free-text hint was not one of three words would strand somebody
    who is waiting to be found.
    """
    low = (text or "").strip().casefold()
    if not low:
        return None
    for known in card_mod.DIMENSIONS:
        if known in low:
            return known
    if "over" in low or low in ("ow", "owo"):
        return "overworld"
    return text.strip()[:40]


class CoordsButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"melonkit:coords:(?P<ticket>\d+)"):
    """Lets the applicant say where to meet, once their request is approved.

    Posted into their own private thread rather than offered on the panel: before a decision
    there is nothing to deliver, and asking up front would collect a location from everyone who
    gets declined. Persistent like every other button here, because the gap between approval and
    somebody actually reading their thread can be hours.
    """

    def __init__(self, ticket_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Set meeting coordinates", emoji="\U0001F4CD",
            style=discord.ButtonStyle.primary,
            custom_id="melonkit:coords:%d" % ticket_id))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "CoordsButton":
        return cls(int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_coords_open(interaction, self.ticket_id)


class CoordsModal(discord.ui.Modal, title="Where should we meet you?"):
    coords = discord.ui.TextInput(
        label="Coordinates (X Y Z)",
        placeholder="e.g. 1200 64 -840",
        max_length=120, required=True)
    dimension = discord.ui.TextInput(
        label="Which dimension? (optional)",
        placeholder="overworld / nether / end",
        max_length=40, required=False)

    def __init__(self, app: "KitBot", ticket_id: int) -> None:
        # No timeout. Discord puts no deadline on submitting a modal -- the dialog stays
        # open as long as the applicant keeps typing -- but discord.py's own view timeout
        # does, and when it expires the submission dispatches to nothing: "This
        # interaction failed", and everything they wrote is gone. Someone assembling a
        # materials list is checking their stock in game while the form sits open, so ten
        # minutes is a normal answer rather than an abandoned one.
        super().__init__(timeout=None)
        self.app = app
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.app.handle_coords_submit(
            interaction, self.ticket_id, str(self.coords), str(self.dimension or ""))

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception) -> None:      # pragma: no cover - Discord runtime
        LOG.error("coords modal failed ticket=%d err=%s", self.ticket_id,
                  type(error).__name__)
        await _safe_followup(interaction,
                             "Something broke saving that. Press the button and try again.")


class ChatHistoryButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"melonkit:chat:(?P<ticket>\d+)"):
    """Opens the reviewer-only chat pager for a ticket.

    This is the durable entry point, and it has to be: the pager itself is an ephemeral message,
    which Discord discards when the reviewer's client restarts. Coming back to a ticket means
    pressing this again, so it lives on the queue post and is re-added to every view that
    replaces the original one.
    """

    def __init__(self, ticket_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Chat history", emoji="\U0001F4DC",
            style=discord.ButtonStyle.secondary,
            custom_id="melonkit:chat:%d" % ticket_id))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "ChatHistoryButton":
        return cls(int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_chat_page(interaction, self.ticket_id, 0, edit=False)


class ChatPageButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"melonkit:chatpage:(?P<ticket>\d+):(?P<page>\d+)"):
    """Prev/Next. The TARGET page is in the custom_id, so no page state is held anywhere.

    The alternative -- an ordinary View holding `self.page` in memory -- looks simpler and is
    broken: discord.py *overrides* the timeout of any view sent on an ephemeral message to 15
    minutes and there is no way to opt out, after which a press dispatches to nothing at all.
    No exception, no log line, just "This interaction failed" on a button that still looks
    live. A reviewer who opens the chat, goes to check the applicant's other tickets and comes
    back is well past 15 minutes, and that is the normal reviewing rhythm rather than an edge
    case. Deriving the page from the custom_id has no expiry and survives a redeploy.

    Every child of the pager view MUST be a DynamicItem for a related reason. When a view
    holding dynamic items times out, discord.py unregisters their templates process-wide -- but
    only if the view also has a *non*-dynamic dispatchable child. So adding an innocent disabled
    "Page 3/5" button here would, fifteen minutes later, break Prev/Next for every reviewer in
    every guild until the next restart. The page counter goes in the embed footer.
    """

    def __init__(self, ticket_id: int, page: int, label: str) -> None:
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary,
            custom_id="melonkit:chatpage:%d:%d" % (ticket_id, page)))
        self.ticket_id = ticket_id
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "ChatPageButton":
        # Label read off the live component, so a rebuild after a restart does not guess it.
        return cls(int(match["ticket"]), int(match["page"]), item.label or "Page")

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_chat_page(interaction, self.ticket_id, self.page, edit=True)


class ChatFileButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"melonkit:chatfile:(?P<ticket>\d+)"):
    """The escape hatch: the same lines as a .txt, regenerated on demand.

    The queue card no longer carries an attachment, so this is what keeps the old workflow
    available for the reviewer who wants to search the whole log at once instead of paging it.
    """

    def __init__(self, ticket_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Download .txt", emoji="\U0001F4C4",
            style=discord.ButtonStyle.secondary,
            custom_id="melonkit:chatfile:%d" % ticket_id))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction,
                             item: discord.ui.Button,
                             match: "re.Match[str]") -> "ChatFileButton":
        return cls(int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app: KitBot = interaction.client            # type: ignore[assignment]
        await app.handle_chat_file(interaction, self.ticket_id)


class DeclineModal(discord.ui.Modal, title="Decline this request"):
    reason = discord.ui.TextInput(
        label="Reason (required)",
        style=discord.TextStyle.paragraph,
        placeholder="Written for whoever reads the ledger in a year - "
                    "\"known alt of <name>\" beats \"no\".",
        min_length=3, max_length=400, required=True)

    def __init__(self, app: "KitBot", ticket_id: int) -> None:
        # No timeout. Discord puts no deadline on submitting a modal -- the dialog stays
        # open as long as the applicant keeps typing -- but discord.py's own view timeout
        # does, and when it expires the submission dispatches to nothing: "This
        # interaction failed", and everything they wrote is gone. Someone assembling a
        # materials list is checking their stock in game while the form sits open, so ten
        # minutes is a normal answer rather than an abandoned one.
        super().__init__(timeout=None)
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
        # NO privileged intents, ever -- including for transcript conversation capture.
        #
        # MESSAGE_CONTENT gates message content in *gateway events*. It does NOT gate REST
        # history fetches, which are governed by Read Message History. Transcripts read the
        # thread over REST at archive time, so they get full content without it. Verified
        # against real messages, because the docs read as though the intent covers both and
        # requesting it here would have meant the bot refusing to start until someone
        # enabled it in the Developer Portal -- a hard failure in exchange for nothing.
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

    # ------------------------------------------------------------- guild config

    def gcfg(self, guild_id: Optional[int]) -> Dict[str, Any]:
        """This guild's settings, over the file's defaults.

        The file supplies things that are the same everywhere -- the token variable, the 2b2t
        client, screening, the store path -- plus starting values for the per-guild policy.
        Channel and role ids only ever come from the database, because they are meaningless
        across servers.
        """
        merged: Dict[str, Any] = {
            "panel_channel_id": 0,
            "queue_channel_id": 0,
            "transcript_channel_id": 0,
            "reviewer_role_id": 0,
            "runner_role_id": 0,
            "capture_thread_messages": self.cfg["discord"]["capture_thread_messages"],
        }
        merged.update(self.cfg["policy"])
        merged.update(self.cfg["panel"])
        if guild_id:
            merged.update(self.store.get_guild_config(int(guild_id)))
        return merged

    def is_configured(self, guild_id: Optional[int]) -> bool:
        g = self.gcfg(guild_id)
        return bool(g.get("panel_channel_id") and g.get("queue_channel_id"))

    async def require_setup(self, interaction: discord.Interaction) -> Optional[Dict[str, Any]]:
        """Merged config, or None after telling the caller to run /setup."""
        if interaction.guild_id is None:
            await _safe_followup(interaction, "This only works inside a server.")
            return None
        if not self.is_configured(interaction.guild_id):
            await _safe_followup(
                interaction,
                "This server hasn't been set up yet. Someone with **Manage Server** needs to "
                "run **/setup** first — it makes the channels and posts the panel.")
            return None
        return self.gcfg(interaction.guild_id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        LOG.info("joined guild=%s (%s) members=%s", guild.id, guild.name,
                 guild.member_count)
        if self.is_configured(guild.id):
            return
        # Point whoever added it at the one command that matters. Best effort: many guilds
        # have no system channel, or deny the bot there.
        target = guild.system_channel
        if target is None or not target.permissions_for(guild.me).send_messages:
            target = next((c for c in guild.text_channels
                           if c.permissions_for(guild.me).send_messages), None)
        if target is None:
            return
        try:
            await target.send(embed=discord.Embed(
                title="\U0001F348 Melon Kits",
                description=(
                    "Thanks for adding me. Someone with **Manage Server** should run "
                    "**/setup** to get started.\n\n"
                    "It creates three channels — a public one for requests, a staff-only "
                    "queue, and a staff-only archive — and posts the request panel. Takes "
                    "about five seconds and asks you nothing."),
                colour=discord.Colour.from_str("#2E6B3F")))
        except discord.HTTPException:
            pass

    async def on_error(self, event: str, *args, **kwargs) -> None:
        """Never let an unhandled listener exception die silently in the library's logger."""
        LOG.exception("unhandled exception in event=%s", event)

    async def _on_app_command_error(self, interaction: discord.Interaction,
                                    error: Exception) -> None:
        """Without this a crashing slash command leaves the user staring at a spinner.

        Discord shows "the application did not respond" and the traceback goes only to
        discord.py's logger, so the operator learns nothing and the user learns less. The
        message is deliberately vague about the cause -- an exception string can contain a
        row body -- and points at the log instead.
        """
        cmd = getattr(interaction.command, "name", "?")
        LOG.exception("slash command failed command=%s user=%s", cmd, interaction.user.id)
        await _safe_followup(
            interaction,
            "That didn't work. Nothing was changed - the error is in the bot log "
            "(`journalctl -u melonkit-bot`).")

    async def setup_hook(self) -> None:
        self.add_view(PanelView())
        for item in (ApproveButton, DeclineButton, ClaimButton, DeliveredButton,
                     ChatHistoryButton, ChatPageButton, ChatFileButton, CoordsButton):
            self.add_dynamic_items(item)
        self.tree.on_error = self._on_app_command_error
        register_commands(self)
        self._adopt_legacy_config()

        # ALWAYS sync globally: the bot has to work in servers it has not met yet, and a
        # guild-scoped sync only reaches the guilds named at startup.
        await self.tree.sync()
        LOG.info("commands synced globally")

        # A home guild additionally gets a guild-scoped copy, purely so command edits show up
        # instantly there instead of waiting on Discord's global propagation.
        home = int(self.cfg["discord"]["home_guild_id"] or 0)
        if home:
            obj = discord.Object(id=home)
            self.tree.copy_global_to(guild=obj)
            await self.tree.sync(guild=obj)
            LOG.info("commands also synced to home guild=%d for instant updates", home)

    def _adopt_legacy_config(self) -> None:
        """Move a single-guild file config into the database, once.

        Without this, upgrading the existing deployment would look exactly like the ledger
        having been wiped: every query is guild-scoped now, and the old rows carry no guild,
        so cooldowns would reset and flags would vanish. Runs only when the home guild has no
        stored config yet, so it never overwrites anything /setup has done.
        """
        home = int(self.cfg["discord"]["home_guild_id"] or 0)
        if not home or self.store.get_guild_config(home):
            return
        legacy = {k: self.cfg["discord"].get(k) for k in
                  ("panel_channel_id", "queue_channel_id", "transcript_channel_id",
                   "reviewer_role_id", "runner_role_id")}
        legacy = {k: v for k, v in legacy.items() if v}
        if not legacy:
            return
        self.store.set_guild_config(home, legacy)
        adopted = self.store.adopt_legacy_rows(home)
        LOG.info("adopted legacy config into guild=%d keys=%s and stamped %d pre-existing "
                 "row(s) with that guild", home, sorted(legacy), adopted)

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
            # Edit in place when a panel is already there. Re-posting on every copy change
            # would lose the pin and the position and leave stale panels with live buttons.
            existing = await find_existing_panel(channel)
            if existing is not None:
                await existing.edit(embed=panel_embed(self.gcfg(channel.guild.id)),
                                    view=PanelView())
                LOG.info("existing panel updated channel=%d message=%d",
                         channel.id, existing.id)
                return
            msg = await channel.send(embed=panel_embed(self.gcfg(channel.guild.id)),
                                     view=PanelView())
            LOG.info("panel posted channel=%d message=%d", channel.id, msg.id)
            try:
                await msg.pin()
                LOG.info("panel pinned")
            except discord.HTTPException as exc:
                LOG.warning("panel posted but not pinned (needs the Pin Messages "
                            "permission, which is separate from Manage Messages): status=%s",
                            getattr(exc, "status", "?"))
        finally:
            await self.close()

    # ------------------------------------------------------------- panel -> modal

    async def _heal_orphaned_ticket(self, guild_id: int, user_id: int) -> None:
        """Close an open ticket whose applicant thread no longer exists.

        Deleting the thread in Discord does nothing to the ledger, so the row stays `open`
        and the pre-check below bars that person from ever requesting again -- the same
        permanent lockout /close exists for, arrived at by accident instead of neglect. This
        runs on every button press rather than relying only on the on_thread_delete event,
        because a thread deleted while the bot was down generates no event to catch.
        """
        row = self.store.open_ticket_for(guild_id, user_id)
        if row is None or not row["thread_id"]:
            # No thread recorded is not the same as a deleted thread: the queue post may
            # still be live and awaiting review, so leave it alone.
            return
        tid = int(row["thread_id"])
        if self.get_channel(tid) is not None:
            return
        try:
            await self.fetch_channel(tid)
            return                      # exists, just was not cached
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return                      # transient: do not close a ticket on a network blip
        self.store.record_decision(int(row["id"]), self.user.id,
                                   store_mod.STATUS_CANCELLED,
                                   "auto-closed: applicant thread was deleted")
        LOG.info("auto-closed orphaned ticket ticket=%s user=%s thread=%s gone",
                 row["id"], user_id, tid)
        # Fire-and-forget, because this runs from the panel pre-check -- BEFORE the button
        # press has been answered, on a 3-second clock. Archiving means building a file and
        # uploading it to another channel, which is multipart and can take a rate-limit sleep;
        # awaiting it here is what would make the button read as broken. The ledger write above
        # has already happened, and it is the part the next line depends on.
        asyncio.create_task(self._post_transcript(int(row["id"])))

    async def handle_panel_click(self, interaction: discord.Interaction,
                                 kind: str = store_mod.KIND_RESCUE) -> None:
        """Pre-check, THEN show the form. Never the other way round."""
        gid = interaction.guild_id
        if gid is None or not self.is_configured(gid):
            # A panel can outlive a /setup being undone, so the button has to cope.
            await interaction.response.send_message(
                "This server isn't set up for kit requests right now. Someone with Manage "
                "Server needs to run **/setup**.", ephemeral=True)
            return
        g = self.gcfg(gid)
        user_id = interaction.user.id

        await self._heal_orphaned_ticket(gid, user_id)
        open_n = self.store.open_ticket_count(gid, user_id)
        if open_n >= int(g["max_open_tickets_per_user"]):
            existing = self.store.open_ticket_for(gid, user_id)
            where = ""
            if existing and existing["thread_id"]:
                where = " Here: <#%d>" % int(existing["thread_id"])
            await interaction.response.send_message(
                "You already have a request open, so there's nothing more to do for now.%s"
                % where, ephemeral=True)
            return

        # Per kind, so a funded build does not block a rescue and a rescue does not block a
        # build. The one-open-ticket rule above is deliberately NOT per kind: two open tickets
        # is two threads and two cards for one person, and being stuck open is the most
        # expensive failure this app has.
        cd = self.store.cooldown(gid, g["cooldown_days"], discord_user_id=user_id, kind=kind)
        if cd["blocked"]:
            await interaction.response.send_message(
                "You had %s from us recently, so you're inside the %d-day cooldown for that - "
                "about %d day(s) to go. This is to spread help around rather than anything "
                "against you.%s" % (
                    "a rescue kit" if kind == store_mod.KIND_RESCUE else "project funding",
                    g["cooldown_days"], cd["days_left"],
                    "\n\nThe other kind of request has its own cooldown, so that one may "
                    "still be open to you."), ephemeral=True)
            return

        await interaction.response.send_modal(
            FundingModal(self) if kind == store_mod.KIND_FUNDING else RequestModal(self))

    # ---------------------------------------------------------------- the ticket

    async def handle_request(self, interaction: discord.Interaction,
                             mc_name: str, note: str,
                             kind: str = store_mod.KIND_RESCUE,
                             details: Optional[Dict[str, Any]] = None) -> None:
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

        gid = interaction.guild_id
        g = self.gcfg(gid)

        # Note: what the applicant types here is stored as written. Chat pulled from
        # api.2b2t.vc is coordinate-redacted (see card.gather) because that is third-party
        # public chat being repurposed as evidence; a form field somebody chose to fill in
        # about their own project is not, and stripping "the hub at 500 60 500" out of a
        # project description would leave the reviewer unable to tell what was being asked for.
        ticket_id = self.store.create_ticket(gid, user.id, canonical, mc_uuid, note or None,
                                             request_type=kind, details=details)
        LOG.info("ticket opened guild=%s ticket=%d kind=%s user=%s uuid=%s", gid, ticket_id,
                 kind, user.id, mc_uuid or "unresolved")

        thread = await self._open_thread(interaction, g, ticket_id, canonical)
        if thread is not None:
            self.store.set_ticket_thread(ticket_id, thread.id)

        built = await loop.run_in_executor(None, lambda: card_mod.gather(
            gid, canonical, mc_uuid, user.id, self.cfg, self.vc, self.store, self.lex, LOG,
            request_type=kind, details=details))

        # Instrumentation: the lines shown, stored redacted. Impossible to backfill.
        try:
            self.store.record_shown_chats(ticket_id, built["chat_lines"])
        except Exception:
            LOG.error("could not record shown chats ticket=%d", ticket_id)

        # The applicant's thread gets a receipt and nothing else. The reviewer card must
        # never land here: it carries the ledger fan-out, the reviewer flag list and the
        # screening counts, and showing someone the exact criteria applied to them both
        # exposes notes written about them and teaches them how to game the next request.
        if thread is not None:
            try:
                await thread.send(
                    "Thanks %s - this is request **#%d**, for **%s**, on `%s`.\n\n"
                    "A reviewer will look at it when someone's around. Help here is "
                    "voluntary, so there's no queue position to give you, and it isn't "
                    "guaranteed. You don't need to do anything else - if it's approved, "
                    "whoever is delivering will message you in this thread to sort out "
                    "where to meet." % (user.mention, ticket_id,
                                        store_mod.KIND_LABEL[kind], canonical))
            except discord.HTTPException:
                LOG.warning("could not post receipt to applicant thread ticket=%d", ticket_id)

        posted = await self._post_queue_card(g, ticket_id, canonical, user, built, thread)
        if not posted:
            # No reviewer will ever see this ticket, and leaving it `open` would bar the
            # applicant from trying again -- the lockout, reached by a third route. Close it
            # so they can immediately retry; nothing was reviewed, so nothing is lost.
            self.store.record_decision(
                ticket_id, self.user.id, store_mod.STATUS_CANCELLED,
                "auto-closed: the reviewer card could not be posted to the queue")
            LOG.error("ticket auto-closed, queue card failed ticket=%d", ticket_id)
            await interaction.followup.send(
                "Something went wrong on our side posting your request to the reviewers, so "
                "**#%d has been cancelled rather than left stuck** - please press the button "
                "again and it should go through. Sorry about that." % ticket_id,
                ephemeral=True)
            return

        where = "<#%d>" % thread.id if thread is not None else "a reviewer thread"
        await interaction.followup.send(
            "Request #%d is in, and %s is yours to follow it in. Help is voluntary, so "
            "there's no queue position to give you - but you don't need to do anything else."
            % (ticket_id, where), ephemeral=True)

    async def _post_queue_card(self, g: Dict[str, Any], ticket_id: int, mc_name: str,
                               user: Any, built: Dict[str, Any],
                               thread: Optional[discord.Thread]) -> bool:
        """Put the reviewer card in the staff-only queue. Returns False if it could not."""
        channel_id = int(g.get("queue_channel_id") or 0)
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None:
            LOG.error("queue channel %s unavailable, ticket=%d has no card posted",
                      channel_id, ticket_id)
            return False

        kind = card_mod.kind_of(built)
        view = discord.ui.View(timeout=None)
        view.add_item(ApproveButton(ticket_id))
        view.add_item(DeclineButton(ticket_id))
        # No .txt attachment any more: the same lines are paged in Discord, and the pager
        # offers the file on demand for anyone who wants the whole log at once. Only added when
        # there is something to page -- an empty pager could not say whether the applicant was
        # silent or the lookup failed, and the card's chat field already distinguishes those.
        if built["chat_lines"]:
            view.add_item(ChatHistoryButton(ticket_id))

        role_id = int(g.get("reviewer_role_id") or 0)
        header = "%sTicket #%d - **%s** requested by %s%s" % (
            "<@&%d> " % role_id if role_id else "", ticket_id,
            store_mod.KIND_LABEL[kind], user.mention,
            "\nApplicant thread: <#%d>" % thread.id if thread is not None else "")
        mentions = discord.AllowedMentions(roles=True, users=True)

        try:
            if isinstance(channel, discord.ForumChannel):
                created = await channel.create_thread(
                    # The type is in the title so the forum index shows it without relying on
                    # the tag, which a server set up before the tags existed will not have.
                    name="#%d %s - %s" % (ticket_id, mc_name, store_mod.KIND_LABEL[kind]),
                    content=header, embed=_embed_for(built, self.cfg), view=view,
                    allowed_mentions=mentions,
                    applied_tags=(_state_tags(channel, store_mod.KIND_LABEL[kind])
                                  + _state_tags(channel, "awaiting review")))
                # A forum post's starter message shares the thread's id, so storing the
                # thread id is enough to find and edit the card later.
                self.store.set_queue_thread(ticket_id, created.thread.id)
            else:
                msg = await channel.send(content=header,
                                         embed=_embed_for(built, self.cfg), view=view,
                                         allowed_mentions=mentions)
                self.store.set_queue_thread(ticket_id, msg.id)
            return True
        except discord.HTTPException as exc:
            LOG.error("could not post queue card ticket=%d status=%s",
                      ticket_id, getattr(exc, "status", "?"))
            return False

    # ------------------------------------------------------------- the chat pager

    def _chat_rows(self, guild_id: int, ticket_id: int):
        """Guild-scoped, because the ticket number arrived in a custom_id a user pressed."""
        return self.store.shown_chats_for_guild(int(guild_id), int(ticket_id))

    def _with_chat(self, view: Optional[discord.ui.View], guild_id: Optional[int],
                   ticket_id: int) -> Optional[discord.ui.View]:
        """Keep the chat-history button on a view that is replacing a queue card's.

        Every lifecycle step swaps the card's whole view for a new one, and components do not
        merge: anything the replacement lacks is gone. While the chat log was an attachment it
        survived those edits for free, because an edit that omits `attachments` leaves them
        alone. A button gets no such treatment, so each replacement has to re-add it -- or a
        ticket loses its chat the moment it is decided, which is exactly when a reviewer goes
        back to check something.

        Returns `view` untouched when the ticket has no chat, so a `None` stays `None` and the
        no-chat case behaves exactly as it did before.
        """
        if guild_id is None or not self._chat_rows(guild_id, ticket_id):
            return view
        view = view or discord.ui.View(timeout=None)
        view.add_item(ChatHistoryButton(ticket_id))
        return view

    async def handle_chat_page(self, interaction: discord.Interaction, ticket_id: int,
                               page: int, edit: bool = False) -> None:
        """Show one page of a ticket's chat, ephemerally, to a reviewer.

        Not deferred, on purpose. The work is one indexed read of at most a hundred rows plus
        string formatting, comfortably inside the 3-second window -- and `edit_message` raises
        InteractionResponded after a deferral, so deferring would force the paging path onto
        `followup` for no gain.
        """
        gid = interaction.guild_id
        # Role first, before the ticket is looked up: #kit-queue is visible to the delivery
        # role too, so a runner pressing this is the ordinary case being refused rather than a
        # hypothetical, and answering before the lookup keeps the refusal from doubling as a
        # way to probe which ticket numbers exist.
        if gid is None or not is_reviewer(interaction.user, self.gcfg(gid)):
            await interaction.response.send_message(
                "Only reviewers can read an applicant's chat history.", ephemeral=True)
            return
        if self.store.get_ticket(ticket_id, gid) is None:
            await interaction.response.send_message(
                "There's no ticket #%d in this server." % ticket_id, ephemeral=True)
            return

        rows = self._chat_rows(gid, ticket_id)
        pages = card_mod.chat_pages(rows)
        # Clamp rather than error. A reviewer can be holding a pager from before the rows were
        # reset, naming a page that no longer exists.
        index = max(0, min(int(page), len(pages) - 1))

        embed = discord.Embed(
            title="Chat · ticket #%d" % ticket_id,
            description=card_mod.chat_page_body(pages[index]),
            colour=discord.Colour.from_str(
                "#8A6D1F" if pages[index]["flagged"] else "#2E6B3F"))
        if pages[index]["flagged"]:
            embed.add_field(
                name="Flagged on this page",
                value="%s - marked `!` in the left column"
                      % ", ".join(str(p) for p in pages[index]["flagged"]),
                inline=False)
        embed.set_footer(text=card_mod.chat_page_footer(pages, index, len(rows), ticket_id))

        view = discord.ui.View(timeout=None)
        # Every child here must be a DynamicItem -- see ChatPageButton's docstring for the
        # global-unregistration footgun that a plain disabled button would trigger.
        if index > 0:
            view.add_item(ChatPageButton(ticket_id, index - 1, "◀ Previous"))
        if index < len(pages) - 1:
            view.add_item(ChatPageButton(ticket_id, index + 1, "Next ▶"))
        if rows:
            view.add_item(ChatFileButton(ticket_id))

        try:
            if edit:
                await interaction.response.edit_message(
                    embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(
                    embed=embed, view=view, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            LOG.warning("could not show chat page ticket=%d page=%d status=%s",
                        ticket_id, index, getattr(exc, "status", "?"))

    async def handle_chat_file(self, interaction: discord.Interaction,
                              ticket_id: int) -> None:
        """The same lines as a .txt, rebuilt from the ledger on demand."""
        gid = interaction.guild_id
        if gid is None or not is_reviewer(interaction.user, self.gcfg(gid)):
            await interaction.response.send_message(
                "Only reviewers can read an applicant's chat history.", ephemeral=True)
            return
        ticket = self.store.get_ticket(ticket_id, gid)
        if ticket is None:
            await interaction.response.send_message(
                "There's no ticket #%d in this server." % ticket_id, ephemeral=True)
            return
        rows = self._chat_rows(gid, ticket_id)
        if not rows:
            await interaction.response.send_message(
                "No chat lines are recorded for ticket #%d." % ticket_id, ephemeral=True)
            return
        body = card_mod.chat_file(rows, ticket_id, str(ticket["mc_name"]))
        await interaction.response.send_message(
            file=discord.File(io.BytesIO(body.encode("utf-8")),
                              filename="chat-ticket%d.txt" % ticket_id),
            ephemeral=True)

    async def _queue_post(self, ticket: Any):
        """(thread_or_None, partial_message_or_None) for a ticket's queue post."""
        qid = ticket["queue_thread_id"]
        if not qid:
            return None, None
        channel = self.get_channel(int(qid))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(qid))
            except (discord.HTTPException, discord.NotFound):
                channel = None
        if isinstance(channel, discord.Thread):
            return channel, channel.get_partial_message(channel.id)
        # Text-channel queue: qid is a message id inside queue_channel_id.
        parent = self.get_channel(
            int(self.gcfg(ticket["guild_id"]).get("queue_channel_id") or 0))
        if isinstance(parent, discord.TextChannel):
            return None, parent.get_partial_message(int(qid))
        return None, None

    async def _open_thread(self, interaction: discord.Interaction, g: Dict[str, Any],
                           ticket_id: int,
                           mc_name: str) -> Optional[discord.Thread]:
        channel = interaction.channel
        panel_id = int(g.get("panel_channel_id") or 0)
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
        gid = interaction.guild_id
        g = self.gcfg(gid)
        if not is_reviewer(interaction.user, g):
            await _safe_followup(interaction, "Only reviewers can decide a request.")
            return

        # Acknowledge BEFORE any network work. Everything below can involve a round trip --
        # _queue_post may fetch_channel, then there are message edits and thread edits -- and
        # the initial response deadline is 3 seconds. Blowing it makes Discord show "This
        # interaction failed" *after* the decision has already been recorded, which invites
        # the reviewer to press again; combined with a non-atomic decision that used to mean
        # two kits for one request.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        ticket = self.store.get_ticket(ticket_id, gid)
        if ticket is None:
            await _safe_followup(interaction, "Ticket #%d no longer exists." % ticket_id)
            return

        outcome = store_mod.STATUS_APPROVED if approve else store_mod.STATUS_DECLINED
        # Conditional at the database, not a read-then-write: this is what actually stops two
        # simultaneous reviewers both winning.
        if not self.store.record_decision(ticket_id, interaction.user.id, outcome, reason):
            fresh = self.store.get_ticket(ticket_id, gid)
            await _safe_followup(
                interaction,
                "Ticket #%d was already **%s** - somebody got there first, so nothing "
                "changed." % (ticket_id, fresh["status"] if fresh else "decided"))
            return
        LOG.info("ticket decided ticket=%d outcome=%s reviewer=%s",
                 ticket_id, outcome, interaction.user.id)

        # Re-read: record_decision changed the status, and the queue id may have been set
        # after the row we were handed was fetched.
        ticket = self.store.get_ticket(ticket_id, gid) or ticket
        qthread, qmsg = await self._queue_post(ticket)

        if not approve:
            await _safe_followup(
                interaction,
                "Ticket #%d declined. Reason recorded in the ledger." % ticket_id)
            if qmsg is not None:
                try:
                    await qmsg.edit(
                        content="Ticket #%d - **declined** by %s\n> %s"
                                % (ticket_id, interaction.user.mention, reason or ""),
                        view=self._with_chat(None, gid, ticket_id),
                        allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    LOG.warning("could not update declined queue post ticket=%d", ticket_id)
            await _set_queue_state(qthread, "declined", archive=True)
            # The applicant is told the outcome, never the internal reason: that text is
            # written for whoever reads the ledger in a year, and it names the criteria and
            # sometimes other accounts. A reviewer who wants to say more can just type in
            # the thread.
            await self._notify_thread(
                ticket,
                # Self-contained on purpose: it used to point at a list of reasons on the
                # panel, and that list is no longer there to point at.
                "Request #%d wasn't approved this time. We don't go into specifics on "
                "individual decisions, and it isn't permanent - you're welcome to ask again "
                "later." % ticket_id)
            # Archive before the thread gets cleaned up, not after.
            await self._post_transcript(ticket_id)
            return

        # The kind is copied from the ticket at the point of granting, so the two can never
        # disagree: this is what the cooldown reads, and it has to work for a grant recorded
        # with no ticket behind it too, where there would be nothing to join to.
        kit_id = self.store.record_kit(gid, ticket_id, int(ticket["discord_user_id"]),
                                       str(ticket["mc_name"]), ticket["mc_uuid"],
                                       kind=store_mod.ticket_kind(ticket))

        # The same post becomes the dispatch: one post is the entire record of a ticket.
        if qmsg is not None:
            claim_view = discord.ui.View(timeout=None)
            claim_view.add_item(ClaimButton(kit_id))
            try:
                await qmsg.edit(
                    content="Ticket #%d - **approved** by %s\nDispatch #%d, unclaimed."
                            % (ticket_id, interaction.user.mention, kit_id),
                    view=self._with_chat(claim_view, gid, ticket_id),
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                LOG.warning("could not attach claim button ticket=%d kit=%d",
                            ticket_id, kit_id)
        await _set_queue_state(qthread, "approved")

        await _safe_followup(
            interaction,
            "Ticket #%d approved. Dispatch #%d is claimable on this post." % (ticket_id, kit_id))
        # Approval is the first moment there is anything to deliver, so it is also the first
        # moment it is worth asking where. The button goes with the message rather than being a
        # separate post, so the ask and the reason for it stay together.
        coords_view = discord.ui.View(timeout=None)
        coords_view.add_item(CoordsButton(ticket_id))
        await self._notify_thread(
            ticket,
            "Request #%d is **approved**. Times aren't guaranteed - everyone here is a "
            "volunteer.\n\n"
            "**Press the button and tell us where to meet you** (X Y Z, and which dimension). "
            "Whoever claims the delivery sees it straight away, and you can update it here if "
            "you move." % ticket_id,
            view=coords_view)

    # ------------------------------------------------------------- transcripts

    async def on_thread_delete(self, thread: discord.Thread) -> None:
        """Close a ticket whose thread someone deleted, and archive it while we still can.

        Fires promptly for deletions that happen with the bot running; the pre-check in
        _heal_orphaned_ticket covers the ones that happen while it is down.
        """
        row = self.store.ticket_for_thread(thread.id)
        if row is None or row["status"] != store_mod.STATUS_OPEN:
            return
        self.store.record_decision(int(row["id"]), self.user.id,
                                   store_mod.STATUS_CANCELLED,
                                   "auto-closed: applicant thread was deleted")
        LOG.info("ticket auto-closed on thread delete ticket=%s thread=%s",
                 row["id"], thread.id)
        fresh = self.store.get_ticket(int(row["id"])) or row  # thread ids are unique
        qthread, qmsg = await self._queue_post(fresh)
        if qmsg is not None:
            try:
                # `_with_chat`, not view=None: this is the one lifecycle path with no
                # interaction to take a guild id from, so it comes off the ledger row.
                await qmsg.edit(
                    content="Ticket #%s - **closed**: the applicant thread was deleted."
                            % row["id"],
                    view=self._with_chat(None, fresh["guild_id"], int(row["id"])))
            except discord.HTTPException:
                pass
        await _set_queue_state(qthread, "closed", archive=True)
        await self._post_transcript(int(row["id"]))

    async def _thread_conversation(self, ticket: Any) -> Optional[str]:
        """The applicant thread's messages, oldest first. None when unavailable."""
        if not self.gcfg(ticket["guild_id"]).get("capture_thread_messages")                 or not ticket["thread_id"]:
            return None
        ch = self.get_channel(int(ticket["thread_id"]))
        if ch is None:
            try:
                ch = await self.fetch_channel(int(ticket["thread_id"]))
            except (discord.HTTPException, discord.NotFound):
                return None
        if not isinstance(ch, discord.Thread):
            return None
        lines = []
        human_msgs = 0
        human_with_text = 0
        try:
            async for msg in ch.history(limit=500, oldest_first=True):
                stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%SZ")
                text = msg.content or ""
                if not msg.author.bot:
                    human_msgs += 1
                    if text:
                        human_with_text += 1
                body = text.replace("\n", "\n" + " " * 30)
                lines.append("%s  %-18s %s" % (stamp, msg.author.display_name[:18], body))
                for att in msg.attachments:
                    lines.append("%s  %-18s [attachment: %s]"
                                 % (" " * 20, "", att.filename))
        except discord.HTTPException:
            return None
        if not lines:
            return None
        # Defensive: if every human message came back with empty content, we are being
        # redacted rather than reading a genuinely silent thread. Recording a wall of blank
        # lines would look like the applicant said nothing, which is a worse lie than
        # admitting we could not read it -- and it is how a future Discord change extending
        # MESSAGE_CONTENT to REST would show up.
        if human_msgs and not human_with_text:
            LOG.warning("thread history returned empty content for all %d human message(s) "
                        "in thread=%s: message content appears to be gated now",
                        human_msgs, ch.id)
            return None
        return "\n".join(lines)

    async def _post_transcript(self, ticket_id: int) -> None:
        """One self-contained record of a finished ticket, in the staff archive.

        Built from the ledger rather than by scraping Discord, so it still works after the
        thread and the queue post are gone -- which is the situation it exists for.
        """
        t0 = self.store.get_ticket(ticket_id)
        if t0 is None:
            return
        g = self.gcfg(t0["guild_id"])
        channel_id = int(g.get("transcript_channel_id") or 0)
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            LOG.warning("transcript channel %d unavailable, ticket=%d not archived",
                        channel_id, ticket_id)
            return

        t = self.store.get_ticket(ticket_id)
        if t is None:
            return
        decisions = self.store.decisions_for(ticket_id)
        # Queried by ticket, not filtered out of kit_history: that is capped and ordered by
        # recency, so for a heavy repeat applicant this ticket's kit could fall off the end
        # and the transcript would quietly report no delivery.
        kits = self.store.kits_for_ticket(ticket_id)
        shown = self.store.shown_chats(ticket_id)
        flagged = [r for r in shown if r["flagged"]]

        opened = card_mod.parse_ts_epoch(t["created_at"])
        closed = card_mod.parse_ts_epoch(t["closed_at"])
        status = str(t["status"])
        colour = {store_mod.STATUS_APPROVED: "#2E6B3F",
                  store_mod.STATUS_DECLINED: "#8A2F2F",
                  store_mod.STATUS_CANCELLED: "#4A4A4A"}.get(status, "#4A4A4A")

        # Deliberately three dense lines in the description rather than stacked fields.
        # Discord gives every inline=False field its own row plus a heading, so the six-field
        # version ran to ~18 rendered lines per ticket and a few dozen tickets became
        # unscrollable. Everything cut from here is still in the attachment, which is the
        # authoritative copy; the message only has to be enough to scan and to search.
        # The colour stripe is kept because it is the fastest outcome cue there is.
        delivered_label = status.upper()
        if kits and kits[0]["delivered_at"]:
            delivered_label = "DELIVERED"
        kind = store_mod.ticket_kind(t)
        details = store_mod.ticket_details(t)
        lines = ["<@%d> → **%s**  ·  `%s`  ·  %s"
                 % (int(t["discord_user_id"]), t["mc_name"],
                    (t["mc_uuid"] or "unresolved")[:8], store_mod.KIND_LABEL[kind])]

        span = []
        if opened:
            span.append("opened %s" % opened.strftime("%d %b %H:%M"))
        if closed:
            span.append("closed %s" % closed.strftime("%d %b %H:%M"))
            if opened:
                span.append("open %s"
                            % card_mod.duration(int((closed - opened).total_seconds())))
        lines.append(" · ".join(span) or "timing unknown")

        third = []
        if decisions:
            d = decisions[-1]
            third.append("reviewer %s" % ("the bot" if int(d["reviewer_id"]) == int(self.user.id)
                                          else "<@%d>" % int(d["reviewer_id"])))
            if d["reason"]:
                third.append(str(d["reason"])[:120])
        if kits and kits[0]["claimed_by"]:
            third.append("runner <@%d>" % int(kits[0]["claimed_by"]))
        elif kits:
            third.append("never claimed")
        third.append("%d lines%s" % (len(shown),
                                     ", %d flagged" % len(flagged) if flagged else ""))
        if t["meet_coords"]:
            third.append("met at %s" % t["meet_coords"].split("  (")[0])
        lines.append(" · ".join(third))

        if t["note"]:
            lines.append("> %s" % str(t["note"]).replace("\n", " ")[:160])
        # A funding ticket's whole case is what it asked for, so the one-line summary has to
        # carry it -- a transcript reading only "project funding, declined" records nothing
        # anybody could learn from later.
        if kind == store_mod.KIND_FUNDING:
            ask = " · ".join(
                "%s: %s" % (label, str(details.get(key) or "-").replace("\n", " "))
                for key, label in card_mod.FUNDING_FIELDS)
            lines.append("> %s" % ask[:400])

        embed = discord.Embed(
            title="Ticket #%d · %s" % (ticket_id, delivered_label),
            description="\n".join(lines)[:4000],
            colour=discord.Colour.from_str(colour))

        # The attachment is what makes this survive a deleted thread.
        parts = ["MELON KITS - TICKET #%d TRANSCRIPT" % ticket_id,
                 "=" * 72,
                 "status      : %s" % t["status"],
                 "request for : %s" % store_mod.KIND_LABEL[kind],
                 "applicant   : discord %s" % t["discord_user_id"],
                 "minecraft   : %s (%s)" % (t["mc_name"], t["mc_uuid"] or "unresolved"),
                 "opened      : %s" % (opened.isoformat() if opened else "?"),
                 "closed      : %s" % (closed.isoformat() if closed else "-"),
                 "request note: %s" % (t["note"] or "-"),
                 "meeting     : %s%s" % (
                     t["meet_coords"] or "not given",
                     " (%s)" % t["meet_dimension"] if t["meet_dimension"] else "")]
        for key, label in card_mod.FUNDING_FIELDS:
            if kind == store_mod.KIND_FUNDING:
                parts.append("%-12s: %s" % (label.lower(), details.get(key) or "-"))
        parts.append("")
        parts.append("DECISIONS")
        for d in decisions:
            parts.append("  %s  %s by %s: %s"
                         % (card_mod.parse_ts_epoch(d["decided_at"]), d["outcome"],
                            d["reviewer_id"], d["reason"] or "-"))
        if not decisions:
            parts.append("  (none)")
        parts += ["", "CHAT SHOWN TO THE REVIEWER (coordinate-redacted at capture)"]
        if shown:
            for r in shown:
                parts.append("  %4d %s %s %s"
                             % (r["position"], "[FLAGGED]" if r["flagged"] else "         ",
                                (r["chat_ts"] or "")[:19], r["chat"]))
        else:
            parts.append("  (no chat on record)")

        convo = await self._thread_conversation(t)
        parts += ["", "APPLICANT THREAD CONVERSATION"]
        if convo:
            parts.append(convo)
        elif g.get("capture_thread_messages"):
            parts.append("  (unavailable - the thread was already deleted, or its content "
                         "could not be read; see the bot log)")
        else:
            parts.append("  (not captured - discord.capture_thread_messages is off)")

        data = ("\n".join(parts) + "\n").encode("utf-8")
        try:
            await channel.send(
                embed=embed,
                file=discord.File(io.BytesIO(data),
                                  filename="ticket-%d-%s.txt" % (ticket_id, t["mc_name"])),
                allowed_mentions=discord.AllowedMentions.none())
            LOG.info("transcript archived ticket=%d bytes=%d", ticket_id, len(data))
        except discord.HTTPException as exc:
            LOG.error("could not post transcript ticket=%d status=%s",
                      ticket_id, getattr(exc, "status", "?"))

    async def _notify_thread(self, ticket: Any, text: str,
                             view: Optional[discord.ui.View] = None) -> None:
        thread_id = ticket["thread_id"]
        if not thread_id:
            return
        channel = self.get_channel(int(thread_id))
        if channel is None:
            return
        try:
            await channel.send(text, view=view) if view else await channel.send(text)
        except discord.HTTPException:
            LOG.warning("could not post to thread=%s", thread_id)

    # -------------------------------------------------------- the meeting point

    async def handle_coords_open(self, interaction: discord.Interaction,
                                 ticket_id: int) -> None:
        """Open the coordinates form. The applicant's own button, in their own thread."""
        gid = interaction.guild_id
        ticket = self.store.get_ticket(ticket_id, gid) if gid else None
        if ticket is None:
            await interaction.response.send_message(
                "There's no ticket #%d in this server." % ticket_id, ephemeral=True)
            return
        # The applicant, or a reviewer relaying coordinates somebody gave them in chat. Anyone
        # else in the thread -- and the runner is in the thread -- gets nothing: a delivery
        # turning up somewhere the applicant did not choose is worse than no delivery.
        if int(ticket["discord_user_id"]) != interaction.user.id \
                and not is_reviewer(interaction.user, self.gcfg(gid)):
            await interaction.response.send_message(
                "Only the person who opened this request can set the meeting point.",
                ephemeral=True)
            return
        await interaction.response.send_modal(CoordsModal(self, ticket_id))

    async def handle_coords_submit(self, interaction: discord.Interaction, ticket_id: int,
                                   raw: str, dimension: str) -> None:
        gid = interaction.guild_id
        ticket = self.store.get_ticket(ticket_id, gid) if gid else None
        if ticket is None:
            await interaction.response.send_message(
                "There's no ticket #%d in this server." % ticket_id, ephemeral=True)
            return

        coords = card_mod.parse_coords(raw)
        if coords is None:
            # Ephemeral and specific, and nothing is stored: the applicant is mid-conversation
            # with somebody about to fly out to them, so "that didn't work" without saying what
            # would work is the one unhelpful answer available.
            await interaction.response.send_message(
                "I couldn't read three numbers out of that. Send it as **X Y Z** - for "
                "example `1200 64 -840`. Press the button again and nothing is lost.",
                ephemeral=True)
            return

        dim = _normalise_dimension(dimension)
        pretty = "%d %d %d" % coords
        # Store the parsed form plus anything else they wrote. A landmark ("by the big cobble
        # tower") is genuinely useful to whoever is flying out and would be thrown away by
        # keeping only the three numbers.
        extra = raw.strip()
        stored = pretty if card_mod.parse_coords(extra) == coords and extra == pretty \
            else "%s  (as given: %s)" % (pretty, extra[:200])
        if not self.store.set_meet_coords(gid, ticket_id, stored, dim):
            await interaction.response.send_message(
                "Couldn't save that against ticket #%d." % ticket_id, ephemeral=True)
            return
        # Note what happened, never the coordinates themselves -- the journal is not the place
        # for someone's location, and the ledger already has it.
        LOG.info("meeting point set ticket=%d by=%s dimension=%s",
                 ticket_id, interaction.user.id, dim or "unspecified")

        await interaction.response.send_message(
            "Got it - **%s**%s. Whoever is delivering will see this. If you move, press the "
            "button again and it'll be updated." % (pretty, " in the %s" % dim if dim else ""),
            ephemeral=True)

        # The thread gets it too, so the runner reading the conversation sees it in place.
        await self._notify_thread(
            ticket, "\U0001F4CD Meeting point set: **%s**%s"
                    % (pretty, " in the %s" % dim if dim else ""))
        await self._show_coords_on_queue_post(ticket, pretty, dim)

    async def _show_coords_on_queue_post(self, ticket: Any, pretty: str,
                                         dim: Optional[str]) -> None:
        """Put the meeting point where the runner is actually looking: the queue post."""
        qthread, qmsg = await self._queue_post(ticket)
        text = "\U0001F4CD Meeting point: **%s**%s" % (pretty,
                                                       " in the %s" % dim if dim else "")
        try:
            if qthread is not None:
                await qthread.send(text, allowed_mentions=discord.AllowedMentions.none())
            elif qmsg is not None:
                # Text-channel queue: a reply keeps it attached to the card rather than
                # floating loose in the channel.
                await qmsg.reply(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            LOG.warning("could not post meeting point to the queue ticket=%s", ticket["id"])

    # -------------------------------------------------------------- the dispatch

    async def handle_claim(self, interaction: discord.Interaction, kit_id: int) -> None:
        gid = interaction.guild_id
        g = self.gcfg(gid)
        if not may_claim(interaction.user, g):
            await interaction.response.send_message(
                "You're not on the delivery team for this.", ephemeral=True)
            return
        kit = self.store.get_kit(kit_id, gid)
        if kit is None:
            await interaction.response.send_message(
                "Dispatch #%d no longer exists." % kit_id, ephemeral=True)
            return

        # Conditional UPDATE, so two people pressing at once is resolved by the database
        # rather than by whichever callback happened to run second.
        if not self.store.claim_kit(kit_id, interaction.user.id):
            holder = self.store.get_kit(kit_id, gid)
            who = "<@%d>" % int(holder["claimed_by"]) if holder and holder["claimed_by"] \
                else "someone else"
            await interaction.response.send_message(
                "%s claimed this one first." % who, ephemeral=True)
            return

        LOG.info("dispatch claimed kit=%d by=%s", kit_id, interaction.user.id)
        view = discord.ui.View(timeout=None)
        view.add_item(DeliveredButton(kit_id))
        if kit["ticket_id"]:
            view = self._with_chat(view, gid, int(kit["ticket_id"]))
        # The interaction response has to land first -- it is on a 3-second clock. Retagging
        # is a separate call and is allowed to be slow or to fail. Only the content changes:
        # the reviewer card embed stays put, because the post is the record of the ticket.
        # Whether the applicant has said where to meet is the first thing the runner needs to
        # know, so it goes in the content rather than leaving them to scroll the post for it.
        ticket = (self.store.get_ticket(int(kit["ticket_id"]), gid)
                  if kit["ticket_id"] else None)
        where = ""
        if ticket is not None and ticket["meet_coords"]:
            where = "\n\U0001F4CD Meeting point: **%s**%s" % (
                ticket["meet_coords"],
                " in the %s" % ticket["meet_dimension"] if ticket["meet_dimension"] else "")
        elif ticket is not None:
            where = "\n\U0001F4CD No meeting point yet - they've been asked for one in their " \
                    "thread."
        await interaction.response.edit_message(
            content="Dispatch #%d - claimed by %s.%s"
                    % (kit_id, interaction.user.mention, where),
            view=view, allowed_mentions=discord.AllowedMentions.none())
        await _set_queue_state(interaction.channel, "claimed")
        # Tell the applicant who is coming, in their own thread.
        if ticket is not None:
            await self._notify_thread(
                ticket, "%s has picked up this delivery and will sort out a meeting "
                        "point with you here." % interaction.user.mention)

    async def handle_delivered(self, interaction: discord.Interaction,
                               kit_id: int) -> None:
        gid = interaction.guild_id
        g = self.gcfg(gid)
        kit = self.store.get_kit(kit_id, gid)
        if kit is None:
            await interaction.response.send_message(
                "Dispatch #%d no longer exists." % kit_id, ephemeral=True)
            return
        claimer = kit["claimed_by"]
        if claimer and int(claimer) != interaction.user.id \
                and not is_reviewer(interaction.user, g):
            await interaction.response.send_message(
                "<@%d> claimed this one - they or a reviewer can close it." % int(claimer),
                ephemeral=True)
            return
        if not self.store.mark_delivered(kit_id):
            await interaction.response.send_message(
                "Dispatch #%d was already marked delivered." % kit_id, ephemeral=True)
            return
        LOG.info("dispatch delivered kit=%d by=%s", kit_id, interaction.user.id)
        await interaction.response.edit_message(
            content="Dispatch #%d - delivered by %s. \U0001F348"
                    % (kit_id, interaction.user.mention),
            view=(self._with_chat(None, gid, int(kit["ticket_id"]))
                  if kit["ticket_id"] else None),
            allowed_mentions=discord.AllowedMentions.none())
        # Archived, not locked: the forum's default view stays on live work without losing
        # the record, and a delivery that falls through after being marked done can be
        # reopened.
        await _set_queue_state(interaction.channel, "delivered", archive=True)
        if kit["ticket_id"]:
            ticket = self.store.get_ticket(int(kit["ticket_id"]))
            if ticket is not None:
                await self._notify_thread(
                    ticket, "Marked delivered. Good luck out there \U0001F348")
                # A delivered kit is the end of the ticket, so this is where the approved
                # path gets archived.
                await self._post_transcript(int(kit["ticket_id"]))


# ------------------------------------------------------------------------ commands

SETUP_TAGS = [
    ("awaiting review", "\U0001F552"), ("approved", "✅"), ("declined", "\U0001F6AB"),
    ("claimed", "\U0001F91D"), ("delivered", "\U0001F348"), ("closed", "\U0001F5C3"),
    # The request type, so the forum's tag filter can separate the two queues. A convenience
    # only: the type is also in every post's title and at the top of every card, because a
    # server that ran /setup before these tags existed will not have them -- adding a tag
    # needs another /setup, which needs Manage Channels back, which /setup itself tells
    # admins to remove. A display that depends on this tag would silently show nothing there.
    ("rescue kit", "\U0001F198"), ("project funding", "\U0001F9F1"),
]


async def build_channels(guild: discord.Guild, reviewer: Optional[discord.Role],
                         runner: Optional[discord.Role]) -> Dict[str, Any]:
    """Create (or adopt) the three channels. Returns their ids plus a readable report.

    Idempotent: a channel with the expected name is reused rather than duplicated, so a
    second /setup after a partial failure finishes the job instead of making a mess.

    Overwrites are only set **at creation**. Editing them afterwards needs Manage Roles
    ("Manage Permissions"), a different permission from Manage Channels and one this bot
    deliberately does not ask for -- so an adopted channel keeps whatever permissions it
    already has, and the report says so rather than implying it was configured.
    """
    me = guild.me
    everyone = guild.default_role
    notes: list = []
    out: Dict[str, Any] = {}

    def find(name, cls):
        return next((c for c in guild.channels
                     if c.name == name and isinstance(c, cls)), None)

    # --- public requests channel: readable by all, postable by none ----------
    ch = find("kit-requests", discord.TextChannel)
    if ch is None:
        ow = {
            everyone: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False,
                create_public_threads=False, create_private_threads=False,
                # True so an applicant can still reply inside their own ticket thread.
                send_messages_in_threads=True),
            me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                read_message_history=True, create_private_threads=True,
                send_messages_in_threads=True, manage_threads=True, manage_messages=True),
        }
        if reviewer:
            ow[reviewer] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                send_messages_in_threads=True, manage_threads=True)
        ch = await guild.create_text_channel(
            "kit-requests", overwrites=ow, reason="Melon Kits setup",
            topic="Ask for a kit. Press the button on the pinned panel - each request "
                  "opens a private thread just for you.")
        notes.append("created %s" % ch.mention)
    else:
        notes.append("reused %s (existing permissions left alone)" % ch.mention)
    out["panel_channel_id"] = ch.id

    # --- staff queue: a forum, invisible to @everyone ------------------------
    forum = find("kit-queue", discord.ForumChannel)
    if forum is None:
        qow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                read_message_history=True, create_public_threads=True,
                send_messages_in_threads=True, manage_threads=True, manage_messages=True),
        }
        for role in (reviewer, runner):
            if role:
                qow[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    send_messages_in_threads=True, attach_files=True, embed_links=True)
        forum = await guild.create_forum(
            "kit-queue", overwrites=qow, reason="Melon Kits setup",
            available_tags=[discord.ForumTag(name=n, emoji=e) for n, e in SETUP_TAGS],
            topic="Staff only. One post per request: the reviewer card, the decision and "
                  "the delivery claim. Filter by tag.")
        notes.append("created %s with %d tags" % (forum.mention, len(SETUP_TAGS)))
    else:
        have = {t.name.casefold() for t in forum.available_tags}
        added = 0
        for name, emoji in SETUP_TAGS:
            if name.casefold() not in have:
                try:
                    await forum.create_tag(name=name, emoji=emoji)
                    added += 1
                except discord.HTTPException:
                    break
        notes.append("reused %s%s" % (forum.mention,
                                      " (+%d tags)" % added if added else ""))
    out["queue_channel_id"] = forum.id

    # --- staff archive: text, read-only for humans ---------------------------
    arch = find("kit-archive", discord.TextChannel)
    if arch is None:
        aow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                attach_files=True, read_message_history=True),
        }
        for role in (reviewer, runner):
            if role:
                aow[role] = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=False,
                    create_public_threads=False, create_private_threads=False)
        arch = await guild.create_text_channel(
            "kit-archive", overwrites=aow, reason="Melon Kits setup",
            topic="Staff only, append-only. One transcript per finished ticket, with the "
                  "chat log attached, so the record outlives the thread and the post.")
        notes.append("created %s" % arch.mention)
    else:
        notes.append("reused %s" % arch.mention)
    out["transcript_channel_id"] = arch.id

    out["notes"] = notes
    return out


def register_commands(app: KitBot) -> None:
    tree = app.tree

    @tree.command(name="setup",
                  description="Set this server up for kit requests: makes the channels and "
                              "posts the panel.")
    @app_commands.describe(
        reviewer_role="Who may approve, decline and see reviewer cards",
        delivery_role="Who may claim a delivery (defaults to the reviewer role)")
    async def setup(interaction: discord.Interaction,
                    reviewer_role: discord.Role,
                    delivery_role: Optional[discord.Role] = None) -> None:
        """The whole install: three channels, the tags, the config, the panel.

        Gated on Manage Server rather than the reviewer role, because at this point there is
        no reviewer role configured -- gating setup on the thing setup creates would be a
        chicken-and-egg lockout.
        """
        if interaction.guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "You need **Manage Server** to set the bot up.", ephemeral=True)
            return

        me = interaction.guild.me
        missing = [n for n in ("manage_channels", "send_messages", "embed_links",
                               "attach_files", "create_private_threads", "manage_threads")
                   if not getattr(me.guild_permissions, n)]
        if missing:
            # Checked up front: a half-finished setup is worse than one that refuses.
            await interaction.response.send_message(
                "I'm missing these server permissions, so setup would half-finish:\n%s\n\n"
                "Re-invite me with them and run `/setup` again. **Manage Channels is only "
                "needed for setup** - you can remove it afterwards."
                % "\n".join("- `%s`" % m.replace("_", " ") for m in missing),
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            built = await build_channels(interaction.guild, reviewer_role,
                                         delivery_role or reviewer_role)
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord refused to create a channel. That is usually my role sitting below "
                "the roles it is being asked to grant, or a category overwrite beating mine.",
                ephemeral=True)
            return

        values = {k: v for k, v in built.items() if k != "notes"}
        values["reviewer_role_id"] = reviewer_role.id
        values["runner_role_id"] = (delivery_role or reviewer_role).id
        app.store.set_guild_config(interaction.guild.id, values)
        LOG.info("guild configured guild=%s by=%s", interaction.guild.id,
                 interaction.user.id)

        g = app.gcfg(interaction.guild.id)
        panel_channel = interaction.guild.get_channel(built["panel_channel_id"])
        try:
            existing = await find_existing_panel(panel_channel)
            if existing is not None:
                await existing.edit(embed=panel_embed(g), view=PanelView())
                panel_note = "updated the panel already in %s" % panel_channel.mention
            else:
                msg = await panel_channel.send(embed=panel_embed(g), view=PanelView())
                panel_note = "posted the panel in %s" % panel_channel.mention
                try:
                    await msg.pin()
                    panel_note += " and pinned it"
                except discord.HTTPException:
                    # Pin Messages is its own permission, separate from Manage Messages.
                    panel_note += (" (couldn't pin - that needs the separate **Pin "
                                   "Messages** permission; harmless, nobody else can post "
                                   "there so it stays the newest message)")
        except discord.HTTPException:
            panel_note = ("couldn't post the panel - run `/panel` in %s once I can post "
                          "there" % panel_channel.mention)

        embed = discord.Embed(
            title="\U0001F348 Melon Kits is set up",
            description="\n".join("- %s" % n for n in built["notes"] + [panel_note]),
            colour=discord.Colour.from_str("#2E6B3F"))
        embed.add_field(
            name="Roles",
            value="Reviewers: %s\nDelivery: %s"
                  % (reviewer_role.mention, (delivery_role or reviewer_role).mention),
            inline=False)
        embed.add_field(
            name="What happens now",
            value="People press the button in %s. Reviewers get a card in the queue with "
                  "Approve and Decline, and finished tickets archive themselves.\n\n"
                  "`/setup` is safe to re-run - it reuses whatever already exists. "
                  "**Manage Channels was only needed for setup and can be removed now.**"
                  % panel_channel.mention,
            inline=False)
        embed.set_footer(text="This server's tickets, cooldowns and flags are its own.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="panel", description="Post the kit-request panel in this channel.")
    async def panel(interaction: discord.Interaction) -> None:
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
            await interaction.response.send_message(
                "Only reviewers can post the panel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        existing = await find_existing_panel(interaction.channel)
        if existing is not None:
            await existing.edit(embed=panel_embed(app.gcfg(interaction.guild_id)),
                                view=PanelView())
            await interaction.followup.send(
                "Updated the panel that was already here, so it keeps its pin and its place. "
                "Run this again after editing any `panel` value in the config.",
                ephemeral=True)
            return
        await interaction.channel.send(
            embed=panel_embed(app.gcfg(interaction.guild_id)), view=PanelView())
        await interaction.followup.send(
            "Panel posted - pin it. The button keeps working indefinitely, so it only needs "
            "posting once; running /panel again edits it in place rather than adding another.",
            ephemeral=True)

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
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
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
        flag_id = app.store.set_flag(interaction.guild_id, kind.value,
                                     interaction.user.id, uuid, mc_name, note)
        await interaction.followup.send(
            "Flagged **%s** as `%s` (flag #%d).%s" % (
                mc_name, kind.value, flag_id,
                "" if uuid else " Couldn't resolve a UUID, so this flag is keyed on the "
                                "name and will follow the name if they rename."),
            ephemeral=True)

    @tree.command(name="unflag", description="Clear a flag by its id.")
    async def unflag(interaction: discord.Interaction, flag_id: int) -> None:
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
            await interaction.response.send_message(
                "Only reviewers can clear flags.", ephemeral=True)
            return
        ok = app.store.clear_flag(interaction.guild_id, flag_id)
        await interaction.response.send_message(
            "Flag #%d cleared." % flag_id if ok
            else "Flag #%d isn't set (or was already cleared)." % flag_id, ephemeral=True)

    @tree.command(name="lookup", description="Build a reviewer card without opening a ticket.")
    async def lookup(interaction: discord.Interaction, mc_name: str) -> None:
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
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
            interaction.guild_id, mc_name, uuid, interaction.user.id, app.cfg, app.vc,
            app.store, app.lex, LOG))
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
                           lines="Line numbers from the Chat history panel, e.g. 3,7,12")
    async def flagline(interaction: discord.Interaction, ticket: int, lines: str) -> None:
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
            await interaction.response.send_message(
                "Only reviewers can label chat.", ephemeral=True)
            return
        positions = []
        for chunk in re.split(r"[,\s]+", lines.strip()):
            if chunk.isdigit():
                positions.append(int(chunk))
        if not positions:
            await interaction.response.send_message(
                "Give me line numbers from the **Chat history** panel on the queue post, "
                "e.g. `3,7,12`.", ephemeral=True)
            return
        if app.store.get_ticket(ticket, interaction.guild_id) is None:
            await interaction.response.send_message(
                "There's no ticket #%d in this server." % ticket, ephemeral=True)
            return
        done = sum(1 for p in positions if app.store.flag_chat(ticket, p))
        await interaction.response.send_message(
            "Labelled %d of %d line(s) on ticket #%d. This is the only labelled screening "
            "data that exists, so thank you - it can't be reconstructed later."
            % (done, len(positions), ticket), ephemeral=True)

    @tree.command(name="close",
                  description="Close a ticket without approving or declining it.")
    @app_commands.describe(ticket="Ticket number",
                           reason="Why - goes in the ledger, not to the applicant")
    async def close(interaction: discord.Interaction, ticket: int, reason: str) -> None:
        """The escape hatch for a ticket that will never get a decision.

        Without this an undecided ticket stays `open` forever, and because the panel's
        pre-check counts open tickets, **the applicant can never request again** -- a silent
        permanent lockout, which is the most expensive failure this app has given that the
        kits are disposable and under-helping is the costly direction.

        Open to the applicant as well as to reviewers: someone withdrawing their own request
        only frees their own slot, and needing to find a staff member to do it is friction
        with no upside.
        """
        row = app.store.get_ticket(ticket, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no ticket #%d." % ticket, ephemeral=True)
            return
        mine = int(row["discord_user_id"]) == interaction.user.id
        if not (mine or is_reviewer(interaction.user, app.gcfg(interaction.guild_id))):
            await interaction.response.send_message(
                "Only reviewers, or the person who opened it, can close a ticket.",
                ephemeral=True)
            return
        if row["status"] != store_mod.STATUS_OPEN:
            await interaction.response.send_message(
                "Ticket #%d is already **%s** - nothing to close." % (ticket, row["status"]),
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        app.store.record_decision(ticket, interaction.user.id,
                                  store_mod.STATUS_CANCELLED, reason)
        LOG.info("ticket closed ticket=%d by=%s self=%s",
                 ticket, interaction.user.id, mine)

        row = app.store.get_ticket(ticket, interaction.guild_id) or row
        qthread, qmsg = await app._queue_post(row)
        if qmsg is not None:
            try:
                await qmsg.edit(
                    content="Ticket #%d - **closed** by %s (no decision)\n> %s"
                            % (ticket, interaction.user.mention, reason),
                    view=app._with_chat(None, interaction.guild_id, ticket),
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                LOG.warning("could not update closed queue post ticket=%d", ticket)
        await _set_queue_state(qthread, "closed", archive=True)

        await app._notify_thread(
            row,
            "This request was **withdrawn**." if mine else
            "This request has been **closed** without a decision. That isn't a no forever - "
            "you're welcome to open a new one whenever you like.")
        if row["thread_id"]:
            ch = app.get_channel(int(row["thread_id"]))
            if isinstance(ch, discord.Thread):
                try:
                    await ch.edit(archived=True)
                except discord.HTTPException:
                    pass

        await app._post_transcript(ticket)
        await interaction.followup.send(
            "Ticket #%d closed. %s can open a new request straight away."
            % (ticket, "You" if mine else "The applicant"), ephemeral=True)

    @tree.command(name="unclaim",
                  description="Hand a delivery back to the pool.")
    @app_commands.describe(kit="Dispatch number from the queue post")
    async def unclaim(interaction: discord.Interaction, kit: int) -> None:
        """For a runner who said they'd do it and can't, or a reviewer prising a stale claim
        off someone who has gone quiet. Leaves the ticket approved -- the kit is still owed,
        it just needs somebody else."""
        row = app.store.get_kit(kit, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no dispatch #%d." % kit, ephemeral=True)
            return
        if row["delivered_at"]:
            await interaction.response.send_message(
                "Dispatch #%d is already marked delivered." % kit, ephemeral=True)
            return
        if row["claimed_by"] is None:
            await interaction.response.send_message(
                "Dispatch #%d isn't claimed by anyone." % kit, ephemeral=True)
            return

        holder = int(row["claimed_by"])
        reviewer = is_reviewer(interaction.user, app.gcfg(interaction.guild_id))
        if holder == interaction.user.id:
            ok = app.store.unclaim_kit(kit, interaction.user.id)
        elif reviewer:
            ok = app.store.release_kit(kit)
        else:
            await interaction.response.send_message(
                "<@%d> claimed this one - they or a reviewer can hand it back." % holder,
                ephemeral=True)
            return
        if not ok:
            await interaction.response.send_message(
                "Couldn't release dispatch #%d - someone changed it just now." % kit,
                ephemeral=True)
            return

        LOG.info("dispatch released kit=%d by=%s was_held_by=%s",
                 kit, interaction.user.id, holder)
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Put the Claim button back on the queue post and return it to `approved`.
        ticket = (app.store.get_ticket(int(row["ticket_id"]), interaction.guild_id)
                  if row["ticket_id"] else None)
        if ticket is not None:
            qthread, qmsg = await app._queue_post(ticket)
            if qmsg is not None:
                view = discord.ui.View(timeout=None)
                view.add_item(ClaimButton(kit))
                view = app._with_chat(view, interaction.guild_id, int(ticket["id"]))
                try:
                    await qmsg.edit(
                        content="Dispatch #%d - back in the pool, unclaimed." % kit,
                        view=view, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    LOG.warning("could not restore claim button kit=%d", kit)
            if qthread is not None:
                try:
                    await qthread.edit(archived=False)
                except discord.HTTPException:
                    pass
            await _set_queue_state(qthread, "approved")

        await interaction.followup.send(
            "Dispatch #%d is claimable again." % kit, ephemeral=True)

    @tree.command(name="ledger", description="Kit history and flags for an account.")
    async def ledger(interaction: discord.Interaction, mc_name: str) -> None:
        if not is_reviewer(interaction.user, app.gcfg(interaction.guild_id)):
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

        kits = (app.store.kit_history(interaction.guild_id, mc_uuid=uuid, limit=10)
                if uuid else [])
        if kits:
            for k in kits:
                when = card_mod.ago(card_mod.parse_ts_epoch(k["created_at"]))
                state = ("delivered" if k["delivered_at"] else
                         "claimed" if k["claimed_by"] else "unclaimed")
                text.append("- kit #%d, %s, %s" % (k["id"], when, state))
        else:
            text.append("No kits on record for this account.")

        cd = app.store.cooldown(interaction.guild_id,
                                app.gcfg(interaction.guild_id)["cooldown_days"],
                                mc_uuid=uuid)
        text.append("Cooldown: %s" % ("**%d day(s) left**" % cd["days_left"]
                                      if cd["blocked"] else "clear"))

        flags = app.store.flags_for(interaction.guild_id, mc_uuid=uuid, mc_name=mc_name)
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
