from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from typing import Any, Optional

import agent as agent_module
from agent.tools.runtime import ToolRegistry

CONSOLE = agent_module.CONSOLE
_atomic_write_text = agent_module._atomic_write_text
DEFAULT_OUTPUT_DIR = agent_module.DEFAULT_OUTPUT_DIR


def _datestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

@dataclass
class SkillBundle:
    id: str
    name: str
    description: str
    path: Path
    source: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    supporting_files: list[str] = field(default_factory=list)
    user_invocable: bool = True
    disable_model_invocation: bool = False


@dataclass
class ExplicitSkillRequest:
    skill_ref: str
    remaining_text: str = ""


def _parse_frontmatter_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if value[0] in ('"', "'"):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value.strip("'\"")
    if value[0] in "[{(":
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text.strip()

    lines = text.splitlines()
    try:
        closing_index = lines[1:].index("---") + 1
    except ValueError:
        return {}, text.strip()

    metadata: dict[str, Any] = {}
    for line in lines[1:closing_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        metadata[key.strip()] = _parse_frontmatter_value(raw_value)
    body = "\n".join(lines[closing_index + 1 :]).strip()
    return metadata, body


def parse_explicit_skill_request(text: str) -> Optional[ExplicitSkillRequest]:
    stripped = text.strip()
    if not stripped:
        return None

    slash_match = re.match(r"^/skill\s+([^\s]+)(?:\s+(.*))?$", stripped, re.IGNORECASE)
    if slash_match:
        return ExplicitSkillRequest(
            skill_ref=slash_match.group(1),
            remaining_text=(slash_match.group(2) or "").strip(),
        )

    slash_direct = re.match(r"^/([^\s/][^\s]*)(?:\s+(.*))?$", stripped)
    if slash_direct:
        return ExplicitSkillRequest(
            skill_ref=slash_direct.group(1),
            remaining_text=(slash_direct.group(2) or "").strip(),
        )

    natural_match = re.match(
        r"^(?:please\s+)?(?:use|activate|run)\s+([^\s,.:：，]+)(?:\s+(.*))?$",
        stripped,
        re.IGNORECASE,
    )
    if natural_match:
        return ExplicitSkillRequest(
            skill_ref=natural_match.group(1),
            remaining_text=(natural_match.group(2) or "").strip(),
        )

    chinese_match = re.match(
        r"^(?:请)?(?:使用|启用)\s*([^\s,.:：，]+)(?:\s+(.*))?$",
        stripped,
    )
    if chinese_match:
        return ExplicitSkillRequest(
            skill_ref=chinese_match.group(1),
            remaining_text=(chinese_match.group(2) or "").strip(),
        )

    return None


def prepare_user_message_for_skills(
    user_message: str, skill_catalog: SkillCatalog
) -> tuple[str, list[str]]:
    parsed = parse_explicit_skill_request(user_message)
    if parsed is None:
        return user_message, []
    bundle = skill_catalog.get(parsed.skill_ref)
    if bundle is None or not bundle.user_invocable:
        return user_message, []
    normalized = parsed.remaining_text.strip()
    if not normalized:
        normalized = (
            f"The user explicitly requested the skill '{bundle.id}'. "
            "Activate it and briefly explain how you will apply it."
        )
    return normalized, [bundle.id]


class SkillCatalog:
    """Load skill bundles from user and built-in skill directories."""

    def __init__(
        self, user_root: Optional[Path] = None, builtin_root: Optional[Path] = None
    ):
        self.user_root = user_root or agent_module.SKILLS_DIR
        self.builtin_root = builtin_root or agent_module.BUILTIN_SKILLS_DIR
        self._skills: dict[str, SkillBundle] = {}
        self._aliases: dict[str, str] = {}
        self._registry: Optional[ToolRegistry] = None
        self._dirty: bool = False

    def load_all(self) -> None:
        self.user_root.mkdir(parents=True, exist_ok=True)
        self._skills.clear()
        self._aliases.clear()
        self._load_root(self.builtin_root, source="builtin")
        self._load_root(self.user_root, source="user")

    def _load_root(self, root: Path, *, source: str) -> None:
        if not root.exists():
            return
        for skill_file in sorted(root.rglob("SKILL.md")):
            bundle = self._read_bundle(skill_file, root=root, source=source)
            if bundle is None:
                continue
            self._skills[bundle.id] = bundle
        self._rebuild_aliases()

    def _read_bundle(
        self, skill_file: Path, *, root: Path, source: str
    ) -> Optional[SkillBundle]:
        try:
            raw_text = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            CONSOLE.print(f"[yellow]Failed to read skill {skill_file}: {e}[/yellow]")
            return None

        metadata, body = parse_skill_markdown(raw_text)
        bundle_dir = skill_file.parent
        bundle_id = bundle_dir.relative_to(root).as_posix()
        if not bundle_id or bundle_id == ".":
            bundle_id = bundle_dir.name
        supporting_files = sorted(
            p.relative_to(bundle_dir).as_posix()
            for p in bundle_dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        return SkillBundle(
            id=bundle_id,
            name=str(metadata.get("name") or bundle_dir.name),
            description=str(metadata.get("description") or ""),
            path=bundle_dir,
            source=source,
            body=body,
            metadata=metadata,
            supporting_files=supporting_files,
            user_invocable=bool(metadata.get("user-invocable", True)),
            disable_model_invocation=bool(
                metadata.get("disable-model-invocation", False)
            ),
        )

    def _rebuild_aliases(self) -> None:
        self._aliases.clear()
        counts: dict[str, int] = {}
        for skill_id in self._skills:
            leaf = skill_id.rsplit("/", 1)[-1]
            counts[leaf] = counts.get(leaf, 0) + 1
        for skill_id, bundle in self._skills.items():
            self._aliases[skill_id] = skill_id
            leaf = skill_id.rsplit("/", 1)[-1]
            if counts.get(leaf, 0) == 1:
                self._aliases[leaf] = skill_id
            self._aliases[bundle.name] = skill_id

    def reload(self) -> None:
        self.load_all()
        self._dirty = True

    def consume_dirty(self) -> bool:
        """Return True and clear if the catalog was mutated since last check."""
        if self._dirty:
            self._dirty = False
            return True
        return False

    def get(self, skill_ref: str) -> Optional[SkillBundle]:
        resolved = self.resolve_ref(skill_ref)
        if resolved is None:
            return None
        return self._skills.get(resolved)

    def resolve_ref(self, skill_ref: str) -> Optional[str]:
        ref = skill_ref.strip()
        if not ref:
            return None
        if ref in self._skills:
            return ref
        return self._aliases.get(ref)

    def list_skills(self) -> list[SkillBundle]:
        return [self._skills[key] for key in sorted(self._skills)]

    def summary_lines(self) -> list[str]:
        if not self._skills:
            return []
        lines = [
            "## Available Skills",
            "Available skills:",
            "Skills are instruction bundles loaded on demand. Use activate_skill only when a skill is relevant.",
        ]
        for bundle in self.list_skills():
            lines.append(
                "- "
                f"{bundle.id} ({bundle.source}; user-invocable={'yes' if bundle.user_invocable else 'no'}; "
                f"model-invocable={'no' if bundle.disable_model_invocation else 'yes'}): "
                f"{bundle.description or 'No description'}"
            )
        return lines

    @staticmethod
    def _bundle_root_label(bundle: SkillBundle) -> str:
        if bundle.source == "user":
            return str(bundle.path)
        if bundle.source == "builtin":
            return f"builtin://{bundle.id}"
        if bundle.source.startswith("plugin:"):
            plugin_name = bundle.source.split(":", 1)[1] or "plugin"
            return f"plugin://{plugin_name}/{bundle.id}"
        return f"{bundle.source}://{bundle.id}"

    def register_tools(self, registry: ToolRegistry) -> None:
        self._registry = registry

        async def activate_skill(skill_name: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            if bundle.disable_model_invocation:
                return {
                    "ok": False,
                    "error": f"Skill '{bundle.id}' cannot be activated by the model",
                }
            return self._activation_payload(bundle, registry=registry)

        def list_skill_files(skill_name: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            return {
                "ok": True,
                "skill": bundle.id,
                "bundle_root": self._bundle_root_label(bundle),
                "files": bundle.supporting_files,
            }

        def read_skill_file(skill_name: str, path: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            rel_path = Path(path)
            if rel_path.is_absolute():
                return {
                    "ok": False,
                    "error": "Skill file paths must be relative to the skill bundle",
                }
            target = (bundle.path / rel_path).resolve(strict=False)
            if target != bundle.path and bundle.path not in target.parents:
                return {"ok": False, "error": "Requested path escapes the skill bundle"}
            if not target.exists() or not target.is_file():
                return {"ok": False, "error": f"Skill file '{path}' not found"}
            return {
                "ok": True,
                "skill": bundle.id,
                "path": rel_path.as_posix(),
                "bundle_root": self._bundle_root_label(bundle),
                "content": target.read_text(encoding="utf-8"),
            }

        registry.register(
            "activate_skill",
            "Load a skill bundle's full instructions and supporting-file index.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    }
                },
                "required": ["skill_name"],
            },
            activate_skill,
            replace=True,
            source="runtime:skill",
        )
        registry.register(
            "list_skill_files",
            "List supporting files inside a skill bundle.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    }
                },
                "required": ["skill_name"],
            },
            list_skill_files,
            replace=True,
            source="runtime:skill",
        )
        registry.register(
            "read_skill_file",
            "Read a supporting file from a skill bundle.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the skill bundle",
                    },
                },
                "required": ["skill_name", "path"],
            },
            read_skill_file,
            replace=True,
            source="runtime:skill",
        )

        # ── Skill management tools ───────────────────────────────────────────

        def _validate_skill_id(skill_id: str) -> Optional[str]:
            """Return an error message if skill_id is invalid, else None."""
            if not skill_id or not skill_id.strip():
                return "Skill ID must not be empty"
            if re.search(r"[^a-zA-Z0-9/_\-]", skill_id):
                return "Skill ID may only contain alphanumerics, '/', '-', and '_'"
            if skill_id.startswith("/") or skill_id.endswith("/"):
                return "Skill ID must not start or end with '/'"
            if ".." in skill_id:
                return "Skill ID must not contain '..'"
            return None

        def _compose_skill_md(
            name: str,
            description: str,
            instructions: str,
            user_invocable: bool = True,
            disable_model_invocation: bool = False,
        ) -> str:
            lines = ["---"]
            lines.append(f"name: {name}")
            if description:
                lines.append(f"description: {description}")
            lines.append(f"user-invocable: {'true' if user_invocable else 'false'}")
            lines.append(
                f"disable-model-invocation: {'true' if disable_model_invocation else 'false'}"
            )
            lines.append("---")
            lines.append("")
            lines.append(instructions)
            return "\n".join(lines)

        def create_skill(
            skill_id: str,
            name: str,
            description: str = "",
            instructions: str = "",
            user_invocable: bool = True,
            disable_model_invocation: bool = False,
        ) -> dict[str, Any]:
            err = _validate_skill_id(skill_id)
            if err:
                return {"ok": False, "error": err}
            bundle_dir = self.user_root / skill_id
            skill_file = bundle_dir / "SKILL.md"
            if skill_file.exists():
                return {
                    "ok": False,
                    "error": f"Skill '{skill_id}' already exists at {bundle_dir}. Use update_skill to modify it.",
                }
            try:
                bundle_dir.mkdir(parents=True, exist_ok=True)
                content = _compose_skill_md(
                    name=name,
                    description=description,
                    instructions=instructions,
                    user_invocable=user_invocable,
                    disable_model_invocation=disable_model_invocation,
                )
                skill_file.write_text(content, encoding="utf-8")
                self.reload()
                bundle = self.get(skill_id)
                return {
                    "ok": True,
                    "skill_id": skill_id,
                    "path": str(bundle_dir),
                    "message": f"Skill '{skill_id}' created successfully",
                    "skill": {
                        "id": bundle.id,
                        "name": bundle.name,
                        "description": bundle.description,
                    }
                    if bundle
                    else None,
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to create skill: {e}"}

        def update_skill(
            skill_id: str,
            name: Optional[str] = None,
            description: Optional[str] = None,
            instructions: Optional[str] = None,
            user_invocable: Optional[bool] = None,
            disable_model_invocation: Optional[bool] = None,
        ) -> dict[str, Any]:
            bundle = self.get(skill_id)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_id}' not found"}
            if bundle.source != "user":
                return {
                    "ok": False,
                    "error": (
                        f"Skill '{bundle.id}' is a built-in skill and cannot be modified. "
                        "Create a user skill with the same ID to override it."
                    ),
                }
            skill_file = bundle.path / "SKILL.md"
            if not skill_file.exists():
                return {"ok": False, "error": f"SKILL.md not found at {skill_file}"}
            try:
                final_name = name if name is not None else bundle.name
                final_desc = (
                    description if description is not None else bundle.description
                )
                final_body = instructions if instructions is not None else bundle.body
                final_user_inv = (
                    user_invocable
                    if user_invocable is not None
                    else bundle.user_invocable
                )
                final_disable_model = (
                    disable_model_invocation
                    if disable_model_invocation is not None
                    else bundle.disable_model_invocation
                )
                content = _compose_skill_md(
                    name=final_name,
                    description=final_desc,
                    instructions=final_body,
                    user_invocable=final_user_inv,
                    disable_model_invocation=final_disable_model,
                )
                skill_file.write_text(content, encoding="utf-8")
                self.reload()
                updated = self.get(bundle.id)
                return {
                    "ok": True,
                    "skill_id": bundle.id,
                    "path": str(bundle.path),
                    "message": f"Skill '{bundle.id}' updated successfully",
                    "skill": {
                        "id": updated.id,
                        "name": updated.name,
                        "description": updated.description,
                    }
                    if updated
                    else None,
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to update skill: {e}"}

        def delete_skill(skill_id: str) -> dict[str, Any]:
            bundle = self.get(skill_id)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_id}' not found"}
            if bundle.source != "user":
                return {
                    "ok": False,
                    "error": f"Skill '{bundle.id}' is a built-in skill and cannot be deleted",
                }
            try:
                bundle_dir = bundle.path
                shutil.rmtree(bundle_dir)
                self.reload()
                return {
                    "ok": True,
                    "skill_id": bundle.id,
                    "path": str(bundle_dir),
                    "message": f"Skill '{bundle.id}' deleted successfully",
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to delete skill: {e}"}

        def write_skill_file(
            skill_name: str, path: str, content: str
        ) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            if bundle.source != "user":
                return {
                    "ok": False,
                    "error": f"Skill '{bundle.id}' is a built-in skill and cannot be modified",
                }
            rel_path = Path(path)
            if rel_path.is_absolute():
                return {
                    "ok": False,
                    "error": "Skill file paths must be relative to the skill bundle",
                }
            target = (bundle.path / rel_path).resolve(strict=False)
            if target != bundle.path and bundle.path not in target.parents:
                return {"ok": False, "error": "Requested path escapes the skill bundle"}
            if target.name == "SKILL.md":
                return {
                    "ok": False,
                    "error": "Use update_skill to modify SKILL.md, not write_skill_file",
                }
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                self.reload()
                return {
                    "ok": True,
                    "skill": bundle.id,
                    "path": rel_path.as_posix(),
                    "message": f"File '{rel_path.as_posix()}' written to skill '{bundle.id}'",
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to write skill file: {e}"}

        registry.register(
            "create_skill",
            "Create a new user skill bundle with SKILL.md entrypoint.",
            {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": (
                            "Unique ID for the skill, using '/' for nesting "
                            "(e.g., 'code-review', 'quality/lint')"
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the skill",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description of the skill",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Instruction body for SKILL.md (the skill's behavior when activated)",
                    },
                    "user_invocable": {
                        "type": "boolean",
                        "description": "Whether the user can explicitly invoke this skill (default: true)",
                    },
                    "disable_model_invocation": {
                        "type": "boolean",
                        "description": "Whether to prevent the model from auto-activating (default: false)",
                    },
                },
                "required": ["skill_id", "name"],
            },
            create_skill,
            replace=True,
            source="runtime:skill",
        )

        registry.register(
            "update_skill",
            "Update an existing user skill's metadata or instructions. Only user skills can be modified.",
            {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill ID, leaf name, or display name of the skill to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New display name (omit to keep current)",
                    },
                    "description": {
                        "type": "string",
                        "description": "New one-line description (omit to keep current)",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "New instruction body for SKILL.md (omit to keep current)",
                    },
                    "user_invocable": {
                        "type": "boolean",
                        "description": "Whether the user can invoke this skill (omit to keep current)",
                    },
                    "disable_model_invocation": {
                        "type": "boolean",
                        "description": "Whether to prevent model auto-activation (omit to keep current)",
                    },
                },
                "required": ["skill_id"],
            },
            update_skill,
            replace=True,
            source="runtime:skill",
        )

        registry.register(
            "delete_skill",
            "Delete a user skill bundle. Built-in skills cannot be deleted.",
            {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill ID, leaf name, or display name of the skill to delete",
                    },
                },
                "required": ["skill_id"],
            },
            delete_skill,
            replace=True,
            source="runtime:skill",
        )

        registry.register(
            "write_skill_file",
            "Write or update a supporting file inside a user skill bundle.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill ID, leaf name, or display name",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the skill bundle (e.g., 'templates/checklist.md')",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["skill_name", "path", "content"],
            },
            write_skill_file,
            replace=True,
            source="runtime:skill",
        )

    def _activation_payload(
        self, bundle: SkillBundle, registry: Optional[ToolRegistry] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "skill": {
                "id": bundle.id,
                "name": bundle.name,
                "description": bundle.description,
                "source": bundle.source,
                "bundle_root": self._bundle_root_label(bundle),
                "instructions": bundle.body,
                "supporting_files": bundle.supporting_files,
                "metadata": bundle.metadata,
            },
            "hints": {
                "file_access": (
                    "Use `read_skill_file` (not `read_file`) to read files inside "
                    "the skill bundle. `read_file` is restricted to the workspace root."
                ),
            },
        }
        output_dir = registry.get_context("output_dir") if registry else None
        if output_dir:
            payload["hints"]["output_dir"] = (
                f"Save generated files to: {output_dir} "
                f"(also available as $AGENT_OUTPUT_DIR in shell commands)"
            )
        return payload

    def activation_text(
        self, skill_ref: str, *, explicit: bool = False
    ) -> Optional[str]:
        bundle = self.get(skill_ref)
        if bundle is None:
            return None
        lines = [f"Skill `{bundle.id}` ({bundle.name}) is active for this turn."]
        if explicit:
            lines.append(
                "This skill was explicitly requested by the user and must be followed."
            )
        if bundle.description:
            lines.append(f"Description: {bundle.description}")
        lines.append(f"Bundle root: {self._bundle_root_label(bundle)}")
        if bundle.supporting_files:
            lines.append(
                "Supporting files (use `read_skill_file` to read, NOT `read_file`):"
            )
            lines.extend(f"- {path}" for path in bundle.supporting_files)
        else:
            lines.append("Supporting files available on demand: none")
        output_dir = (
            self._registry.get_context("output_dir") if self._registry else None
        )
        if output_dir:
            lines.append(
                f"Output directory for generated files: {output_dir} "
                f"(also available as $AGENT_OUTPUT_DIR in shell)"
            )
        lines.append(
            "Agent-managed paths are separate from the workspace root: "
            f"user tools live in {agent_module.TOOLS_DIR}, "
            f"user skills live in {agent_module.SKILLS_DIR}."
        )
        lines.append("")
        lines.append(bundle.body or "(No instructions in SKILL.md body)")
        return "\n".join(lines)
