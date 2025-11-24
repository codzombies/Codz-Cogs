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
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import asyncio

_ = Translator("Check", __file__)
_T = TypeVar("_T")


def chunks(l: List[_T], n: int):
    for i in range(0, len(l), n):
        yield l[i : i + n]


@cog_i18n(_)
class Check(commands.Cog):
    """Check"""

    __version__ = "2.2.0-etn_1.2"

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
        # Track ongoing message log generation to prevent duplicates
        self._message_log_locks = {}

    @commands.command()
    @checks.mod()
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def check(self, ctx, member: discord.Member):
        ctx.assume_yes = True
        await ctx.send(
            _(":mag_right: Starting lookup for: {usermention}({userid})").format(
                usermention=member.mention, userid=member.id
            )
        )
        await self._userinfo(ctx, member)
        await self._maybe_altmarker(ctx, member)
        await self._warnings_or_read(ctx, member)
        await self._maybe_listflag(ctx, member)
        await self._defender_messages(ctx, member)

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

    async def _defender_messages(self, ctx, member):
        """Display cached messages from Defender for the user"""
        try:
            # Try to get Defender cog
            defender = ctx.bot.get_cog("Defender")
            if not defender:
                self.log.debug("Defender cog not found.")
                return

            # Create a unique lock key for this guild + member combination
            lock_key = (ctx.guild.id, member.id)
            
            # Check if message log is already being generated
            if lock_key in self._message_log_locks:
                # Skip silently to avoid duplicate output
                self.log.debug(f"Message log already being generated for {member.id}, skipping duplicate call")
                return
            
            # Create a lock and set a timeout flag
            lock = asyncio.Lock()
            self._message_log_locks[lock_key] = lock
            
            try:
                # Try to acquire the lock with a timeout
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=0.1)
                except asyncio.TimeoutError:
                    # Another task is generating the log, skip this one
                    self.log.debug(f"Could not acquire lock for {member.id}, another task is processing")
                    return
                
                # Use Defender's make_message_log method directly
                pages = await defender.make_message_log(
                    member, 
                    guild=ctx.guild, 
                    requester=ctx.author, 
                    pagify_log=True, 
                    replace_backtick=True
                )

                if not pages:
                    await ctx.send(f"No cached messages found for {member.display_name}.")
                    return

                # Log to monitor like the original command does
                send_to_monitor = getattr(defender, 'send_to_monitor', None)
                if send_to_monitor:
                    send_to_monitor(
                        ctx.guild, 
                        f"{ctx.author} ({ctx.author.id}) accessed message history "
                        f"of user {member} ({member.id}) via check command"
                    )

                # Display using the same format as the defender command
                if len(pages) == 1:
                    await ctx.send(cf.box(pages[0], lang="md"))
                else:
                    pages = [cf.box(p, lang="md") for p in pages]
                    await menu(ctx, pages, DEFAULT_CONTROLS)
                    
            finally:
                # Always release the lock and clean up
                if lock.locked():
                    lock.release()
                # Remove the lock after a short delay to prevent immediate re-execution
                await asyncio.sleep(2)
                self._message_log_locks.pop(lock_key, None)
            
        except Exception as e:
            self.log.exception(f"Error retrieving defender messages: {e}", exc_info=True)
            # Clean up lock on error
            lock_key = (ctx.guild.id, member.id)
            self._message_log_locks.pop(lock_key, None)