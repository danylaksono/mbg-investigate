"""The one User-Agent every stage sends. Set MBG_USER_AGENT to add your contact
details; Wikimedia and the news sites we crawl are entitled to know who is asking."""

from __future__ import annotations

import os

DEFAULT_UA = "mbg-investigate/0.2 (academic research crawler; set MBG_USER_AGENT to add contact details)"


def user_agent() -> str:
    return os.environ.get("MBG_USER_AGENT", DEFAULT_UA)
