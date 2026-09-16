"""Console command policy: refuse dangerous commands even if they were queued."""

from __future__ import annotations

import re

BLOCKED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("winget", re.compile(r"winget\s+(upgrade|install|remove)", re.I)),
    ("apt", re.compile(r"apt(-get)?\s+(install|upgrade|remove|purge|dist-upgrade)", re.I)),
    ("dnf", re.compile(r"dnf\s+(install|upgrade|update|remove)", re.I)),
    ("yum", re.compile(r"yum\s+(install|upgrade|update|remove)", re.I)),
    ("rm-rf", re.compile(r"rm\s+-rf", re.I)),
    ("format", re.compile(r"format\s+[a-z]:", re.I)),
    ("del", re.compile(r"del\s+/[fqs]", re.I)),
    ("shutdown", re.compile(r"\bshutdown\b", re.I)),
    ("reboot", re.compile(r"\breboot\b", re.I)),
    ("passwd", re.compile(r"\bpasswd\b", re.I)),
    ("user-mgmt", re.compile(r"useradd|userdel|usermod", re.I)),
    ("net-user", re.compile(r"net\s+user", re.I)),
    ("net-localgroup", re.compile(r"net\s+localgroup", re.I)),
    ("reg", re.compile(r"reg\s+(delete|add|import|export)", re.I)),
    ("agent-stop", re.compile(r"__ADT_AGENT_STOP__", re.I)),
    ("curl-pipe", re.compile(r"curl\s+.*\|\s*(bash|sh|python)", re.I)),
    ("wget-pipe", re.compile(r"wget\s+.*\|\s*(bash|sh|python)", re.I)),
    ("encoded-ps", re.compile(r"powershell.*EncodedCommand", re.I)),
    ("base64-d", re.compile(r"base64\s+-d", re.I)),
    ("eval", re.compile(r"eval\s*\(", re.I)),
    ("exec", re.compile(r"exec\s*\(", re.I)),
    ("rundll32", re.compile(r"\brundll32\b", re.I)),
    ("diskpart", re.compile(r"\bdiskpart\b", re.I)),
    ("bcdedit", re.compile(r"\bbcdedit\b", re.I)),
    ("takeown", re.compile(r"\btakeown\b", re.I)),
    ("icacls", re.compile(r"\bicacls\b", re.I)),
    ("wusa", re.compile(r"\bwusa\b", re.I)),
    ("iex", re.compile(r"(invoke-expression|\biex\b)", re.I)),
]


def is_blocked_command(cmd: str) -> str | None:
    text = (cmd or "").strip()
    if not text:
        return None
    for name, pattern in BLOCKED_PATTERNS:
        if pattern.search(text):
            return name
    return None


def command_age_expired(created_at: str | None, max_age_s: int = 3600) -> bool:
    if not created_at:
        return False
    try:
        from datetime import datetime, timezone

        text = created_at.replace("Z", "+00:00")
        enqueued = datetime.fromisoformat(text)
        if enqueued.tzinfo is None:
            enqueued = enqueued.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - enqueued).total_seconds()
        return age > max_age_s
    except Exception:
        return False
