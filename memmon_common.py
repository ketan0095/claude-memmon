"""Rules shared by memmon.py's legacy view and the owners modules, kept free
of their imports: the gate and the legacy collect() load this without
paying for libproc."""

from __future__ import annotations

import os
import re

CLAIM_SOCK_RE = re.compile(r"(\S+\.claim\.sock)")


def spare_is_idle(cmd: str) -> bool:
    """An unclaimed prewarm advertises itself on a .claim.sock; claiming it
    removes the socket. A claimed spare is a real session: never idle, never
    reclaimable."""
    m = CLAIM_SOCK_RE.search(cmd)
    return bool(m) and os.path.exists(m.group(1))
