"""Builds the Components V2 LayoutView for a configured Plutonium status embed."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import discord

from .api import SITE_BASE, GameServeClient, server_url

GAMES = ("T4", "T5", "T6")
GAME_DEFAULTS = {
    "T4": ("Plutonium T4", "Gameservers for Call of Duty: World at War on Plutonium"),
    "T5": ("Plutonium T5", "Gameservers for Call of Duty: Black Ops on Plutonium"),
    "T6": ("Plutonium T6", "Gameservers for Call of Duty: Black Ops II on Plutonium"),
}
EMOJI_KEYS = ("t4", "t5", "t6", "online", "full", "offline", "maintenance")
DEFAULT_EMOJIS = {
    "t4": "",
    "t5": "",
    "t6": "",
    "online": "\N{LARGE GREEN CIRCLE}",
    "full": "",  # empty -> falls back to the online emoji
    "offline": "\N{LARGE RED CIRCLE}",
    "maintenance": "\N{HAMMER AND WRENCH}\N{VARIATION SELECTOR-16}",
}

TEXT_LIMIT = 4000  # Discord's total text budget for a Components V2 message
_COLOR_CODE_RE = re.compile(r"\^[0-9:;]")


def game_from_api(game_id: Optional[str]) -> Optional[str]:
    """Map an API game tag (T4ZM, T6, ...) to a section key (T4/T5/T6)."""
    game_id = (game_id or "").upper()
    for game in GAMES:
        if game_id.startswith(game):
            return game
    return None


def clean_name(name: Optional[str]) -> str:
    """Strip Plutonium/Quake colour codes (^1, ^7, ...) and whitespace."""
    return _COLOR_CODE_RE.sub("", name or "").strip()


def _simple_brackets(text: str) -> bool:
    """True if [ ] are balanced and un-nested (safe inside masked-link text)."""
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
            if depth > 1:
                return False
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def md_escape(text: str) -> str:
    """
    Neutralise markdown in a server name with as little escaping as possible.

    Discord can display escaping backslashes literally (e.g. "\\|"), so a
    character is only touched when it could actually form markdown.
    """
    text = text.replace("\\", "\\\\")
    # Only "||" starts a spoiler; break runs of pipes with a zero-width space.
    text = re.sub(r"\|(?=\|)", "|​", text)
    # Emphasis/strike/code need a pair, so a single occurrence is harmless.
    for ch in "*_~`":
        if text.count(ch) >= 2:
            text = text.replace(ch, "\\" + ch)
    # Balanced brackets like "[HGM] Zombies" are fine inside link text.
    if "](" in text or not _simple_brackets(text):
        text = text.replace("[", "\\[").replace("]", "\\]")
    return text


def new_embed_config(channel_id: int) -> dict:
    return {
        "channel_id": channel_id,
        "message_id": None,
        "header": "",
        "accent_color": None,
        "show_links": True,
        "emojis": dict(DEFAULT_EMOJIS),
        "sections": {g: {"title": "", "description": ""} for g in GAMES},
        "servers": {},
    }


def new_server_config(game: str) -> dict:
    return {"game": game, "label": "", "note": "", "maintenance": False, "last_name": ""}


def display_name(address: str, cfg: dict, data: Optional[dict]) -> str:
    return (
        cfg.get("label")
        or clean_name((data or {}).get("name"))
        or cfg.get("last_name")
        or address
    )


# --------------------------------------------------------------------- compose


class _Server:
    __slots__ = ("address", "cfg", "state", "data", "name")

    def __init__(self, address: str, cfg: dict, api: GameServeClient):
        self.address = address
        self.cfg = cfg
        self.state, self.data = api.state(address)
        if cfg.get("maintenance"):
            self.state = "maintenance"
        self.name = display_name(address, cfg, self.data)

    @property
    def players(self) -> Tuple[Optional[int], Optional[int]]:
        players = (self.data or {}).get("players") or {}
        return players.get("current"), players.get("max")

    def block(self, emojis: dict, links: bool, notes: bool, details: bool) -> str:
        current, maximum = self.players
        if self.state == "online":
            full = bool(maximum) and current is not None and current >= maximum
            emoji = (emojis.get("full") if full else "") or emojis.get("online", "")
            if current is None:
                count = "players unknown"
            elif maximum:
                count = f"{current}/{maximum} players"
            else:
                count = f"{current} players"
        elif self.state == "maintenance":
            emoji, count = emojis.get("maintenance", ""), "maintenance"
        elif self.state == "offline":
            emoji, count = emojis.get("offline", ""), "offline"
        else:
            emoji, count = emojis.get("offline", ""), "status unknown"

        name = md_escape(self.name)
        if links:
            sid = (self.data or {}).get("id")
            name = f"[{name}]({server_url(self.address, sid)})"
        lines = [f"{emoji} {name} ({count})".strip()]

        if details and self.state == "online" and self.data:
            parts = [
                (self.data.get("map") or {}).get("display"),
                (self.data.get("mode") or {}).get("display"),
            ]
            rnd = self.data.get("zombiesRound")
            if self.data.get("isZombies") and rnd and current:
                parts.append(f"Round {rnd}")
            parts = [md_escape(str(p)) for p in parts if p]
            if parts:
                lines.append("-# " + " \N{MIDDLE DOT} ".join(parts))
        if notes and self.cfg.get("note"):
            lines.append(f"-# {self.cfg['note']}")
        return "\n".join(lines)


def _section_head(emb: dict, game: str) -> str:
    sect = (emb.get("sections") or {}).get(game) or {}
    title = sect.get("title") or GAME_DEFAULTS[game][0]
    desc = sect.get("description") or GAME_DEFAULTS[game][1]
    emoji = (emb.get("emojis") or {}).get(game.lower(), "")
    head = f"## {emoji} {title}" if emoji else f"## {title}"
    return f"{head}\n{desc}" if desc else head


def _compose(
    emb: dict, servers: Dict[str, List[_Server]], level: int
) -> List[Tuple[str, List[str]]]:
    """level 0: everything, 1: no links, 2: no notes, 3: no details lines."""
    emojis = {**DEFAULT_EMOJIS, **(emb.get("emojis") or {})}
    links = bool(emb.get("show_links", True)) and level < 1
    return [
        (
            _section_head(emb, game),
            [s.block(emojis, links, notes=level < 2, details=level < 3) for s in servers[game]],
        )
        for game in GAMES
        if servers.get(game)
    ]


def _section_text(head: str, blocks: List[str]) -> str:
    return f"{head}\n\n" + "\n".join(blocks) if blocks else head


def _total(header: str, sections, footer: str) -> int:
    return len(header) + len(footer) + sum(len(_section_text(h, b)) for h, b in sections)


def _footer(groups: Dict[str, List[_Server]], api: GameServeClient) -> str:
    every = [s for group in groups.values() for s in group]
    online = [s for s in every if s.state == "online"]
    playing = sum(s.players[0] or 0 for s in online)
    if api.last_success:
        updated = f"Updated <t:{int(api.last_success)}:R>"
    else:
        updated = "Waiting for first update"
    return (
        f"-# {len(online)}/{len(every)} online \N{MIDDLE DOT} {playing} playing "
        f"\N{MIDDLE DOT} {updated} \N{MIDDLE DOT} Data from [gameserve.rs]({SITE_BASE})"
    )


def build_view(emb: dict, api: GameServeClient, prefix: str = "[p]") -> discord.ui.LayoutView:
    groups: Dict[str, List[_Server]] = {g: [] for g in GAMES}
    for address, cfg in (emb.get("servers") or {}).items():
        game = cfg.get("game")
        if game in groups:
            groups[game].append(_Server(address, cfg, api))
    for group in groups.values():
        group.sort(key=lambda s: (s.name.casefold(), s.address))

    header = (emb.get("header") or "").strip()
    footer = _footer(groups, api)

    # Degrade gracefully if the content would exceed Discord's text budget.
    for level in range(4):
        sections = _compose(emb, groups, level)
        if _total(header, sections, footer) <= TEXT_LIMIT:
            break
    else:
        hidden = {}
        while _total(header, sections, footer) > TEXT_LIMIT - 200 and any(b for _, b in sections):
            idx = max(
                (i for i in range(len(sections)) if sections[i][1]),
                key=lambda i: len(_section_text(*sections[i])),
            )
            sections[idx][1].pop()
            hidden[idx] = hidden.get(idx, 0) + 1
        for idx, count in hidden.items():
            sections[idx][1].append(f"-# \N{HORIZONTAL ELLIPSIS}and {count} more")

    children: List[discord.ui.Item] = []
    if header:
        children += [discord.ui.TextDisplay(header), discord.ui.Separator()]
    if sections:
        for i, (head, blocks) in enumerate(sections):
            if i:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(_section_text(head, blocks)))
    else:
        children.append(
            discord.ui.TextDisplay(
                f"No gameservers configured yet. Add one with `{prefix}pluto add`."
            )
        )
    children += [discord.ui.Separator(), discord.ui.TextDisplay(footer)]

    accent = emb.get("accent_color")
    container = (
        discord.ui.Container(*children, accent_color=discord.Color(accent))
        if accent is not None
        else discord.ui.Container(*children)
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view
