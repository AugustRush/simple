"""Masking provider credentials on the way to the browser, and restoring them on the way back.

Two halves of one rule, and the reason they are a module rather than two helpers: the settings
page round-trips the whole provider block, so masking that is not exactly symmetric with
restoring writes ``******`` over a working credential.  The first half hides ``api_key`` and any
header whose name reads like a credential; the second half puts the real values back when the
UI saves the placeholder it was shown.

Extracted from ``agent/channels/web.py`` and re-imported there, so the callers and the guard
tests keep the path they have always used.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "_SECRET_HEADER_RE",
    "_mask_api_keys",
    "_restore_masked_api_keys",
]


_SECRET_HEADER_RE = re.compile(r"authorization|cookie|(^|[-_])key$|key[-_]|token|secret")


def _mask_api_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cfg`` with provider credentials masked for display.

    ``api_key`` and any header whose name reads like a credential: the settings
    page round-trips the whole provider block, so a token that happens to be
    spelled as a header would otherwise be handed back to the browser in the
    clear.  The masking must be symmetric with :func:`_restore_masked_api_keys`
    or a save would write ``******`` over a working credential.
    """
    import copy

    masked = copy.deepcopy(cfg)
    providers = masked.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            if provider.get("api_key"):
                provider["api_key"] = "******"
            headers = provider.get("headers")
            if isinstance(headers, dict):
                for name, value in headers.items():
                    if value and _SECRET_HEADER_RE.search(str(name).lower()):
                        headers[name] = "******"
    return masked


def _restore_masked_api_keys(new_cfg: dict[str, Any], current_cfg: dict[str, Any]) -> None:
    """Keep existing credentials when the UI saved the masked placeholder back."""
    providers = new_cfg.get("providers")
    current_providers = current_cfg.get("providers")
    if not isinstance(providers, dict) or not isinstance(current_providers, dict):
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        current = current_providers.get(name)
        if provider.get("api_key") == "******":
            if isinstance(current, dict) and current.get("api_key"):
                provider["api_key"] = current["api_key"]
        headers = provider.get("headers")
        current_headers = current.get("headers") if isinstance(current, dict) else None
        if isinstance(headers, dict) and isinstance(current_headers, dict):
            for header_name, value in headers.items():
                if value == "******" and current_headers.get(header_name):
                    headers[header_name] = current_headers[header_name]



