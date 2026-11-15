"""
Brutus - Warn, mute, kick and ban from a message's Apps menu.
Copyright (C) 2026-present  EternalllZM

Quick action logic adapted from Defender by Twentysix (https://github.com/Twentysix26/)
and the codzombies fork of it.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import contextlib
import datetime
import logging
import re
from copy import copy
from types import SimpleNamespace
from typing import TYPE_CHECKING, List, Optional, Union

import discord
from redbot.core import commands, modlog
from redbot.core.utils.chat_formatting import bold
from redbot.core.utils.mod import get_audit_reason

if TYPE_CHECKING:
    from .brutus import Brutus

log = logging.getLogger("red.codz.brutus")

ACTIONS = ("warn", "mute", "kick", "ban")
TITLES = {"warn": "Warn", "mute": "Mute", "kick": "Kick", "ban": "Ban"}
DONE = {"warn": "warned", "mute": "muted", "kick": "kicked", "ban": "banned"}

# The Red command each action stands in for, and the cog that owns it. Whoever can run the
# command in the message's channel can use the action, so Red's permission rules apply as-is
COMMANDS = {
    "warn": ("warn", "Warnings"),
    "mute": ("mute", "Mutes"),
    "kick": ("kick", "Mod"),
    "ban": ("ban", "Mod"),
}

MAX_REASONS = 25  # Discord's limit of options in a select menu
LABEL_MAX = 100  # Discord's limit for a select option's label
REASON_MAX = 200
DETAILS_MAX = 300  # Reason + details stay under the 512 characters of the audit log
WARN_POINTS_MAX = 9999


def parse_reasons(text: str) -> List[List[str]]:
    """Parses `Label:Reason|Label:Reason` into [[label, reason], ...]

    A part without `:` uses its text as both label and reason. Raises ValueError on invalid input"""
    entries = []
    seen = set()
    for raw in text.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        label, sep, reason = raw.partition(":")
        label = label.strip()
        reason = reason.strip() if sep else label
        if not label:
            raise ValueError(f"`{raw}` has no label before the `:`.")
        if not reason:
            raise ValueError(f"`{label}` has no reason after the `:`.")
        if len(label) > LABEL_MAX:
            raise ValueError(f"The label `{label}` is longer than {LABEL_MAX} characters.")
        if len(reason) > REASON_MAX:
            raise ValueError(f"The reason for `{label}` is longer than {REASON_MAX} characters.")
        if label.lower() in seen:
            raise ValueError(f"The label `{label}` is used more than once.")
        seen.add(label.lower())
        entries.append([label, reason])
    if not entries:
        raise ValueError("No reasons given. The format is `Label:Reason|Label:Reason`.")
    return entries


async def text_prefix(bot, guild: discord.Guild) -> str:
    """The first non-mention prefix, as a mention one would make commands read `<@id> unwarn ...`"""
    prefixes = await bot.get_valid_prefixes(guild)
    return next((p for p in prefixes if not re.fullmatch(r"<@!?\d+> ", p)), prefixes[0])


def anchor_message(inter: discord.Interaction, source: Optional[discord.Message] = None) -> discord.Message:
    """A message in the channel the action is taken from: the targeted message, or (from a user's
    Apps menu, where there's none) a synthetic one, built like discord.py's Context.from_interaction"""
    if source is not None:
        return source
    payload = {
        "id": inter.id,
        "reactions": [],
        "embeds": [],
        "mention_everyone": False,
        "tts": False,
        "pinned": False,
        "edited_timestamp": None,
        "type": discord.MessageType.context_menu_command.value,
        "flags": 64,
        "content": "",
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
    }
    channel = inter.channel or discord.PartialMessageable(
        state=inter._state, guild_id=inter.guild_id, id=inter.channel_id
    )
    message = discord.Message(state=inter._state, channel=channel, data=payload)
    message.author = inter.user
    return message


def as_moderator(anchor: discord.Message, moderator: discord.Member, content: str) -> discord.Message:
    """A copy of `anchor` that reads as `moderator` having typed `content` in its channel"""
    message = copy(anchor)
    message.author = moderator
    message.content = content
    return message


async def can_run_command(cog: "Brutus", anchor: discord.Message, moderator: discord.Member, action: str) -> bool:
    """Whether `moderator` could run the action's Red command in the channel of `anchor`

    Covers Red's own checks (admin/mod roles, Discord permissions, the bot's permissions)
    and anything set with the Permissions cog, like [p]warningset allowcustomreasons does"""
    name, _ = COMMANDS[action]
    command = cog.bot.get_command(name)
    if command is None:
        return False
    prefix = await text_prefix(cog.bot, moderator.guild)
    ctx = await cog.bot.get_context(as_moderator(anchor, moderator, f"{prefix}{name}"))
    try:
        return await command.can_run(ctx, check_all_parents=True, change_permission_state=False)
    except commands.CommandError:
        return False


async def denial(
    cog: "Brutus", moderator: discord.Member, action: str, target_id: int, anchor: discord.Message
) -> Optional[str]:
    """Returns why `moderator` can't run `action` on `target_id`, or None if they can"""
    bot = cog.bot
    guild = moderator.guild

    if await bot.cog_disabled_in_guild(cog, guild):
        return "Brutus is disabled in this server."

    name, cog_name = COMMANDS[action]
    owner = bot.get_cog(cog_name)
    command = bot.get_command(name)
    if owner is None or command is None or command.cog is not owner:
        return f"The {cog_name} cog is not loaded, so I can't {action} users."
    if await bot.cog_disabled_in_guild(owner, guild):
        return f"The {cog_name} cog is disabled in this server, so I can't {action} users."

    if not await can_run_command(cog, anchor, moderator, action):
        return f"Denied. You can't use `{name}` in this channel, so you can't use this action here either."

    if target_id == moderator.id:
        return "I cannot let you do that. Self-harm is bad \N{PENSIVE FACE}"
    if target_id == bot.user.id:
        return f"I cannot {action} myself."

    target = guild.get_member(target_id)
    if target is None:
        # The user left. Bans can still be carried out (hackban), other actions can't
        if action == "ban":
            return None
        return "That user isn't in the server anymore."

    if target == guild.owner:
        return "I cannot do that due to Discord hierarchy rules."

    if await bot.is_mod(target):
        return "Denied. You're trying to take action on a staff member."

    is_special = moderator == guild.owner or await bot.is_owner(moderator)
    if action in ("kick", "ban"):
        # Same as [p]kick / [p]ban, including Mod's `respect_hierarchy` setting
        respect = await owner.config.guild(guild).respect_hierarchy()
        if respect and not (is_special or moderator.top_role > target.top_role):
            return "I cannot let you do that. You are not higher than the user in the role hierarchy."
        if guild.me.top_role <= target.top_role:
            return "I cannot do that due to Discord hierarchy rules."
    elif not (is_special or moderator.top_role > target.top_role):
        # [p]mute (in Mutes.mute_user) and [p]warn check this one regardless of settings
        return "Denied. Your top role must be higher than the target's to take action on them."

    if action == "warn" and target.bot:
        return "You cannot warn other bots."

    return None


