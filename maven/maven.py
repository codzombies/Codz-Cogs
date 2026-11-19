"""
maven - privileged access management style role checkout for Red-DiscordBot.

User commands:
    [p]maven                    Open the checkout menu
    [p]maven checkin            Check in (remove) all of your checked-out roles
    [p]maven status             Show your active checkouts

Admin commands (Red admin OR Discord Administrator permission):
    [p]setmaven rolecheckout <name> <role> [label:..] [description:..] [emoji:..] [time:..]
    [p]setmaven requirement add|remove|list|clear <name> [roles...]
    [p]setmaven ...             (see [p]help setmaven)
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_timedelta
from redbot.core.utils.mod import is_admin_or_superior

log = logging.getLogger("red.maven")

__version__ = "1.0.3"

NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
DUR_RE = re.compile(r"(\d+)(s|m|h)")
MIN_CHECKOUT = 60  # seconds
MAX_CHECKOUT = 24 * 3600  # seconds
DENY_LOG_INTERVAL = 60  # seconds between logged unauthorized attempts per user
LABEL_MAX = 25
DESC_MAX = 50

DEFAULT_GUILD = {
    "roles": {},  # name -> {role_id, label, description, emoji, requires: [ids], time: seconds}
    # notification
    "notify_channel": None,
    "notify_role": None,
    "notify_mode": "ping",  # ping | silent | none
    "notify_title": "Privileged Access Provisioned",
    "notify_message": "A privileged role has been provisioned to an account.",
    "notify_color": 0xFF0000,
    "notify_footer": "Privileged Access Management Tool",
    "log_checkin": True,
    # menu
    "menu_title": "Privileged Access Management",
    "menu_description": "{user}, all role checkouts are logged.\nSelect a role below to check it out.",
    "menu_footer": "Privileged Access Management Tool",
    "menu_color": 0x55ACEE,
    "menu_image": None,
    "menu_timeout": 60,  # seconds before menu options become unavailable
    "max_active": 1,  # concurrent checkouts per member
}
DEFAULT_MEMBER = {"active": {}}  # name -> {role_id, expires, checked_out}


def ok(msg: str) -> str:
    return f"✅ {msg}"


def err(msg: str) -> str:
    return f"❌ {msg}"


def parse_duration(text: str) -> int:
    """'15m', '1h30m', '90s', or a bare number (minutes) -> seconds."""
    raw = text.strip().lower().replace(" ", "")
    if raw.isdigit():
        seconds = int(raw) * 60
    else:
        matches = list(DUR_RE.finditer(raw))
        if not matches or "".join(m.group(0) for m in matches) != raw:
            raise commands.BadArgument("Use a duration like `5m`, `15m`, `1h` or `1h30m`.")
        mult = {"s": 1, "m": 60, "h": 3600}
        seconds = sum(int(m.group(1)) * mult[m.group(2)] for m in matches)
    if not MIN_CHECKOUT <= seconds <= MAX_CHECKOUT:
        raise commands.BadArgument(
            f"Duration must be between {humanize_timedelta(seconds=MIN_CHECKOUT)} "
            f"and {humanize_timedelta(seconds=MAX_CHECKOUT)}."
        )
    return seconds


def dur(seconds: int) -> str:
    return humanize_timedelta(seconds=seconds) or "0 seconds"


class CheckoutFlags(commands.FlagConverter, prefix="", delimiter=":", case_insensitive=True):
    label: Optional[str] = None
    description: Optional[str] = None
    emoji: Optional[str] = None
    time: Optional[str] = None


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class RoleSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="Select a role to check out…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog._do_checkout(self.view, interaction, self.values[0])


class CheckinButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Check in", style=discord.ButtonStyle.success, emoji="🔓")

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog._do_checkin_all(self.view, interaction)


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.view.finish()
        await interaction.response.edit_message(
            embed=discord.Embed(
                description="Request to check out permissions has been cancelled.",
                color=discord.Colour.light_grey(),
            ),
            view=None,
        )


class CheckoutView(discord.ui.View):
    def __init__(self, cog: "Maven", ctx: commands.Context, options, has_active: bool, timeout: int):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.author = ctx.author
        self.message: Optional[discord.Message] = None
        self.key = (ctx.guild.id, ctx.author.id)
        self.done = False
        if options:
            self.add_item(RoleSelect(options))
        if has_active:
            self.add_item(CheckinButton())
        self.add_item(CancelButton())

    def finish(self):
        self.done = True
        self.cog._open_panels.discard(self.key)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog._open_panels.discard(self.key)
        if self.done or not self.message:
            return
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="Privileged Access Management",
            description="⌛ This menu has expired. Run the command again to start a new request.",
            color=discord.Colour.light_grey(),
        )
        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Maven(commands.Cog):
    """Time-boxed privileged role checkout with audit logging."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4D41564E, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.config.register_member(**DEFAULT_MEMBER)
        self._lock = asyncio.Lock()
        self._open_panels: set = set()
        self._deny_log: Dict[Tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.expiry_loop.start()

    async def cog_unload(self) -> None:
        self.expiry_loop.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        for gid, members in (await self.config.all_members()).items():
            if user_id not in members:
                continue
            # Revoke before forgetting: otherwise a user could run `mydata forgetme`
            # to erase the expiry record and keep a privileged role indefinitely.
            guild = self.bot.get_guild(gid)
            member = guild.get_member(user_id) if guild else None
            if member is not None:
                done, failed = await self._checkin_member(member, "Data deletion request")
                if failed:
                    log.warning("maven: could not revoke %s checkout(s) from %s in %s before "
                                "data deletion; remove them manually.", failed, user_id, gid)
            await self.config.member_from_ids(gid, user_id).clear()

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _fmt(text: str, **kw) -> str:
        for k, v in kw.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    @staticmethod
    def _bot_can_grant(guild: discord.Guild, role: discord.Role) -> Optional[str]:
        me = guild.me
        if role.is_default() or role.managed:
            return "That role can't be managed (it's @everyone or integration-managed)."
        if not me.guild_permissions.manage_roles:
            return "I don't have the **Manage Roles** permission."
        if role >= me.top_role:
            return f"I can't hand out {role.mention}: it is at or above my highest role."
        return None

    @staticmethod
    def _author_hierarchy_error(ctx: commands.Context, role: discord.Role) -> Optional[str]:
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return f"You can't configure {role.mention}: it is at or above your highest role."
        return None

    def _hierarchy_error(self, ctx: commands.Context, role: discord.Role) -> Optional[str]:
        """Return an error string if `role` can't be configured by the invoker."""
        if (e := self._bot_can_grant(ctx.guild, role)) is not None:
            return e
        return self._author_hierarchy_error(ctx, role)

    def _entry_guard(self, ctx: commands.Context, entry: dict) -> Optional[str]:
        """Stop admins from editing checkouts for roles at/above their own top role.

        Without this, a lower-ranked admin could add their own role as a requirement
        on a higher checkout and grant themselves that role.
        """
        role = ctx.guild.get_role(entry["role_id"])
        if role is None:
            return None
        return self._author_hierarchy_error(ctx, role)

    @staticmethod
    def _meets_requirements(member: discord.Member, entry: dict) -> bool:
        reqs = set(entry.get("requires", []))
        return bool(reqs & {r.id for r in member.roles})  # empty requirements = deny

    def _eligible(self, member: discord.Member, entry: dict) -> Optional[discord.Role]:
        """Return the role if this member may check this entry out right now."""
        role = member.guild.get_role(entry["role_id"])
        if role is None or self._bot_can_grant(member.guild, role) is not None:
            return None
        if not self._meets_requirements(member, entry):
            return None
        if role in member.roles:
            return None
        return role

    def _option(self, name: str, entry: dict, role: discord.Role) -> discord.SelectOption:
        emoji = None
        if entry.get("emoji"):
            emoji = discord.PartialEmoji.from_str(entry["emoji"])
        return discord.SelectOption(
            label=(entry.get("label") or role.name)[:LABEL_MAX],
            value=name,
            description=(f"{entry['description']} • {dur(entry['time'])}"
                         if entry.get("description") else dur(entry["time"])),
            emoji=emoji,
        )

    # ------------------------------------------------------------- panel logic
    async def _panel_state(self, member: discord.Member):
        data = await self.config.guild(member.guild).all()
        active = await self.config.member(member).active()
        available: List[Tuple[str, dict, discord.Role]] = []
        at_limit = len(active) >= data["max_active"]
        if not at_limit:
            for name, entry in data["roles"].items():
                role = self._eligible(member, entry)
                if role is not None:
                    available.append((name, entry, role))
        # Alphabetical by what the member sees in the menu (label, falling back to role name).
        available.sort(key=lambda t: ((t[1].get("label") or t[2].name).casefold(), t[0]))
        return data, active, available[:25], at_limit

    def _panel_embed(self, member, data, active, available, at_limit) -> discord.Embed:
        embed = discord.Embed(
            title=data["menu_title"],
            description=self._fmt(
                data["menu_description"], user=member.mention, user_name=member.display_name
            ),
            color=data["menu_color"],
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if at_limit:
            embed.add_field(
                name="Checkout unavailable",
                value="You've reached the maximum number of active checkouts. Check in first.",
                inline=False,
            )
        elif not available:
            embed.add_field(
                name="No roles available",
                value="You don't have access to check out any roles.",
                inline=False,
            )
        if active:
            lines = []
            for rec in active.values():
                lines.append(f"<@&{rec['role_id']}> — expires <t:{int(rec['expires'])}:R>")
            embed.add_field(name="Active access", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"{data['menu_footer']} • Menu expires in {data['menu_timeout']}s")
        if data["menu_image"]:
            embed.set_image(url=data["menu_image"])
        return embed

    async def _open_panel(self, ctx: commands.Context):
        key = (ctx.guild.id, ctx.author.id)
        data, active, available, at_limit = await self._panel_state(ctx.author)

        # Members with no possible access (and nothing checked out) don't get a menu,
        # unless they are a Red admin / Discord Administrator.
        role_ids = {r.id for r in ctx.author.roles}
        has_access = any(set(e.get("requires", [])) & role_ids for e in data["roles"].values())
        if not has_access and not active and not await is_admin_or_superior(self.bot, ctx.author):
            return await self._deny_unauthorized(ctx, data)

        if key in self._open_panels:
            return await ctx.send(
                err("You already have a checkout menu open. Use it or wait for it to expire."),
                delete_after=15,
            )
        if not self._notify_channel(ctx.guild, data):
            return await ctx.send(
                err("Checkout is disabled until an admin sets a notification channel "
                    "(`setmaven notify channel`)."),
                delete_after=15,
            )
        options = [self._option(n, e, r) for n, e, r in available]
        view = CheckoutView(self, ctx, options, bool(active), data["menu_timeout"])
        embed = self._panel_embed(ctx.author, data, active, available, at_limit)
        self._open_panels.add(key)
        try:
            view.message = await ctx.send(embed=embed, view=view)
        except discord.HTTPException:
            # e.g. a stored emoji/image the API rejects; don't leave the user locked out.
            self._open_panels.discard(key)
            view.stop()
            log.exception("Failed to send maven checkout menu in guild %s", ctx.guild.id)
            await ctx.send(err("I couldn't display the checkout menu. An admin should check "
                               "the configured emojis and menu image."))

    async def _deny_unauthorized(self, ctx: commands.Context, data: dict):
        """Silently ignore the user; log the attempt (max one per user per minute)."""
        if not data["roles"]:
            return  # nothing configured, nothing to attempt
        now = time.time()
        key = (ctx.guild.id, ctx.author.id)
        if now - self._deny_log.get(key, 0) < DENY_LOG_INTERVAL:
            return
        chan = self._notify_channel(ctx.guild, data)
        if chan is None:
            return
        self._deny_log = {k: t for k, t in self._deny_log.items() if now - t < DENY_LOG_INTERVAL}
        self._deny_log[key] = now
        embed = discord.Embed(
            title="Unauthorized Access Attempt",
            color=0xFFA500,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Mention", value=ctx.author.mention, inline=True)
        embed.add_field(name="UserID", value=f"`{ctx.author.id}`", inline=True)
        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        embed.add_field(
            name="Automation Description",
            value="A user without checkout access attempted to open the privileged access menu.",
            inline=False,
        )
        embed.set_footer(text=data["notify_footer"])
        try:
            await chan.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to send unauthorized attempt log")

    def _notify_channel(self, guild: discord.Guild, data: dict) -> Optional[discord.abc.Messageable]:
        chan = guild.get_channel(data["notify_channel"]) if data["notify_channel"] else None
        if chan is None:
            return None
        perms = chan.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return None
        return chan

    # ---------------------------------------------------------------- checkout
    async def _do_checkout(self, view: CheckoutView, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        member: discord.Member = interaction.user
        guild = interaction.guild

        async def deny(msg: str):
            await interaction.followup.send(err(msg), ephemeral=True)

        async with self._lock:
            data = await self.config.guild(guild).all()
            entry = data["roles"].get(name)
            if entry is None:
                return await deny("That checkout is no longer configured.")
            if not self._notify_channel(guild, data):
                return await deny("Checkout is disabled: the notification channel is unavailable.")
            conf = self.config.member(member)
            active = await conf.active()
            if len(active) >= data["max_active"]:
                return await deny("You've reached the maximum number of active checkouts.")
            role = self._eligible(member, entry)
            if role is None:
                return await deny("You are not eligible to check out that role (denied).")

            now = int(time.time())
            expires = now + int(entry["time"])
            # Record first: if we crash after granting, the expiry loop still removes it.
            async with conf.active() as act:
                act[name] = {"role_id": role.id, "expires": expires, "checked_out": now}
            try:
                await member.add_roles(role, reason=f"maven: checkout '{name}' for {dur(entry['time'])}")
            except discord.HTTPException:
                log.exception("Failed to grant %s to %s", role.id, member.id)
                async with conf.active() as act:
                    act.pop(name, None)
                return await deny("I couldn't grant that role. Check my permissions and role position.")

        view.finish()
        # Audit log first so a failure to edit the menu can't skip it.
        await self._notify_checkout(guild, member, role, name, entry, expires, data)
        try:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Access checked out",
                    description=(
                        f"{role.mention} has been checked out. It will be removed "
                        f"<t:{expires}:R>, or sooner if you check in."
                    ),
                    color=discord.Colour.green(),
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

    async def _do_checkin_all(self, view: CheckoutView, interaction: discord.Interaction):
        await interaction.response.defer()
        member: discord.Member = interaction.user
        done, failed = await self._checkin_member(member, "Manual check-in")
        view.finish()
        if failed:
            desc, colour = self._CHECKIN_FAILED, discord.Colour.red()
        elif done:
            desc, colour = "Access has been checked back in.", discord.Colour.green()
        else:
            desc, colour = "You have no active access.", discord.Colour.light_grey()
        try:
            await interaction.edit_original_response(
                embed=discord.Embed(description=desc, color=colour), view=None
            )
        except discord.HTTPException:
            pass

    _CHECKIN_FAILED = ("Some roles couldn't be removed. They will be retried automatically "
                       "when they expire. Ask an admin to check my permissions.")

    async def _checkin_member(self, member: discord.Member, reason: str) -> Tuple[int, int]:
        """Revoke all of a member's checkouts. Returns (revoked, failed)."""
        done = failed = 0
        async with self._lock:
            active = await self.config.member(member).active()
            for name, rec in list(active.items()):
                if await self._revoke(member.guild, member, name, rec, reason):
                    done += 1
                else:
                    failed += 1
        return done, failed

    async def _revoke(self, guild, member: discord.Member, name: str, rec: dict, reason: str) -> bool:
        role = guild.get_role(rec["role_id"])
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"maven: {reason} ('{name}')")
            except discord.HTTPException:
                log.warning("Failed to remove %s from %s", rec["role_id"], member.id)
                return False
        async with self.config.member(member).active() as act:
            act.pop(name, None)
        await self._notify_checkin(guild, member, rec["role_id"], name, reason)
        return True

    # ----------------------------------------------------------- notifications
    async def _notify_checkout(self, guild, member, role, name, entry, expires, data):
        chan = self._notify_channel(guild, data)
        if chan is None:
            return
        embed = discord.Embed(
            title=data["notify_title"],
            color=data["notify_color"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Mention", value=member.mention, inline=True)
        embed.add_field(name="UserID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Expires", value=f"<t:{expires}:R> ({dur(entry['time'])})", inline=True)
        embed.add_field(
            name="Automation Description",
            value=self._fmt(
                data["notify_message"],
                user=member.mention,
                user_id=member.id,
                role=role.mention,
                duration=dur(entry["time"]),
            )[:1024],
            inline=False,
        )
        embed.set_footer(text=f"{data['notify_footer']} • {name}")

        content, mentions, silent = None, discord.AllowedMentions.none(), False
        ping_role = guild.get_role(data["notify_role"]) if data["notify_role"] else None
        if ping_role and data["notify_mode"] in ("ping", "silent"):
            content = ping_role.mention
            mentions = discord.AllowedMentions(roles=[ping_role])
            silent = data["notify_mode"] == "silent"
        try:
            await chan.send(content=content, embed=embed, allowed_mentions=mentions, silent=silent)
        except discord.HTTPException:
            log.exception("Failed to send checkout notification")

    async def _notify_checkin(self, guild, member, role_id: int, name: str, reason: str):
        data = await self.config.guild(guild).all()
        if not data["log_checkin"]:
            return
        chan = self._notify_channel(guild, data)
        if chan is None:
            return
        embed = discord.Embed(
            title="Privileged Access Revoked",
            color=discord.Colour.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Mention", value=member.mention, inline=True)
        embed.add_field(name="UserID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Role", value=f"<@&{role_id}>", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"{data['notify_footer']} • {name}")
        try:
            await chan.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ expiry loop
    @tasks.loop(seconds=10)
    async def expiry_loop(self):
        # Any exception escaping a tasks.loop stops it for good, which would mean
        # privileged roles never expire. Isolate failures per member.
        try:
            all_members = await self.config.all_members()
        except Exception:
            log.exception("maven: failed to read member data")
            return
        for gid, members in all_members.items():
            guild = self.bot.get_guild(gid)
            if guild is None or guild.unavailable:
                continue
            for uid, mdata in members.items():
                if not mdata.get("active"):
                    continue
                try:
                    await self._process_member_expiry(guild, uid)
                except Exception:
                    log.exception("maven: expiry check failed for %s in %s", uid, gid)

    async def _process_member_expiry(self, guild: discord.Guild, uid: int) -> None:
        conf = self.config.member_from_ids(guild.id, uid)
        member = guild.get_member(uid)
        if member is None:
            # Not cached doesn't mean gone: only drop records once Discord confirms it.
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                await conf.active.clear()
                return
            except discord.HTTPException:
                return  # try again next tick
        entries = await self.config.guild(guild).roles()
        async with self._lock:
            # Re-read under the lock: the snapshot may be stale if the member
            # checked in / out again since all_members() was read.
            active = await conf.active()
            now = time.time()
            for name, rec in list(active.items()):
                role = guild.get_role(rec["role_id"])
                # Role deleted, or removed by hand: just drop the record.
                if role is None or role not in member.roles:
                    async with conf.active() as act:
                        act.pop(name, None)
                    continue
                if rec["expires"] <= now:
                    reason = "Checkout expired"
                else:
                    entry = entries.get(name)
                    if (entry is not None and entry["role_id"] == rec["role_id"]
                            and not self._meets_requirements(member, entry)):
                        reason = "Eligibility lost (no longer holds a required role)"
                    else:
                        continue
                if not await self._revoke(guild, member, name, rec, reason):
                    async with conf.active() as act:  # retry in a minute
                        if name in act:
                            act[name]["expires"] = min(act[name]["expires"], now + 60)

    @expiry_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_red_ready()

    @expiry_loop.error
    async def _loop_error(self, exc):
        log.exception("maven expiry loop crashed", exc_info=exc)

    # =========================================================================
    # User commands
    # =========================================================================
    @commands.group(name="maven", invoke_without_command=True)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def maven(self, ctx: commands.Context):
        """Open the privileged access checkout menu."""
        await self._open_panel(ctx)

    @maven.command(name="checkin")
    async def maven_checkin(self, ctx: commands.Context):
        """Check in (remove) all of your checked-out roles."""
        done, failed = await self._checkin_member(ctx.author, "Manual check-in")
        if failed:
            await ctx.send(err(self._CHECKIN_FAILED))
        elif done:
            await ctx.send(ok("Access has been checked back in."))
        else:
            await ctx.send(err("You have no active access."))

    @maven.command(name="status")
    async def maven_status(self, ctx: commands.Context):
        """Show your active checkouts."""
        active = await self.config.member(ctx.author).active()
        if not active:
            return await ctx.send("You have no active access.")
        lines = [f"<@&{r['role_id']}> — expires <t:{int(r['expires'])}:R>" for r in active.values()]
        await ctx.send(
            embed=discord.Embed(title="Active access", description="\n".join(lines), color=0x55ACEE)
        )

    # =========================================================================
    # Admin commands
    # =========================================================================
    @commands.group(name="setmaven")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def setmaven(self, ctx: commands.Context):
        """Configure maven role checkouts."""

    # ---- role checkouts -----------------------------------------------------
    @setmaven.command(name="rolecheckout")
    async def sm_rolecheckout(
        self, ctx: commands.Context, name: str, role: discord.Role, *, flags: CheckoutFlags = None
    ):
        """Add or update a role checkout.

        Extras (all optional, any order):
        `label:` (max 25 chars)  `description:` (max 50 chars)
        `emoji:`  `time:` (e.g. 5m, 15m, 1h; default 15m)
        Use `none` as a value to clear label/description/emoji.

        Example:
        `[p]setmaven rolecheckout admin @Administrator label:Administrator
        description:Checks out the Administrator role time:15m`

        Edits keep existing settings unless you pass a new value.
        """
        name = name.lower()
        if not NAME_RE.match(name):
            return await ctx.send(err("Name must be 1-32 chars of `a-z 0-9 _ -`."))
        if (e := self._hierarchy_error(ctx, role)) is not None:
            return await ctx.send(err(e))

        def clearable(v):
            return None if v.strip().lower() in ("none", "clear") else v.strip()

        # Validate everything before touching config: returning inside the
        # `async with` below would still persist partial edits.
        updates = {}
        if flags is not None and flags.label is not None:
            updates["label"] = clearable(flags.label)
            if updates["label"] and len(updates["label"]) > LABEL_MAX:
                return await ctx.send(err(f"Label is max {LABEL_MAX} characters."))
        if flags is not None and flags.description is not None:
            updates["description"] = clearable(flags.description)
            if updates["description"] and len(updates["description"]) > DESC_MAX:
                return await ctx.send(err(f"Description is max {DESC_MAX} characters."))
        if flags is not None and flags.emoji is not None:
            val = clearable(flags.emoji)
            if val:
                pe = discord.PartialEmoji.from_str(val)
                if pe.id:
                    if self.bot.get_emoji(pe.id) is None:
                        return await ctx.send(err("I can't use that custom emoji (I must share a server with it)."))
                elif len(val) > 16 or all(c.isascii() for c in val) or any(
                    c.isascii() and c not in "#*0123456789" for c in val
                ):
                    # PartialEmoji.from_str treats any plain text as "unicode";
                    # an invalid emoji would make Discord reject the whole menu.
                    return await ctx.send(err("That doesn't look like a valid emoji."))
                val = str(pe)
            updates["emoji"] = val
        if flags is not None and flags.time is not None:
            try:
                updates["time"] = parse_duration(flags.time)
            except commands.BadArgument as exc:
                return await ctx.send(err(str(exc)))

        existing = (await self.config.guild(ctx.guild).roles()).get(name)
        if existing is not None:
            if (e := self._entry_guard(ctx, existing)) is not None:
                return await ctx.send(err(e))
            if role.id in existing.get("requires", []):
                return await ctx.send(err(f"{role.mention} is a requirement of `{name}`; "
                                          "a role can't be its own requirement."),
                                      allowed_mentions=discord.AllowedMentions.none())

        async with self.config.guild(ctx.guild).roles() as roles:
            entry = roles.get(name) or {
                "role_id": role.id, "label": None, "description": None,
                "emoji": None, "requires": [], "time": 15 * 60,
            }
            entry["role_id"] = role.id
            entry.update(updates)
            roles[name] = entry

        msg = ok(f"Checkout `{name}` set for {role.mention} ({dur(entry['time'])}).")
        if not entry["requires"]:
            msg += (f"\n⚠️ No requirements yet, so nobody can check it out. "
                    f"Use `{ctx.clean_prefix}setmaven requirement add {name} <roles...>`.")
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @setmaven.group(name="requirement", aliases=["requirements"])
    async def sm_requirement(self, ctx: commands.Context):
        """Manage which role(s) may check out a role.

        Subcommands: `add`, `remove`, `list`, `clear`.
        """

    @sm_requirement.command(name="add")
    async def sm_requirement_add(
        self, ctx: commands.Context, name: str, roles: commands.Greedy[discord.Role]
    ):
        """Add one or more roles that may check out `name`.

        Example: `[p]setmaven requirement add admin @Administrator @Senior Mod`
        """
        name = name.lower()
        if not roles:
            return await ctx.send(err("Provide at least one role."))
        async with self.config.guild(ctx.guild).roles() as cfg:
            entry = cfg.get(name)
            if entry is None:
                return await ctx.send(err(f"No checkout named `{name}`."))
            if (e := self._entry_guard(ctx, entry)) is not None:
                return await ctx.send(err(e))
            bad = [r for r in roles if r.is_default() or r.id == entry["role_id"]]
            if bad:
                return await ctx.send(err("Requirements can't be @everyone or the checkout role itself."))
            current = set(entry["requires"])
            added = [r for r in roles if r.id not in current]
            current.update(r.id for r in roles)
            entry["requires"] = sorted(current)
        if not added:
            return await ctx.send(err("Those roles were already required."))
        await ctx.send(
            ok(f"Added to `{name}`: " + " ".join(r.mention for r in added)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sm_requirement.command(name="remove", aliases=["delete"])
    async def sm_requirement_remove(
        self, ctx: commands.Context, name: str, roles: commands.Greedy[discord.Role]
    ):
        """Remove one or more roles from the requirements of `name`.

        If the last requirement is removed, nobody can check the role out.
        Members who no longer hold any required role lose active checkouts of it.
        """
        name = name.lower()
        if not roles:
            return await ctx.send(err("Provide at least one role."))
        async with self.config.guild(ctx.guild).roles() as cfg:
            entry = cfg.get(name)
            if entry is None:
                return await ctx.send(err(f"No checkout named `{name}`."))
            if (e := self._entry_guard(ctx, entry)) is not None:
                return await ctx.send(err(e))
            current = set(entry["requires"])
            removed = [r for r in roles if r.id in current]
            if not removed:
                return await ctx.send(err("None of those roles were required."))
            current.difference_update(r.id for r in removed)
            entry["requires"] = sorted(current)
        msg = ok(f"Removed from `{name}`: " + " ".join(r.mention for r in removed))
        if not current:
            msg += "\n⚠️ No requirements remain, so nobody can check this role out."
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @sm_requirement.command(name="list", aliases=["show"])
    async def sm_requirement_list(self, ctx: commands.Context, name: str):
        """Show the requirements for `name`."""
        name = name.lower()
        roles = await self.config.guild(ctx.guild).roles()
        entry = roles.get(name)
        if entry is None:
            return await ctx.send(err(f"No checkout named `{name}`."))
        cur = " ".join(f"<@&{r}>" for r in entry["requires"]) or "*none (unavailable)*"
        await ctx.send(f"Requirements for `{name}`: {cur}",
                       allowed_mentions=discord.AllowedMentions.none())

    @sm_requirement.command(name="clear")
    async def sm_requirement_clear(self, ctx: commands.Context, name: str):
        """Remove all requirements from `name`.

        Nobody can then check it out, and active checkouts of it are revoked.
        """
        name = name.lower()
        async with self.config.guild(ctx.guild).roles() as cfg:
            if name not in cfg:
                return await ctx.send(err(f"No checkout named `{name}`."))
            if (e := self._entry_guard(ctx, cfg[name])) is not None:
                return await ctx.send(err(e))
            cfg[name]["requires"] = []
        await ctx.send(ok(f"Cleared requirements for `{name}`."))

    @setmaven.command(name="remove", aliases=["delete"])
    async def sm_remove(self, ctx: commands.Context, name: str):
        """Remove a role checkout. Active checkouts still expire normally."""
        name = name.lower()
        async with self.config.guild(ctx.guild).roles() as cfg:
            if name not in cfg:
                return await ctx.send(err(f"No checkout named `{name}`."))
            if (e := self._entry_guard(ctx, cfg[name])) is not None:
                return await ctx.send(err(e))
            del cfg[name]
        await ctx.send(ok(f"Removed checkout `{name}`. Existing checkouts will still be checked in."))

    @setmaven.command(name="list")
    async def sm_list(self, ctx: commands.Context):
        """List configured role checkouts."""
        roles = await self.config.guild(ctx.guild).roles()
        if not roles:
            return await ctx.send("No role checkouts configured.")
        embed = discord.Embed(title="Maven role checkouts", color=0x55ACEE)
        for name, e in roles.items():
            reqs = " ".join(f"<@&{r}>" for r in e["requires"]) or "⚠️ *none (unavailable)*"
            embed.add_field(
                name=f"{e.get('emoji') or ''} {name}".strip(),
                value=(f"Role: <@&{e['role_id']}>\nTime: {dur(e['time'])}\n"
                       f"Label: {e.get('label') or '—'}\nDesc: {e.get('description') or '—'}\n"
                       f"Requires: {reqs}"),
                inline=True,
            )
        await ctx.send(embed=embed)

    @setmaven.command(name="settings")
    async def sm_settings(self, ctx: commands.Context):
        """Show current maven settings."""
        d = await self.config.guild(ctx.guild).all()
        chan = f"<#{d['notify_channel']}>" if d["notify_channel"] else "⚠️ *not set (checkout disabled)*"
        role = f"<@&{d['notify_role']}>" if d["notify_role"] else "*none*"
        embed = discord.Embed(title=f"Maven settings • v{__version__}", color=0x55ACEE)
        embed.add_field(name="Notify channel", value=chan)
        embed.add_field(name="Notify role", value=role)
        embed.add_field(name="Notify mode", value=d["notify_mode"])
        embed.add_field(name="Log check-ins", value=str(d["log_checkin"]))
        embed.add_field(name="Menu timeout", value=f"{d['menu_timeout']}s")
        embed.add_field(name="Max active/member", value=str(d["max_active"]))
        embed.add_field(name="Checkouts", value=str(len(d["roles"])))
        await ctx.send(embed=embed)

    @setmaven.command(name="quickstart")
    async def sm_quickstart(self, ctx: commands.Context):
        """Step-by-step setup guide (shows what's already done)."""
        d = await self.config.guild(ctx.guild).all()
        roles = d["roles"]
        p = ctx.clean_prefix
        has_chan = self._notify_channel(ctx.guild, d) is not None
        has_roles = bool(roles)
        has_reqs = has_roles and any(e["requires"] for e in roles.values())
        mark = lambda b: "✅" if b else "⬜"
        embed = discord.Embed(
            title="Maven quick start",
            description="Complete these in order. ✅ marks what's already configured.",
            color=0x55ACEE,
        )
        embed.add_field(
            name=f"{mark(has_chan)} 1. Set the notification channel (required)",
            value=f"`{p}setmaven notify channel <#channel>`\nCheckout stays disabled until this is set.",
            inline=False,
        )
        embed.add_field(
            name=f"{mark(has_roles)} 2. Add a role to the checkout menu",
            value=(f"`{p}setmaven rolecheckout <name> <role> label:<text> description:<text> "
                   f"emoji:<emoji> time:15m`\nLabel max {LABEL_MAX} chars, description max {DESC_MAX}. "
                   f"Time can be `5m`, `15m`, `1h`, etc."),
            inline=False,
        )
        embed.add_field(
            name=f"{mark(has_reqs)} 3. Choose who may check it out",
            value=(f"`{p}setmaven requirement add <name> <role> [role...]`\n"
                   f"Nobody can check a role out until it has at least one requirement."),
            inline=False,
        )
        embed.add_field(
            name="4. Optional: notifications and menu",
            value=(f"`{p}setmaven notify role <role>` and `{p}setmaven notify mode <ping|silent|none>`\n"
                   f"`{p}help setmaven notify` and `{p}help setmaven menu` for message, colour, timeout, etc."),
            inline=False,
        )
        embed.add_field(
            name="Where to look next",
            value=(f"`{p}help setmaven`: all admin commands\n"
                   f"`{p}setmaven settings` / `{p}setmaven list`: current settings and checkouts\n"
                   f"`{p}help maven`: user commands (`{p}maven`, `{p}maven checkin`, `{p}maven status`)"),
            inline=False,
        )
        await ctx.send(embed=embed)

    # ---- notification settings ---------------------------------------------
    @setmaven.group(name="notify")
    async def sm_notify(self, ctx: commands.Context):
        """Configure the checkout notification."""

    @sm_notify.command(name="channel")
    async def sm_notify_channel(self, ctx, channel: discord.TextChannel):
        """Set the channel checkout/check-in notifications are sent to."""
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await ctx.send(err("I need Send Messages and Embed Links there."))
        await self.config.guild(ctx.guild).notify_channel.set(channel.id)
        await ctx.send(ok(f"Notifications will go to {channel.mention}."))

    @sm_notify.command(name="role")
    async def sm_notify_role(self, ctx, role: Optional[discord.Role] = None):
        """Set the role to notify (omit to clear)."""
        if role is not None and role.is_default():
            return await ctx.send(err("The notify role can't be @everyone."))
        await self.config.guild(ctx.guild).notify_role.set(role.id if role else None)
        await ctx.send(ok(f"Notify role {'set to ' + role.mention if role else 'cleared'}."),
                       allowed_mentions=discord.AllowedMentions.none())

    @sm_notify.command(name="mode")
    async def sm_notify_mode(self, ctx, mode: str):
        """How the notify role is mentioned: `ping`, `silent` (shows, no notification) or `none`."""
        mode = mode.lower()
        if mode not in ("ping", "silent", "none"):
            return await ctx.send(err("Mode must be `ping`, `silent` or `none`."))
        await self.config.guild(ctx.guild).notify_mode.set(mode)
        await ctx.send(ok(f"Notify mode set to `{mode}`."))

    @sm_notify.command(name="logcheckin")
    async def sm_notify_logcheckin(self, ctx, enabled: bool):
        """Toggle logging of check-ins/expirations to the notification channel."""
        await self.config.guild(ctx.guild).log_checkin.set(enabled)
        await ctx.send(ok(f"Check-in logging {'enabled' if enabled else 'disabled'}."))

    async def _set_text(self, ctx, key: str, text: str, limit: int):
        if text.lower() == "reset":
            text = DEFAULT_GUILD[key]
        if len(text) > limit:
            return await ctx.send(err(f"Max {limit} characters."))
        await self.config.guild(ctx.guild).set_raw(key, value=text)
        await ctx.send(ok("Updated."))

    @sm_notify.command(name="message")
    async def sm_notify_message(self, ctx, *, text: str):
        """Set the "Automation Description" text. Placeholders: {user} {user_id} {role} {duration}. `reset` restores default."""
        await self._set_text(ctx, "notify_message", text, 1000)

    @sm_notify.command(name="title")
    async def sm_notify_title(self, ctx, *, text: str):
        """Set the notification embed title."""
        await self._set_text(ctx, "notify_title", text, 256)

    @sm_notify.command(name="footer")
    async def sm_notify_footer(self, ctx, *, text: str):
        """Set the notification embed footer (the checkout name is appended)."""
        await self._set_text(ctx, "notify_footer", text, 150)

    @sm_notify.command(name="color", aliases=["colour"])
    async def sm_notify_color(self, ctx, colour: discord.Colour):
        """Set the notification embed colour (hex like #FF0000)."""
        await self.config.guild(ctx.guild).notify_color.set(colour.value)
        await ctx.send(ok("Updated."))

    # ---- menu settings ------------------------------------------------------
    @setmaven.group(name="menu")
    async def sm_menu(self, ctx: commands.Context):
        """Configure the checkout menu."""

    @sm_menu.command(name="title")
    async def sm_menu_title(self, ctx, *, text: str):
        """Set the menu title."""
        await self._set_text(ctx, "menu_title", text, 256)

    @sm_menu.command(name="description")
    async def sm_menu_description(self, ctx, *, text: str):
        """Set the menu description. Placeholders: {user} {user_name}."""
        await self._set_text(ctx, "menu_description", text, 1500)

    @sm_menu.command(name="footer")
    async def sm_menu_footer(self, ctx, *, text: str):
        """Set the menu footer."""
        await self._set_text(ctx, "menu_footer", text, 150)

    @sm_menu.command(name="color", aliases=["colour"])
    async def sm_menu_color(self, ctx, colour: discord.Colour):
        """Set the menu embed colour."""
        await self.config.guild(ctx.guild).menu_color.set(colour.value)
        await ctx.send(ok("Updated."))

    @sm_menu.command(name="image")
    async def sm_menu_image(self, ctx, url: str):
        """Set the menu image URL (`none` to clear)."""
        if url.lower() == "none":
            await self.config.guild(ctx.guild).menu_image.set(None)
        elif url.startswith(("http://", "https://")):
            await self.config.guild(ctx.guild).menu_image.set(url)
        else:
            return await ctx.send(err("Provide an http(s) URL or `none`."))
        await ctx.send(ok("Updated."))

    @sm_menu.command(name="timeout", aliases=["cooldown"])
    async def sm_menu_timeout(self, ctx, duration: str):
        """How long the menu stays usable before its options become unavailable (15s-5m)."""
        raw = duration.lower().replace(" ", "")
        matches = list(DUR_RE.finditer(raw))
        if raw.isdigit():
            seconds = int(raw)
        elif matches and "".join(m.group(0) for m in matches) == raw:
            seconds = sum(int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] for m in matches)
        else:
            seconds = 0
        if not 15 <= seconds <= 300:
            return await ctx.send(err("Use a value between `15s` and `5m` (bare numbers are seconds)."))
        await self.config.guild(ctx.guild).menu_timeout.set(seconds)
        await ctx.send(ok(f"Menu timeout set to {dur(seconds)}."))

    @sm_menu.command(name="maxactive")
    async def sm_menu_maxactive(self, ctx, amount: commands.Range[int, 1, 10]):
        """Max simultaneous checkouts per member."""
        await self.config.guild(ctx.guild).max_active.set(amount)
        await ctx.send(ok(f"Members may hold up to {amount} checkout(s) at once."))