import logging
from typing import Dict

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_timedelta, pagify

from .actions import ACTIONS, MAX_REASONS, TITLES, denial, parse_reasons
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
    """Warn, mute, kick and ban from a message's Apps menu

    Right click (or long press) a message > Apps > Warn / Mute / Kick / Ban opens a form
    with the author's ID, a preset reason and optional details. The action runs like the
    matching Red command: same DM (if turned on in that cog), same modlog case. The
    message is deleted afterwards.

    Apps > View User shows the author's modlog cases, recent messages (from Defender's
    cache) or user info, only to you.
    """

    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xB2075, force_registration=True)
        self.config.register_guild(reasons={}, delete_message=True)

        self.menus: Dict[str, app_commands.ContextMenu] = {}
        for action, name in (*TITLES.items(), ("view", "View User")):
            menu = app_commands.ContextMenu(name=name, callback=getattr(self, f"_menu_{action}"))
            menu.default_permissions = DEFAULT_PERMISSIONS[action]
            menu.guild_only = True
            menu.allowed_contexts = app_commands.AppCommandContext(guild=True)
            self.menus[action] = menu

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

    async def _menu_warn(self, inter: discord.Interaction, message: discord.Message):
        await self._open(inter, "warn", message)

    async def _menu_mute(self, inter: discord.Interaction, message: discord.Message):
        await self._open(inter, "mute", message)

    async def _menu_kick(self, inter: discord.Interaction, message: discord.Message):
        await self._open(inter, "kick", message)

    async def _menu_ban(self, inter: discord.Interaction, message: discord.Message):
        await self._open(inter, "ban", message)

    async def _menu_view(self, inter: discord.Interaction, message: discord.Message):
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("This only works in servers.", ephemeral=True)
        if await self.bot.cog_disabled_in_guild(self, inter.guild):
            return await inter.response.send_message("Brutus is disabled in this server.", ephemeral=True)
        if not await can_view(self.bot, inter.user):
            return await inter.response.send_message(
                "Only staff members are allowed to view user details. You sure don't look like one.", ephemeral=True
            )
        if message.webhook_id is not None:
            return await inter.response.send_message(
                "That message was sent by a webhook, so there's no user to look up.", ephemeral=True
            )
        await inter.response.send_modal(ViewUserModal(self, message.author.id))

    async def _open(self, inter: discord.Interaction, action: str, message: discord.Message):
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("This only works in servers.", ephemeral=True)
        if message.webhook_id is not None:
            return await inter.response.send_message(
                "That message was sent by a webhook, so there's no user to take action on.", ephemeral=True
            )

        # Same checks the modal runs on submit, so nobody fills it in just to be denied
        reason_denied = await denial(self, inter.user, action, message.author.id, message)
        if reason_denied:
            return await inter.response.send_message(reason_denied, ephemeral=True)

        guild = inter.guild
        reasons = (await self.config.guild(guild).reasons()).get(action, [])
        kwargs = {}
        if action == "mute":
            mutes_conf = self.bot.get_cog("Mutes").config.guild(guild)
            kwargs["mute_default"] = await mutes_conf.default_time() or None
            # Without a mute role, Mutes times users out, and timeouts need an end
            kwargs["mute_needs_duration"] = guild.get_role(await mutes_conf.mute_role()) is None
        elif action == "ban":
            days = await self.bot.get_cog("Mod").config.guild(guild).default_days()
            kwargs["ban_delete_seconds"] = max(0, min(7, days)) * 86400

        await inter.response.send_modal(ActionModal(self, action, message, reasons, **kwargs))

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
        enabled_menus = [m.name for m in self.menus.values() if m.name in enabled["message"]]

        def yes_no(value) -> str:
            return "On" if value else "Off"

        lines = [
            f"Apps menu entries enabled: {', '.join(enabled_menus) or 'None'}",
            f"Delete the targeted message: {yes_no(conf['delete_message'])}",
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
        if len(enabled_menus) < len(self.menus):
            await ctx.send(
                f"To add the missing Apps menu entries, run `{ctx.clean_prefix}slash enablecog brutus` "
                f"then `{ctx.clean_prefix}slash sync`. Discord allows 5 message entries per bot and Brutus "
                f"has 5: to keep room for another cog, turn one off with "
                f"`{ctx.clean_prefix}slash disable \"View User\" message`."
            )

    @brutusset.command(name="deletemessage")
    async def brutusset_deletemessage(self, ctx: commands.Context, on_or_off: bool):
        """Toggle deleting the targeted message once action is taken (on by default)

        The message is only deleted when the action is taken on its author."""
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