async def _send_kickban_dm(cog: "Brutus", guild: discord.Guild, target: discord.Member, reason, *, ban: bool) -> bool:
    """The DM [p]kick / [p]ban send, if Mod's `dm_on_kickban` is on. Must be sent *before* the user is
    removed, as we can't DM them once we no longer share a server. Returns False if it failed to send"""
    mod_conf = cog.bot.get_cog("Mod").config.guild(guild)
    if not await mod_conf.dm_on_kickban():
        return True
    em = discord.Embed(
        title=bold(f"You have been {'banned' if ban else 'kicked'} from {guild}."),
        color=await cog.bot.get_embed_color(target),
    )
    em.add_field(
        name="**Reason**",
        value=reason if reason is not None else "No reason was given.",
        inline=False,
    )
    if ban and await mod_conf.ban_show_extra():
        em.add_field(
            name=bold(await mod_conf.ban_extra_embed_title(), escape_formatting=False),
            value=await mod_conf.ban_extra_embed_contents(),
            inline=False,
        )
    try:
        await target.send(embed=em)
    except discord.HTTPException:
        return False
    return True


async def _warn_threshold_actions(cog: "Brutus", warnings, moderator, anchor, target, points: int):
    """Runs the Warnings cog's action for the target's new point total, like [p]warn does

    Those actions are commands (e.g. `{prefix}mute {user} 1h`) invoked as if the moderator
    typed them in the channel the warning was given from"""
    try:
        from redbot.cogs.warnings.helpers import warning_points_add_check
    except ImportError:
        log.error("Could not import the Warnings cog's helpers, skipping warning point actions", exc_info=True)
        return
    ctx = SimpleNamespace(
        bot=cog.bot,
        guild=moderator.guild,
        message=as_moderator(anchor, moderator, ""),
        prefix=await text_prefix(cog.bot, moderator.guild),
    )
    try:
        await warning_points_add_check(warnings.config, ctx, target, points)
    except Exception:
        log.error("Failed to run the warning point action for %s", target.id, exc_info=True)


