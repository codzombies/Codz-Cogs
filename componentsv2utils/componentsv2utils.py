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

import io
import json
import typing

import discord
import yaml
from redbot.core import commands, Config, app_commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import text_to_file

# ---------------------------------------------------------------------------
# YAML schema reference (inline docstring shown to users via [p]cv2 help)
# ---------------------------------------------------------------------------
YAML_SCHEMA_HELP = """
**Components V2 YAML / JSON schema**

Top-level keys:
  `components` *(list, required)* – ordered list of component objects.
  `accent_color` *(str, optional)* – hex colour for the outermost Container
    border (e.g. `#5865F2`). Omit for no border / theme-blended look.

Each component object has a `type` key plus type-specific fields:

**`text`** – TextDisplay (markdown)
```yaml
- type: text
  content: "## Hello\\nThis is **bold** and *italic*."
```

**`separator`** – horizontal divider / spacing
```yaml
- type: separator
  visible: true       # default true  – show the line
  spacing: small      # small | large  (default small)
```

**`section`** – text block with an optional accessory (thumbnail or link button)
```yaml
- type: section
  title: "## My Section"
  description: "Some description here."
  # accessory is optional:
  accessory:
    type: thumbnail
    url: "https://example.com/image.png"
  # OR
  accessory:
    type: button
    label: "Click me"
    url: "https://example.com"
```

**`gallery`** – MediaGallery of up to 10 images/videos
```yaml
- type: gallery
  items:
    - url: "https://example.com/image1.png"
      description: "Alt text"   # optional
      spoiler: false             # optional
    - url: "https://example.com/image2.gif"
```

**`container`** – group sub-components with an optional accent colour
```yaml
- type: container
  accent_color: "#5865F2"   # optional
  components:
    - type: text
      content: "Inside the container"
    - type: separator
```

**Full example (YAML)**
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
# Helper: parse hex colour string → discord.Color (or None)
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
# Core builder: dict → list[discord.ui.* components]
# ---------------------------------------------------------------------------
class BuildError(Exception):
    """Raised when the user's YAML/JSON has a structural problem."""


def _build_accessory(
    acc: dict,
) -> typing.Union[discord.ui.Thumbnail, discord.ui.Button, None]:
    """Build a Section accessory (Thumbnail or link Button)."""
    if acc is None:
        return None
    atype = str(acc.get("type", "")).lower()
    if atype == "thumbnail":
        url = acc.get("url")
        if not url:
            raise BuildError("`accessory.url` is required for a thumbnail accessory.")
        description = acc.get("description")
        spoiler = bool(acc.get("spoiler", False))
        return discord.ui.Thumbnail(
            media=url,
            description=description,
            spoiler=spoiler,
        )
    elif atype == "button":
        label = acc.get("label", "Link")
        url = acc.get("url")
        if not url:
            raise BuildError("`accessory.url` is required for a button accessory.")
        return discord.ui.Button(
            label=label,
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
        raise BuildError(f"Each component must be a mapping/dict, got `{type(comp).__name__}`.")
    ctype = str(comp.get("type", "")).lower()

    # ── TextDisplay ────────────────────────────────────────────────────────
    if ctype == "text":
        content = comp.get("content")
        if not content:
            raise BuildError("`text` component requires a `content` field.")
        return discord.ui.TextDisplay(content=str(content))

    # ── Separator ──────────────────────────────────────────────────────────
    elif ctype == "separator":
        visible = bool(comp.get("visible", True))
        spacing_raw = str(comp.get("spacing", "small")).lower()
        spacing = (
            discord.SeparatorSpacing.large
            if spacing_raw == "large"
            else discord.SeparatorSpacing.small
        )
        return discord.ui.Separator(visible=visible, spacing=spacing)

    # ── Section ────────────────────────────────────────────────────────────
    elif ctype == "section":
        title = comp.get("title", "")
        description = comp.get("description", "")
        if not title and not description:
            raise BuildError("`section` component requires at least `title` or `description`.")
        accessory_data = comp.get("accessory")
        accessory = _build_accessory(accessory_data) if accessory_data else discord.utils.MISSING
        texts = []
        if title:
            texts.append(discord.ui.TextDisplay(content=str(title)))
        if description:
            texts.append(discord.ui.TextDisplay(content=str(description)))
        kwargs = {}
        if accessory is not discord.utils.MISSING:
            kwargs["accessory"] = accessory
        return discord.ui.Section(*texts, **kwargs)

    # ── MediaGallery ───────────────────────────────────────────────────────
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

    # ── Container ──────────────────────────────────────────────────────────
    elif ctype == "container":
        sub_components_data = comp.get("components", [])
        if not sub_components_data:
            raise BuildError("`container` component requires at least one item in `components`.")
        sub_components = [_build_component(c) for c in sub_components_data]
        accent = _parse_color(comp.get("accent_color"))
        kwargs = {}
        if accent is not None:
            kwargs["accent_color"] = accent
        return discord.ui.Container(*sub_components, **kwargs)

    else:
        raise BuildError(
            f"Unknown component type `{ctype}`. "
            "Valid types: `text`, `separator`, `section`, `gallery`, `container`."
        )


def build_layout_view(data: dict) -> discord.ui.LayoutView:
    """
    Parse the top-level data dict and return a ready-to-send LayoutView.

    Expected keys:
        components   – list of component dicts (required)
        accent_color – optional hex colour for a wrapping Container
    """
    components_data = data.get("components")
    if not components_data or not isinstance(components_data, list):
        raise BuildError("Your data must have a `components` list with at least one item.")

    built = [_build_component(c) for c in components_data]

    # Optionally wrap everything in a single Container with accent colour
    accent = _parse_color(data.get("accent_color"))
    if accent is not None:
        top_level = [discord.ui.Container(*built, accent_color=accent)]
    else:
        top_level = built

    class _View(discord.ui.LayoutView):
        pass

    view = _View()
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


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------
class ComponentsV2Utils(commands.Cog):
    """
    Send rich Discord Components V2 layout messages from YAML or JSON.

    Supports TextDisplay (markdown), Separators, Sections, MediaGalleries,
    and Containers — no classic embeds required.
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

    # ── Internal send helper ───────────────────────────────────────────────

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
                f"❌ **Discord returned an error while sending:**\n```\n{exc}\n```",
                ephemeral=True,
            )

    # ── Command group ──────────────────────────────────────────────────────

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True)
    @commands.hybrid_group(name="cv2", aliases=["componentsv2"], invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def cv2(self, ctx: commands.Context) -> None:
        """Components V2 message sender.

        Use `[p]cv2 yaml`, `[p]cv2 json`, or `[p]cv2 file` to send messages.
        Use `[p]cv2 schema` to view the YAML/JSON schema reference.
        """
        await ctx.send_help()

    # ── Schema reference ───────────────────────────────────────────────────

    @cv2.command(name="schema", aliases=["help", "reference"])
    async def cv2_schema(self, ctx: commands.Context) -> None:
        """Display the YAML/JSON schema for building Components V2 messages."""
        # Split into pages if needed (Discord message limit)
        if len(YAML_SCHEMA_HELP) <= 2000:
            await ctx.send(YAML_SCHEMA_HELP)
        else:
            # Send as a file for safety
            await ctx.send(
                "Here is the full schema reference:",
                file=text_to_file(text=YAML_SCHEMA_HELP, filename="cv2_schema.md"),
            )

    # ── YAML ───────────────────────────────────────────────────────────────

    @cv2.command(name="yaml", aliases=["fromyaml"])
    async def cv2_yaml(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread, discord.Message]
        ] = None,
        *,
        data: str,
    ) -> None:
        """Send a Components V2 message from inline YAML.

        **Example:**
        ```
        [p]cv2 yaml
        components:
          - type: text
            content: "## Hello World"
          - type: separator
          - type: text
            content: "This is a Components V2 message!"
        ```

        Optionally provide a channel or message link as the first argument
        to send to a different channel or edit an existing message.
        """
        try:
            parsed = _parse_yaml(data)
            view = build_layout_view(parsed)
        except BuildError as exc:
            return await ctx.send(f"❌ **Build error:** {exc}", ephemeral=True)

        target = channel_or_message
        await self._send_or_edit(ctx, view, target)

    # ── JSON ───────────────────────────────────────────────────────────────

    @cv2.command(name="json", aliases=["fromjson"])
    async def cv2_json(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread, discord.Message]
        ] = None,
        *,
        data: str,
    ) -> None:
        """Send a Components V2 message from inline JSON.

        **Example:**
        ```
        [p]cv2 json {"components": [{"type": "text", "content": "Hello!"}]}
        ```

        Optionally provide a channel or message link as the first argument
        to send to a different channel or edit an existing message.
        """
        try:
            parsed = _parse_json(data)
            view = build_layout_view(parsed)
        except BuildError as exc:
            return await ctx.send(f"❌ **Build error:** {exc}", ephemeral=True)

        target = channel_or_message
        await self._send_or_edit(ctx, view, target)

    # ── File (YAML or JSON attachment) ─────────────────────────────────────

    @cv2.command(name="file", aliases=["fromfile"])
    async def cv2_file(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread, discord.Message]
        ] = None,
    ) -> None:
        """Send a Components V2 message from an uploaded YAML or JSON file.

        Attach a `.yaml`, `.yml`, or `.json` file to your message.
        """
        if not ctx.message.attachments:
            return await ctx.send(
                "❌ Please attach a `.yaml`, `.yml`, or `.json` file.", ephemeral=True
            )

        attachment = ctx.message.attachments[0]
        ext = attachment.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("yaml", "yml", "json", "txt"):
            return await ctx.send(
                "❌ Unsupported file type. Please upload a `.yaml`, `.yml`, or `.json` file.",
                ephemeral=True,
            )

        try:
            raw = (await attachment.read()).decode("utf-8")
        except (UnicodeDecodeError, discord.HTTPException) as exc:
            return await ctx.send(f"❌ Could not read attachment: {exc}", ephemeral=True)

        try:
            parsed = _parse_yaml(raw) if ext in ("yaml", "yml", "txt") else _parse_json(raw)
            view = build_layout_view(parsed)
        except BuildError as exc:
            return await ctx.send(f"❌ **Build error:** {exc}", ephemeral=True)

        target = channel_or_message
        await self._send_or_edit(ctx, view, target)

    # ── Store ──────────────────────────────────────────────────────────────

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

        **Example (inline YAML):**
        ```
        [p]cv2 store "my-announcement"
        components:
          - type: text
            content: "## Big news!"
        ```
        """
        if data is None and ctx.message.attachments:
            # Try reading from attachment
            attachment = ctx.message.attachments[0]
            ext = attachment.filename.rsplit(".", 1)[-1].lower()
            try:
                data = (await attachment.read()).decode("utf-8")
            except (UnicodeDecodeError, discord.HTTPException) as exc:
                return await ctx.send(f"❌ Could not read attachment: {exc}", ephemeral=True)
            is_yaml = ext in ("yaml", "yml", "txt")
        elif data is not None:
            # Detect format: try YAML first (it's a superset of JSON)
            is_yaml = True
        else:
            return await ctx.send(
                "❌ Provide YAML/JSON inline or attach a file.", ephemeral=True
            )

        # Validate by building
        try:
            parsed = _parse_yaml(data) if is_yaml else _parse_json(data)
            build_layout_view(parsed)  # validation only
        except BuildError as exc:
            return await ctx.send(f"❌ **Build error:** {exc}", ephemeral=True)

        async with self.config.guild(ctx.guild).stored() as stored:
            if len(stored) >= 100 and name not in stored:
                return await ctx.send(
                    "❌ Reached the 100-layout limit. Remove one with `[p]cv2 unstore` first.",
                    ephemeral=True,
                )
            stored[name] = {
                "author": ctx.author.id,
                "data": parsed,
                "uses": stored.get(name, {}).get("uses", 0),
            }

        await ctx.send(f"✅ Stored layout `{name}`.")

    # ── Unstore ────────────────────────────────────────────────────────────

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="unstore", aliases=["delete", "remove"])
    async def cv2_unstore(self, ctx: commands.Context, name: str) -> None:
        """Remove a stored Components V2 layout."""
        async with self.config.guild(ctx.guild).stored() as stored:
            if name not in stored:
                return await ctx.send(f"❌ No stored layout named `{name}`.", ephemeral=True)
            del stored[name]
        await ctx.send(f"✅ Removed stored layout `{name}`.")

    # ── List stored ────────────────────────────────────────────────────────

    @cv2.command(name="list", aliases=["stored"])
    async def cv2_list(self, ctx: commands.Context) -> None:
        """List all stored Components V2 layouts for this server."""
        stored = await self.config.guild(ctx.guild).stored()
        if not stored:
            return await ctx.send("No layouts stored for this server yet.")

        lines = []
        for name, meta in stored.items():
            lines.append(f"• `{name}` — used {meta.get('uses', 0)}×")

        view = discord.ui.LayoutView()
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content="## Stored Layouts\n" + "\n".join(lines)),
                accent_color=discord.Color.blurple(),
            )
        )
        await ctx.send(view=view)

    # ── Post stored ────────────────────────────────────────────────────────

    @cv2.command(name="post", aliases=["send"])
    async def cv2_post(
        self,
        ctx: commands.Context,
        channel_or_message: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread, discord.Message]
        ] = None,
        *,
        name: str,
    ) -> None:
        """Post a stored Components V2 layout by name."""
        async with self.config.guild(ctx.guild).stored() as stored:
            if name not in stored:
                return await ctx.send(f"❌ No stored layout named `{name}`.", ephemeral=True)
            entry = stored[name]
            entry["uses"] = entry.get("uses", 0) + 1

        try:
            view = build_layout_view(entry["data"])
        except BuildError as exc:
            return await ctx.send(f"❌ **Build error in stored layout:** {exc}", ephemeral=True)

        target = channel_or_message
        await self._send_or_edit(ctx, view, target)

    # ── Download stored ────────────────────────────────────────────────────

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="download")
    async def cv2_download(self, ctx: commands.Context, name: str) -> None:
        """Download a stored layout as a YAML file."""
        stored = await self.config.guild(ctx.guild).stored()
        if name not in stored:
            return await ctx.send(f"❌ No stored layout named `{name}`.", ephemeral=True)
        data = stored[name]["data"]
        yaml_text = yaml.dump(data, allow_unicode=True, sort_keys=False)
        await ctx.send(
            f"Here is the YAML for `{name}`:",
            file=text_to_file(text=yaml_text, filename=f"{name}.yaml"),
        )

    # ── Info stored ────────────────────────────────────────────────────────

    @commands.mod_or_permissions(manage_guild=True)
    @cv2.command(name="info")
    async def cv2_info(self, ctx: commands.Context, name: str) -> None:
        """Show info about a stored layout."""
        stored = await self.config.guild(ctx.guild).stored()
        if name not in stored:
            return await ctx.send(f"❌ No stored layout named `{name}`.", ephemeral=True)
        meta = stored[name]
        author_id = meta.get("author", 0)
        uses = meta.get("uses", 0)
        num_components = len(meta.get("data", {}).get("components", []))

        view = discord.ui.LayoutView()
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    content=(
                        f"## Layout: `{name}`\n"
                        f"**Author:** <@{author_id}>\n"
                        f"**Uses:** {uses}\n"
                        f"**Top-level components:** {num_components}"
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
