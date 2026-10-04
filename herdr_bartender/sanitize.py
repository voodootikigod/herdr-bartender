"""Title/agent sanitization and canonical pane-ID normalization."""

from __future__ import annotations

from .config import AGENT_NAME_OVERRIDES, CONTROL_REGEX, CSI_REGEX, DCS_REGEX, OSC_REGEX


def get_hex_pane_id(raw_id: str | None) -> str:
    if not raw_id:
        return ""
    return str(raw_id).encode("utf-8").hex()


def sanitize_title(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw)
    text = OSC_REGEX.sub('', text)
    text = DCS_REGEX.sub('', text)
    text = CSI_REGEX.sub('', text)
    text = CONTROL_REGEX.sub('', text)
    return text.strip()[:120]


def format_agent_name(raw_agent: str | None) -> str:
    if not raw_agent or not raw_agent.strip():
        return "Herdr"
    clean = raw_agent.strip().lower()
    pretty = AGENT_NAME_OVERRIDES.get(clean, clean.capitalize())
    return f"{pretty} (Herdr)"


def normalize_pane_id(raw_pane_id: str, workspace_id: str | None) -> str:
    if ":" in raw_pane_id:
        return raw_pane_id
    if not workspace_id:
        return ""
    return f"{workspace_id}:{raw_pane_id}"
