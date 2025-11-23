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

    __version__ = "2.2.0"

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