class Outcome:
    """What happened, for the moderator's reply"""

    def __init__(self, error: Optional[str] = None):
        self.error = error
        self.case: Optional[modlog.Case] = None
        self.dm_failed = False
        self.extra = ""  # Appended after "`user` has been <done>"
        self.notes: List[str] = []  # Shown in small text below
        self.target: Union[discord.Member, discord.User, discord.Object, None] = None


async def _warn(cog, inter, moderator, target: discord.Member, reason: str, points: int, anchor) -> Outcome:
    """Warns `target` the way [p]warn does: DM, stored warning, point actions, warn channel, case"""
    out = Outcome()
    bot = cog.bot
    guild = moderator.guild
    warnings = bot.get_cog("Warnings")
    # [p]warn keys warnings by the command message's ID, which [p]unwarn then takes. The interaction
    # ID is a snowflake as well, unique and timestamped the same way
    warning_id = str(inter.id)

    settings = await warnings.config.guild(guild).all()
    colour = await bot.get_embed_colour(anchor.channel)

    def make_embed() -> discord.Embed:
        title = f"Warning from {moderator}" if settings["show_mod"] else "Warning"
        em = discord.Embed(title=title, description=reason, color=colour)
        em.add_field(name="Points", value=str(points))
        return em

    if settings["toggle_dm"]:
        try:
            await target.send(f"You have received a warning in {guild.name}.", embed=make_embed())
        except discord.HTTPException:
            out.dm_failed = True

    member_settings = warnings.config.member(target)
    async with member_settings.warnings() as user_warnings:
        user_warnings[warning_id] = {"points": points, "description": reason, "mod": moderator.id}
    total = await member_settings.total_points() + points
    await member_settings.total_points.set(total)
    await _warn_threshold_actions(cog, warnings, moderator, anchor, target, total)

    if settings["toggle_channel"]:
        warn_channel = bot.get_channel(settings["warn_channel"])
        if warn_channel and warn_channel.permissions_for(guild.me).send_messages:
            with contextlib.suppress(discord.HTTPException):
                await warn_channel.send(f"{target.mention} has been warned.", embed=make_embed())

    prefix = await text_prefix(bot, guild)
    case_reason = (
        f"{reason}\nPoints: {points}\n\n"
        f"Use `{prefix}unwarn {target.id} {warning_id}` to remove this warning."
    )
    out.case = await modlog.create_case(
        bot, guild, discord.utils.utcnow(), "warning", target, moderator, case_reason, until=None, channel=None
    )
    out.extra = f" ({points} point{'s' if points != 1 else ''}, {total} total)"
    return out


async def _mute(cog, moderator, target: discord.Member, reason: str, duration) -> Outcome:
    """Mutes `target` the way [p]mute does, through the Mutes cog (role or timeout, as it's set up)"""
    out = Outcome()
    bot = cog.bot
    guild = moderator.guild
    mutes = bot.get_cog("Mutes")
    now = discord.utils.utcnow()
    if duration is None:
        default = await mutes.config.guild(guild).default_time()
        if default:
            duration = datetime.timedelta(seconds=default)
    until = now + duration if duration else None

    response = await mutes.mute_user(guild, moderator, target, until, get_audit_reason(moderator, reason, shorten=True))
    if not response.success:
        return Outcome(f"I could not mute `{target}`: {response.reason}")

    out.case = await modlog.create_case(
        bot, guild, now, "smute", target, moderator, reason, until=until, channel=None
    )
    # Mutes' own DM, so it looks like [p]mute and honors `[p]muteset senddm`
    send_dm = getattr(mutes, "_send_dm_notification", None)
    if send_dm is not None:
        try:
            await send_dm(target, moderator, guild, "Server mute", reason, duration)
        except Exception:
            log.error("Failed to send the mute DM to %s", target.id, exc_info=True)
    if until:
        out.extra = f" until {discord.utils.format_dt(until)}"
    if response.reason:  # Muted, but with a warning (e.g. couldn't move them out of voice)
        out.notes.append(response.reason)
    return out


