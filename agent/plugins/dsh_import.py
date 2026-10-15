"""Adopt another application's profile as **a plugin this runtime already loads**.

The read half (``agent/plugins/dsh.py``) has existed for a while and never executes
anything: it turns a DSH profile directory into this runtime's terms, with the declarations
it cannot express listed under ``unsupported`` and the reasons attached.  What was missing
was the adoption half, and it was blocked on consent -- "may I run the stdio commands this
file declares" had no answer anyone could give once and have remembered.

After the provenance and digest steps it needs no new concept.  So this module does the
*boring* thing on purpose: it writes a native plugin directory (``plugin.json`` with
``mcpServers``), which the loader already knows how to read, contribute and attribute.  The
alternative -- teaching the loader a second way to receive MCP servers -- would mean two
paths to keep in step, and the profile's servers would be the ones nobody's provenance
report could place.

Nothing here asks for consent or writes to disk: :func:`dsh_import_report` describes, and
:func:`write_imported_plugin` materialises a plan that a caller has already had approved.
The ask lives with the tool layer, which is where a sink can be reached.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent.plugins.dsh import DshProfile, read_dsh_profile
from agent.plugins.report import CapabilityReport
from agent.runtime.provenance import IMPORT, Execution, Provenance, digest_of

__all__ = ["DshImport", "plan_dsh_import", "write_imported_plugin", "imported_plugin_name"]

#: A plugin directory name the loader will accept (``catalog._SAFE_PLUGIN_NAME``), and one a
#: person can recognise as "where my DSH profile's servers went".
_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass(frozen=True)
class DshImport:
    """What importing one profile would do, and what it would not."""

    profile: DshProfile
    report: CapabilityReport
    #: The directory the plugin would be written to.
    destination: Path
    #: The server declarations, in the shape the loader reads from ``plugin.json``.
    servers: tuple[dict, ...] = ()


def imported_plugin_name(profile_dir: Path | str) -> str:
    """A safe plugin name for one profile directory.

    Prefixed, because the profile directory is usually named for the *application*
    (``desktop``, ``default``) and an imports-into-your-plugin-tree path should not be
    able to collide with a plugin the user installed by name.
    """
    stem = _NAME_UNSAFE.sub("-", Path(profile_dir).expanduser().name).strip("-")
    return f"dsh-{stem or 'profile'}"


def plan_dsh_import(
    profile_dir: Path | str,
    *,
    destination: Path | None = None,
) -> DshImport:
    """Read *profile_dir* and describe adopting it, without adopting anything.

    The report's ``would_run`` is one :class:`Execution` per **server**, not one for the
    profile: a person being asked about three servers is answering about three things, and
    a digest that treated the set as one blob would not tell them which one changed.
    """
    profile = read_dsh_profile(profile_dir)
    servers = tuple(
        {
            "name": str(server.get("name") or ""),
            "command": server.get("command"),
            "args": list(server.get("args") or ()),
            "env": dict(server.get("env") or {}),
        }
        for server in profile.mcp_servers
        if server.get("command")
    )
    executions = tuple(
        Execution(
            kind="mcp-stdio",
            payload={
                # The marker is the server, because for a profile there is no file per
                # server: the digest has to change when one of them is edited, and the name
                # is what identifies which.
                "marker": f"mcp:{server['name'] or 'server'}",
                "command": server["command"],
                "args": server["args"],
                "env": server["env"],
            },
        )
        for server in servers
    )
    return DshImport(
        profile=profile,
        report=CapabilityReport(
            source=Provenance(source=str(profile.path), kind=IMPORT),
            would_run=executions,
            digest=digest_of(executions),
            # Carried through rather than dropped: these are the declarations the profile
            # makes that this runtime cannot honour, and a caller that adopts the rest owes
            # the person an account of them.
            unsupported=profile.unsupported,
        ),
        destination=(
            Path(destination)
            if destination is not None
            else Path(profile.path)
        ),
        servers=servers,
    )


def write_imported_plugin(plan: DshImport, *, plugins_dir: Path) -> Path:
    """Materialise *plan* as a native plugin directory; return where it went.

    Refuses to overwrite: a name that already exists is either another import (which the
    user should be told about) or a plugin they wrote, and clobbering either silently is
    not a thing an import should be able to do.  The check-then-write is deliberately not a
    transaction -- the directory is written under a temporary name and moved into place, so
    a failure halfway leaves no half-plugin for the loader to read next start.
    """
    target = Path(plugins_dir) / imported_plugin_name(plan.profile.path)
    if target.exists():
        raise FileExistsError(
            f"{target} already exists; remove it or choose another destination"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.importing")
    if staging.exists():
        raise FileExistsError(f"{staging} is left over from an interrupted import")
    staging.mkdir(parents=True)
    try:
        (staging / "plugin.json").write_text(
            json.dumps(
                {
                    "name": target.name,
                    "description": f"Imported from the DSH profile at {plan.profile.path}",
                    # A **list** of server dicts: that is the shape
                    # `_normalize_mcp_servers` reads without guessing, and the servers are
                    # already in it.
                    "mcpServers": list(plan.servers),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        staging.rename(target)
    except Exception:
        # Leave nothing behind: a directory the loader would try to read next start is
        # worse than a failed import.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
