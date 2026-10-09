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


# Display names for the bundle ids memmon_owners groups by (BROWSERS,
# SHELL_HOSTS, DEV_APPS, GUI_VM_APPS), so a name-only record (a history row's
# `apps`) lands in the same section as the owner list. The ids decide; the
# names only find the id.
APP_NAMES = {
    "com.apple.Safari": ("Safari",),
    "com.google.Chrome": ("Google Chrome", "Chrome"),
    "com.google.Chrome.canary": ("Google Chrome Canary", "Chrome Canary"),
    "com.brave.Browser": ("Brave Browser", "Brave"),
    "org.mozilla.firefox": ("Firefox",),
    "company.thebrowser.Browser": ("Arc",),
    "com.microsoft.edgemac": ("Microsoft Edge", "Edge"),
    "com.operasoftware.Opera": ("Opera",),
    "com.vivaldi.Vivaldi": ("Vivaldi",),
    "com.kagi.kagimacOS": ("Orion",),
    "app.zen-browser.zen": ("Zen", "Zen Browser"),
    "com.apple.Terminal": ("Terminal",),
    "com.googlecode.iterm2": ("iTerm2", "iTerm"),
    "com.mitchellh.ghostty": ("Ghostty",),
    "dev.warp.Warp-Stable": ("Warp",),
    "net.kovidgoyal.kitty": ("kitty",),
    "org.alacritty": ("Alacritty",),
    "com.github.wez.wezterm": ("WezTerm",),
    "com.microsoft.VSCode": ("Visual Studio Code", "VS Code", "Code"),
    "com.todesktop.230313mzl4w4u92": ("Cursor",),
    "dev.zed.Zed": ("Zed",),
    "com.apple.dt.Xcode": ("Xcode",),
    "com.docker.docker": ("Docker Desktop", "Docker"),
    "dev.kdrag0n.MacVirt": ("OrbStack",),
}
# JetBrains IDEs share the com.jetbrains. prefix that memmon_owners checks.
JETBRAINS_NAMES = ("IntelliJ IDEA", "PyCharm", "WebStorm", "GoLand", "CLion", "Rider",
                   "RubyMine", "PhpStorm", "DataGrip", "DataSpell", "RustRover", "Fleet",
                   "Aqua", "Writerside")
NAME_BUNDLE = {name: bid for bid, names in APP_NAMES.items() for name in names}
NAME_BUNDLE.update({name: "com.jetbrains." + name.lower().replace(" ", "-")
                    for name in JETBRAINS_NAMES})
