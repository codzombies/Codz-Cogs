"""
Plutonium - live Components V2 status boards for Plutonium (T4/T5/T6) gameservers,
powered by the public gameserve.rs API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Dict, Optional, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_timedelta, pagify

from .api import APIError, GameServeClient, normalize_address, server_url
from .render import (
    DEFAULT_EMOJIS,
    EMOJI_KEYS,
    GAME_DEFAULTS,
    GAMES,
    build_view,
    clean_name,
    display_name,
    game_from_api,
    new_embed_config,
    new_server_config,
)

log = logging.getLogger("red.codz.plutonium")

MIN_INTERVAL = 60
MAX_INTERVAL = 3600
MAX_EMBEDS_PER_GUILD = 10
MAX_SERVERS_PER_EMBED = 30
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:(\d{15,25})>$")

LIMITS = {"header": 1500, "title": 100, "description": 300, "label": 80, "note": 150}

NO_MENTIONS = discord.AllowedMentions.none()


class GameConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        game = game_from_api(argument)
        if game is None:
            raise commands.BadArgument("Game must be one of `T4`, `T5` or `T6`.")
        return game


class AddressConverter(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        address = normalize_address(argument)
        if address is None:
            raise commands.BadArgument(f"`{argument}` is not a valid `ip:port` address.")
        return address


class Plutonium(commands.Cog):
    """Live status boards for Plutonium T4/T5/T6 gameservers (via gameserve.rs)."""

    __version__ = "1.0.0"

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=519283746501928374, force_registration=True)
        self.config.register_global(interval=MIN_INTERVAL)
        self.config.register_guild(embeds={})
        self.api = GameServeClient()
        self._task: Optional[asyncio.Task] = None
        self._locks: Dict[Tuple[int, str], asyncio.Lock] = {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs) -> None:
        return  # no end-user data is stored

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        await self.api.close()

    # ================================================================ updating

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error while updating Plutonium status embeds")
            interval = await self.config.interval()
            await asyncio.sleep(max(MIN_INTERVAL, min(MAX_INTERVAL, interval)))

    async def _tick(self) -> None:
        targets = []
        addresses = set()
        for guild_id, data in (await self.config.all_guilds()).items():
            guild = self.bot.get_guild(guild_id)
            if guild is None or await self.bot.cog_disabled_in_guild(self, guild):
                continue
            for name, emb in (data.get("embeds") or {}).items():
                if emb.get("message_id"):
                    targets.append((guild, name))
                    addresses.update((emb.get("servers") or {}).keys())
        if not targets:
            return
        try:
            await self.api.refresh(addresses)
        except APIError:
            # Keep the last good render; its "Updated" timestamp shows the staleness.
            return
        for guild, name in targets:
            await self._update_embed(guild, name)

    def _lock(self, guild: discord.Guild, name: str) -> asyncio.Lock:
        return self._locks.setdefault((guild.id, name), asyncio.Lock())

    async def _remember_names(self, guild: discord.Guild, name: str) -> None:
        """Persist the latest live server names so offline servers keep a sensible name."""
        async with self.config.guild(guild).embeds() as embeds:
            emb = embeds.get(name)
            if not emb:
                return
            for address, cfg in emb.get("servers", {}).items():
                state, data = self.api.state(address)
                live = clean_name((data or {}).get("name"))
                if state == "online" and live and live != address and live != cfg.get("last_name"):
                    cfg["last_name"] = live

    async def _update_embed(
        self, guild: discord.Guild, name: str, *, fetch: bool = False
    ) -> Optional[str]:
        """Re-render one embed in place. Returns an error string, or None on success."""
        async with self._lock(guild, name):
            emb = (await self.config.guild(guild).embeds()).get(name)
            if not emb:
                return "That embed no longer exists."
            if fetch:
                with contextlib.suppress(APIError):
                    await self.api.refresh((emb.get("servers") or {}).keys())
            await self._remember_names(guild, name)
            emb = (await self.config.guild(guild).embeds()).get(name) or emb
            if not emb.get("message_id"):
                return "The status message is missing; use `pluto repost`."
            channel = guild.get_channel_or_thread(emb.get("channel_id") or 0)
            if channel is None:
                return "The configured channel no longer exists; use `pluto move`."
            prefix = (await self.bot.get_valid_prefixes(guild))[0]
            view = build_view(emb, self.api, prefix)
            try:
                await channel.get_partial_message(emb["message_id"]).edit(
                    view=view, allowed_mentions=NO_MENTIONS
                )
            except discord.NotFound:
                async with self.config.guild(guild).embeds() as embeds:
                    if name in embeds:
                        embeds[name]["message_id"] = None
                log.info("Plutonium embed %r in guild %s was deleted.", name, guild.id)
                return "The status message was deleted; use `pluto repost`."
            except discord.Forbidden:
                return f"I can't edit the status message in {channel.mention}."
            except discord.HTTPException as exc:
                log.warning("Failed to edit Plutonium embed %r in %s: %s", name, guild.id, exc)
                return f"Discord rejected the update: `{exc}`"
        return None

    async def _post(
        self, guild: discord.Guild, name: str, channel: discord.TextChannel
    ) -> Optional[str]:
        """Post a fresh status message for an embed (deleting the old one)."""
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return f"I can't send messages in {channel.mention}."
        async with self._lock(guild, name):
            emb = (await self.config.guild(guild).embeds()).get(name)
            if not emb:
                return "That embed no longer exists."
            with contextlib.suppress(APIError):
                await self.api.refresh((emb.get("servers") or {}).keys())
            prefix = (await self.bot.get_valid_prefixes(guild))[0]
            try:
                message = await channel.send(
                    view=build_view(emb, self.api, prefix), allowed_mentions=NO_MENTIONS
                )
            except discord.HTTPException as exc:
                return f"Discord rejected the message: `{exc}`"
            old_channel = guild.get_channel_or_thread(emb.get("channel_id") or 0)
            if old_channel and emb.get("message_id"):
                with contextlib.suppress(discord.HTTPException):
                    await old_channel.get_partial_message(emb["message_id"]).delete()
            async with self.config.guild(guild).embeds() as embeds:
                embeds[name]["channel_id"] = channel.id
                embeds[name]["message_id"] = message.id
        await self._remember_names(guild, name)
        return None

    # ================================================================ helpers

    async def _exists(self, ctx: commands.Context, name: str) -> bool:
        if name.lower() in await self.config.guild(ctx.guild).embeds():
            return True
        await ctx.send(
            f"No status embed named `{name}`. See `{ctx.clean_prefix}pluto list`.",
            allowed_mentions=NO_MENTIONS,
        )
        return False

    async def _apply(self, ctx: commands.Context, name: str, message: str) -> None:
        """Re-render after a config change and report the outcome."""
        error = await self._update_embed(ctx.guild, name, fetch=True)
        if error:
            message += f"\n:warning: {error}"
        await ctx.send(message, allowed_mentions=NO_MENTIONS)

    def _jump(self, guild: discord.Guild, emb: dict) -> str:
        channel = guild.get_channel_or_thread(emb.get("channel_id") or 0)
        if channel is None:
            return "channel missing"
        if not emb.get("message_id"):
            return f"{channel.mention} (message missing)"
        return f"https://discord.com/channels/{guild.id}/{channel.id}/{emb['message_id']}"

    @staticmethod
    def _too_long(field: str, text: Optional[str]) -> bool:
        return bool(text) and len(text) > LIMITS[field]

    # ================================================================ commands

    @commands.group(name="pluto", aliases=["plutonium"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def pluto(self, ctx: commands.Context) -> None:
        """Live status embeds for Plutonium T4/T5/T6 gameservers.

        Each status embed has a short name (e.g. `main`) used by every other command.
        Servers are listed alphabetically inside a T4, T5 or T6 section, and a
        section only appears when that embed has a server for it.
        """

    # ---------------------------------------------------------------- embeds

    @pluto.command(name="create")
    async def pluto_create(
        self, ctx: commands.Context, name: str, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Create a new status embed and post it.

        `name` is a short ID (letters, numbers, `-`, `_`) used by the other commands.
        Posts in the current channel unless another is given.
        """
        name = name.lower()
        if not NAME_RE.match(name):
            return await ctx.send("Names may only use `a-z`, `0-9`, `-` and `_` (max 32).")
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.send("Please pick a text channel.")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            if name in embeds:
                return await ctx.send(f"An embed named `{name}` already exists.")
            if len(embeds) >= MAX_EMBEDS_PER_GUILD:
                return await ctx.send(f"This server already has {MAX_EMBEDS_PER_GUILD} embeds.")
            embeds[name] = new_embed_config(channel.id)
        error = await self._post(ctx.guild, name, channel)
        if error:
            return await ctx.send(
                f"Created `{name}`, but couldn't post it: {error}\n"
                f"Fix that and run `{ctx.clean_prefix}pluto repost {name}`."
            )
        await ctx.send(
            f"Created status embed `{name}` in {channel.mention}. "
            f"Add servers with `{ctx.clean_prefix}pluto add {name} <ip:port> [T4|T5|T6]`."
        )

    @pluto.command(name="delete", aliases=["del"])
    async def pluto_delete(self, ctx: commands.Context, name: str) -> None:
        """Delete a status embed, its settings and its message."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        async with self._lock(ctx.guild, name):
            async with self.config.guild(ctx.guild).embeds() as embeds:
                emb = embeds.pop(name)
            channel = ctx.guild.get_channel_or_thread(emb.get("channel_id") or 0)
            if channel and emb.get("message_id"):
                with contextlib.suppress(discord.HTTPException):
                    await channel.get_partial_message(emb["message_id"]).delete()
        self._locks.pop((ctx.guild.id, name), None)
        await ctx.send(f"Deleted status embed `{name}`.")

    @pluto.command(name="list", aliases=["listembeds", "embeds"])
    async def pluto_list(self, ctx: commands.Context) -> None:
        """List this server's status embeds and where they are posted."""
        embeds = await self.config.guild(ctx.guild).embeds()
        if not embeds:
            return await ctx.send(
                f"No status embeds yet. Create one with `{ctx.clean_prefix}pluto create <name>`."
            )
        lines = ["## Plutonium status embeds"]
        for name, emb in sorted(embeds.items()):
            servers = emb.get("servers") or {}
            counts = ", ".join(
                f"{g}: {n}"
                for g in GAMES
                if (n := sum(1 for s in servers.values() if s.get("game") == g))
            )
            lines.append(
                f"- **`{name}`** \N{EM DASH} {len(servers)} server(s)"
                f"{f' ({counts})' if counts else ''} \N{EM DASH} {self._jump(ctx.guild, emb)}"
            )
        lines.append(f"-# {self._api_status()}")
        for page in pagify("\n".join(lines)):
            await ctx.send(page, allowed_mentions=NO_MENTIONS)

    def _api_status(self) -> str:
        parts = []
        if self.api.last_success:
            parts.append(f"Last API success <t:{int(self.api.last_success)}:R>")
        if self.api.backoff_remaining:
            parts.append(
                f"backing off {humanize_timedelta(seconds=int(self.api.backoff_remaining)) or '<1s'}"
            )
            if self.api.last_error:
                parts.append(f"last error: {self.api.last_error}")
        return " \N{MIDDLE DOT} ".join(parts) or "API not queried yet"

    @pluto.command(name="settings", aliases=["showsettings", "show", "info"])
    async def pluto_settings(self, ctx: commands.Context, name: str) -> None:
        """Show every setting and server for a status embed."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        emb = (await self.config.guild(ctx.guild).embeds())[name]
        emojis = {**DEFAULT_EMOJIS, **(emb.get("emojis") or {})}
        accent = emb.get("accent_color")
        lines = [
            f"## Status embed `{name}`",
            f"**Location:** {self._jump(ctx.guild, emb)}",
            f"**Accent colour:** {f'#{accent:06X}' if accent is not None else 'none'}",
            f"**Server links:** {'on' if emb.get('show_links', True) else 'off'}",
            "**Emojis:** "
            + ", ".join(f"`{k}` {emojis.get(k) or '*(none)*'}" for k in EMOJI_KEYS),
            "**Header:**",
            box(emb.get("header") or "(none)"),
        ]
        servers = emb.get("servers") or {}
        for game in GAMES:
            sect = (emb.get("sections") or {}).get(game) or {}
            in_game = sorted(
                ((a, c) for a, c in servers.items() if c.get("game") == game),
                key=lambda ac: display_name(ac[0], ac[1], self.api.state(ac[0])[1]).casefold(),
            )
            title = sect.get("title") or f"{GAME_DEFAULTS[game][0]} (default)"
            desc = sect.get("description") or f"{GAME_DEFAULTS[game][1]} (default)"
            lines.append(f"### {game} \N{EM DASH} {title}\n-# {desc}")
            if not in_game:
                lines.append("*No servers \N{EM DASH} section hidden.*")
            for address, cfg in in_game:
                state, data = self.api.state(address)
                if cfg.get("maintenance"):
                    state = "maintenance"
                extras = []
                if cfg.get("label"):
                    extras.append(f"label: {cfg['label']}")
                if cfg.get("note"):
                    extras.append(f"note: {cfg['note']}")
                shown = discord.utils.escape_markdown(display_name(address, cfg, data))
                lines.append(
                    f"- `{address}` **{shown}** \N{EM DASH} {state}"
                    + (f"\n  -# {' | '.join(extras)}" if extras else "")
                )
        lines.append(f"-# {self._api_status()}")
        for page in pagify("\n".join(lines), delims=["\n### ", "\n"], priority=True):
            await ctx.send(page, allowed_mentions=NO_MENTIONS)

    @pluto.command(name="move")
    async def pluto_move(self, ctx: commands.Context, name: str, channel: discord.TextChannel) -> None:
        """Move a status embed to another channel (reposts it there)."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        error = await self._post(ctx.guild, name, channel)
        await ctx.send(f":x: {error}" if error else f"Moved `{name}` to {channel.mention}.")

    @pluto.command(name="repost")
    async def pluto_repost(self, ctx: commands.Context, name: str) -> None:
        """Post the status embed again in its channel (e.g. if it was deleted)."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        emb = (await self.config.guild(ctx.guild).embeds())[name]
        channel = ctx.guild.get_channel_or_thread(emb.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            return await ctx.send(
                f"Its channel no longer exists; use `{ctx.clean_prefix}pluto move {name} <channel>`."
            )
        error = await self._post(ctx.guild, name, channel)
        await ctx.send(f":x: {error}" if error else f"Reposted `{name}` in {channel.mention}.")

    @pluto.command(name="refresh", aliases=["update"])
    async def pluto_refresh(self, ctx: commands.Context, name: Optional[str] = None) -> None:
        """Update one (or every) status embed now.

        Still respects the API cache, so data is never more than ~30s fresher than normal.
        """
        embeds = await self.config.guild(ctx.guild).embeds()
        names = [name.lower()] if name else sorted(embeds)
        if name and not await self._exists(ctx, name):
            return
        if not names:
            return await ctx.send("There are no status embeds to refresh.")
        async with ctx.typing():
            errors = [
                f"`{n}`: {err}"
                for n in names
                if (err := await self._update_embed(ctx.guild, n, fetch=True))
            ]
        if self.api.backoff_remaining:
            errors.append(f"API: {self._api_status()}")
        await ctx.send(
            "Refreshed." if not errors else "Refreshed with problems:\n" + "\n".join(errors),
            allowed_mentions=NO_MENTIONS,
        )

    # --------------------------------------------------------------- servers

    @pluto.command(name="add")
    async def pluto_add(
        self,
        ctx: commands.Context,
        name: str,
        address: AddressConverter,
        game: Optional[GameConverter] = None,
    ) -> None:
        """Add a gameserver (`ip:port`) to a status embed.

        The section (T4/T5/T6) is detected from gameserve.rs automatically; pass it
        explicitly if the server is offline or to override the detection.
        """
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        data, api_error = None, None
        async with ctx.typing():
            try:
                data = await self.api.lookup(address)
            except APIError as exc:
                api_error = str(exc)
        detected = game_from_api(((data or {}).get("game") or {}).get("id"))
        notes = []
        if game is None:
            if detected is None:
                why = (
                    f"gameserve.rs couldn't be reached ({api_error})"
                    if api_error
                    else "gameserve.rs doesn't currently list it"
                    if data is None
                    else f"it reports game `{(data.get('game') or {}).get('id')}`"
                )
                return await ctx.send(
                    f"Couldn't detect the game for `{address}` because {why}.\n"
                    f"Specify it: `{ctx.clean_prefix}pluto add {name} {address} T4|T5|T6`."
                )
            game = detected
        elif detected and detected != game:
            notes.append(f":warning: gameserve.rs reports this as a {detected} server.")
        if data is None and not api_error:
            notes.append(":warning: gameserve.rs doesn't list this server right now; it will show as offline.")
        if data is not None and data.get("platform") not in (None, "plutonium"):
            notes.append(f":warning: gameserve.rs reports platform `{data.get('platform')}`, not Plutonium.")

        async with self.config.guild(ctx.guild).embeds() as embeds:
            servers = embeds[name].setdefault("servers", {})
            if address in servers:
                existing = servers[address]
                if existing.get("game") == game:
                    return await ctx.send(f"`{address}` is already in `{name}` under {game}.")
                existing["game"] = game
                verb = "Moved"
            else:
                if len(servers) >= MAX_SERVERS_PER_EMBED:
                    return await ctx.send(f"`{name}` already has {MAX_SERVERS_PER_EMBED} servers.")
                servers[address] = new_server_config(game)
                verb = "Added"
            live = clean_name((data or {}).get("name"))
            if live:
                servers[address]["last_name"] = live
        shown = discord.utils.escape_markdown(live or address)
        await self._apply(
            ctx, name, "\n".join([f"{verb} **{shown}** (`{address}`) to `{name}` under {game}.", *notes])
        )

    @pluto.command(name="remove", aliases=["rm"])
    async def pluto_remove(self, ctx: commands.Context, name: str, address: AddressConverter) -> None:
        """Remove a gameserver from a status embed."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        async with self.config.guild(ctx.guild).embeds() as embeds:
            if embeds[name].get("servers", {}).pop(address, None) is None:
                return await ctx.send(f"`{address}` isn't in `{name}`.")
        await self._apply(ctx, name, f"Removed `{address}` from `{name}`.")

    async def _set_server_field(
        self, ctx: commands.Context, name: str, address: str, field: str, value: Optional[str]
    ) -> None:
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        value = (value or "").strip()
        if self._too_long(field, value):
            return await ctx.send(f"That {field} is too long (max {LIMITS[field]} characters).")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            cfg = embeds[name].get("servers", {}).get(address)
            if cfg is None:
                return await ctx.send(f"`{address}` isn't in `{name}`.")
            cfg[field] = value
        await self._apply(
            ctx, name, f"{'Set' if value else 'Cleared'} the {field} for `{address}` in `{name}`."
        )

    @pluto.command(name="label", aliases=["rename"])
    async def pluto_label(
        self, ctx: commands.Context, name: str, address: AddressConverter, *, label: Optional[str] = None
    ) -> None:
        """Override a server's displayed name. Leave empty to use its live name again."""
        await self._set_server_field(ctx, name, address, "label", label)

    @pluto.command(name="note")
    async def pluto_note(
        self, ctx: commands.Context, name: str, address: AddressConverter, *, note: Optional[str] = None
    ) -> None:
        """Add a small note under a server (e.g. its map rotation). Leave empty to clear."""
        await self._set_server_field(ctx, name, address, "note", note)

    @pluto.command(name="maintenance", aliases=["maint"])
    async def pluto_maintenance(
        self, ctx: commands.Context, name: str, address: str, enabled: Optional[bool] = None
    ) -> None:
        """Mark a server (or `all`) as under maintenance. Toggles if on/off is omitted.

        Maintenance overrides the live status with the maintenance emoji.
        """
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        if address.lower() == "all":
            targets = None
        else:
            targets = normalize_address(address)
            if targets is None:
                return await ctx.send(f"`{address}` is not a valid `ip:port` address or `all`.")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            servers = embeds[name].get("servers", {})
            if targets is not None and targets not in servers:
                return await ctx.send(f"`{targets}` isn't in `{name}`.")
            chosen = list(servers) if targets is None else [targets]
            if not chosen:
                return await ctx.send(f"`{name}` has no servers.")
            if enabled is None:
                enabled = not all(servers[a].get("maintenance") for a in chosen)
            for a in chosen:
                servers[a]["maintenance"] = enabled
        what = "all servers" if targets is None else f"`{targets}`"
        await self._apply(
            ctx, name, f"Maintenance {'enabled' if enabled else 'disabled'} for {what} in `{name}`."
        )

    # ------------------------------------------------------------ appearance

    @pluto.command(name="header", aliases=["message"])
    async def pluto_header(self, ctx: commands.Context, name: str, *, text: Optional[str] = None) -> None:
        """Set the message shown at the top of the embed (markdown). Leave empty to remove."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        text = (text or "").strip()
        if self._too_long("header", text):
            return await ctx.send(f"Header too long (max {LIMITS['header']} characters).")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            embeds[name]["header"] = text
        await self._apply(ctx, name, f"{'Set' if text else 'Removed'} the header for `{name}`.")

    @pluto.command(name="section")
    async def pluto_section(
        self,
        ctx: commands.Context,
        name: str,
        game: GameConverter,
        field: str,
        *,
        text: Optional[str] = None,
    ) -> None:
        """Customise a section's `title` or `description`. Leave text empty for the default.

        Examples:
        `[p]pluto section main T5 title Black Ops Zombies`
        `[p]pluto section main T5 description Our BO1 servers`
        """
        name = name.lower()
        field = {"title": "title", "desc": "description", "description": "description"}.get(field.lower())
        if field is None:
            return await ctx.send("Field must be `title` or `description`.")
        if not await self._exists(ctx, name):
            return
        text = (text or "").strip()
        if self._too_long(field, text):
            return await ctx.send(f"That {field} is too long (max {LIMITS[field]} characters).")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            sections = embeds[name].setdefault("sections", {})
            sections.setdefault(game, {})[field] = text
        await self._apply(
            ctx, name, f"{'Set' if text else 'Reset'} the {game} section {field} for `{name}`."
        )

    @pluto.command(name="emoji")
    async def pluto_emoji(
        self, ctx: commands.Context, name: str, kind: str, emoji: Optional[str] = None
    ) -> None:
        """Set an emoji used by the embed. Leave emoji empty to reset to default.

        Kinds:
        `t4`, `t5`, `t6` - shown next to each section title
        `online` - server is up
        `full` - server is up and full (falls back to `online` if unset)
        `offline` - server is down / not listed
        `maintenance` - server is marked under maintenance
        Use `none` as the emoji to show nothing.
        """
        name = name.lower()
        kind = kind.lower()
        if kind not in EMOJI_KEYS:
            return await ctx.send(f"Kind must be one of: {', '.join(f'`{k}`' for k in EMOJI_KEYS)}.")
        if not await self._exists(ctx, name):
            return
        warning = ""
        if emoji is None:
            value = DEFAULT_EMOJIS[kind]
        elif emoji.lower() == "none":
            value = ""
        else:
            value = emoji.strip()
            if len(value) > 64 or (" " in value):
                return await ctx.send("That doesn't look like a single emoji.")
            match = CUSTOM_EMOJI_RE.match(value)
            if match and self.bot.get_emoji(int(match[1])) is None:
                warning = "\n:warning: I can't see that custom emoji; it may not render. Use one from a server I'm in or an application emoji."
            elif not match and value.isascii():
                return await ctx.send("That doesn't look like an emoji.")
        async with self.config.guild(ctx.guild).embeds() as embeds:
            embeds[name].setdefault("emojis", dict(DEFAULT_EMOJIS))[kind] = value
        shown = value or "*(none)*"
        await self._apply(ctx, name, f"Set the `{kind}` emoji for `{name}` to {shown}.{warning}")

    @pluto.command(name="color", aliases=["colour", "accent"])
    async def pluto_color(
        self, ctx: commands.Context, name: str, color: Optional[discord.Color] = None
    ) -> None:
        """Set the embed's accent colour (e.g. `#5865F2`). Leave empty to remove it."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        async with self.config.guild(ctx.guild).embeds() as embeds:
            embeds[name]["accent_color"] = color.value if color is not None else None
        await self._apply(
            ctx, name, f"{'Set' if color is not None else 'Removed'} the accent colour for `{name}`."
        )

    @pluto.command(name="links")
    async def pluto_links(self, ctx: commands.Context, name: str, enabled: Optional[bool] = None) -> None:
        """Toggle linking each server name to its gameserve.rs page."""
        name = name.lower()
        if not await self._exists(ctx, name):
            return
        async with self.config.guild(ctx.guild).embeds() as embeds:
            current = embeds[name].get("show_links", True)
            embeds[name]["show_links"] = (not current) if enabled is None else enabled
            enabled = embeds[name]["show_links"]
        await self._apply(ctx, name, f"Server links {'enabled' if enabled else 'disabled'} for `{name}`.")

    # ----------------------------------------------------------------- misc

    @pluto.command(name="lookup", aliases=["check"])
    async def pluto_lookup(self, ctx: commands.Context, address: AddressConverter) -> None:
        """Look up a single server on gameserve.rs without adding it."""
        async with ctx.typing():
            try:
                data = await self.api.lookup(address)
            except APIError as exc:
                return await ctx.send(f":x: gameserve.rs request failed: {exc}")
        if data is None:
            return await ctx.send(f"gameserve.rs doesn't currently list `{address}` (offline or unknown).")
        players = data.get("players") or {}
        game = data.get("game") or {}
        lines = [
            f"**{discord.utils.escape_markdown(clean_name(data.get('name')) or address)}**",
            f"Address: `{address}` \N{MIDDLE DOT} [gameserve.rs page](<{server_url(address, data.get('id'))}>)",
            f"Game: {game.get('name', '?')} (`{game.get('id', '?')}`) \N{MIDDLE DOT} platform `{data.get('platform', '?')}`",
            f"Map: {(data.get('map') or {}).get('display', '?')} \N{MIDDLE DOT} Mode: {(data.get('mode') or {}).get('display', '?')}",
            f"Players: {players.get('current', '?')}/{players.get('max', '?')}",
        ]
        if data.get("isZombies") and data.get("zombiesRound"):
            lines.append(f"Round: {data['zombiesRound']}")
        await ctx.send("\n".join(lines), allowed_mentions=NO_MENTIONS)

    @pluto.command(name="interval")
    @commands.is_owner()
    async def pluto_interval(self, ctx: commands.Context, seconds: Optional[int] = None) -> None:
        """(Owner) Show or set how often every status embed refreshes (60-3600s)."""
        if seconds is None:
            current = await self.config.interval()
            return await ctx.send(f"Status embeds refresh every {current} seconds.\n-# {self._api_status()}")
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            return await ctx.send(f"Interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds.")
        await self.config.interval.set(seconds)
        await ctx.send(f"Status embeds will refresh every {seconds} seconds (from the next cycle).")
