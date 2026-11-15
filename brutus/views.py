import datetime
import logging
from typing import TYPE_CHECKING, List, Optional

import discord
from discord import SelectOption, ui
from redbot.core import commands
from redbot.core.commands.converter import parse_timedelta
from redbot.core.utils.chat_formatting import humanize_timedelta

from .actions import DETAILS_MAX, MAX_REASONS, TITLES, WARN_POINTS_MAX, run_action

if TYPE_CHECKING:
    from .brutus import Brutus

log = logging.getLogger("red.codz.brutus")

# Same choices as Discord's own ban dialog. Red's [p]ban takes whole days (0 to 7)
DELETE_HISTORY = (
    (0, "Don't delete any"),
    (3600, "Previous hour"),
    (6 * 3600, "Previous 6 hours"),
    (12 * 3600, "Previous 12 hours"),
    (86400, "Previous 24 hours"),
    (3 * 86400, "Previous 3 days"),
    (7 * 86400, "Previous 7 days"),
)


def delete_history_options(default_seconds: int) -> List[SelectOption]:
    choices = dict(DELETE_HISTORY)
    if default_seconds not in choices:  # Mod's `defaultdays` can be any day from 0 to 7
        days = default_seconds // 86400
        choices[default_seconds] = f"Previous {days} day{'s' if days != 1 else ''}"
    return [
        SelectOption(
            label=label,
            value=str(seconds),
            default=seconds == default_seconds,
            description="Server default" if seconds == default_seconds else None,
        )
        for seconds, label in sorted(choices.items())
    ]


class ActionModal(ui.Modal):
    """Asks for the target, a preset reason (if the action has any), optional details
    and what the action needs on top: a duration to mute, points to warn, messages to delete on ban"""

    def __init__(
        self,
        cog: "Brutus",
        action: str,
        source: discord.Message,
        reasons: List[List[str]],
        *,
        mute_default: Optional[int] = None,
        mute_needs_duration: bool = False,
        ban_delete_seconds: int = 0,
    ):
        super().__init__(title=f"Quick action: {TITLES[action]}", timeout=600)
        self.cog = cog
        self.action = action
        self.source = source
        self.reasons = reasons[:MAX_REASONS]

        self.target_input = ui.TextInput(default=str(source.author.id), min_length=15, max_length=21)
        self.add_item(
            ui.Label(
                text="User ID",
                description="The author of the message. Their message is deleted once action is taken.",
                component=self.target_input,
            )
        )

        self.reason_select: Optional[ui.Select] = None
        if self.reasons:
            options = []
            for i, (label, reason) in enumerate(self.reasons):
                description = None
                if reason != label:
                    description = reason if len(reason) <= 100 else reason[:99] + "…"
                options.append(SelectOption(label=label, value=str(i), description=description))
            self.reason_select = ui.Select(placeholder="Pick a reason", options=options, required=True)
            self.add_item(ui.Label(text="Reason", component=self.reason_select))

        self.details_input = ui.TextInput(
            required=not self.reasons,  # Without presets this is the only reason there is
            max_length=DETAILS_MAX,
            placeholder="Context for the case, e.g. what they posted",
        )
        self.add_item(
            ui.Label(
                text="Details" if self.reasons else "Reason",
                description=(
                    "Optional. Added after the reason."
                    if self.reasons
                    else "Required, as this action has no preset reasons."
                ),
                component=self.details_input,
            )
        )

        self.duration_input: Optional[ui.TextInput] = None
        if action == "mute":
            if mute_default:
                hint = f"Optional. Defaults to {humanize_timedelta(seconds=mute_default)}."
            elif mute_needs_duration:
                hint = "Required, as mutes are timeouts in this server."
            else:
                hint = "Optional. Leave empty to mute until unmuted."
            self.duration_input = ui.TextInput(
                required=mute_needs_duration and not mute_default, max_length=30, placeholder="e.g. 30m, 2h, 3d"
            )
            self.add_item(ui.Label(text="Duration", description=hint, component=self.duration_input))

        self.points_input: Optional[ui.TextInput] = None
        if action == "warn":
            self.points_input = ui.TextInput(required=False, max_length=4, placeholder="1")
            self.add_item(ui.Label(text="Points", description="Optional. Defaults to 1.", component=self.points_input))

        self.delete_select: Optional[ui.Select] = None
        if action == "ban":
            self.delete_select = ui.Select(options=delete_history_options(ban_delete_seconds), required=True)
            self.add_item(ui.Label(text="Delete message history", component=self.delete_select))

    async def on_submit(self, inter: discord.Interaction):
        async def error(text: str):
            await inter.response.send_message(text, ephemeral=True)

        try:
            target_id = int(self.target_input.value.strip())
        except ValueError:
            return await error(f"`{self.target_input.value}` is not a valid user ID.")

        preset = None
        if self.reason_select is not None and self.reason_select.values:
            preset = self.reasons[int(self.reason_select.values[0])][1]
        details = self.details_input.value.strip()
        reason = " - ".join(part for part in (preset, details) if part) or None

        duration = None
        raw_duration = self.duration_input.value.strip() if self.duration_input is not None else ""
        if raw_duration:
            try:
                duration = parse_timedelta(raw_duration, minimum=datetime.timedelta(minutes=1))
            except commands.BadArgument as e:
                return await error(f"Invalid duration: {e}")
            if duration is None:  # parse_timedelta returns None when nothing in the text looks like a time
                return await error(f"`{raw_duration}` is not a valid duration. Use something like `30m`, `2h` or `3d`.")

        points = 1
        raw_points = self.points_input.value.strip() if self.points_input is not None else ""
        if raw_points:
            try:
                points = int(raw_points)
            except ValueError:
                points = -1
            if not 0 <= points <= WARN_POINTS_MAX:
                return await error(
                    f"`{raw_points}` is not a valid number of points. Use a whole number from 0 to {WARN_POINTS_MAX}."
                )

        delete_seconds = 0
        if self.delete_select is not None and self.delete_select.values:
            delete_seconds = int(self.delete_select.values[0])

        await run_action(
            inter,
            self.cog,
            self.action,
            target_id,
            reason,
            self.source,
            duration=duration,
            points=points,
            delete_seconds=delete_seconds,
        )

    async def on_error(self, inter: discord.Interaction, error: Exception):
        log.error("Brutus %s failed", self.action, exc_info=error)
        text = "Something went wrong while taking action. Check the bot's logs."
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass
