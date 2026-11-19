"""
Parsing, normalization and validation for Components V2 layouts.

Everything the user writes (friendly YAML, friendly JSON, or raw Discord API
JSON with numeric ``type`` values) is normalized into the exact payload
Discord expects, validated against the documented limits, and only then turned
into a ``discord.ui.LayoutView``. Keeping the raw payload as the single source
of truth is what lets the cog round-trip messages (download), export link
preview embeds (webembed) and show the exact payload (payload).

Nothing in here depends on Red, so it can be tested on its own.
"""

from __future__ import annotations

import html
import json
import re
import typing
import urllib.parse
from dataclasses import dataclass, field

import discord
import yaml

# discord.py does not expose a public "dict -> ui.Item" constructor. These two
# private helpers are what LayoutView.from_message uses internally and have
# been stable since Components V2 landed in discord.py 2.6.
from discord.components import _component_factory
from discord.ui.view import _component_to_item


class BuildError(Exception):
    """Raised when the user's layout has a structural problem."""


# ---------------------------------------------------------------------------
# Discord constants
# ---------------------------------------------------------------------------
ACTION_ROW = 1
BUTTON = 2
SECTION = 9
TEXT = 10
THUMBNAIL = 11
GALLERY = 12
FILE = 13
SEPARATOR = 14
CONTAINER = 17

TYPE_NAMES = {
    ACTION_ROW: "row",
    BUTTON: "button",
    SECTION: "section",
    TEXT: "text",
    THUMBNAIL: "thumbnail",
    GALLERY: "gallery",
    FILE: "file",
    SEPARATOR: "separator",
    CONTAINER: "container",
}

TYPE_ALIASES = {
    "row": ACTION_ROW,
    "action_row": ACTION_ROW,
    "actionrow": ACTION_ROW,
    "buttons": ACTION_ROW,
    "button": BUTTON,
    "link": BUTTON,
    "section": SECTION,
    "text": TEXT,
    "text_display": TEXT,
    "textdisplay": TEXT,
    "markdown": TEXT,
    "thumbnail": THUMBNAIL,
    "gallery": GALLERY,
    "media_gallery": GALLERY,
    "mediagallery": GALLERY,
    "images": GALLERY,
    "file": FILE,
    "separator": SEPARATOR,
    "divider": SEPARATOR,
    "container": CONTAINER,
}

# Where each component type is allowed to appear.
ALLOWED_IN = {
    "root": {ACTION_ROW, SECTION, TEXT, GALLERY, FILE, SEPARATOR, CONTAINER},
    "container": {ACTION_ROW, SECTION, TEXT, GALLERY, FILE, SEPARATOR},
    "section": {TEXT},
    "accessory": {BUTTON, THUMBNAIL},
    "row": {BUTTON},
}
PLACE_NAMES = {
    "root": "the top level",
    "container": "a container",
    "section": "a section's `components`",
    "accessory": "a section `accessory`",
    "row": "a row",
}

# Documented limits.
MAX_COMPONENTS = 40
MAX_TEXT_LENGTH = 4000
MAX_GALLERY_ITEMS = 10
MAX_ROW_BUTTONS = 5
MAX_SECTION_TEXTS = 3
MAX_BUTTON_LABEL = 80
MAX_BUTTON_URL = 512
MAX_DESCRIPTION = 1024
MAX_MEDIA_URL = 2048
MAX_ID = 2**31 - 1
WEBEMBED_MAX_BYTES = 3000
LINK_STYLE = 5

# Keys each friendly component understands (anything else gets a warning so
# typos like `colour_accent` don't fail silently).
COMMON_KEYS = {"type", "id"}
KNOWN_KEYS = {
    TEXT: {"content", "text"},
    SEPARATOR: {"divider", "visible", "spacing"},
    SECTION: {"components", "texts", "title", "description", "content", "accessory", "thumbnail", "button"},
    THUMBNAIL: {"url", "media", "description", "spoiler"},
    BUTTON: {"url", "label", "emoji", "disabled", "style"},
    GALLERY: {"items"},
    FILE: {"url", "file", "filename", "name", "size", "spoiler"},
    CONTAINER: {"components", "accent_color", "color", "colour", "spoiler"},
    ACTION_ROW: {"components", "buttons"},
}
ROOT_KEYS = {
    "components", "component", "accent_color", "color", "colour", "spoiler", "flags",
    "username", "avatar_url",
}
MAX_WEBHOOK_USERNAME = 80


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class Layout:
    components: typing.List[dict]
    warnings: typing.List[str] = field(default_factory=list)
    attachments: typing.List[str] = field(default_factory=list)
    component_count: int = 0
    text_length: int = 0
    gallery_items: int = 0
    # When either is set, the layout is posted through a webhook.
    username: typing.Optional[str] = None
    avatar_url: typing.Optional[str] = None

    @property
    def uses_webhook(self) -> bool:
        return self.username is not None or self.avatar_url is not None

    def has(self, ctype: int) -> bool:
        return any(c.get("type") == ctype for c in _walk(self.components))


class _State:
    def __init__(self) -> None:
        self.warnings: typing.List[str] = []
        self.attachments: typing.List[str] = []
        self.ids: typing.Set[int] = set()


# ---------------------------------------------------------------------------
# Parsing text -> dict
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Remove a surrounding ```yaml / ```json fence if present."""
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text.strip()


def parse_yaml(text: str) -> typing.Any:
    try:
        return yaml.safe_load(strip_code_fence(text))
    except yaml.YAMLError as exc:
        raise BuildError(f"YAML parse error: {exc}")


def parse_json(text: str) -> typing.Any:
    try:
        return json.loads(strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise BuildError(f"JSON parse error: {exc}")


def parse_any(text: str) -> typing.Any:
    """YAML is a superset of JSON, so YAML handles both. Fall back to strict
    JSON only to give a JSON-flavoured error message."""
    try:
        return parse_yaml(text)
    except BuildError:
        return parse_json(text)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------
def _color_names() -> typing.Set[str]:
    names = set()
    for name in dir(discord.Color):
        if name.startswith("_") or name.startswith("from_") or name in ("to_rgb", "value", "r", "g", "b"):
            continue
        attr = getattr(discord.Color, name)
        if callable(attr):
            try:
                if isinstance(attr(), discord.Color):
                    names.add(name)
            except TypeError:
                pass
    return names


COLOR_NAMES = _color_names()


def parse_color(value: typing.Any, path: str) -> typing.Optional[int]:
    """Accepts a decimal int (as Discord sends it), `#RRGGBB`, `#RGB`,
    `0xRRGGBB`, bare `RRGGBB`, `rgb(r, g, b)`, or a name like `blurple`."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise BuildError(f"{path}: `{value}` is not a colour.")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise BuildError(f"{path}: colour `{value}` is out of range (0 - 16777215).")
        return value
    text = str(value).strip()
    if not text:
        return None
    key = text.lower().replace(" ", "_").replace("-", "_")
    if key in COLOR_NAMES:
        return getattr(discord.Color, key)().value
    bare = text.lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", bare):
        return int("".join(ch * 2 for ch in bare), 16)
    if re.fullmatch(r"[0-9a-fA-F]{6}", bare):
        return int(bare, 16)
    try:
        return discord.Color.from_str(text).value
    except ValueError:
        pass
    raise BuildError(
        f"{path}: `{text}` is not a valid colour. Use `#5865F2`, a decimal number, or a name like `blurple`."
    )


def _bool(value: typing.Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "no", "off", "0"):
        return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise BuildError(f"{path}: expected `true` or `false`, got `{value}`.")


def _str(value: typing.Any, path: str, *, max_len: typing.Optional[int] = None) -> str:
    if isinstance(value, (dict, list)) or value is None:
        raise BuildError(f"{path}: expected text, got `{type(value).__name__}`.")
    text = str(value)
    if max_len is not None and len(text) > max_len:
        raise BuildError(f"{path}: must be at most {max_len} characters (got {len(text)}).")
    return text


def _media_url(value: typing.Any, path: str, state: _State) -> str:
    """A media reference: a string URL or an unfurled media item `{url: ...}`."""
    if isinstance(value, dict):
        value = value.get("url")
    if not value:
        raise BuildError(f"{path}: a media `url` is required.")
    url = _str(value, path, max_len=MAX_MEDIA_URL).strip()
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme == "attachment":
        name = url.split("://", 1)[1]
        if not name:
            raise BuildError(f"{path}: `attachment://` needs a filename, e.g. `attachment://banner.png`.")
        if name not in state.attachments:
            state.attachments.append(name)
    elif scheme not in ("http", "https"):
        raise BuildError(
            f"{path}: `{url}` is not a valid media URL. Use `https://...` or `attachment://filename`."
        )
    return url


def _emoji(value: typing.Any, path: str) -> dict:
    if isinstance(value, dict):
        emoji = discord.PartialEmoji.from_dict(value)
    else:
        emoji = discord.PartialEmoji.from_str(_str(value, path).strip())
    if not emoji.name and not emoji.id:
        raise BuildError(f"{path}: `{value}` is not a valid emoji.")
    return dict(emoji.to_dict())


def _resolve_type(raw: dict, path: str) -> int:
    if "type" not in raw:
        raise BuildError(f"{path}: missing `type`.")
    ctype = raw["type"]
    if isinstance(ctype, bool):
        raise BuildError(f"{path}: invalid `type` `{ctype}`.")
    if isinstance(ctype, int) or (isinstance(ctype, str) and ctype.strip().isdigit()):
        num = int(ctype)
        if num in TYPE_NAMES:
            return num
        if num in (3, 5, 6, 7, 8):
            raise BuildError(
                f"{path}: select menus (type {num}) need a bot-side handler and are not supported."
            )
        raise BuildError(f"{path}: component type `{num}` is not supported in messages by this cog.")
    name = str(ctype).strip().lower().replace(" ", "_").replace("-", "_")
    if name in TYPE_ALIASES:
        return TYPE_ALIASES[name]
    valid = ", ".join(f"`{n}`" for n in TYPE_NAMES.values())
    raise BuildError(f"{path}: unknown component type `{ctype}`. Valid types: {valid}.")


def _check_keys(raw: dict, ctype: int, path: str, state: _State) -> None:
    unknown = set(raw) - COMMON_KEYS - KNOWN_KEYS.get(ctype, set())
    if unknown:
        keys = ", ".join(f"`{k}`" for k in sorted(map(str, unknown)))
        state.warnings.append(f"{path}: ignored unknown key(s) {keys}.")


def _component_id(raw: dict, path: str, state: _State) -> typing.Optional[int]:
    if raw.get("id") in (None, 0):
        return None
    cid = raw["id"]
    if isinstance(cid, bool) or not isinstance(cid, int) or not 0 < cid <= MAX_ID:
        raise BuildError(f"{path}: `id` must be a whole number between 1 and {MAX_ID}.")
    if cid in state.ids:
        raise BuildError(f"{path}: `id` {cid} is used more than once. Component ids must be unique.")
    state.ids.add(cid)
    return cid


def _list(value: typing.Any, path: str, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BuildError(f"{path}: `{what}` must be a list.")
    return value


# ---------------------------------------------------------------------------
# Friendly / raw dict -> raw Discord payload
# ---------------------------------------------------------------------------
def _component(raw: typing.Any, path: str, state: _State, place: str) -> dict:
    # A bare string anywhere a text display is allowed is shorthand for one.
    if isinstance(raw, str) or (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
        raw = {"type": "text", "content": str(raw)}
    if not isinstance(raw, dict):
        raise BuildError(f"{path}: each component must be a mapping, got `{type(raw).__name__}`.")

    ctype = _resolve_type(raw, path)
    if ctype not in ALLOWED_IN[place]:
        name = TYPE_NAMES[ctype]
        if ctype == CONTAINER:
            raise BuildError(
                f"{path}: containers cannot be nested. A `container` cannot go inside another "
                "container, or inside a layout that uses a root `accent_color` (which already "
                "wraps everything in a container)."
            )
        if ctype in (BUTTON, THUMBNAIL) and place in ("root", "container"):
            hint = "Put buttons in a `row`, or use it as a section `accessory`." if ctype == BUTTON else "Thumbnails can only be a section `accessory`; use a `gallery` for standalone images."
            raise BuildError(f"{path}: a `{name}` cannot be placed in {PLACE_NAMES[place]}. {hint}")
        raise BuildError(f"{path}: a `{name}` cannot be placed in {PLACE_NAMES[place]}.")

    _check_keys(raw, ctype, path, state)
    out: dict = {"type": ctype}
    cid = _component_id(raw, path, state)
    if cid is not None:
        out["id"] = cid

    builder = _BUILDERS[ctype]
    builder(raw, out, path, state)
    return out


def _build_text(raw: dict, out: dict, path: str, state: _State) -> None:
    content = raw.get("content", raw.get("text"))
    if content is None or str(content).strip() == "":
        raise BuildError(f"{path}: a `text` component needs non-empty `content`.")
    out["content"] = _str(content, f"{path}.content")


def _build_separator(raw: dict, out: dict, path: str, state: _State) -> None:
    divider = raw.get("divider", raw.get("visible", True))
    out["divider"] = _bool(divider, f"{path}.divider")
    spacing = raw.get("spacing", 1)
    key = str(spacing).strip().lower()
    if key in ("1", "small", "sm"):
        out["spacing"] = 1
    elif key in ("2", "large", "lg", "big"):
        out["spacing"] = 2
    else:
        raise BuildError(f"{path}.spacing: must be `small` or `large`, got `{spacing}`.")


def _build_section(raw: dict, out: dict, path: str, state: _State) -> None:
    texts: typing.List[dict] = []
    # Legacy friendly keys, kept for layouts stored by earlier versions.
    for key in ("title", "description", "content"):
        if raw.get(key) not in (None, ""):
            texts.append({"type": TEXT, "content": _str(raw[key], f"{path}.{key}")})
    children = raw.get("components", raw.get("texts"))
    for i, child in enumerate(_list(children, path, "components")):
        texts.append(_component(child, f"{path}.components[{i}]", state, "section"))
    if not texts:
        raise BuildError(f"{path}: a `section` needs 1-{MAX_SECTION_TEXTS} texts in `components`.")
    if len(texts) > MAX_SECTION_TEXTS:
        raise BuildError(
            f"{path}: a `section` can hold at most {MAX_SECTION_TEXTS} texts (got {len(texts)}). "
            "Merge some of them, or move the rest below the section."
        )
    out["components"] = texts

    accessory = raw.get("accessory")
    shorthands = [k for k in ("thumbnail", "button") if raw.get(k) is not None]
    if (accessory is not None and shorthands) or len(shorthands) > 1:
        raise BuildError(f"{path}: a `section` has exactly one accessory; use only one of `accessory`, `thumbnail`, `button`.")
    if shorthands:
        key = shorthands[0]
        value = raw[key]
        accessory = dict(value) if isinstance(value, dict) else {"url": value}
        accessory.setdefault("type", key)
    if accessory is None:
        raise BuildError(
            f"{path}: Discord requires every `section` to have an `accessory` "
            "(a `thumbnail` or a link `button`). Without one, use plain `text` components instead."
        )
    if isinstance(accessory, dict) and "type" not in accessory:
        raise BuildError(f"{path}.accessory: missing `type` (`thumbnail` or `button`).")
    out["accessory"] = _component(accessory, f"{path}.accessory", state, "accessory")


def _build_thumbnail(raw: dict, out: dict, path: str, state: _State) -> None:
    out["media"] = {"url": _media_url(raw.get("media", raw.get("url")), f"{path}.url", state)}
    if raw.get("description") not in (None, ""):
        out["description"] = _str(raw["description"], f"{path}.description", max_len=MAX_DESCRIPTION)
    if raw.get("spoiler") is not None:
        out["spoiler"] = _bool(raw["spoiler"], f"{path}.spoiler")


def _build_button(raw: dict, out: dict, path: str, state: _State) -> None:
    style = raw.get("style", LINK_STYLE)
    if str(style).strip().lower() not in ("5", "link", "url"):
        raise BuildError(
            f"{path}: only link buttons are supported. Interactive buttons (primary, secondary, "
            "success, danger) need code in a bot to respond when clicked."
        )
    for bad in ("custom_id", "sku_id"):
        if bad in raw:
            raise BuildError(f"{path}: `{bad}` is not allowed on a link button.")
    out["style"] = LINK_STYLE
    url = _str(raw.get("url") or "", f"{path}.url", max_len=MAX_BUTTON_URL).strip()
    if not url:
        raise BuildError(f"{path}: a link `button` needs a `url`.")
    if url.split("://", 1)[0].lower() not in ("http", "https", "discord") or "://" not in url:
        raise BuildError(f"{path}.url: `{url}` must start with `https://`, `http://` or `discord://`.")
    out["url"] = url
    if raw.get("label") not in (None, ""):
        out["label"] = _str(raw["label"], f"{path}.label", max_len=MAX_BUTTON_LABEL)
    if raw.get("emoji") not in (None, ""):
        out["emoji"] = _emoji(raw["emoji"], f"{path}.emoji")
    if "label" not in out and "emoji" not in out:
        raise BuildError(f"{path}: a `button` needs a `label`, an `emoji`, or both.")
    if raw.get("disabled") is not None:
        out["disabled"] = _bool(raw["disabled"], f"{path}.disabled")


def _build_gallery(raw: dict, out: dict, path: str, state: _State) -> None:
    items = _list(raw.get("items"), path, "items")
    if not items:
        raise BuildError(f"{path}: a `gallery` needs at least one entry in `items`.")
    if len(items) > MAX_GALLERY_ITEMS:
        raise BuildError(f"{path}: a `gallery` holds at most {MAX_GALLERY_ITEMS} items (got {len(items)}).")
    built = []
    for i, item in enumerate(items):
        ipath = f"{path}.items[{i}]"
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise BuildError(f"{ipath}: each gallery item must be a URL or a mapping with `url`.")
        unknown = set(item) - {"url", "media", "description", "spoiler"}
        if unknown:
            state.warnings.append(f"{ipath}: ignored unknown key(s) {', '.join(f'`{k}`' for k in sorted(map(str, unknown)))}.")
        entry: dict = {"media": {"url": _media_url(item.get("media", item.get("url")), f"{ipath}.url", state)}}
        if item.get("description") not in (None, ""):
            entry["description"] = _str(item["description"], f"{ipath}.description", max_len=MAX_DESCRIPTION)
        if item.get("spoiler") is not None:
            entry["spoiler"] = _bool(item["spoiler"], f"{ipath}.spoiler")
        built.append(entry)
    out["items"] = built


def _build_file(raw: dict, out: dict, path: str, state: _State) -> None:
    value = raw.get("file", raw.get("url", raw.get("filename", raw.get("name"))))
    if isinstance(value, dict):
        value = value.get("url")
    if not value:
        raise BuildError(f"{path}: a `file` needs the attachment's filename, e.g. `file: rules.pdf`.")
    value = _str(value, f"{path}.file").strip()
    if "://" not in value:
        value = f"attachment://{value}"
    elif not value.lower().startswith("attachment://"):
        raise BuildError(
            f"{path}: `file` components only work with files attached to your command message "
            "(`attachment://name`), not links. Use a `gallery` for images hosted elsewhere."
        )
    out["file"] = {"url": _media_url(value, f"{path}.file", state)}
    if raw.get("spoiler") is not None:
        out["spoiler"] = _bool(raw["spoiler"], f"{path}.spoiler")


def _build_container(raw: dict, out: dict, path: str, state: _State) -> None:
    children = _list(raw.get("components"), path, "components")
    if not children:
        raise BuildError(f"{path}: a `container` needs at least one entry in `components`.")
    out["components"] = [
        _component(child, f"{path}.components[{i}]", state, "container") for i, child in enumerate(children)
    ]
    color = raw.get("accent_color", raw.get("color", raw.get("colour")))
    parsed = parse_color(color, f"{path}.accent_color")
    if parsed is not None:
        out["accent_color"] = parsed
    if raw.get("spoiler") is not None:
        out["spoiler"] = _bool(raw["spoiler"], f"{path}.spoiler")


def _build_row(raw: dict, out: dict, path: str, state: _State) -> None:
    children = _list(raw.get("components", raw.get("buttons")), path, "buttons")
    if not children:
        raise BuildError(f"{path}: a `row` needs 1-{MAX_ROW_BUTTONS} entries in `buttons`.")
    if len(children) > MAX_ROW_BUTTONS:
        raise BuildError(f"{path}: a `row` holds at most {MAX_ROW_BUTTONS} buttons (got {len(children)}).")
    built = []
    for i, child in enumerate(children):
        if isinstance(child, dict) and "type" not in child:
            child = {**child, "type": "button"}
        built.append(_component(child, f"{path}.buttons[{i}]", state, "row"))
    out["components"] = built


_BUILDERS = {
    TEXT: _build_text,
    SEPARATOR: _build_separator,
    SECTION: _build_section,
    THUMBNAIL: _build_thumbnail,
    BUTTON: _build_button,
    GALLERY: _build_gallery,
    FILE: _build_file,
    CONTAINER: _build_container,
    ACTION_ROW: _build_row,
}


def _walk(components: typing.Iterable[dict]) -> typing.Iterator[dict]:
    for comp in components:
        yield comp
        yield from _walk(comp.get("components", []))
        if "accessory" in comp:
            yield comp["accessory"]


def normalize(data: typing.Any) -> Layout:
    """Turn parsed YAML/JSON into a validated raw Discord components payload."""
    if isinstance(data, list):
        data = {"components": data}
    if not isinstance(data, dict):
        raise BuildError("Your layout must be a mapping with a `components` list.")

    state = _State()
    if "components" in data:
        components = data["components"]
    elif "component" in data:
        # Component-embed format: {"component": {type: 17, ...}}
        components = [data["component"]]
    else:
        raise BuildError("Your layout needs a `components` list with at least one item.")
    if not isinstance(components, list) or not components:
        raise BuildError("`components` must be a list with at least one item.")

    unknown = set(data) - ROOT_KEYS
    for legacy in ("content", "embeds", "embed"):
        if legacy in unknown:
            state.warnings.append(
                f"`{legacy}` is not supported in Components V2 messages and was ignored. "
                "Use `text` components instead."
            )
            unknown.discard(legacy)
    if unknown:
        state.warnings.append(f"Ignored unknown top-level key(s) {', '.join(f'`{k}`' for k in sorted(map(str, unknown)))}.")

    accent = parse_color(data.get("accent_color", data.get("color", data.get("colour"))), "accent_color")
    spoiler = _bool(data["spoiler"], "spoiler") if data.get("spoiler") is not None else False
    wrap = accent is not None or spoiler
    place = "container" if wrap else "root"
    built = [_component(c, f"components[{i}]", state, place) for i, c in enumerate(components)]
    if wrap:
        container: dict = {"type": CONTAINER, "components": built}
        if accent is not None:
            container["accent_color"] = accent
        if spoiler:
            container["spoiler"] = True
        built = [container]

    layout = Layout(components=built, warnings=state.warnings, attachments=state.attachments)
    layout.username, layout.avatar_url = _webhook_identity(data)
    for comp in _walk(built):
        layout.component_count += 1
        if comp["type"] == TEXT:
            layout.text_length += len(comp["content"])
        elif comp["type"] == GALLERY:
            layout.gallery_items += len(comp["items"])
    if layout.component_count > MAX_COMPONENTS:
        raise BuildError(
            f"This layout has {layout.component_count} components; Discord allows at most "
            f"{MAX_COMPONENTS} per message (every text, separator, button, thumbnail, row, "
            "section and container counts)."
        )
    if layout.text_length > MAX_TEXT_LENGTH:
        raise BuildError(
            f"This layout has {layout.text_length} characters of text; Discord allows at most "
            f"{MAX_TEXT_LENGTH} across all text components in one message."
        )
    return layout


def _webhook_identity(data: dict) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
    username = data.get("username")
    if username is not None:
        username = _str(username, "username").strip()
        if not 1 <= len(username) <= MAX_WEBHOOK_USERNAME:
            raise BuildError(f"username: must be 1-{MAX_WEBHOOK_USERNAME} characters.")
        lowered = username.lower()
        if "discord" in lowered or "clyde" in lowered or lowered in ("everyone", "here"):
            raise BuildError("username: Discord doesn't allow webhook names containing `discord` or `clyde`, or named `everyone`/`here`.")
    avatar_url = data.get("avatar_url")
    if avatar_url is not None:
        avatar_url = _str(avatar_url, "avatar_url", max_len=MAX_MEDIA_URL).strip()
        if avatar_url.split("://", 1)[0].lower() not in ("http", "https") or "://" not in avatar_url:
            raise BuildError("avatar_url: must be an `https://` image URL.")
    return username or None, avatar_url or None


def build_view(layout: Layout) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    try:
        for payload in layout.components:
            view.add_item(_component_to_item(_component_factory(payload)))
    except (ValueError, TypeError, KeyError) as exc:
        raise BuildError(f"discord.py rejected the layout: {exc}")
    return view


# ---------------------------------------------------------------------------
# Raw payload / messages -> friendly YAML
# ---------------------------------------------------------------------------
def _media_out(media: typing.Any) -> str:
    return (media or {}).get("url", "") if isinstance(media, dict) else str(media or "")


def to_friendly(comp: dict, warnings: typing.List[str]) -> typing.Optional[typing.Any]:
    """Convert one raw Discord component into the friendly YAML form.
    Returns None for components this cog cannot recreate (selects, interactive
    buttons) and records a warning."""
    ctype = comp.get("type")
    if ctype == TEXT:
        return {"type": "text", "content": comp.get("content", "")}
    if ctype == SEPARATOR:
        out: dict = {"type": "separator"}
        if comp.get("divider", True) is False:
            out["divider"] = False
        if comp.get("spacing", 1) == 2:
            out["spacing"] = "large"
        return out
    if ctype == SECTION:
        texts = []
        for child in comp.get("components", []):
            converted = to_friendly(child, warnings)
            if isinstance(converted, dict) and converted.get("type") == "text":
                converted = converted["content"]
            if converted is not None:
                texts.append(converted)
        out = {"type": "section", "components": texts}
        accessory = to_friendly(comp.get("accessory") or {}, warnings)
        if accessory is not None:
            out["accessory"] = accessory
        return out
    if ctype == THUMBNAIL:
        out = {"type": "thumbnail", "url": _media_out(comp.get("media"))}
        if comp.get("description"):
            out["description"] = comp["description"]
        if comp.get("spoiler"):
            out["spoiler"] = True
        return out
    if ctype == BUTTON:
        if comp.get("style") != LINK_STYLE or not comp.get("url"):
            warnings.append(f"Skipped an interactive button (`{comp.get('label') or comp.get('custom_id')}`); only link buttons can be recreated.")
            return None
        out = {"type": "button"}
        if comp.get("label"):
            out["label"] = comp["label"]
        out["url"] = comp["url"]
        if comp.get("emoji"):
            out["emoji"] = str(discord.PartialEmoji.from_dict(comp["emoji"]))
        if comp.get("disabled"):
            out["disabled"] = True
        return out
    if ctype == GALLERY:
        items = []
        for item in comp.get("items", []):
            url = _media_out(item.get("media"))
            if item.get("description") or item.get("spoiler"):
                entry: dict = {"url": url}
                if item.get("description"):
                    entry["description"] = item["description"]
                if item.get("spoiler"):
                    entry["spoiler"] = True
                items.append(entry)
            else:
                items.append(url)
        return {"type": "gallery", "items": items}
    if ctype == FILE:
        url = _media_out(comp.get("file"))
        if not url.startswith("attachment://"):
            name = comp.get("name") or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "file"
            url = f"attachment://{name}"
            warnings.append(f"The file `{name}` must be attached to your command when you re-send this layout.")
        out = {"type": "file", "file": url[len("attachment://"):]}
        if comp.get("spoiler"):
            out["spoiler"] = True
        return out
    if ctype == CONTAINER:
        out = {"type": "container"}
        if comp.get("accent_color") is not None:
            out["accent_color"] = f"#{comp['accent_color']:06X}"
        if comp.get("spoiler"):
            out["spoiler"] = True
        out["components"] = [c for c in (to_friendly(ch, warnings) for ch in comp.get("components", [])) if c is not None]
        return out
    if ctype == ACTION_ROW:
        buttons = [b for b in (to_friendly(ch, warnings) for ch in comp.get("components", [])) if b is not None]
        if not buttons:
            return None
        for b in buttons:
            b.pop("type", None)
        return {"type": "row", "buttons": buttons}
    if ctype in (3, 5, 6, 7, 8):
        warnings.append("Skipped a select menu; select menus need a bot handler and cannot be recreated.")
        return None
    warnings.append(f"Skipped an unsupported component (type {ctype}).")
    return None


def _drop_empty(components: list) -> list:
    """Containers/sections whose children were all skipped would be invalid."""
    kept = []
    for comp in components:
        if not isinstance(comp, dict):
            kept.append(comp)
            continue
        if comp.get("type") == "container":
            comp["components"] = _drop_empty(comp["components"])
            if not comp["components"]:
                continue
        if comp.get("type") == "section" and not comp.get("components"):
            continue
        kept.append(comp)
    return kept


def embed_to_friendly(embed: discord.Embed) -> dict:
    """Convert a classic embed into an equivalent container."""
    parts: typing.List[typing.Any] = []
    header = []
    if embed.author and embed.author.name:
        name = embed.author.name
        header.append(f"-# [{name}]({embed.author.url})" if embed.author.url else f"-# {name}")
    if embed.title:
        header.append(f"## [{embed.title}]({embed.url})" if embed.url else f"## {embed.title}")
    if embed.description:
        header.append(embed.description)
    head_text = "\n".join(header)
    if embed.thumbnail and embed.thumbnail.url and head_text:
        parts.append({"type": "section", "components": [head_text], "accessory": {"type": "thumbnail", "url": embed.thumbnail.url}})
    elif head_text:
        parts.append(head_text)
    if embed.fields:
        parts.append("\n\n".join(f"**{f.name}**\n{f.value}" for f in embed.fields))
    if embed.image and embed.image.url:
        parts.append({"type": "gallery", "items": [embed.image.url]})
    footer = []
    if embed.footer and embed.footer.text:
        footer.append(embed.footer.text)
    if embed.timestamp:
        footer.append(f"<t:{int(embed.timestamp.timestamp())}:f>")
    if footer:
        parts.append({"type": "separator"})
        parts.append("-# " + " • ".join(footer))
    container: dict = {"type": "container"}
    if embed.colour is not None:
        container["accent_color"] = f"#{embed.colour.value:06X}"
    container["components"] = parts or ["(empty embed)"]
    return container


def message_to_friendly(message: discord.Message) -> typing.Tuple[dict, typing.List[str]]:
    warnings: typing.List[str] = []
    components: typing.List[typing.Any] = []
    if message.flags.components_v2:
        for comp in message.components:
            converted = to_friendly(comp.to_dict(), warnings)
            if converted is not None:
                components.append(converted)
    else:
        if message.content:
            components.append({"type": "text", "content": message.content})
        for embed in message.embeds:
            if embed.type == "rich":
                components.append(embed_to_friendly(embed))
        for comp in message.components:
            converted = to_friendly(comp.to_dict(), warnings)
            if converted is not None:
                components.append(converted)
        if message.embeds:
            warnings.append("Embeds were converted to containers; check the result before posting it.")
    components = _drop_empty(components)
    result: dict = {}
    if message.webhook_id is not None:
        result["username"] = message.author.name
        if message.author.avatar is not None:
            result["avatar_url"] = message.author.avatar.url
    result["components"] = components
    urls = [c.get("url", "") for c in _walk_friendly(components)]
    if any("discordapp.com/attachments" in u or "discordapp.net/attachments" in u for u in urls):
        warnings.append(
            "Some media points at Discord attachment links, which expire. "
            "Re-host those images, or attach them and use `attachment://filename`."
        )
    return result, warnings


def _walk_friendly(components: list) -> typing.Iterator[dict]:
    for comp in components:
        if not isinstance(comp, dict):
            continue
        yield comp
        for item in comp.get("items", []):
            if isinstance(item, dict):
                yield item
            else:
                yield {"url": item}
        yield from _walk_friendly(comp.get("components", []) + comp.get("buttons", []))
        if isinstance(comp.get("accessory"), dict):
            yield comp["accessory"]


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _represent_str)


def dump_yaml(data: typing.Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)


# ---------------------------------------------------------------------------
# Link preview (component embed) export
# ---------------------------------------------------------------------------
@dataclass
class WebEmbed:
    payload: dict
    json_text: str
    inline_text: str
    json_bytes: int
    inline_bytes: int
    title: str


def _first_heading(container: dict) -> str:
    for comp in _walk([container]):
        if comp.get("type") == TEXT:
            for line in comp["content"].splitlines():
                line = re.sub(r"^(#{1,3}|-#)\s*", "", line.strip())
                line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
                line = line.replace("**", "").replace("__", "").strip("*_ ")
                if line:
                    return line[:100]
    return "Untitled"


def to_web_embed(layout: Layout) -> WebEmbed:
    """Validate a layout against the stricter component-embed rules and return
    the payload for a website's link preview."""
    if layout.attachments or layout.has(FILE):
        raise BuildError(
            "Link previews cannot use `file` components or `attachment://` media; "
            "every image must be a public `https://` URL Discord can fetch."
        )
    comps = layout.components
    if len(comps) == 1 and comps[0]["type"] == CONTAINER:
        container = comps[0]
    elif not any(c["type"] == CONTAINER for c in comps):
        container = {"type": CONTAINER, "components": comps}
    else:
        raise BuildError(
            "A link preview must be exactly one container. Merge your containers into one, "
            "or use a root `accent_color` to wrap everything."
        )
    total = layout.component_count + (0 if container is comps[0] else 1)
    if total > MAX_COMPONENTS:
        raise BuildError(f"A link preview allows at most {MAX_COMPONENTS} components including the container (got {total}).")
    if layout.gallery_items > MAX_GALLERY_ITEMS:
        raise BuildError(
            f"A link preview allows at most {MAX_GALLERY_ITEMS} gallery items across the whole embed "
            f"(got {layout.gallery_items})."
        )
    for comp in _walk([container]):
        if comp["type"] == BUTTON and comp["url"].lower().startswith("discord://"):
            raise BuildError("Link previews only allow `http`/`https` button URLs.")

    payload = {"component": container}
    json_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Inside an inline <script>, `<`, `>` and `&` must be escaped so the HTML
    # parser can't end the script early. Discord counts the escapes at full length.
    inline_text = json_text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    embed = WebEmbed(
        payload=payload,
        json_text=json_text,
        inline_text=inline_text,
        json_bytes=len(json_text.encode("utf-8")),
        inline_bytes=len(inline_text.encode("utf-8")),
        title=_first_heading(container),
    )
    if embed.json_bytes > WEBEMBED_MAX_BYTES:
        raise BuildError(
            f"The link preview payload is {embed.json_bytes} bytes; Discord allows at most "
            f"{WEBEMBED_MAX_BYTES}. Shorten the text or remove components."
        )
    return embed


def web_embed_html(embed: WebEmbed) -> str:
    title = html.escape(embed.title, quote=True)
    inline_ok = embed.inline_bytes <= WEBEMBED_MAX_BYTES
    script = (
        '<script id="discord:component-embed" type="application/json">\n'
        f"{embed.inline_text}\n"
        "</script>"
        if inline_ok
        else f"<!-- Inline version is {embed.inline_bytes} bytes after HTML escaping (limit "
        f"{WEBEMBED_MAX_BYTES}); use the linked JSON option below. -->"
    )
    return f"""<!--
  Discord component embed (link preview)
  Paste inside the <head> of the page. It must be in the server-rendered HTML;
  Discord does not run JavaScript. Check it with https://discord.com/developers/embeds
-->

<!-- Option 1: inline ({embed.inline_bytes}/{WEBEMBED_MAX_BYTES} bytes) -->
{script}

<!-- Option 2: linked JSON ({embed.json_bytes}/{WEBEMBED_MAX_BYTES} bytes).
     Upload component-embed.json to the same site (or a subdomain/parent domain)
     and use this instead of Option 1. -->
<!-- <link rel="discord:component-embed" type="application/json" href="https://example.com/component-embed.json"> -->

<!-- Open Graph fallback, shown when the component embed can't be. Fill these in. -->
<meta property="og:title" content="{title}" />
<meta property="og:description" content="" />
<meta property="og:image" content="" />
<meta property="og:url" content="" />
<meta property="og:type" content="website" />
<meta name="twitter:card" content="summary_large_image" />
"""
