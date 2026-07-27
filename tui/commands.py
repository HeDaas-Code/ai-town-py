"""Parse CommandBar text into typed Command variants.

Grammar:
- ``<int>``                 -> JumpById(index)
- ``:jump <name>``          -> JumpByName(name)
- ``:lock``                 -> Lock
- ``:unlock``               -> Unlock
- ``:list``                 -> ListCmd
- ``:help``                 -> Help
- ``:quit``                 -> Quit
- anything else             -> Unknown(text)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JumpById:
    index: int


@dataclass
class JumpByName:
    name: str


@dataclass(frozen=True)
class Lock:
    pass


@dataclass(frozen=True)
class Unlock:
    pass


@dataclass(frozen=True)
class ListCmd:
    pass


@dataclass(frozen=True)
class Help:
    pass


@dataclass(frozen=True)
class Quit:
    pass


@dataclass
class Unknown:
    text: str


def parse_command(text: str):
    text = text.strip()
    if not text:
        return Unknown(text)
    # Numeric jump
    if text.isdigit():
        return JumpById(index=int(text))
    # Colon command
    if text.startswith(":"):
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return Unknown(text)
        keyword = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if keyword == "jump":
            if not arg:
                return Unknown(text)
            return JumpByName(name=arg)
        if keyword == "lock":
            return Lock()
        if keyword == "unlock":
            return Unlock()
        if keyword == "list":
            return ListCmd()
        if keyword == "help":
            return Help()
        if keyword == "quit":
            return Quit()
    return Unknown(text)
