"""
ComponentsV2Utils - A Red-DiscordBot cog for sending Discord Components V2 messages.

Supports YAML (preferred) and JSON input to build rich layout messages using:
  - TextDisplay  (markdown text blocks)
  - Separator    (horizontal dividers / spacing)
  - Container    (grouping with optional accent colour)
  - Section      (text + thumbnail/button accessory)
  - MediaGallery (image/video gallery)

Author: you (built from EmbedUtils inspiration + discord.py Components V2 masterclass)
Requires: discord.py >= 2.6, Red-DiscordBot >= 3.5.0
"""

from __future__ import annotations

import json
import typing

import discord
import yaml
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import text_to_file

# ---------------------------------------------------------------------------
# YAML schema reference
# ---------------------------------------------------------------------------
YAML_SCHEMA_HELP = """
**Components V2 YAML / JSON schema**

Top-level keys:
  `components` *(list, required)* - ordered list of component objects.
  `accent_color` *(str, optional)* - hex colour wrapping everything in a
    tinted Container border (e.g. `#5865F2`). Omit for no border.

Each component object has a `type` key plus type-specific fields:

**`text`** - TextDisplay (full Discord Markdown)
```yaml
- type: text
  content: "## Hello\\nThis is **bold** and *italic*."
```

**`separator`** - horizontal divider / spacing
```yaml
- type: separator
  visible: true       # default true  - show the line
  spacing: small      # small | large  (default small)
```

**`section`** - text block with an optional accessory (thumbnail or link button)
```yaml
- type: section
  title: "## My Section"
  description: "Some description here."
  accessory:
    type: thumbnail
    url: "https://example.com/image.png"
  # OR
  accessory:
    type: button
    label: "Click me"
    url: "https://example.com"
```

**`gallery`** - MediaGallery of up to 10 images/videos
```yaml
- type: gallery
  items:
    - url: "https://example.com/image1.png"
      description: "Alt text"
      spoiler: false
    - url: "https://example.com/image2.gif"
```

**`container`** - group sub-components with an optional accent colour
```yaml
- type: container
  accent_color: "#5865F2"
  components:
    - type: text
      content: "Inside the container"
    - type: separator
```

**Full YAML example**
```yaml
accent_color: "#5865F2"
components:
  - type: text
    content: "# Announcement\\nWelcome to the server!"
  - type: separator
    visible: true
    spacing: small
  - type: section
    title: "## Rules"
    description: "Please read the rules carefully."
    accessory:
      type: thumbnail
      url: "https://i.imgur.com/9sDnoUW.jpeg"
  - type: gallery
    items:
      - url: "https://i.imgur.com/9sDnoUW.jpeg"
```
"""


# ---------------------------------------------------------------------------
# Converter: channel mention/ID  OR  message link  -> union target
# ---------------------------------------------------------------------------
class ChannelOrMessageConverter(commands.Converter):
    """
    Accepts either:
      - a channel mention / channel ID
      - a message link (https://discord.com/channels/...)
    Returns the resolved object, or raises BadArgument.
    """

    async def convert(
        self, ctx: commands.Context, argument: str
    ) -> typing.Union[discord.abc.Messageable, discord.Message]:
        # Try message link / ID first
        try:
            return await commands.MessageConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        # Try TextChannel
        try:
            return await commands.TextChannelConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        # Try Thread
        try:
            return await commands.ThreadConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        raise commands.BadArgument(
            f"Could not resolve `{argument}` as a channel or message link."
        )


# ---------------------------------------------------------------------------
# Helper: parse hex colour string -> discord.Color (or None)
# ---------------------------------------------------------------------------
def _parse_color(value: typing.Optional[str]) -> typing.Optional[discord.Color]:
    if not value:
        return None
    value = str(value).strip().lstrip("#")
    try:
        return discord.Color(int(value, 16))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Core builder: dict -> discord.ui.LayoutView
# ---------------------------------------------------------------------------
class BuildError(Exception):
    """Raised when the user's YAML/JSON has a structural problem."""


def _build_accessory(
    acc: dict,
) -> typing.Union[discord.ui.Thumbnail, discord.ui.Button]:
    """Build a Section accessory (Thumbnail or link Button)."""
    atype = str(acc.get("type", "")).lower()
    if atype == "thumbnail":
        url = acc.get("url")
        if not url:
            raise BuildError("`accessory.url` is required for a thumbnail accessory.")
        return discord.ui.Thumbnail(
            media=url,
            description=acc.get("description"),
            spoiler=bool(acc.get("spoiler", False)),
        )
    elif atype == "button":
        url = acc.get("url")
        if not url:
            raise BuildError("`accessory.url` is required for a button accessory.")
        return discord.ui.Button(
            label=acc.get("label", "Link"),
            url=url,
            style=discord.ButtonStyle.link,
        )
    else:
        raise BuildError(
            f"Unknown accessory type `{atype}`. Valid types: `thumbnail`, `button`."
        )


def _build_component(comp: dict) -> discord.ui.Item:
    """Recursively build a single Component V2 item from a dict."""
    if not isinstance(comp, dict):
        raise BuildError(
            f"Each component must be a mapping/dict, got `{type(comp).__name__}`."
        )
    ctype = str(comp.get("type", "")).lower()

    # TextDisplay
    if ctype == "text":
        content = comp.get("content")
        if not content:
            raise BuildError("`text` component requires a `content` field.")
        return discord.ui.TextDisplay(content=str(content))

    # Separator
    elif ctype == "separator":
        visible = bool(comp.get("visible", True))
        spacing_raw = str(comp.get("spacing", "small")).lower()
        spacing = (
            discord.SeparatorSpacing.large
            if spacing_raw == "large"
            else discord.SeparatorSpacing.small
        )
        return discord.ui.Separator(visible=visible, spacing=spacing)

    # Section
    elif ctype == "section":
        title = comp.get("title", "")
        description = comp.get("description", "")
        if not title and not description:
            raise BuildError(
                "`section` component requires at least `title` or `description`."
            )
        texts = []
        if title:
            texts.append(discord.ui.TextDisplay(content=str(title)))
        if description:
            texts.append(discord.ui.TextDisplay(content=str(description)))
        accessory_data = comp.get("accessory")
        if accessory_data:
            return discord.ui.Section(*texts, accessory=_build_accessory(accessory_data))
        return discord.ui.Section(*texts)

    # MediaGallery
    elif ctype == "gallery":
        items_data = comp.get("items", [])
        if not items_data:
            raise BuildError("`gallery` component requires at least one item in `items`.")
        if len(items_data) > 10:
            raise BuildError("`gallery` supports a maximum of 10 items.")
        gallery_items = []
        for item in items_data:
            url = item.get("url")
            if not url:
                raise BuildError("Each gallery item requires a `url` field.")
            gallery_items.append(
                discord.MediaGalleryItem(
                    media=url,
                    description=item.get("description"),
                    spoiler=bool(item.get("spoiler", False)),
                )
            )
        return discord.ui.MediaGallery(*gallery_items)

    # Container
    elif ctype == "container":
        sub_data = comp.get("components", [])
        if not sub_data:
            raise BuildError(
                "`container` component requires at least one item in `components`."
            )
        sub_components = [_build_component(c) for c in sub_data]
        accent = _parse_color(comp.get("accent_color"))
        if accent is not None:
            return discord.ui.Container(*sub_components, accent_color=accent)
        return discord.ui.Container(*sub_components)

    else:
        raise BuildError(
            f"Unknown component type `{ctype}`. "
            "Valid types: `text`, `separator`, `section`, `gallery`, `container`."
        )


def build_layout_view(data: dict) -> discord.ui.LayoutView:
    """
    Parse the top-level data dict and return a ready-to-send LayoutView.

    Expected keys:
        components   - list of component dicts (required)
        accent_color - optional hex colour wrapping everything in a Container
    """
    components_data = data.get("components")
    if not components_data or not isinstance(components_data, list):
        raise BuildError(
            "Your data must have a `components` list with at least one item."
        )

    built = [_build_component(c) for c in components_data]

    accent = _parse_color(data.get("accent_color"))
    if accent is not None:
        top_level = [discord.ui.Container(*built, accent_color=accent)]
    else:
        top_level = built

    view = discord.ui.LayoutView()
    for item in top_level:
        view.add_item(item)
    return view


# ---------------------------------------------------------------------------
# YAML / JSON parsing helpers
# ---------------------------------------------------------------------------
def _parse_yaml(text: str) -> dict:
    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BuildError(f"YAML parse error: {exc}")
    if not isinstance(result, dict):
        raise BuildError("Your YAML must be a mapping at the top level.")
    return result


def _parse_json(text: str) -> dict:
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuildError(f"JSON parse error: {exc}")
    if not isinstance(result, dict):
        raise BuildError("Your JSON must be an object at the top level.")
    return result