async def _kick(cog, moderator, target: discord.Member, reason: str) -> Outcome:
    out = Outcome()
    guild = moderator.guild
    out.dm_failed = not await _send_kickban_dm(cog, guild, target, reason, ban=False)
    try:
        await guild.kick(target, reason=get_audit_reason(moderator, reason, shorten=True))
    except discord.Forbidden:
        return Outcome("I'm not allowed to do that.")
    except discord.HTTPException as e:
        log.exception("%s (%s) attempted to kick %s (%s), but an error occurred.", moderator, moderator.id, target, target.id)
        return Outcome(f"I could not kick `{target}`: {e}")
    log.info("%s (%s) kicked %s (%s)", moderator, moderator.id, target, target.id)
    out.case = await modlog.create_case(
        cog.bot, guild, discord.utils.utcnow(), "kick", target, moderator, reason, until=None, channel=None
    )
    return out


async def _ban(cog, moderator, target_id: int, reason: str, delete_seconds: int) -> Outcome:
    """Bans the way [p]ban does. Users who left are hackbanned, like [p]ban <user_id>"""
    out = Outcome()
    bot = cog.bot
    guild = moderator.guild
    target = guild.get_member(target_id)

    if target is not None:
        ban_type = "ban"
        out.dm_failed = not await _send_kickban_dm(cog, guild, target, reason, ban=True)
    else:
        ban_type = "hackban"
        target = bot.get_user(target_id)
        if target is None:
            try:
                target = await bot.fetch_user(target_id)
            except discord.NotFound:
                return Outcome(f"No Discord account exists with the ID `{target_id}`.")
            except discord.HTTPException:
                target = discord.Object(id=target_id)
        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass  # Can't tell. The ban below will say if something's wrong
        else:
            return Outcome(f"User with ID {target_id} is already banned.")

    try:
        await guild.ban(
            target, reason=get_audit_reason(moderator, reason, shorten=True), delete_message_seconds=delete_seconds
        )
    except discord.Forbidden:
        return Outcome("I'm not allowed to do that.")
    except discord.NotFound:
        return Outcome(f"User with ID {target_id} not found")
    except discord.HTTPException as e:
        log.exception("%s (%s) attempted to %s %s, but an error occurred.", moderator, moderator.id, ban_type, target_id)
        return Outcome(f"I could not ban that user: {e}")
    log.info(
        "%s (%s) %sned %s (%s), deleting %s seconds worth of messages.",
        moderator, moderator.id, ban_type, target, target_id, delete_seconds,
    )

    out.target = target
    out.case = await modlog.create_case(
        bot, guild, discord.utils.utcnow(), ban_type, target, moderator, reason, until=None, channel=None
    )
    if ban_type == "hackban":
        out.extra = " (they had already left the server)"
    return out


async def run_action(
    inter: discord.Interaction,
    cog: "Brutus",
    action: str,
    target_id: int,
    reason: str,
    source: Optional[discord.Message],
    *,
    duration: Optional[datetime.timedelta] = None,
    points: int = 1,
    delete_seconds: int = 0,
):
    """Runs `action` after the modal is submitted, then deletes `source` (the targeted message,
    None from a user's Apps menu) if it's the target's"""
    await inter.response.defer(ephemeral=True, thinking=True)
    moderator: discord.Member = inter.user
    anchor = anchor_message(inter, source)

    async def reply(text: str):
        await inter.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # Checked again: the ID may have been edited, and things may have changed while the modal was open
    reason_denied = await denial(cog, moderator, action, target_id, anchor)
    if reason_denied:
        return await reply(reason_denied)

    target = moderator.guild.get_member(target_id)
    if action == "warn":
        out = await _warn(cog, inter, moderator, target, reason, points, anchor)
    elif action == "mute":
        out = await _mute(cog, moderator, target, reason, duration)
    elif action == "kick":
        out = await _kick(cog, moderator, target, reason)
    else:
        out = await _ban(cog, moderator, target_id, reason, delete_seconds)
    if out.error:
        return await reply(out.error)
    target = out.target or target

    text = f"`{target if isinstance(target, discord.abc.User) else target_id}` has been {DONE[action]}{out.extra}."
    notes = list(out.notes)
    if out.case is not None:
        notes.append(f"Case #{out.case.case_number}")
    if out.dm_failed:
        notes.append("I couldn't DM them about it")

    # The message is only the target's if the user ID wasn't changed to someone else in the modal
    if source is not None and source.author.id == target_id and await cog.config.guild(moderator.guild).delete_message():
        try:
            await source.delete()
        except discord.NotFound:
            pass  # Already gone, e.g. removed by the ban's message deletion
        except discord.HTTPException:
            notes.append("I couldn't delete their message")

    if notes:
        text += f"\n-# {' · '.join(notes)}"
    await reply(text)
