import logging
from typing import Dict, Optional, Tuple, Union

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_timedelta, pagify

from .actions import ACTIONS, MAX_REASONS, TITLES, anchor_message, denial, parse_reasons
from .views import ActionModal
from .viewuser import ViewUserModal, can_view, get_defender

log = logging.getLogger("red.codz.brutus")

# Who sees each entry in the Apps menu by default. Server admins can change this in
# Server Settings > Integrations. Whoever sees it still needs to be able to run the matching
# Red command ([p]warn, [p]mute, [p]kick, [p]ban) to use it
DEFAULT_PERMISSIONS = {
    "warn": discord.Permissions(moderate_members=True),
    "mute": discord.Permissions(moderate_members=True),
    "kick": discord.Permissions(kick_members=True),
    "ban": discord.Permissions(ban_members=True),
    "view": discord.Permissions(moderate_members=True),
}


def reason_action(argument: str) -> str:
    argument = argument.lower()
    if argument not in ACTIONS:
        raise commands.BadArgument(f"Action must be one of: {', '.join(ACTIONS)}.")
    return argument


class Brutus(commands.Cog):
    """Warn, mute, kick, ban and view users from the Apps menu

    Right click (or long press) a message or a user > Apps > Warn / Mute / Kick / Ban opens
    a form with their ID, a preset reason and optional details. The action runs like the
    matching Red command: same DM (if turned on in that cog), same modlog case. When it's
    taken from a message, the message is deleted afterwards.

    Apps > View User shows their modlog cases, recent messages (from Defender's cache)
    or user info (or all three with Check, like [p]check), only to you.
    """

    __version__ = "1.1"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xB2075, force_registration=True)
        self.config.register_guild(reasons={}, delete_message=True, ban_export=False, ban_export_channel=None)

        # The same five entries in the Apps menu of messages and of users. Discord keys them by name and
        # type, so both can be called "Warn" etc. Each type has its own limit of 5 per bot
        self.menus: Dict[Tuple[str, str], app_commands.ContextMenu] = {}
        for kind in ("message", "user"):
            for entry, name in (*TITLES.items(), ("view", "View User")):
                menu = app_commands.ContextMenu(name=name, callback=self._make_callback(kind, entry))
                menu.default_permissions = DEFAULT_PERMISSIONS[entry]
                menu.guild_only = True
                menu.allowed_contexts = app_commands.AppCommandContext(guild=True)
                self.menus[(kind, entry)] = menu

    def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete: Brutus stores no user data"""
        return

    async def cog_load(self):
        for menu in self.menus.values():
            self.bot.tree.add_command(menu)

    async def cog_unload(self):
        for menu in self.menus.values():
            self.bot.tree.remove_command(menu.name, type=menu.type)

    #
    #   Apps menu
    #

    def _make_callback(self, kind: str, entry: str):
        """The callback of an Apps menu entry. discord.py reads its annotation to know if it's for messages or users"""
        if kind == "message":

            async def on_message(inter: discord.Interaction, message: discord.Message):
                if message.webhook_id is not None:
                    return await inter.response.send_message(
                        "That message was sent by a webhook, so there's no user to act on.", ephemeral=True
                    )
                await self._dispatch(inter, entry, message.author.id, message)

            return on_message

        async def on_user(inter: discord.Interaction, user: Union[discord.Member, discord.User]):
            await self._dispatch(inter, entry, user.id, None)

        return on_user

    async def _dispatch(self, inter: discord.Interaction, entry: str, target_id: int, source: Optional[discord.Message]):
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("This only works in servers.", ephemeral=True)
        if entry == "view":
            await self._open_view(inter, target_id, source)
        else:
            await self._open(inter, entry, target_id, source)

    async def _open_view(self, inter: discord.Interaction, target_id: int, source: Optional[discord.Message]):
        if await self.bot.cog_disabled_in_guild(self, inter.guild):
            return await inter.response.send_message("Brutus is disabled in this server.", ephemeral=True)
        if not await can_view(self.bot, inter.user):
            return await inter.response.send_message(
                "Only staff members are allowed to view user details. You sure don't look like one.", ephemeral=True
            )
        await inter.response.send_modal(ViewUserModal(self, target_id, from_message=source is not None))

    async def _open(self, inter: discord.Interaction, action: str, target_id: int, source: Optional[discord.Message]):
        # Same checks the modal runs on submit, so nobody fills it in just to be denied
        reason_denied = await denial(self, inter.user, action, target_id, anchor_message(inter, source))
        if reason_denied:
            return await inter.response.send_message(reason_denied, ephemeral=True)

        guild = inter.guild
        reasons = (await self.config.guild(guild).reasons()).get(action, [])
        kwargs = {"deletes_message": source is not None and await self.config.guild(guild).delete_message()}
        if action == "mute":
            mutes_conf = self.bot.get_cog("Mutes").config.guild(guild)
            kwargs["mute_default"] = await mutes_conf.default_time() or None
            # Without a mute role, Mutes times users out, and timeouts need an end
            kwargs["mute_needs_duration"] = guild.get_role(await mutes_conf.mute_role()) is None
        elif action == "ban":
            days = await self.bot.get_cog("Mod").config.guild(guild).default_days()
            kwargs["ban_delete_seconds"] = max(0, min(7, days)) * 86400

        await inter.response.send_modal(ActionModal(self, action, target_id, source, reasons, **kwargs))

    #
    #   Settings
    #

    @commands.group(name="brutusset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def brutusset(self, ctx: commands.Context):
        """Brutus settings"""

    @brutusset.command(name="show", aliases=["settings"])
    async def brutusset_show(self, ctx: commands.Context):
        """Show Brutus' settings and the Red settings it follows"""
        guild = ctx.guild
        conf = await self.config.guild(guild).all()
        enabled = await self.bot.list_enabled_app_commands()
        enabled_menus = {
            kind: [m.name for (k, _), m in self.menus.items() if k == kind and m.name in enabled[kind]]
            for kind in ("message", "user")
        }

        export_channel = guild.get_channel_or_thread(conf["ban_export_channel"] or 0)

        def yes_no(value) -> str:
            return "On" if value else "Off"

        lines = [
            f"Brutus version: {self.__version__}",
            "",
            "Apps menu entries enabled:",
            f"  Messages: {', '.join(enabled_menus['message']) or 'None'}",
            f"  Users: {', '.join(enabled_menus['user']) or 'None'}",
            f"Delete the targeted message: {yes_no(conf['delete_message'])}",
            f"Ban message export: {yes_no(conf['ban_export'])}"
            + (f" (to #{export_channel.name})" if export_channel else " (no channel set)"),
            "",
            "Preset reasons:",
        ]
        for action in ACTIONS:
            lines.append(f"  {TITLES[action]}: {len(conf['reasons'].get(action, []))}/{MAX_REASONS}")

        lines.append(
            "View User recent messages: "
            + ("On (Defender's cache)" if get_defender(self.bot) else "Off (needs Defender's message cache)")
        )
        lines += ["", "Red settings used (change them in their cogs):"]
        mod = self.bot.get_cog("Mod")
        if mod is not None:
            mod_conf = await mod.config.guild(guild).all()
            lines.append(f"  [Mod] DM on kick/ban: {yes_no(mod_conf['dm_on_kickban'])}")
            lines.append(f"  [Mod] Ban extra embed: {yes_no(mod_conf['ban_show_extra'])}")
            lines.append(f"  [Mod] Respect hierarchy: {yes_no(mod_conf['respect_hierarchy'])}")
            lines.append(f"  [Mod] Default days of messages deleted on ban: {mod_conf['default_days']}")
        else:
            lines.append("  [Mod] Not loaded: Kick and Ban are unavailable")
        mutes = self.bot.get_cog("Mutes")
        if mutes is not None:
            mutes_conf = await mutes.config.guild(guild).all()
            role = guild.get_role(mutes_conf["mute_role"])
            default = mutes_conf["default_time"]
            lines.append(f"  [Mutes] DM on mute: {yes_no(mutes_conf['dm'])}")
            lines.append(f"  [Mutes] Mutes with: {('role @' + role.name) if role else 'timeouts'}")
            lines.append(f"  [Mutes] Default duration: {humanize_timedelta(seconds=default) if default else 'None'}")
        else:
            lines.append("  [Mutes] Not loaded: Mute is unavailable")
        warnings = self.bot.get_cog("Warnings")
        if warnings is not None:
            warn_conf = await warnings.config.guild(guild).all()
            lines.append(f"  [Warnings] DM on warn: {yes_no(warn_conf['toggle_dm'])}")
            lines.append(f"  [Warnings] Show moderator: {yes_no(warn_conf['show_mod'])}")
            lines.append(f"  [Warnings] Post in warn channel: {yes_no(warn_conf['toggle_channel'])}")
        else:
            lines.append("  [Warnings] Not loaded: Warn is unavailable")

        for page in pagify("\n".join(lines), delims=["\n\n", "\n"]):
            await ctx.send(box(page, lang="yaml"))
        if sum(map(len, enabled_menus.values())) < len(self.menus):
            await ctx.send(
                f"To add the missing Apps menu entries, run `{ctx.clean_prefix}slash enablecog brutus` "
                f"then `{ctx.clean_prefix}slash sync`. Discord allows 5 message and 5 user entries per bot, "
                f"and Brutus has 5 of each: to keep room for another cog, turn one off with e.g. "
                f"`{ctx.clean_prefix}slash disable \"View User\" user`."
            )

    @brutusset.command(name="banexport")
    async def brutusset_banexport(self, ctx: commands.Context, on_or_off: bool):
        """Toggle exporting a user's cached messages when they're banned from the Apps menu

        The export is taken right before the ban (which can delete their recent messages)
        and posted with the user, moderator, reason and case number, like
        `[p]defender messages exportuser`. It needs Defender's message cache.
        Set the channel with `[p]brutusset banexportchannel`."""
        channel = None
        if on_or_off:
            channel = ctx.guild.get_channel_or_thread(await self.config.guild(ctx.guild).ban_export_channel() or 0)
            if channel is None:
                return await ctx.send(f"Set the channel first with `{ctx.clean_prefix}brutusset banexportchannel`.")
        await self.config.guild(ctx.guild).ban_export.set(on_or_off)
        if not on_or_off:
            return await ctx.send("Bans from the Apps menu won't export messages anymore.")
        text = f"Bans from the Apps menu will export the user's cached messages to {channel.mention}."
        if get_defender(self.bot) is None:
            text += " Defender's message cache isn't loaded right now, so there's nothing to export until it is."
        await ctx.send(text)

    @brutusset.command(name="banexportchannel")
    async def brutusset_banexportchannel(
        self, ctx: commands.Context, channel: Union[discord.TextChannel, discord.Thread]
    ):
        """Set the channel where bans from the Apps menu export the user's messages

        Anyone who can see this channel can read the exports. Messages from channels the
        banning moderator can't read are left out, as with `[p]defender messages exportuser`."""
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.attach_files):
            return await ctx.send("I need permission to send messages and attach files in that channel.")
        await self.config.guild(ctx.guild).ban_export_channel.set(channel.id)
        enabled = await self.config.guild(ctx.guild).ban_export()
        tip = "" if enabled else f" Turn it on with `{ctx.clean_prefix}brutusset banexport on`."
        await ctx.send(f"Ban message exports will be posted in {channel.mention}.{tip}")

    @brutusset.command(name="deletemessage")
    async def brutusset_deletemessage(self, ctx: commands.Context, on_or_off: bool):
        """Toggle deleting the targeted message once action is taken (on by default)

        Only applies to actions taken from a message's Apps menu, and only when the
        action is taken on its author."""
        await self.config.guild(ctx.guild).delete_message.set(on_or_off)
        if on_or_off:
            await ctx.send("The targeted message will be deleted once action is taken.")
        else:
            await ctx.send("The targeted message will be left as is.")

    @brutusset.group(name="reasons")
    async def brutusset_reasons(self, ctx: commands.Context):
        """Preset reasons shown when an action is used

        Each action (`warn`, `mute`, `kick`, `ban`) has its own list of up to
        25 reasons. Staff pick one from a menu and can add details. An action
        without preset reasons asks for a typed reason instead.
        """

    @brutusset_reasons.command(name="set")
    async def reasons_set(self, ctx: commands.Context, action: reason_action, *, reasons: str):
        """Replace the preset reasons of an action

        Format: `Label:Reason|Label:Reason`. The label is what staff see in the menu,
        the reason goes in the case and the DM. Without `:` the label is also the reason.

        Example:
        `[p]brutusset reasons set ban Spam:Spamming|Alt:Alternate account usage`
        """
        try:
            entries = parse_reasons(reasons)
        except ValueError as e:
            return await ctx.send(str(e))
        if len(entries) > MAX_REASONS:
            return await ctx.send(
                f"That's {len(entries)} reasons: a menu can show at most {MAX_REASONS}. Nothing was changed."
            )
        async with self.config.guild(ctx.guild).reasons() as all_reasons:
            all_reasons[action] = entries
        await ctx.send(f"Set {len(entries)} preset reasons for `{action}`.")

    @brutusset_reasons.command(name="add")
    async def reasons_add(self, ctx: commands.Context, action: reason_action, *, reason: str):
        """Add one preset reason to an action

        Format: `Label:Reason`. Example:
        `[p]brutusset reasons add ban Alternate Account:Alternate account usage`
        """
        try:
            new = parse_reasons(reason)
        except ValueError as e:
            return await ctx.send(str(e))
        async with self.config.guild(ctx.guild).reasons() as all_reasons:
            entries = all_reasons.get(action, [])
            labels = {label.lower() for label, _ in entries}
            for label, _ in new:
                if label.lower() in labels:
                    return await ctx.send(f"`{action}` already has a reason labeled `{label}`.")
            if len(entries) + len(new) > MAX_REASONS:
                return await ctx.send(f"A menu can show at most {MAX_REASONS} reasons. Remove some first.")
            entries.extend(new)
            all_reasons[action] = entries
        await ctx.send(f"Added. `{action}` now has {len(entries)} preset reasons.")

    @brutusset_reasons.command(name="remove")
    async def reasons_remove(self, ctx: commands.Context, action: reason_action, *, label: str):
        """Remove a preset reason from an action, by label"""
        async with self.config.guild(ctx.guild).reasons() as all_reasons:
            entries = all_reasons.get(action, [])
            kept = [e for e in entries if e[0].lower() != label.strip().lower()]
            if len(kept) == len(entries):
                return await ctx.send(f"`{action}` has no reason labeled `{label}`.")
            all_reasons[action] = kept
        await ctx.send(f"Removed. `{action}` now has {len(kept)} preset reasons.")

    @brutusset_reasons.command(name="clear")
    async def reasons_clear(self, ctx: commands.Context, action: reason_action):
        """Remove all the preset reasons of an action"""
        async with self.config.guild(ctx.guild).reasons() as all_reasons:
            all_reasons.pop(action, None)
        await ctx.send(f"`{action}` now asks for a typed reason.")

    @brutusset_reasons.command(name="list", aliases=["show"])
    async def reasons_list(self, ctx: commands.Context):
        """Show the preset reasons of every action"""
        all_reasons = await self.config.guild(ctx.guild).reasons()
        text = ""
        for action in ACTIONS:
            entries = all_reasons.get(action, [])
            text += f"[{action}] {len(entries)}/{MAX_REASONS}\n"
            if not entries:
                text += "  (none, staff type a reason)\n"
            for label, reason in entries:
                text += f"  {label}" + (f" -> {reason}\n" if reason != label else "\n")
            text += "\n"
        for page in pagify(text, delims=["\n\n", "\n"]):
            await ctx.send(box(page, lang="ini"))

    @brutusset_reasons.command(name="import")
    async def reasons_import(self, ctx: commands.Context):
        """Copy the preset reasons of Defender's quick actions

        Replaces the reasons of each action Defender has any for (`[p]dset qa reasons`)."""
        defender = self.bot.get_cog("Defender")
        if defender is None or not hasattr(defender, "config"):
            return await ctx.send("Defender isn't loaded.")
        try:
            qa_reasons = await defender.config.guild(ctx.guild).qa_reasons()
        except (AttributeError, KeyError):
            return await ctx.send("This version of Defender has no quick action reasons to import.")

        imported = []
        async with self.config.guild(ctx.guild).reasons() as all_reasons:
            for action in ACTIONS:
                entries = [list(e) for e in qa_reasons.get(action, [])][:MAX_REASONS]
                if entries:
                    all_reasons[action] = entries
                    imported.append(f"`{action}` ({len(entries)})")
        if not imported:
            return await ctx.send("Defender has no quick action reasons set for warn, mute, kick or ban.")
        await ctx.send(f"Imported reasons for {', '.join(imported)}.")