def _autoparse(text: str) -> dict:
    """Try YAML first (superset of JSON), fall back to strict JSON."""
    try:
        return _parse_yaml(text)
    except BuildError:
        return _parse_json(text)


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------
class ComponentsV2Utils(commands.Cog):
    """
    Send rich Discord Components V2 layout messages from YAML or JSON.

    Supports TextDisplay (markdown), Separators, Sections, MediaGalleries,
    and Containers - no classic embeds required.
    """

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config: Config = Config.get_conf(
            self,
            identifier=847392016574839201,
            force_registration=True,
        )
        self.config.register_guild(stored={})

    # Internal send helper

    async def _send_or_edit(
        self,
        ctx: commands.Context,
        view: discord.ui.LayoutView,
        target: typing.Union[discord.abc.Messageable, discord.Message, None],
    ) -> None:
        """Send the LayoutView to a channel, or edit an existing message."""
        try:
            if isinstance(target, discord.Message):
                await target.edit(view=view)
            else:
                channel = target or ctx.channel
                await channel.send(view=view)
        except discord.HTTPException as exc:
            await ctx.send(
                f":x: **Discord returned an error while sending:**\n```\n{exc}\n```",
                ephemeral=True,
            )

    # Command group
    # NOTE: using commands.group (not hybrid_group) so subcommands that accept
    # ChannelOrMessageConverter don't trip discord.py's app_commands Union
    # channel-type restriction.

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True)
    @commands.group(name="cv2", aliases=["componentsv2"], invoke_without_command=True)
    async def cv2(self, ctx: commands.Context) -> None:
        """Components V2 message sender.

        Use `[p]cv2 yaml`, `[p]cv2 json`, or `[p]cv2 file` to send messages.
        Use `[p]cv2 schema` to view the YAML/JSON schema reference.
        """
        await ctx.send_help()

    # Schema reference

    @cv2.command(name="schema", aliases=["help", "reference"])
    async def cv2_schema(self, ctx: commands.Context) -> None:
        """Display the YAML/JSON schema for building Components V2 messages."""
        if len(YAML_SCHEMA_HELP) <= 2000:
            await ctx.send(YAML_SCHEMA_HELP)
        else:
            await ctx.send(
                "Here is the full schema reference:",
                file=text_to_file(text=YAML_SCHEMA_HELP, filename="cv2_schema.md"),
            )

    # YAML

    @cv2.command(name="yaml", aliases=["fromyaml"])
    async def cv2_yaml(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[ChannelOrMessageConverter] = None,
        *,
        data: str,
    ) -> None:
        """Send a Components V2 message from inline YAML.

        Optionally provide a channel mention/ID or message link as the first
        argument to send to a different channel or edit an existing message.

        Example:
        [p]cv2 yaml
        components:
          - type: text
            content: "## Hello World"
          - type: separator
          - type: text
            content: "This is a Components V2 message!"
        """
        try:
            view = build_layout_view(_parse_yaml(data))
        except BuildError as exc:
            return await ctx.send(f":x: **Build error:** {exc}", ephemeral=True)
        await self._send_or_edit(ctx, view, channel_or_message)

    # JSON

    @cv2.command(name="json", aliases=["fromjson"])
    async def cv2_json(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[ChannelOrMessageConverter] = None,
        *,
        data: str,
    ) -> None:
        """Send a Components V2 message from inline JSON.

        Optionally provide a channel mention/ID or message link as the first
        argument to send to a different channel or edit an existing message.

        Example: [p]cv2 json {"components": [{"type": "text", "content": "Hello!"}]}
        """
        try:
            view = build_layout_view(_parse_json(data))
        except BuildError as exc:
            return await ctx.send(f":x: **Build error:** {exc}", ephemeral=True)
        await self._send_or_edit(ctx, view, channel_or_message)

    # File (YAML or JSON attachment)

    @cv2.command(name="file", aliases=["fromfile"])
    async def cv2_file(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[ChannelOrMessageConverter] = None,
    ) -> None:
        """Send a Components V2 message from an uploaded YAML or JSON file.

        Attach a .yaml, .yml, or .json file to your message.
        Optionally provide a channel or message link to redirect the output.
        """
        if not ctx.message.attachments:
            return await ctx.send(
                ":x: Please attach a `.yaml`, `.yml`, or `.json` file.", ephemeral=True
            )

        attachment = ctx.message.attachments[0]
        ext = attachment.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("yaml", "yml", "json", "txt"):
            return await ctx.send(
                ":x: Unsupported file type. Please upload a `.yaml`, `.yml`, or `.json` file.",
                ephemeral=True,
            )

        try:
            raw = (await attachment.read()).decode("utf-8")
        except (UnicodeDecodeError, discord.HTTPException) as exc:
            return await ctx.send(f":x: Could not read attachment: {exc}", ephemeral=True)

        try:
            parsed = _parse_yaml(raw) if ext in ("yaml", "yml", "txt") else _parse_json(raw)
            view = build_layout_view(parsed)
        except BuildError as exc:
            return await ctx.send(f":x: **Build error:** {exc}", ephemeral=True)

        await self._send_or_edit(ctx, view, channel_or_message)

    # Store

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="store")
    async def cv2_store(
        self,
        ctx: commands.Context,
        name: str,
        *,
        data: str = None,
    ) -> None:
        """Store a Components V2 layout by name (YAML or JSON inline, or attach a file).

        Put the name in quotes if it contains spaces.

        Example (inline YAML):
        [p]cv2 store "my-announcement"
        components:
          - type: text
            content: "## Big news!"
        """
        if data is None and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            ext = attachment.filename.rsplit(".", 1)[-1].lower()
            try:
                data = (await attachment.read()).decode("utf-8")
            except (UnicodeDecodeError, discord.HTTPException) as exc:
                return await ctx.send(f":x: Could not read attachment: {exc}", ephemeral=True)
            parse_fn = _parse_yaml if ext in ("yaml", "yml", "txt") else _parse_json
        elif data is not None:
            parse_fn = _autoparse
        else:
            return await ctx.send(
                ":x: Provide YAML/JSON inline or attach a file.", ephemeral=True
            )

        try:
            parsed = parse_fn(data)
            build_layout_view(parsed)  # validate only
        except BuildError as exc:
            return await ctx.send(f":x: **Build error:** {exc}", ephemeral=True)

        async with self.config.guild(ctx.guild).stored() as stored:
            if len(stored) >= 100 and name not in stored:
                return await ctx.send(
                    ":x: Reached the 100-layout limit. Remove one with `[p]cv2 unstore` first.",
                    ephemeral=True,
                )
            stored[name] = {
                "author": ctx.author.id,
                "data": parsed,
                "uses": stored.get(name, {}).get("uses", 0),
            }

        await ctx.send(f":white_check_mark: Stored layout `{name}`.")

    # Unstore

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="unstore", aliases=["delete", "remove"])
    async def cv2_unstore(self, ctx: commands.Context, name: str) -> None:
        """Remove a stored Components V2 layout."""
        async with self.config.guild(ctx.guild).stored() as stored:
            if name not in stored:
                return await ctx.send(f":x: No stored layout named `{name}`.", ephemeral=True)
            del stored[name]
        await ctx.send(f":white_check_mark: Removed stored layout `{name}`.")

    # List stored

    @cv2.command(name="list", aliases=["stored"])
    async def cv2_list(self, ctx: commands.Context) -> None:
        """List all stored Components V2 layouts for this server."""
        stored = await self.config.guild(ctx.guild).stored()
        if not stored:
            return await ctx.send("No layouts stored for this server yet.")

        lines = "\n".join(
            f"- `{name}` - used {meta.get('uses', 0)}x"
            for name, meta in stored.items()
        )
        view = discord.ui.LayoutView()
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content=f"## Stored Layouts\n{lines}"),
                accent_color=discord.Color.blurple(),
            )
        )
        await ctx.send(view=view)

    # Post stored

    @cv2.command(name="post", aliases=["send"])
    async def cv2_post(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[ChannelOrMessageConverter] = None,
        *,
        name: str,
    ) -> None:
        """Post a stored Components V2 layout by name.

        Optionally provide a channel or message link as the first argument.
        """
        async with self.config.guild(ctx.guild).stored() as stored:
            if name not in stored:
                return await ctx.send(f":x: No stored layout named `{name}`.", ephemeral=True)
            entry = stored[name]
            entry["uses"] = entry.get("uses", 0) + 1

        try:
            view = build_layout_view(entry["data"])
        except BuildError as exc:
            return await ctx.send(
                f":x: **Build error in stored layout:** {exc}", ephemeral=True
            )

        await self._send_or_edit(ctx, view, channel_or_message)

    # Download stored

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="download")
    async def cv2_download(self, ctx: commands.Context, name: str) -> None:
        """Download a stored layout as a YAML file."""
        stored = await self.config.guild(ctx.guild).stored()
        if name not in stored:
            return await ctx.send(f":x: No stored layout named `{name}`.", ephemeral=True)
        yaml_text = yaml.dump(stored[name]["data"], allow_unicode=True, sort_keys=False)
        await ctx.send(
            f"Here is the YAML for `{name}`:",
            file=text_to_file(text=yaml_text, filename=f"{name}.yaml"),
        )

    # Info stored

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="info")
    async def cv2_info(self, ctx: commands.Context, name: str) -> None:
        """Show info about a stored layout."""
        stored = await self.config.guild(ctx.guild).stored()
        if name not in stored:
            return await ctx.send(f":x: No stored layout named `{name}`.", ephemeral=True)
        meta = stored[name]
        view = discord.ui.LayoutView()
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    content=(
                        f"## Layout: `{name}`\n"
                        f"**Author:** <@{meta.get('author', 0)}>\n"
                        f"**Uses:** {meta.get('uses', 0)}\n"
                        f"**Top-level components:** "
                        f"{len(meta.get('data', {}).get('components', []))}"
                    )
                ),
                accent_color=discord.Color.blurple(),
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions(users=False))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
async def setup(bot: Red) -> None:
    await bot.add_cog(ComponentsV2Utils(bot))
