"""The permission envelope a scheduled run executes inside, written down.

Why this module exists
----------------------
A scheduled run has no human attached to it, so every gate that asks a human
("may I run this command?") is unanswerable.  Before this module that fact was
expressed as *absence*: the scheduler called ``handle_turn`` without a sink,
``_active_sink`` stayed ``None``, and every gate failed closed anonymously.
The envelope a task really ran under was therefore whatever the global config
happened to say, and nobody -- including the person who created the task --
could see it.  ``permission_profile`` was a stored string that one ``if``
branch in ``cli.py`` interpreted; adding a profile meant adding a branch, and
two places to keep in sync.

The fix is not a second permission system.  It is to write the envelope down
once, as a table: a profile names the config overrides it applies, so "which
profile is this?" and "what may this run do?" become the same question with
one answer.  The machinery underneath is the machinery that already existed --
``file_access.workspace.write``, ``permissions.shell_level`` and
``permissions.shell_sandbox`` -- because a second gate beside the existing one
is how the two come to disagree.

What is deliberately *not* here
-------------------------------
Consent.  A profile cannot grant a single approval; see
``agent.scheduler.unattended`` for why the grant side is intentionally empty.

Scale of the boundary, stated honestly
--------------------------------------
``shell_sandbox`` protects secrets (``~/.ssh`` and friends), code that gets
executed later (``~/.zshrc``, PATH directories), the agent's own home and
protected user data.  It does **not** confine writes to the workspace: writes
are open by default and those classes are carved out.  ``workspace.write``
below bounds the *file tools*; a shell command can still write outside the
workspace, and that is a property of the sandbox vocabulary, not of this
table.  Profiles whose copy claims "只能在项目内写入" would be lying.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PermissionProfile:
    """One selectable envelope for an unattended run.

    ``overrides_config`` False means the profile applies nothing and the run
    inherits the global configuration verbatim (the historical behaviour of
    ``inherit``).  The remaining fields are the overrides, and are ignored in
    that case -- they are still filled in so that the dataclass always
    describes a complete posture rather than a partial one.
    """

    key: str
    label: str
    summary: str
    detail: str
    overrides_config: bool
    workspace_write: bool
    shell_level: str
    shell_sandbox: str
    requires_workspace_root: bool

    def to_payload(self) -> dict[str, Any]:
        """Public shape for the API, so the UI never hardcodes the list."""
        data = asdict(self)
        data.pop("overrides_config", None)
        data.pop("requires_workspace_root", None)
        return data


#: Profiles in the order the UI should offer them: least authority first.
PERMISSION_PROFILES: dict[str, PermissionProfile] = {
    "inherit": PermissionProfile(
        key="inherit",
        label="继承全局权限",
        summary="不改动任何权限配置，与全局设置保持一致。",
        detail=(
            "运行前不施加任何覆盖。无人值守时，需要人工确认的动作会被拒绝，"
            "因此实际能做的通常只有读取、分析与产出报告。"
        ),
        overrides_config=False,
        workspace_write=False,
        shell_level="ask",
        shell_sandbox="read_all",
        requires_workspace_root=False,
    ),
    "read_only": PermissionProfile(
        key="read_only",
        label="强制只读",
        summary="不写工作区文件，高风险命令与管道重定向一律拒绝。",
        detail=(
            "工作区文件工具只读；shell 在 ask 档位运行，删除类命令、管道与重定向"
            "都会请求确认，而无人值守下确认必然被拒。"
            "注意：沙箱保护的是密钥、自启动脚本、Agent 自身配置与受保护的用户数据，"
            "并不把写入限制在工作区内，所以中低风险的命令仍可能在其它位置写入缓存。"
        ),
        overrides_config=True,
        workspace_write=False,
        shell_level="ask",
        shell_sandbox="read_all",
        requires_workspace_root=False,
    ),
    "workspace_write": PermissionProfile(
        key="workspace_write",
        label="可在项目内写入",
        summary="可在工作区内改文件、跑测试、生成报告。",
        detail=(
            "工作区文件工具可读写；shell 在 high 档位运行，不再就删除类命令、"
            "管道与重定向提问，因此能完成改文件、跑测试、出报告这一整条链路。"
            "边界仍由沙箱负责：密钥、自启动脚本、Agent 自身配置与受保护的用户数据"
            "不可读写，但写入并不因此被限制在工作区内。"
            "另外，Python 环境变更（pip / poetry 等）在任何档位都会被拒绝——"
            "无人值守的运行不应自行改写项目的依赖。"
        ),
        overrides_config=True,
        workspace_write=True,
        shell_level="high",
        shell_sandbox="read_all",
        requires_workspace_root=True,
    ),
}

#: Where an unrecognised profile key lands.  Failing closed to the profile
#: that can only read is the safe direction, and unlike falling back to
#: ``inherit`` it does not depend on what the global config happens to be.
FALLBACK_PROFILE_KEY = "read_only"

#: The profile an unnamed/blank value resolves to.  ``inherit`` keeps tasks
#: created before this module behaving exactly as they did.
DEFAULT_PROFILE_KEY = "inherit"


def resolve_permission_profile(key: Any) -> PermissionProfile:
    """Map a stored profile key onto a profile, failing closed when unknown.

    An unknown key is never passed through as-is: a typo, or a value written
    by a newer UI than this backend, must not silently widen what a run may
    do.  Callers that want to surface the substitution compare
    ``profile.key`` with the requested key.
    """
    normalized = str(key or "").strip()
    if not normalized:
        return PERMISSION_PROFILES[DEFAULT_PROFILE_KEY]
    return PERMISSION_PROFILES.get(
        normalized, PERMISSION_PROFILES[FALLBACK_PROFILE_KEY]
    )


def profile_payloads() -> list[dict[str, Any]]:
    """Profiles as API payloads, in offer order."""
    return [profile.to_payload() for profile in PERMISSION_PROFILES.values()]


def apply_profile_to_config(cfg: dict, profile: PermissionProfile) -> dict:
    """Return *cfg* with the profile's overrides applied.

    Pure: the input mapping is never mutated.  Nested sections are copied
    rather than replaced so a run-level override cannot leak back into the
    process-wide configuration the other channels are using.
    """
    resolved = dict(cfg)
    if not profile.overrides_config:
        return resolved
    file_access = dict(resolved.get("file_access") or {})
    workspace_access = dict(file_access.get("workspace") or {})
    workspace_access["read"] = True
    workspace_access["write"] = profile.workspace_write
    file_access["workspace"] = workspace_access
    resolved["file_access"] = file_access
    permissions = dict(resolved.get("permissions") or {})
    permissions["shell_level"] = profile.shell_level
    permissions["shell_sandbox"] = profile.shell_sandbox
    resolved["permissions"] = permissions
    return resolved


def describe_profile_for_prompt(profile: PermissionProfile) -> str:
    """One paragraph telling the model what this run may and may not do.

    Without this the model discovers the envelope by bumping into it, retries
    the same refused action, and spends the run's budget on a wall.  Stating
    the refusal up front is cheaper than any amount of error recovery.
    """
    if profile.overrides_config:
        posture = (
            f"This run may {'write inside the workspace' if profile.workspace_write else 'read but not write the workspace'}, "
            f"and its shell commands run at permission level `{profile.shell_level}` "
            f"with sandbox `{profile.shell_sandbox}`."
        )
    else:
        posture = "This run inherits the global permission configuration."
    return (
        "This is an unattended scheduled run: there is no human to approve "
        f"anything. Permission profile `{profile.key}` ({profile.label}). "
        f"{posture} Any action that would require human confirmation is "
        "refused automatically, and repeating it will be refused again -- "
        "work around it or report it as a blocker instead of retrying."
    )


__all__ = [
    "DEFAULT_PROFILE_KEY",
    "FALLBACK_PROFILE_KEY",
    "PERMISSION_PROFILES",
    "PermissionProfile",
    "apply_profile_to_config",
    "describe_profile_for_prompt",
    "profile_payloads",
    "resolve_permission_profile",
]
