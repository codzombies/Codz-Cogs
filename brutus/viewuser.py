import asyncio
import datetime
import importlib
import logging
from typing import TYPE_CHECKING, List, Optional, Union

import discord
from discord import SelectOption, ui
from redbot.core import modlog
from redbot.core.utils.chat_formatting import box, escape, humanize_timedelta

if TYPE_CHECKING:
    from .brutus import Brutus

log = logging.getLogger("red.codz.brutus")

Page = Union[str, discord.Embed]

VIEW_OPTIONS = (
    ("modlog", "Modlog", "📜", "The user's modlog cases"),
    ("messages", "Recent Messages", "💬", "Messages cached by Defender, like [p]defender messages user"),
    ("info", "User Info", "👤", "Account and server details"),
    ("check", "Check", "🔍", "All of the above at once, like [p]check"),
)

# What gets added after "isn't in the server right now." on each kind of result
ABSENT_NOTES = {
    "modlog": "",
    "messages": " Showing whatever is still cached from before they left.",
    "info": " Showing their Discord account only: no server details are available.",
}

CASES_PER_PAGE = 6
CASE_REASON_MAX = 800  # 6 fields of this stay under an embed's 6000 characters
FIELD_VALUE_MAX = 1024


async def can_view(bot, user: discord.Member) -> bool:
    """Red's mod or admin roles, the server owner, administrators or a bot owner, like [p]check"""
    guild = user.guild
    return (
        user == guild.owner
        or user.guild_permissions.administrator
        or await bot.is_mod(user)
        or await bot.is_owner(user)
    )


def get_defender(bot):
    """The Defender cog, if it's loaded and has the message cache (make_message_log) of the codzombies fork"""
    defender = bot.get_cog("Defender")
    if defender is None or not hasattr(defender, "make_message_log"):
        return None
    return defender


def _defender_subject(defender, guild: discord.Guild, user_id: int):
    """What Defender's message log takes: the member, or its CacheUser for someone who left"""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    # Imported from wherever Defender was loaded, as its isinstance checks need its own class
    package = type(defender).__module__.rpartition(".")[0]
    try:
        cache_user = importlib.import_module(f"{package}.core.cache").CacheUser
    except (ImportError, AttributeError):
        log.debug("Could not import Defender's CacheUser", exc_info=True)
        return None
    return cache_user(user_id, guild)


def _case_moderator(case) -> str:
    if case.moderator is None:
        return "Unknown"
    if isinstance(case.moderator, int):
        if case.moderator == 0xDE1:
            return "Deleted User."
        return f"[Unknown or Deleted User] ({case.moderator})"
    return f"{case.moderator} ({case.moderator.id})"


