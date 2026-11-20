import asyncio
import logging
from datetime import datetime, timezone
from typing import List, TypeVar, Union

import discord
from redbot.core import checks, commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.cogs.modlog import ModLog
from redbot.core.bot import Red
from redbot.core.modlog import Case, get_cases_for_member, get_casetype
from redbot.core.utils import chat_formatting as cf
from redbot.core.utils.chat_formatting import box, pagify
from redbot.core.utils.views import SimpleMenu

_ = Translator("Check", __file__)
_T = TypeVar("_T")


def chunks(l: List[_T], n: int):
    for i in range(0, len(l), n):
        yield l[i : i + n]


@cog_i18n(_)
class Check(commands.Cog):
    """Check"""

    __version__ = "2.4.1"

    def format_help_for_context(self, ctx: commands.Context) -> str:
        # Thanks Sinbad! And Trusty in whose cogs I found this.
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester, user_id):
        # This cog stores no EUD
        return

    def __init__(self, bot):
        self.bot = bot
        self.log = logging.getLogger("red.cog.dav-cogs.check")

    @commands.command()
    @checks.mod()
    @commands.max_concurrency(3, commands.BucketType.guild)
    async def check(self, ctx, user_id: Union[discord.Member, int]):
        """Check information about a user (can be a member or user ID)"""
        ctx.assume_yes = True

        member = user_id if isinstance(user_id, discord.Member) else None
        user_id = member.id if member else user_id

        # Fetch the slow data concurrently, but send the results in a fixed order:
        # userinfo -> cases -> flags -> defender messages.
        cases_task = asyncio.create_task(self._build_cases(ctx, user_id))
        defender_task = asyncio.create_task(self._build_defender_messages(ctx, user_id))

        try:
            if member:
                header = _("Starting lookup for: {usermention}({userid})").format(
                    usermention=member.mention, userid=user_id
                )
                await self._userinfo(ctx, member, header)
                await self._maybe_altmarker(ctx, member)
            else:
                await ctx.send(
                    _("Starting lookup for: User ID {userid}").format(userid=user_id)
                    + "\n"
                    + _("User is not in the server. Userinfo display skipped.")
                )

            await self._send_pages(ctx, await cases_task)
            await self._maybe_listflag(ctx, user_id)

            defender_result = await defender_task
            if defender_result is not None:
                pages, on_sent = defender_result
                await self._send_pages(ctx, pages)
                if on_sent:
                    on_sent()
        except Exception:
            self.log.exception("Error while running check", exc_info=True)
        finally:
            for task in (cases_task, defender_task):
                if not task.done():
                    task.cancel()

    async def _send_pages(self, ctx, pages):
        """Send a string, a single page, or a paged menu."""
        if not pages:
            return
        if isinstance(pages, str):
            await ctx.send(pages)
        elif len(pages) == 1:
            page = pages[0]
            if isinstance(page, discord.Embed):
                await ctx.send(embed=page)
            else:
                await ctx.send(page)
        else:
            # SimpleMenu.start returns once the message is posted, so ordering is kept.
            await SimpleMenu(pages).start(ctx)

    async def _userinfo(self, ctx, member, header: str):
        """Invoke userinfo, attaching ``header`` as content to the first message it sends."""
        attached = False
        original_send = ctx.send

        async def send_with_header(content=None, **kwargs):
            nonlocal attached
            if not attached:
                attached = True
                content = f"{header}\n{content}" if content else header
            return await original_send(content, **kwargs)

        ctx.send = send_with_header
        try:
            command = ctx.bot.get_command("userinfo")
            if command is not None:
                try:
                    await ctx.invoke(command, member=member)
                except TypeError:
                    try:
                        await ctx.invoke(command, user=member)
                    except Exception:
                        pass
        except Exception as e:
            self.log.exception(f"Error in userinfo {e}", exc_info=True)
        finally:
            del ctx.send  # restore the class method

        if not attached:
            await ctx.send(header)

    async def _build_cases(self, ctx, user_id: int):
        """Build modlog case pages for a user. Returns a str or a list of embeds."""
        member = ctx.guild.get_member(user_id)
        display_name = member.display_name if member else f"Unknown User ({user_id})"

        try:
            if member is None:
                # Mock member object since we only have an ID
                from collections import namedtuple
                MockMember = namedtuple("MockMember", ["id", "guild"])
                member = MockMember(id=user_id, guild=ctx.guild)

            cases = await get_cases_for_member(bot=ctx.bot, guild=ctx.guild, member=member)
        except discord.NotFound:
            return "❌ That user does not exist."
        except discord.HTTPException:
            return "❌ Something unexpected went wrong while fetching that user by ID."

        if not cases:
            return f"No cases listed for {display_name}."

        total_pages = (len(cases) + 5) // 6
        rendered_cases = []
        for page, ccases in enumerate(chunks(cases, 6), 1):
            embed = discord.Embed(
                title=f"Cases for `{display_name}` (Page {page} / {total_pages})",
            )
            for case in ccases:
                if case.moderator is None:
                    moderator = "Unknown"
                elif isinstance(case.moderator, int):
                    if case.moderator == 0xDE1:
                        moderator = "Deleted User."
                    else:
                        translated = "Unknown or Deleted User"
                        moderator = f"[{translated}] ({case.moderator})"
                else:
                    moderator = f"{case.moderator} ({case.moderator.id})"

                length = ""
                if case.until:
                    start = datetime.fromtimestamp(case.created_at, tz=timezone.utc)
                    end = datetime.fromtimestamp(case.until, tz=timezone.utc)
                    end_fmt = discord.utils.format_dt(end)
                    dur_fmt = cf.humanize_timedelta(timedelta=end - start)
                    length = f"Until: {end_fmt}\nLength: {dur_fmt}\n"

                created_at = datetime.fromtimestamp(case.created_at, tz=timezone.utc)
                embed.add_field(
                    name=f"Case #{case.case_number} | {(await get_casetype(case.action_type, ctx.guild)).case_str}",
                    value=f"{cf.bold('Moderator:')} {moderator}\n"
                    f"{cf.bold('Reason:')} {case.reason}\n"
                    f"{length}"
                    f"{cf.bold('Timestamp:')} {discord.utils.format_dt(created_at)}\n\n",
                    inline=False,
                )
            rendered_cases.append(embed)

        return rendered_cases

    async def _maybe_listflag(self, ctx, user_id):
        """Display flags for a user (accepts member or user ID)"""
        try:
            member = ctx.guild.get_member(user_id)
            if not member:
                # Can't run listflag without a member object
                return


            await ctx.invoke(ctx.bot.get_command("listflag"), member=member)
        except:
            self.log.debug("Command listflag not found.")

    async def _maybe_altmarker(self, ctx, member):
        try:
            await ctx.invoke(ctx.bot.get_command("alt get"), member=member)
        except:
            self.log.debug("Altmarker not found.")

    async def _build_defender_messages(self, ctx, user_id: int):
        """Build Defender cached message pages for a user.

        Returns ``None`` if Defender is unavailable, otherwise ``(pages, on_sent)`` where
        ``pages`` is a str or list of str and ``on_sent`` is an optional callback to run
        after the pages have been sent.
        """
        try:
            defender_cog = ctx.bot.get_cog("Defender")
            if not defender_cog:
                self.log.debug("Defender cog not found.")
                return None

            try:
                from defender.core import cache as df_cache
            except ImportError:
                self.log.debug("Could not import Defender's cache module.")
                return None

            member = ctx.guild.get_member(user_id)
            if member:
                display_str = str(member)
            else:
                # Mock user object for cache lookup
                from defender.core.cache import CacheUser
                member = CacheUser(_id=user_id, guild=ctx.guild)
                display_str = f"Unknown User ({user_id})"

            total_messages = 0
            if hasattr(defender_cog, "get_total_recorded_messages"):
                try:
                    total_messages = await defender_cog.get_total_recorded_messages(member)
                except Exception as e:
                    self.log.debug(f"Error getting message count: {e}")

            no_messages = (
                f"No cached messages found for {display_str}. "
                f"(Total messages sent in server: {total_messages})"
            )

            messages = df_cache.get_user_messages(member)
            if not messages:
                return no_messages, None

            # Format the messages similar to Defender's format
            text_unauthorized = "[You are not authorized to access that channel]"
            _log = []

            def fmt_ts(dt) -> str:
                # Match Defender: host-local time with the day, since the cache spans days
                return dt.astimezone().strftime("%m/%d %H:%M:%S")

            for m in messages:
                ts = fmt_ts(m.created_at)
                channel = ctx.guild.get_channel(m.channel_id) or ctx.guild.get_thread(m.channel_id)

                if channel:
                    requester_can_rm = channel.permissions_for(ctx.author).read_messages
                else:
                    requester_can_rm = True

                channel_name = f"#{channel.name}" if channel else str(m.channel_id)
                content = m.content if requester_can_rm else text_unauthorized

                if m.edits:
                    entry = len(m.edits) + 1
                    _log.append(f"[{ts}]({channel_name})[{entry}] {content}")
                    for edit in m.edits:
                        entry -= 1
                        ts_edit = fmt_ts(edit.edited_at)
                        content_edit = edit.content if requester_can_rm else text_unauthorized
                        _log.append(f"[{ts_edit}]({channel_name})[{entry}] {content_edit}")
                else:
                    _log.append(f"[{ts}]({channel_name}) {content}")

            if not _log:
                return no_messages, None

            # Replace backticks to prevent formatting issues
            _log = [e.replace("`", "'") for e in _log]

            pages = [box(page, lang="md") for page in pagify("\n".join(_log), page_length=1300)]
            pages[0] = (
                f"Cached Messages for {display_str}: "
                f"(Total messages sent in server: {total_messages})\n{pages[0]}"
            )

            def on_sent():
                if hasattr(defender_cog, "send_to_monitor"):
                    defender_cog.send_to_monitor(
                        ctx.guild,
                        f"{ctx.author} ({ctx.author.id}) accessed message history "
                        f"of user {user_id} via check command",
                    )

            return pages, on_sent

        except Exception as e:
            self.log.exception(f"Error retrieving Defender message cache: {e}", exc_info=True)
            return None
