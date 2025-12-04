import logging
from datetime import datetime, timezone
from typing import List, Optional, TypeVar, Union

import discord
from redbot.core import checks, commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.cogs.modlog import ModLog
from redbot.core.bot import Red
from redbot.core.modlog import Case, get_cases_for_member, get_casetype
from redbot.core.utils import chat_formatting as cf
from redbot.core.utils.menus import menu

_ = Translator("Check", __file__)
_T = TypeVar("_T")


def chunks(l: List[_T], n: int):
    for i in range(0, len(l), n):
        yield l[i : i + n]


@cog_i18n(_)
class Check(commands.Cog):
    """Check"""

    __version__ = "2.2.0-etn_1.3"

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
    async def check(self, ctx, member: discord.Member):
        ctx.assume_yes = True
        await ctx.send(
            _(":mag_right: Starting lookup for: {usermention}({userid})").format(
                usermention=member.mention, userid=member.id
            )
        )
        await self._userinfo(ctx, member)
        await self._maybe_altmarker(ctx, member)
        
        # Create tasks for modlog and defender messages to run concurrently
        import asyncio
        tasks = [
            asyncio.create_task(self._warnings_or_read(ctx, member)),
            asyncio.create_task(self._maybe_listflag(ctx, member)),
            asyncio.create_task(self._maybe_defender_messages(ctx, member))
        ]
        
        # Wait for all tasks to complete
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _userinfo(self, ctx, member):
        try:
            await ctx.invoke(ctx.bot.get_command("userinfo"), member=member)
        except TypeError:
            try:
                await ctx.invoke(ctx.bot.get_command("userinfo"), user=member)
            except:
                pass
        except Exception as e:
            self.log.exception(f"Error in userinfo {e}", exc_info=True)

    async def _warnings_or_read(self, ctx, member):
        async with ctx.typing():
            try:
                cases = await get_cases_for_member(
                    bot=ctx.bot, guild=ctx.guild, member=member
                )
            except discord.NotFound:
                return await ctx.send("That user does not exist.")
            except discord.HTTPException:
                return await ctx.send(
                    "Something unexpected went wrong while fetching that user by ID."
                )
            if not cases:
                return await ctx.send("That user does not have any cases.")

            rendered_cases = []
            for page, ccases in enumerate(chunks(cases, 6), 1):
                embed = discord.Embed(
                    title=f"Cases for `{member.display_name}` (Page {page} / {len(cases) // 6 + 1 if len(cases) % 6 else len(cases) // 6})",
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
                        duration = end - start
                        dur_fmt = cf.humanize_timedelta(timedelta=duration)
                        until = f"Until: {end_fmt}\n"
                        duration = f"Length: {dur_fmt}\n"
                        length = until + duration

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

        await menu(ctx, rendered_cases)

    async def _maybe_listflag(self, ctx, member):
        try:
            await ctx.invoke(ctx.bot.get_command("listflag"), member=member)
        except:
            self.log.debug("Command listflag not found.")

    async def _maybe_altmarker(self, ctx, member):
        try:
            await ctx.invoke(ctx.bot.get_command("alt get"), member=member)
        except:
            self.log.debug("Altmarker not found.")

    async def _maybe_defender_messages(self, ctx, member):
        """Display cached messages from Defender if available"""
        try:
            defender_cog = ctx.bot.get_cog("Defender")
            if not defender_cog:
                self.log.debug("Defender cog not found.")
                return

            # Import the cache module from defender
            try:
                from defender.core import cache as df_cache
            except ImportError:
                self.log.debug("Could not import Defender's cache module.")
                return

            # Get cached messages for the user
            messages = df_cache.get_user_messages(member)
            
            if not messages:
                await ctx.send(f"No cached messages found for {member.mention}.")
                return

            # Format the messages similar to Defender's format
            text_unauthorized = "[You are not authorized to access that channel]"
            _log = []

            for m in messages:
                ts = m.created_at.strftime("%H:%M:%S")
                channel = ctx.guild.get_channel(m.channel_id) or ctx.guild.get_thread(m.channel_id)
                
                # Check if requester has read permissions
                if channel:
                    requester_can_rm = channel.permissions_for(ctx.author).read_messages
                else:
                    requester_can_rm = True
                
                channel_name = f"#{channel.name}" if channel else str(m.channel_id)
                content = m.content if requester_can_rm else text_unauthorized
                
                # Handle message edits
                if m.edits:
                    entry = len(m.edits) + 1
                    _log.append(f"[{ts}]({channel_name})[{entry}] {content}")
                    for edit in m.edits:
                        entry -= 1
                        ts_edit = edit.edited_at.strftime("%H:%M:%S")
                        content_edit = edit.content if requester_can_rm else text_unauthorized
                        _log.append(f"[{ts_edit}]({channel_name})[{entry}] {content_edit}")
                else:
                    _log.append(f"[{ts}]({channel_name}) {content}")

            if not _log:
                await ctx.send(f"No cached messages found for {member.mention}.")
                return

            # Replace backticks to prevent formatting issues
            _log = [e.replace("`", "'") for e in _log]

            # Create pages for the message log
            from redbot.core.utils.chat_formatting import pagify, box
            pages = []
            for page in pagify("\n".join(_log), page_length=1300):
                pages.append(box(page, lang="md"))

            if len(pages) == 1:
                await ctx.send(f"**Cached Messages for {member}:**\n{pages[0]}")
            else:
                # Add title to first page
                pages[0] = f"**Cached Messages for {member}:**\n{pages[0]}"
                await menu(ctx, pages)

            # Log access to monitor if available
            if hasattr(defender_cog, 'send_to_monitor'):
                defender_cog.send_to_monitor(
                    ctx.guild,
                    f"{ctx.author} ({ctx.author.id}) accessed message history "
                    f"of user {member} ({member.id}) via Check command"
                )

        except Exception as e:
            self.log.exception(f"Error retrieving Defender message cache: {e}", exc_info=True)