async def _view_modlog(inter: discord.Interaction, user_id: int) -> List[Page]:
    """The user's cases, displayed like the check cog's "bettermodlog": 6 per embed page"""
    guild = inter.guild
    # Someone who left is usually still in the bot's user cache, which keeps their name
    user = guild.get_member(user_id) or inter.client.get_user(user_id)
    display_name = user.display_name if user else f"Unknown User ({user_id})"
    try:
        cases = await modlog.get_cases_for_member(guild, inter.client, member_id=user_id)
    except discord.NotFound:
        return ["❌ That user does not exist."]
    except discord.HTTPException:
        return ["❌ Something unexpected went wrong while fetching that user by ID."]
    if not cases:
        return [f"**👉 No cases listed for {escape(display_name, formatting=True)}.**"]

    total_pages = -(-len(cases) // CASES_PER_PAGE)
    pages = []
    for i in range(total_pages):
        embed = discord.Embed(title=f"Cases for `{display_name}` (Page {i + 1} / {total_pages})")
        for case in cases[i * CASES_PER_PAGE : (i + 1) * CASES_PER_PAGE]:
            length = ""
            if case.until:
                start = datetime.datetime.fromtimestamp(case.created_at, tz=datetime.timezone.utc)
                end = datetime.datetime.fromtimestamp(case.until, tz=datetime.timezone.utc)
                length = f"Until: {discord.utils.format_dt(end)}\nLength: {humanize_timedelta(timedelta=end - start)}\n"
            created_at = datetime.datetime.fromtimestamp(case.created_at, tz=datetime.timezone.utc)
            casetype = await modlog.get_casetype(case.action_type, guild)
            reason = case.reason or "None"
            if len(reason) > CASE_REASON_MAX:
                reason = reason[: CASE_REASON_MAX - 1] + "…"
            embed.add_field(
                name=f"Case #{case.case_number} | {casetype.case_str if casetype else case.action_type}",
                value=(
                    f"**Moderator:** {_case_moderator(case)}\n"
                    f"**Reason:** {reason}\n"
                    f"{length}"
                    f"**Timestamp:** {discord.utils.format_dt(created_at)}"
                ),
                inline=False,
            )
        pages.append(embed)
    return pages


async def _view_messages(inter: discord.Interaction, user_id: int) -> List[Page]:
    guild = inter.guild
    moderator = inter.user
    defender = get_defender(inter.client)
    if defender is None:
        return ["❌ Recent messages come from Defender's message cache, and Defender isn't loaded."]
    subject = _defender_subject(defender, guild, user_id)
    if subject is None:
        return ["❌ Couldn't read Defender's message cache for a user who isn't in the server."]
    # Same output and channel permission filtering as [p]defender messages user
    pages = await defender.make_message_log(
        subject, guild=guild, requester=moderator, pagify_log=True, replace_backtick=True
    )
    if not pages:
        return [f"No messages recorded for <@{user_id}> (`{user_id}`)."]
    defender.send_to_monitor(guild, f"{moderator} ({moderator.id}) accessed message history of user {subject} ({user_id})")
    header = f"Cached message log for <@{user_id}> (`{user_id}`)"
    if isinstance(subject, discord.Member):
        header += f" (Total messages sent in server: {await defender.get_total_recorded_messages(subject)})"
    return [f"{header}\n{box(page, lang='md')}" for page in pages]


def _role_list(roles: List[discord.Role]) -> str:
    text = ""
    for i, role in enumerate(roles):
        chunk = f"{role.mention}, " if i < len(roles) - 1 else role.mention
        more = f"and {len(roles) - i} more"
        if len(text) + len(chunk) > FIELD_VALUE_MAX - len(more):
            return text + more
        text += chunk
    return text


async def _view_info(inter: discord.Interaction, user_id: int) -> Page:
    guild = inter.guild
    member = guild.get_member(user_id)
    user = member
    if user is None:
        try:
            user = await inter.client.fetch_user(user_id)
        except discord.NotFound:
            return f"❌ No Discord account exists with the ID `{user_id}`."
        except discord.HTTPException:
            return "❌ Something unexpected went wrong while fetching that user by ID."

    def when(dt: Optional[datetime.datetime]) -> str:
        return f"{discord.utils.format_dt(dt)}\n{discord.utils.format_dt(dt, 'R')}" if dt else "Unknown"

    name = str(user)
    if member is not None and member.nick:
        name += f" ~ {member.nick}"
    em = discord.Embed(
        description=f"{user.mention} (`{user.id}`)",
        colour=member.colour if member else discord.Colour.default(),
    )
    avatar = user.display_avatar.replace(static_format="png")
    em.set_author(name=name, url=avatar)
    em.set_thumbnail(url=avatar)
    em.add_field(name="Joined Discord on", value=when(user.created_at))

    if member is not None:
        em.add_field(name="Joined this server on", value=when(member.joined_at))
        defender = get_defender(inter.client)
        if defender is not None:
            try:
                em.add_field(name="Defender rank", value=f"Rank {int(await defender.rank_user(member))}")
                em.add_field(name="Messages sent in server", value=str(await defender.get_total_recorded_messages(member)))
            except Exception:
                log.error("Failed to get Defender's details of %s", user_id, exc_info=True)
        if member.is_timed_out():
            em.add_field(name="Timed out until", value=when(member.timed_out_until))
        roles = member.roles[-1:0:-1]  # Highest first, without @everyone
        if roles:
            em.add_field(name="Roles" if len(roles) > 1 else "Role", value=_role_list(roles), inline=False)

        # Name history, if Red's Mod cog is loaded and tracking it
        get_names = getattr(inter.client.get_cog("Mod"), "get_names", None)
        if get_names is not None:
            try:
                usernames, display_names, nicks = await get_names(member)
            except Exception:
                log.error("Failed to get the name history of %s", user_id, exc_info=True)
            else:
                for title, names in (
                    ("Previous usernames", usernames),
                    ("Previous display names", display_names),
                    ("Previous nicknames", nicks),
                ):
                    if names:
                        value = escape(", ".join(names), formatting=True)
                        em.add_field(name=title, value=value[:FIELD_VALUE_MAX], inline=False)

    em.set_footer(text=f"User ID: {user.id}")
    return em


class PagedMenu(ui.View):
    """Button version of Red's reaction menu (same emojis). Only whoever opened it can use it,
    ❌ deletes the message"""

    def __init__(self, author: discord.abc.User, pages: List[Page], *, timeout=180):
        super().__init__(timeout=timeout)
        self.author = author
        self.pages = pages
        self.page = 0
        self.message: Optional[discord.WebhookMessage] = None
        multiple = len(pages) > 1
        for emoji, callback, show in (
            ("⬅️", self._prev, multiple),
            ("❌", self._close, True),
            ("➡️", self._next, multiple),
        ):
            if not show:
                continue
            button = ui.Button(emoji=emoji, style=discord.ButtonStyle.danger if emoji == "❌" else discord.ButtonStyle.secondary)
            button.callback = callback
            self.add_item(button)

    def page_kwargs(self) -> dict:
        page = self.pages[self.page]
        if isinstance(page, discord.Embed):
            return {"content": None, "embed": page}
        return {"content": page, "embed": None}

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.author.id:
            await inter.response.send_message("Only the user who opened this menu can use it.", ephemeral=True)
            return False
        return True

    async def _prev(self, inter: discord.Interaction):
        self.page = (self.page - 1) % len(self.pages)  # Wraps around, like Red's menu
        await inter.response.edit_message(**self.page_kwargs(), view=self)

    async def _next(self, inter: discord.Interaction):
        self.page = (self.page + 1) % len(self.pages)
        await inter.response.edit_message(**self.page_kwargs(), view=self)

    async def _close(self, inter: discord.Interaction):
        self.stop()
        await inter.response.defer()
        try:
            await inter.delete_original_response()  # On a component, the (ephemeral) message it's attached to
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class ViewUserModal(ui.Modal):
    """Asks which user and which of their details to show, then shows them ephemerally"""

    def __init__(self, cog: "Brutus", user_id: int, *, from_message: bool = True):
        super().__init__(title="View User", timeout=600)
        self.cog = cog

        self.target_input = ui.TextInput(default=str(user_id), min_length=15, max_length=21)
        self.add_item(
            ui.Label(
                text="User ID",
                description="The author of the message." if from_message else "The selected user.",
                component=self.target_input,
            )
        )

        has_messages = get_defender(cog.bot) is not None
        self.choice = ui.Select(
            placeholder="What do you want to see?",
            options=[
                SelectOption(value=value, label=label, emoji=emoji, description=description)
                for value, label, emoji, description in VIEW_OPTIONS
                if value != "messages" or has_messages
            ],
            required=True,
        )
        self.add_item(ui.Label(text="Show", component=self.choice))

    async def on_submit(self, inter: discord.Interaction):
        try:
            user_id = int(self.target_input.value.strip())
        except ValueError:
            return await inter.response.send_message(f"`{self.target_input.value}` is not a valid user ID.", ephemeral=True)

        await inter.response.defer(ephemeral=True, thinking=True)
        choice = self.choice.values[0]
        if choice == "check":
            # Same order as [p]check: user info, then modlog, then recent messages, fetched concurrently
            kinds = ["info", "modlog"]
            if get_defender(self.cog.bot) is not None:
                kinds.append("messages")
        else:
            kinds = [choice]
        results = await asyncio.gather(*(self._fetch(inter, kind, user_id) for kind in kinds))

        absent = inter.guild.get_member(user_id) is None
        for kind, pages in zip(kinds, results):
            if absent:
                self._add_absent_notice(pages, kind, user_id)
            await self._send(inter, pages)

    @staticmethod
    async def _fetch(inter: discord.Interaction, kind: str, user_id: int) -> List[Page]:
        if kind == "modlog":
            return await _view_modlog(inter, user_id)
        if kind == "messages":
            return await _view_messages(inter, user_id)
        return [await _view_info(inter, user_id)]

    @staticmethod
    def _add_absent_notice(pages: List[Page], kind: str, user_id: int):
        """A heads-up that the user isn't here, on every page so it doesn't disappear when flipping
        through them. Errors (❌) already say what's wrong"""
        if isinstance(pages[0], str) and pages[0].startswith("❌"):
            return
        notice = f"ℹ️ <@{user_id}> (`{user_id}`) isn't in the server right now.{ABSENT_NOTES[kind]}"
        for i, page in enumerate(pages):
            if isinstance(page, discord.Embed):
                page.description = f"{notice}\n\n{page.description}" if page.description else notice
            else:
                pages[i] = f"{notice}\n{page}"

    @staticmethod
    async def _send(inter: discord.Interaction, pages: List[Page]):
        none = discord.AllowedMentions.none()
        page = pages[0]
        kwargs = {"embed": page} if isinstance(page, discord.Embed) else {"content": page}
        if len(pages) == 1:
            return await inter.followup.send(**kwargs, ephemeral=True, allowed_mentions=none)
        view = PagedMenu(inter.user, pages)
        view.message = await inter.followup.send(**kwargs, view=view, ephemeral=True, allowed_mentions=none, wait=True)

    async def on_error(self, inter: discord.Interaction, error: Exception):
        log.error("View User failed", exc_info=error)
        text = "Something went wrong while looking up that user. Check the bot's logs."
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass
