"""
Brutus - Warn, mute, kick and ban from a message's Apps menu.
Copyright (C) 2026-present  EternalllZM

Ban message export adapted from the codzombies fork of Defender by Twentysix (https://github.com/Twentysix26/).

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

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, List, Optional, Tuple

import discord
from redbot.core.utils.chat_formatting import escape

from .viewuser import _defender_subject, get_defender

if TYPE_CHECKING:
    from .brutus import Brutus

log = logging.getLogger("red.codz.brutus")


@dataclass
class Capture:
    """The target's cached messages, read right before the ban"""

    channel: discord.abc.Messageable
    lines: List[str]
    total: Optional[int]  # Messages sent in the server, if they're still a member


def _monitor(guild: discord.Guild, bot, entry: str):
    """Logs to Defender's monitor, like the fork's quick action export does"""
    defender = get_defender(bot)
    if defender is not None and hasattr(defender, "send_to_monitor"):
        defender.send_to_monitor(guild, f"[Brutus] {entry}")


async def capture_ban_export(
    cog: "Brutus", guild: discord.Guild, moderator: discord.Member, target_id: int
) -> Tuple[Optional[Capture], Optional[str]]:
    """Reads the target's cached messages *before* the ban, if the server wants them exported

    Returns (capture or None, a note for the moderator if the export can't be done)"""
    conf = cog.config.guild(guild)
    if not await conf.ban_export():
        return None, None
    channel = guild.get_channel_or_thread(await conf.ban_export_channel())
    if channel is None:
        _monitor(guild, cog.bot, "Ban message export is on but its channel is gone. Set a new one.")
        return None, "Messages not exported: the export channel is gone"
    defender = get_defender(cog.bot)
    if defender is None:
        return None, "Messages not exported: Defender's message cache isn't loaded"
    subject = _defender_subject(defender, guild, target_id)
    if subject is None:
        return None, "Messages not exported: couldn't read Defender's message cache"
    try:
        # Same output (and channel permission filtering) as the mod running [p]defender messages exportuser
        lines = await defender.make_message_log(subject, guild=guild, requester=moderator, full_timestamps=True)
        total = await defender.get_total_recorded_messages(subject) if isinstance(subject, discord.Member) else None
    except Exception:
        log.error("Failed to build the ban message export for %s", target_id, exc_info=True)
        return None, "Messages not exported: something went wrong, check the bot's logs"
    return Capture(channel, lines, total), None


async def post_ban_export(cog: "Brutus", guild: discord.Guild, capture: Capture, target, moderator, reason, case) -> str:
    """Posts the export with the user, moderator, reason and case. Returns a note for the moderator"""
    target_name = str(target) if isinstance(target, discord.abc.User) else "Unknown user"
    case_text = "None"
    if case is not None:
        case_text = f"#{case.case_number}"
        if getattr(case, "message", None) is not None:
            case_text = f"[#{case.case_number}]({case.message.jump_url})"
    counted = f"{len(capture.lines)} cached" if capture.lines else "None cached"
    if capture.total is not None:
        counted += f" (total sent in server: {capture.total})"
    header = (
        f"**Ban message export**\n"
        f"**User:** {escape(target_name, formatting=True)} (`{target.id}`) <@{target.id}>\n"
        f"**Moderator:** {escape(str(moderator), formatting=True)} (`{moderator.id}`)\n"
        f"**Reason:** {reason or 'None'}\n"
        f"**Case:** {case_text}\n"
        f"**Messages:** {counted}"
    )[:2000]
    file = None
    if capture.lines:
        name = f"{discord.utils.utcnow().strftime('%Y-%m-%d')}-{target.id}.txt"
        file = discord.File(BytesIO("\n".join(capture.lines).encode("utf-8")), name)
    try:
        await capture.channel.send(header, file=file, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        _monitor(guild, cog.bot, f"Failed to post the ban message export of {target_name} ({target.id}): {e}")
        return f"Messages not exported: I couldn't post in {capture.channel.mention}"
    if capture.lines:
        _monitor(guild, cog.bot, f"{moderator} ({moderator.id}) exported message history of user {target_name} ({target.id})")
        return f"{len(capture.lines)} cached messages exported to {capture.channel.mention}"
    return f"No cached messages: export posted to {capture.channel.mention}"
