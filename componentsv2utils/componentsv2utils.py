"""
ComponentsV2Utils - send, store, download and export Discord Components V2
layouts from YAML or JSON.

Layouts are normalized to Discord's raw payload and validated by builder.py
before anything is sent.
"""

from __future__ import annotations

import io
import json
import re
import typing

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify, text_to_file

from . import builder
from .builder import BuildError, Layout

__version__ = "2.0.0"

LAYOUT_EXTENSIONS = ("yaml", "yml", "json", "txt")
MAX_STORED = 100
MAX_NAME_LENGTH = 100
EMBED_DEBUGGER = "https://discord.com/developers/embeds"
WEBHOOK_NAME = "ComponentsV2Utils"

# ---------------------------------------------------------------------------
# Schema reference
# ---------------------------------------------------------------------------
SCHEMA_TYPES: typing.Dict[str, str] = {
    "text": """**`text`** - markdown text (headings, lists, links, mentions, emoji, `-#` subtext)
```yaml
- type: text
  content: |
    ## Hello
    This is **bold** and *italic*.
- "A plain string is shorthand for a text component."
```""",
    "separator": """**`separator`** - spacing between components, with or without a line
```yaml
- type: separator
  divider: true     # show the line (default true; `visible` also works)
  spacing: small    # small | large (default small)
```""",
    "section": """**`section`** - 1 to 3 texts beside an accessory (Discord requires the accessory)
```yaml
- type: section
  components:
    - "## Rules"
    - "Please read them carefully."
  accessory:
    type: thumbnail            # or: type: button, label, url, emoji
    url: https://example.com/icon.png
    description: Alt text      # optional
    spoiler: false             # optional
# Shorthands:  thumbnail: https://...   or   button: {label: Open, url: https://...}
# `title:` / `description:` still work in place of `components:`.
```""",
    "button": """**`button`** - link button, used in a `row` or as a section `accessory`
```yaml
label: Website          # up to 80 characters
url: https://example.com
emoji: "🔥"             # optional; custom: "<:name:123456789012345678>"
disabled: false         # optional
```
Only link buttons are supported; other styles need bot code to respond to clicks.""",
    "row": """**`row`** - up to 5 link buttons side by side
```yaml
- type: row
  buttons:
    - {label: Store, url: https://example.com/store}
    - {label: Wiki, url: https://example.com/wiki, emoji: "📖"}
```""",
    "gallery": """**`gallery`** - 1 to 10 images/videos
```yaml
- type: gallery
  items:
    - https://example.com/one.png
    - url: https://example.com/two.gif
      description: Alt text
      spoiler: true
    - attachment://three.png     # a file attached to your command
```""",
    "file": """**`file`** - shows a file attached to your command message as a download
```yaml
- type: file
  file: rules.pdf       # name of a file attached to the command
  spoiler: false
```""",
    "container": """**`container`** - groups components inside a box with an optional coloured bar
```yaml
- type: container
  accent_color: "#5865F2"   # hex, decimal number, or name (blurple, red, ...)
  spoiler: false            # blur the whole container
  components:
    - type: text
      content: Inside the container
```
Containers cannot be nested.""",
}

SCHEMA_OVERVIEW = """# Components V2 layout reference

A layout is a mapping with a `components` list. Write it in YAML or JSON.

```yaml
accent_color: "#5865F2"   # optional: wrap everything in one coloured container
spoiler: false            # optional: blur that container
username: Announcements   # optional: post through a webhook with this name...
avatar_url: https://example.com/logo.png   # ...and/or this avatar
components:
  - type: text
    content: "# Announcement"
```

Every component can also take an `id` (a unique whole number). Raw Discord JSON
(numeric `type` values, e.g. from a message payload or a component-embed file)
is accepted as-is.

## Posting as a webhook
If `username` or `avatar_url` is set, the layout is posted through a webhook in
the target channel so it shows that name/avatar instead of the bot's. The bot
creates and reuses its own webhook. You and the bot both need Manage Webhooks.
Messages posted this way can still be edited with a message link, but the name
and avatar can't be changed afterwards.

## Images and files from your command message
Attach files to the command and refer to them as `attachment://filename` in a
`gallery`, a `thumbnail`, or a `file` component. They are uploaded with the message.
Stored layouts don't keep files; attach them again when you `post`.

## Limits
- 40 components per message, counting every nested one
- 4000 characters of text across the whole message
- Sections: 1-3 texts plus one accessory. Rows: 1-5 buttons. Galleries: 1-10 items.
- Containers cannot contain other containers.

## Components
"""

FULL_EXAMPLE = """
## Full example
```yaml
components:
  - type: container
    accent_color: blurple
    components:
      - type: section
        components:
          - "# Server update"
          - "New channels and roles are live."
        thumbnail: https://example.com/icon.png
      - type: separator
      - type: gallery
        items:
          - https://example.com/banner.png
      - type: row
        buttons:
          - {label: Read more, url: https://example.com/news, emoji: "📰"}
  - type: text
    content: "-# Posted by the mod team"
```
"""


def schema_text() -> str:
    return SCHEMA_OVERVIEW + "\n\n".join(SCHEMA_TYPES.values()) + "\n" + FULL_EXAMPLE


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------
class ChannelOrMessageConverter(commands.Converter):
    """A messageable channel in this server, or a message link/ID in this server."""

    async def convert(
        self, ctx: commands.Context, argument: str
    ) -> typing.Union[discord.abc.GuildChannel, discord.Thread, discord.Message]:
        try:
            message = await commands.MessageConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        else:
            if message.guild is None or message.guild.id != ctx.guild.id:
                raise commands.BadArgument("That message is not in this server.")
            return message
        for converter in (commands.GuildChannelConverter(), commands.ThreadConverter()):
            try:
                channel = await converter.convert(ctx, argument)
            except commands.BadArgument:
                continue
            if isinstance(channel, discord.abc.Messageable):
                return channel
        raise commands.BadArgument(f"`{argument}` is not a channel or a message link in this server.")


class MessageInGuildConverter(commands.MessageConverter):
    async def convert(self, ctx: commands.Context, argument: str) -> discord.Message:
        message = await super().convert(ctx, argument)
        if message.guild is None or message.guild.id != ctx.guild.id:
            raise commands.BadArgument("That message is not in this server.")
        return message


TargetType = typing.Union[discord.abc.Messageable, discord.Message, None]


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class ComponentsV2Utils(commands.Cog):
    """Send, store and export Discord Components V2 layout messages from YAML or JSON.

    Supports text, separators, sections (thumbnail/button accessories), media
    galleries, files, link-button rows and containers.
    """

    __version__ = __version__

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config: Config = Config.get_conf(self, identifier=847392016574839201, force_registration=True)
        self.config.register_guild(stored={})

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Stored layouts keep the author's ID; drop it. The layouts stay."""
        for guild_id, data in (await self.config.all_guilds()).items():
            stored = data.get("stored", {})
            if not any(meta.get("author") == user_id for meta in stored.values()):
                continue
            async with self.config.guild_from_id(guild_id).stored() as stored_conf:
                for meta in stored_conf.values():
                    if meta.get("author") == user_id:
                        meta["author"] = None

    # -- input -------------------------------------------------------------

    @staticmethod
    def _layout_attachment(ctx: commands.Context) -> typing.Optional[discord.Attachment]:
        for attachment in ctx.message.attachments:
            if attachment.filename.rsplit(".", 1)[-1].lower() in LAYOUT_EXTENSIONS:
                return attachment
        return None

    async def _read_source(self, ctx: commands.Context, data: typing.Optional[str]) -> typing.Tuple[typing.Any, typing.Optional[discord.Attachment]]:
        """Parse inline data, or the first .yaml/.yml/.json/.txt attachment.
        Returns the parsed data and the attachment it came from (if any)."""
        if data:
            return builder.parse_any(data), None
        attachment = self._layout_attachment(ctx)
        if attachment is None:
            raise BuildError("Provide the layout inline, or attach a `.yaml`, `.yml` or `.json` file.")
        try:
            raw = (await attachment.read()).decode("utf-8")
        except UnicodeDecodeError:
            raise BuildError(f"`{attachment.filename}` is not UTF-8 text.")
        except discord.HTTPException as exc:
            raise BuildError(f"Could not download `{attachment.filename}`: {exc}")
        ext = attachment.filename.rsplit(".", 1)[-1].lower()
        parsed = builder.parse_json(raw) if ext == "json" else builder.parse_yaml(raw)
        return parsed, attachment

    @staticmethod
    async def _collect_files(
        ctx: commands.Context, layout: Layout, exclude: typing.Optional[discord.Attachment]
    ) -> typing.List[discord.File]:
        """Re-upload the command attachments the layout references via attachment://."""
        if not layout.attachments:
            return []
        available = {a.filename: a for a in ctx.message.attachments if a != exclude}
        missing = [name for name in layout.attachments if name not in available]
        if missing:
            have = ", ".join(f"`{n}`" for n in available) or "none"
            raise BuildError(
                "The layout uses " + ", ".join(f"`attachment://{n}`" for n in missing)
                + f" but no file with that name is attached to your command (attached: {have}). "
                "Discord replaces spaces in filenames with underscores."
            )
        files = []
        for name in layout.attachments:
            try:
                files.append(await available[name].to_file())
            except discord.HTTPException as exc:
                raise BuildError(f"Could not download attachment `{name}`: {exc}")
        return files

    # -- permission checks ------------------------------------------------

    async def _check_target(self, ctx: commands.Context, target: TargetType, layout: Layout) -> None:
        """The command author must be allowed to post where the bot is posting,
        so the bot can't be used to reach channels the author can't."""
        target = target or ctx.channel
        me = ctx.guild.me
        is_owner = await self.bot.is_owner(ctx.author)
        if isinstance(target, discord.Message):
            channel = target.channel
            if target.webhook_id is not None:
                # Ownership of the webhook is checked when it is looked up.
                parent = _webhook_parent(channel)
                if parent is None or not parent.permissions_for(me).manage_webhooks:
                    raise BuildError(f"I need Manage Webhooks in {channel.mention} to edit webhook messages there.")
            elif target.author.id != self.bot.user.id:
                raise BuildError("I can only edit messages I sent, or that my webhooks sent.")
            author_perms = channel.permissions_for(ctx.author)
            if not (author_perms.view_channel and author_perms.manage_messages) and not is_owner:
                raise BuildError(f"You need Manage Messages in {channel.mention} to edit messages there.")
            if layout.attachments and not channel.permissions_for(me).attach_files:
                raise BuildError(f"I need Attach Files in {channel.mention} to upload the attachments.")
            return
        channel = target
        author_perms = channel.permissions_for(ctx.author)
        send = author_perms.send_messages_in_threads if isinstance(channel, discord.Thread) else author_perms.send_messages
        if not (author_perms.view_channel and send) and not is_owner:
            raise BuildError(f"You can't send messages in {channel.mention}.")
        bot_perms = channel.permissions_for(me)
        if layout.uses_webhook:
            # A custom name/avatar can impersonate people, so it takes the
            # same permission as creating a webhook by hand.
            if not author_perms.manage_webhooks and not is_owner:
                raise BuildError(f"You need Manage Webhooks in {channel.mention} to post with a custom `username`/`avatar_url`.")
            if _webhook_parent(channel) is None:
                raise BuildError(f"{channel.mention} doesn't support webhooks.")
            if not bot_perms.manage_webhooks:
                raise BuildError(f"I need Manage Webhooks in {channel.mention} to post with a custom `username`/`avatar_url`.")
        else:
            bot_send = bot_perms.send_messages_in_threads if isinstance(channel, discord.Thread) else bot_perms.send_messages
            if not (bot_perms.view_channel and bot_send):
                raise BuildError(f"I can't send messages in {channel.mention}.")
            if layout.attachments and not bot_perms.attach_files:
                raise BuildError(f"I need Attach Files in {channel.mention} to upload the attachments.")

    @staticmethod
    def _can_read(ctx: commands.Context, message: discord.Message) -> bool:
        perms = message.channel.permissions_for(ctx.author)
        return perms.view_channel and perms.read_message_history

    # -- output -----------------------------------------------------------

    async def _deliver(
        self,
        ctx: commands.Context,
        layout: Layout,
        target: TargetType,
        source_attachment: typing.Optional[discord.Attachment] = None,
    ) -> None:
        """Validate permissions, build the view and send/edit it. Raises BuildError.
        Extra warnings are appended to ``layout.warnings``."""
        await self._check_target(ctx, target, layout)
        files = await self._collect_files(ctx, layout, source_attachment)
        view = builder.build_view(layout)
        try:
            if isinstance(target, discord.Message):
                kwargs: typing.Dict[str, typing.Any] = {"view": view}
                if not target.flags.components_v2:
                    # Discord won't add the V2 flag while content/embeds remain.
                    kwargs.update(content=None, embeds=[])
                if files:
                    kwargs["attachments"] = files
                if layout.uses_webhook:
                    layout.warnings.append("`username`/`avatar_url` can't be changed on an existing message and were ignored.")
                if target.webhook_id is not None:
                    await self._edit_webhook_message(target, kwargs)
                else:
                    await target.edit(**kwargs)
            elif layout.uses_webhook:
                await self._send_webhook(ctx, target or ctx.channel, layout, view, files)
            else:
                channel = target or ctx.channel
                await channel.send(view=view, files=files or None)
        except discord.HTTPException as exc:
            raise BuildError(f"Discord rejected the message:\n{box(str(exc)[:1800])}")
        if (target is not None and target != ctx.channel) or layout.uses_webhook:
            await ctx.tick()

    # -- webhooks ---------------------------------------------------------

    async def _own_webhooks(self, parent: typing.Any) -> typing.List[discord.Webhook]:
        try:
            hooks = await parent.webhooks()
        except discord.Forbidden:
            raise BuildError(f"I need Manage Webhooks in {parent.mention}.")
        return [h for h in hooks if h.user is not None and h.user.id == self.bot.user.id and h.token]

    async def _get_webhook(self, parent: typing.Any) -> discord.Webhook:
        """Reuse a webhook this bot owns in the channel, creating one if needed.
        Looked up fresh every time so a deleted webhook is never used."""
        hooks = await self._own_webhooks(parent)
        if hooks:
            return next((h for h in hooks if h.name == WEBHOOK_NAME), hooks[0])
        try:
            return await parent.create_webhook(name=WEBHOOK_NAME, reason="ComponentsV2Utils webhook posting")
        except discord.HTTPException as exc:
            raise BuildError(f"Could not create a webhook in {parent.mention}: {exc}")

    async def _send_webhook(
        self,
        ctx: commands.Context,
        channel: typing.Any,
        layout: Layout,
        view: discord.ui.LayoutView,
        files: typing.List[discord.File],
    ) -> None:
        parent = _webhook_parent(channel)
        me = ctx.guild.me
        kwargs: typing.Dict[str, typing.Any] = {"view": view, "wait": True}
        username = layout.username or me.display_name
        if "discord" not in username.lower() and "clyde" not in username.lower():
            kwargs["username"] = username
        kwargs["avatar_url"] = layout.avatar_url or me.display_avatar.url
        if files:
            kwargs["files"] = files
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        hook = await self._get_webhook(parent)
        await hook.send(**kwargs)

    async def _edit_webhook_message(self, message: discord.Message, kwargs: typing.Dict[str, typing.Any]) -> None:
        parent = _webhook_parent(message.channel)
        hook = next((h for h in await self._own_webhooks(parent) if h.id == message.webhook_id), None)
        if hook is None:
            raise BuildError("That message was sent by a webhook I don't own, so I can't edit it.")
        if isinstance(message.channel, discord.Thread):
            kwargs["thread"] = message.channel
        await hook.edit_message(message.id, **kwargs)

    async def _report_warnings(self, ctx: commands.Context, warnings: typing.List[str]) -> None:
        if warnings:
            text = "\n".join(f"- {w}" for w in warnings)
            for page in pagify(f":warning: **Warnings:**\n{text}", page_length=1900):
                await ctx.send(page)

    async def _run(self, ctx: commands.Context, coro: typing.Awaitable) -> None:
        try:
            await coro
        except BuildError as exc:
            for page in pagify(f":x: {exc}", page_length=1900, shorten_by=0):
                await ctx.send(page)

    async def _send_layout(self, ctx: commands.Context, target: TargetType, data: typing.Optional[str]) -> None:
        parsed, source = await self._read_source(ctx, data)
        layout = builder.normalize(parsed)
        await self._deliver(ctx, layout, target, source)
        await self._report_warnings(ctx, layout.warnings)

    # -- commands ---------------------------------------------------------

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True)
    @commands.group(name="cv2", aliases=["componentsv2"], invoke_without_command=True)
    async def cv2(self, ctx: commands.Context) -> None:
        """Send and manage Components V2 layout messages.

        Layouts are written in YAML (or JSON). See `[p]cv2 schema` for every
        component and option.

        Any command that sends can take a channel, or a link to one of my
        messages to edit it instead, as its first argument.

        Add `username:` and/or `avatar_url:` to a layout to post it through a
        webhook with a custom name and avatar.
        """
        await ctx.send_help()

    @cv2.command(name="schema", aliases=["reference", "docs"])
    async def cv2_schema(self, ctx: commands.Context, component_type: typing.Optional[str] = None) -> None:
        """Show the layout reference, or the reference for one component type.

        Types: text, separator, section, button, row, gallery, file, container.
        """
        if component_type:
            key = component_type.lower()
            key = builder.TYPE_NAMES.get(builder.TYPE_ALIASES.get(key, -1), key)
            if key not in SCHEMA_TYPES:
                return await ctx.send(f":x: Unknown type. Choose from: {', '.join(f'`{k}`' for k in SCHEMA_TYPES)}.")
            return await ctx.send(SCHEMA_TYPES[key])
        await ctx.send(
            "Full reference attached. Use `{p}cv2 schema <type>` for a single component.".format(p=ctx.clean_prefix),
            file=text_to_file(schema_text(), filename="cv2_schema.md"),
        )

    @cv2.command(name="yaml", aliases=["fromyaml"])
    async def cv2_yaml(
        self, ctx: commands.Context, channel_or_message: typing.Optional[ChannelOrMessageConverter] = None, *, data: str
    ) -> None:
        """Send a layout from inline YAML (code blocks are fine).

        Example:
        [p]cv2 yaml
        components:
          - "## Hello World"
          - type: separator
          - "This is a Components V2 message!"
        """
        await self._run(ctx, self._send_layout(ctx, channel_or_message, data))

    @cv2.command(name="json", aliases=["fromjson"])
    async def cv2_json(
        self, ctx: commands.Context, channel_or_message: typing.Optional[ChannelOrMessageConverter] = None, *, data: str
    ) -> None:
        """Send a layout from inline JSON. Raw Discord payloads work too.

        Example: [p]cv2 json {"components": [{"type": "text", "content": "Hello!"}]}
        """

        async def run() -> None:
            layout = builder.normalize(builder.parse_json(data))
            await self._deliver(ctx, layout, channel_or_message)
            await self._report_warnings(ctx, layout.warnings)

        await self._run(ctx, run())

    @cv2.command(name="file", aliases=["fromfile", "yamlfile", "fromyamlfile", "jsonfile", "fromjsonfile"])
    async def cv2_file(
        self, ctx: commands.Context, channel_or_message: typing.Optional[ChannelOrMessageConverter] = None
    ) -> None:
        """Send a layout from an attached .yaml, .yml or .json file.

        Other attached files can be used by the layout as `attachment://filename`.
        """
        await self._run(ctx, self._send_layout(ctx, channel_or_message, None))

    @cv2.command(name="message", aliases=["frommessage", "copy"])
    async def cv2_message(
        self,
        ctx: commands.Context,
        message: MessageInGuildConverter,
        channel_or_message: typing.Optional[ChannelOrMessageConverter] = None,
    ) -> None:
        """Copy an existing message as a Components V2 layout.

        Works with Components V2 messages, and converts regular messages:
        content becomes text and embeds become containers.
        """

        async def run() -> None:
            if not self._can_read(ctx, message):
                raise BuildError("You can't read that message's channel.")
            data, warnings = builder.message_to_friendly(message)
            if not data["components"]:
                raise BuildError("That message has nothing I can turn into a layout.")
            layout = builder.normalize(data)
            await self._deliver(ctx, layout, channel_or_message)
            await self._report_warnings(ctx, warnings + layout.warnings)

        await self._run(ctx, run())

    @cv2.command(name="download", aliases=["export"])
    async def cv2_download(self, ctx: commands.Context, *, message_or_name: str) -> None:
        """Download a stored layout, or any message, as editable YAML.

        Give a stored layout's name or a message link. Regular messages are
        converted (embeds become containers), so this also migrates embeds.
        """

        async def run() -> None:
            stored = await self.config.guild(ctx.guild).stored()
            if message_or_name in stored:
                text = builder.dump_yaml(stored[message_or_name]["data"])
                return await ctx.send(
                    f"YAML for stored layout `{message_or_name}`:",
                    file=text_to_file(text, filename=f"{_safe_filename(message_or_name)}.yaml"),
                )
            try:
                message = await MessageInGuildConverter().convert(ctx, message_or_name)
            except commands.BadArgument:
                raise BuildError(f"`{message_or_name}` is not a stored layout or a message link in this server.")
            if not self._can_read(ctx, message):
                raise BuildError("You can't read that message's channel.")
            data, warnings = builder.message_to_friendly(message)
            if not data["components"]:
                raise BuildError("That message has nothing I can turn into a layout.")
            text = f"# Source: {message.jump_url}\n" + builder.dump_yaml(data)
            await ctx.send(
                "Here is the layout:", file=text_to_file(text, filename=f"message-{message.id}.yaml")
            )
            await self._report_warnings(ctx, warnings)

        await self._run(ctx, run())

    @cv2.command(name="payload", aliases=["debug", "validate", "check"])
    async def cv2_payload(self, ctx: commands.Context, *, data: typing.Optional[str] = None) -> None:
        """Validate a layout and show the raw JSON payload sent to Discord.

        Give the layout inline, attach a file, or give a stored layout's name.
        Nothing is sent.
        """

        async def run() -> None:
            layout, _ = await self._layout_from_any(ctx, data)
            text = json.dumps(layout.components, indent=2, ensure_ascii=False)
            stats = (
                f":white_check_mark: Valid. **{layout.component_count}/{builder.MAX_COMPONENTS}** components, "
                f"**{layout.text_length}/{builder.MAX_TEXT_LENGTH}** text characters"
            )
            if layout.attachments:
                stats += ", attachments: " + ", ".join(f"`{n}`" for n in layout.attachments)
            await ctx.send(stats + ".", file=text_to_file(text, filename="payload.json"))
            await self._report_warnings(ctx, layout.warnings)

        await self._run(ctx, run())

    @cv2.command(name="webembed", aliases=["linkpreview", "componentembed"])
    async def cv2_webembed(self, ctx: commands.Context, *, data: typing.Optional[str] = None) -> None:
        """Export a layout as a website link preview (Discord "component embed").

        When someone posts a link to your website in Discord, Discord shows
        this layout as the preview. The bot can't send these; you host the
        output on your site. Give the layout inline, attach a file, or give a
        stored layout's name.

        Link previews are stricter: one container, link buttons only, public
        https images (no attachments or file components), at most 10 gallery
        items in total, and a 3000-byte payload.
        """

        async def run() -> None:
            layout, name = await self._layout_from_any(ctx, data)
            embed = builder.to_web_embed(layout)
            base = _safe_filename(name) if name else "component-embed"
            files = [
                discord.File(io.BytesIO(builder.web_embed_html(embed).encode("utf-8")), filename=f"{base}.html"),
                discord.File(io.BytesIO(embed.json_text.encode("utf-8")), filename="component-embed.json"),
            ]
            await ctx.send(
                f"Link preview payload: **{embed.inline_bytes}/{builder.WEBEMBED_MAX_BYTES}** bytes inline, "
                f"**{embed.json_bytes}/{builder.WEBEMBED_MAX_BYTES}** as a linked file.\n"
                "Paste the HTML snippet into your page's `<head>` (or host the JSON file and link it), "
                f"then check it with the Embed Debugger: <{EMBED_DEBUGGER}>",
                files=files,
            )
            await self._report_warnings(ctx, layout.warnings)

        await self._run(ctx, run())

    async def _layout_from_any(self, ctx: commands.Context, data: typing.Optional[str]) -> typing.Tuple[Layout, typing.Optional[str]]:
        """A stored layout name, inline data, or an attached file."""
        if data:
            stored = await self.config.guild(ctx.guild).stored()
            if data in stored:
                return builder.normalize(stored[data]["data"]), data
        parsed, _ = await self._read_source(ctx, data)
        return builder.normalize(parsed), None

    # -- stored layouts -----------------------------------------------------

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="store", aliases=["save"])
    async def cv2_store(self, ctx: commands.Context, name: str, *, data: typing.Optional[str] = None) -> None:
        """Store a layout by name (inline YAML/JSON, or attach a file).

        Put the name in quotes if it contains spaces. Storing under an existing
        name replaces it.

        Example:
        [p]cv2 store announcement
        components:
          - "## Big news!"
        """

        async def run() -> None:
            if len(name) > MAX_NAME_LENGTH:
                raise BuildError(f"Names can be at most {MAX_NAME_LENGTH} characters.")
            parsed, _ = await self._read_source(ctx, data)
            layout = builder.normalize(parsed)
            async with self.config.guild(ctx.guild).stored() as stored:
                if len(stored) >= MAX_STORED and name not in stored:
                    raise BuildError(f"This server has reached the {MAX_STORED}-layout limit. Remove one with `{ctx.clean_prefix}cv2 unstore` first.")
                replaced = name in stored
                stored[name] = {
                    "author": ctx.author.id,
                    "data": parsed,
                    "uses": stored.get(name, {}).get("uses", 0),
                }
            msg = f":white_check_mark: {'Replaced' if replaced else 'Stored'} layout `{name}`."
            if layout.attachments:
                msg += (
                    "\nIt uses " + ", ".join(f"`{n}`" for n in layout.attachments)
                    + ". Files aren't stored: attach them again each time you post it."
                )
            await ctx.send(msg)
            await self._report_warnings(ctx, layout.warnings)

        await self._run(ctx, run())

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="unstore", aliases=["delete", "remove"])
    async def cv2_unstore(self, ctx: commands.Context, *, name: str) -> None:
        """Remove a stored layout."""
        async with self.config.guild(ctx.guild).stored() as stored:
            if name not in stored:
                return await ctx.send(f":x: No stored layout named `{name}`.")
            del stored[name]
        await ctx.send(f":white_check_mark: Removed stored layout `{name}`.")

    @cv2.command(name="list", aliases=["stored"])
    async def cv2_list(self, ctx: commands.Context) -> None:
        """List this server's stored layouts."""
        stored = await self.config.guild(ctx.guild).stored()
        if not stored:
            return await ctx.send("No layouts stored for this server yet.")
        lines = "\n".join(
            f"- `{discord.utils.escape_markdown(name)}` - used {meta.get('uses', 0)}x"
            for name, meta in sorted(stored.items())
        )
        header = f"## Stored layouts ({len(stored)}/{MAX_STORED})\n"
        if len(header) + len(lines) <= builder.MAX_TEXT_LENGTH:
            view = discord.ui.LayoutView()
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(header + lines), accent_color=discord.Color.blurple()))
            return await ctx.send(view=view)
        for page in pagify(header + lines, delims=["\n"], page_length=1900):
            await ctx.send(page)

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="info")
    async def cv2_info(self, ctx: commands.Context, *, name: str) -> None:
        """Show details about a stored layout."""
        stored = await self.config.guild(ctx.guild).stored()
        if name not in stored:
            return await ctx.send(f":x: No stored layout named `{name}`.")
        meta = stored[name]
        try:
            layout = builder.normalize(meta.get("data"))
            details = (
                f"**Components:** {layout.component_count}/{builder.MAX_COMPONENTS}\n"
                f"**Text characters:** {layout.text_length}/{builder.MAX_TEXT_LENGTH}"
            )
            if layout.uses_webhook:
                details += f"\n**Posts as webhook:** {discord.utils.escape_markdown(layout.username or '(bot name)')}"
            if layout.attachments:
                details += "\n**Needs attachments:** " + ", ".join(f"`{n}`" for n in layout.attachments)
        except BuildError as exc:
            details = f"**Invalid:** {exc}"
        author = f"<@{meta['author']}>" if meta.get("author") else "Unknown"
        view = discord.ui.LayoutView()
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## Layout `{discord.utils.escape_markdown(name)}`\n"
                    f"**Author:** {author}\n**Uses:** {meta.get('uses', 0)}\n{details}"
                ),
                accent_color=discord.Color.blurple(),
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cv2.command(name="post", aliases=["send"])
    async def cv2_post(
        self, ctx: commands.Context, channel_or_message: typing.Optional[ChannelOrMessageConverter] = None, *, name: str
    ) -> None:
        """Post a stored layout, optionally to another channel or over one of my messages."""

        async def run() -> None:
            stored = await self.config.guild(ctx.guild).stored()
            if name not in stored:
                raise BuildError(f"No stored layout named `{name}`.")
            try:
                layout = builder.normalize(stored[name]["data"])
            except BuildError as exc:
                raise BuildError(f"Stored layout `{name}` is invalid: {exc}")
            await self._deliver(ctx, layout, channel_or_message)
            async with self.config.guild(ctx.guild).stored() as stored_conf:
                if name in stored_conf:
                    stored_conf[name]["uses"] = stored_conf[name].get("uses", 0) + 1
            await self._report_warnings(ctx, layout.warnings)

        await self._run(ctx, run())


def _webhook_parent(channel: typing.Any) -> typing.Any:
    """The channel that owns webhooks for this channel (threads use their parent)."""
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    return parent if hasattr(parent, "create_webhook") else None


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "layout"
