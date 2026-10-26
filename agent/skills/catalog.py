from __future__ import annotations

import ast
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
from pathlib import Path
import re
import shutil
from typing import Any, Optional

from agent import shared
from agent.tools.runtime import ToolRegistry


def _datestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


#: A superseded SKILL.md is archived beside the live one as ``SKILL.md.<stamp>``.
#: The prefix is load-bearing twice over: the loader matches the *exact* name
#: ``SKILL.md``, so an archive is never mistaken for a second skill; and
#: ``_read_bundle`` files it under ``versions`` rather than ``supporting_files``,
#: so it is not advertised in the prompt as material to go and read.
_SKILL_VERSION_PREFIX = "SKILL.md."


def _is_skill_version(leaf_name: str) -> bool:
    """Whether *leaf_name* is an archived SKILL.md, not the live entrypoint."""
    return (
        leaf_name.startswith(_SKILL_VERSION_PREFIX)
        and len(leaf_name) > len(_SKILL_VERSION_PREFIX)
    )


#: The one frontmatter field that records *who authored* a bundle, and the value
#: ``create_skill`` writes into it.  A SKILL.md is not just data: it is pasted into
#: the system prompt, so rewriting one rewrites the agent's own instructions.  The
#: boundary is therefore drawn on **ownership**, not on the verb: a bundle the agent
#: made is its own to revise freely, while a bundle a person wrote (or installed)
#: needs a stated reason before the agent may change it.
#:
#: This is in the frontmatter rather than a sidecar file because of how the loader
#: reads a bundle: every file under the bundle directory other than SKILL.md is
#: collected into ``supporting_files`` and advertised in the prompt as material to
#: go and read.  A sidecar marker would either show up there as noise or need a
#: loader special case, whereas frontmatter is already parsed, travels with the file
#: when a bundle is copied or moved, and is visible in the same place a person would
#: look to change it.
#:
#: Default-safe, and deliberately strict: only the exact value ``agent`` marks a
#: bundle as the agent's.  Absent, misspelled, or any other value (``user``, a
#: plugin's name) leaves the bundle protected -- an unrecognised marker can fail to
#: unlock a bundle, but it can never be used to unlock one.
_SKILL_ORIGIN_KEY = "created-by"
#: Digest of the last SKILL.md the agent wrote, so "the agent owns this" can be
#: narrowed to "the agent owns this *as it stands now*".  Without it, a person who
#: edits an agent-made skill leaves a bundle still marked ``created-by: agent``, and
#: the agent goes on overwriting their edits without asking -- the boundary would
#: protect hand-written skills and not hand-*edited* ones.
_SKILL_DIGEST_KEY = "agent-digest"
_SKILL_ORIGIN_AGENT = "agent"


def _resolve_within_bundle(bundle_root: Path, rel_path: Path) -> Optional[Path]:
    """Resolve *rel_path* only when it stays beneath the resolved bundle root.

    Resolve both sides of the containment test.  Resolving only the child makes
    every valid file look like an escape when the bundle root itself sits below
    a symlink -- on macOS the ordinary ``/var`` → ``/private/var`` alias is
    enough to trigger it.  Resolving the child also keeps the original security
    property: an in-bundle symlink that points outside still resolves outside
    and is rejected.
    """
    root = bundle_root.resolve(strict=False)
    target = (root / rel_path).resolve(strict=False)
    if target == root or root in target.parents:
        return target
    return None


def _skill_digest_source(text: str) -> str:
    """The part of a SKILL.md the digest is computed over.

    The digest line itself is excluded -- otherwise writing the digest would change
    what it digests, and every stamp would invalidate itself.  Everything else is
    hashed verbatim, including the frontmatter: a person who retitles a skill has
    changed what the agent will follow, so their edit counts too.
    """
    kept = [
        line
        for line in text.splitlines()
        if not line.strip().startswith(f"{_SKILL_DIGEST_KEY}:")
    ]
    return "\n".join(kept)


def _normalized_digest_source(text: str) -> str:
    """The exact bytes the digest is taken over, as the file will hold them.

    ``splitlines``/``join`` on purpose: the composer may end with a blank line and a
    stamp may not, and hashing the text *as read* while writing it *as joined* made
    every freshly stamped file disagree with its own digest -- so the first edit
    looked like somebody else's.  Normalising on both sides is what makes stamp and
    check agree.
    """
    return "\n".join(_skill_digest_source(text).splitlines())


def _skill_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(
        _normalized_digest_source(text).encode("utf-8")
    ).hexdigest()


def stamp_skill_digest(text: str) -> str:
    """Return *text* with a fresh ``agent-digest`` line in its frontmatter.

    Idempotent: any existing digest line is removed first, so re-stamping a file
    that already has one replaces it instead of accumulating.
    """
    body = _normalized_digest_source(text)
    digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        # A SKILL.md without frontmatter cannot carry provenance; the composer
        # always writes one, so this is a guard against a shape nobody produces.
        return body
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            lines.insert(index, f"{_SKILL_DIGEST_KEY}: {digest}")
            return "\n".join(lines)
    return body


def skill_is_agent_owned(bundle: "SkillBundle") -> bool:
    """Whether the agent wrote this bundle **and nobody has edited it since**.

    Both halves matter.  ``created-by: agent`` alone says who made the file, not who
    last touched it, and the case this exists for is exactly the second one: a person
    fixes an agent-made skill by hand, and the next consolidation pass would overwrite
    their fix without a word.  A bundle stamped before digests existed has no digest,
    so it reads as unowned -- asking once more is the safe direction to be wrong in.
    """
    if not bundle.agent_authored:
        return False
    stored = str(bundle.metadata.get(_SKILL_DIGEST_KEY) or "").strip()
    if not stored:
        return False
    skill_file = bundle.path / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return hmac.compare_digest(stored, _skill_digest(text))


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
    #: Superseded copies of SKILL.md, newest last.  Kept apart from
    #: ``supporting_files`` because the two are read differently: supporting
    #: files are advertised in the injected prompt, while versions are history
    #: the model should only see when it asks to roll one back.
    versions: list[str] = field(default_factory=list)
    user_invocable: bool = True
    disable_model_invocation: bool = False
    #: Whether the agent authored this bundle (see ``_SKILL_ORIGIN_KEY``).  Read from
    #: the frontmatter when the bundle is loaded, never inferred from where it lives:
    #: both agent-created and hand-written skills sit in the same user root, so a
    #: path cannot tell them apart, and an inference is a rule that silently changes
    #: answer when a directory is copied.
    agent_authored: bool = False


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


#: "This id was not in the catalog before the read" -- distinct from ``None``, which a
#: bundle is never equal to but which reads as "no previous bundle" at a glance.
_ABSENT = object()


class SkillCatalog:
    """Load skill bundles from user and built-in skill directories.

    A skill can be switched off in ``config.json`` (``skills.<id>.enabled``).
    The switch is a *decision about a bundle*, not a property of it: built-in
    skills ship with the package and cannot be deleted, so the only thing a
    person can do with one they do not want is decline it here.  A disabled
    bundle stays loaded and stays listed -- it has to be findable in order to
    be switched back on -- and is simply not discoverable: it leaves the
    prompt's skill list, its slash command, and every lookup by name.

    Absent means enabled.  A skill nobody has expressed an opinion about is
    not silently disabled by a missing entry, which is also what makes every
    config written before this setting existed read correctly.
    """

    def __init__(
        self,
        user_root: Optional[Path] = None,
        builtin_root: Optional[Path] = None,
        skill_config: Optional[dict[str, Any]] = None,
    ):
        self.user_root = user_root or shared.SKILLS_DIR
        self.builtin_root = builtin_root or shared.BUILTIN_SKILLS_DIR
        self._skills: dict[str, SkillBundle] = {}
        self._aliases: dict[str, str] = {}
        self._registry: Optional[ToolRegistry] = None
        self._dirty: bool = False
        self._prompt_generation: int = 0
        self._user_root_signature: Optional[tuple[float, int, int]] = None
        self._skill_config = skill_config if isinstance(skill_config, dict) else {}
        #: The roots other layers attached, in the order they were attached, with the
        #: undo record each read produced.  A *contribution* is a record here, not a
        #: bundle: ``load_all`` re-reads every record (so a refresh cannot silently
        #: drop what a plugin contributed), and the record's removal restores exactly
        #: the ids that root touched -- see :meth:`add_root`.
        self._roots: list[dict[str, Any]] = []
        #: What the intrinsic layers produced -- the base every replay starts from.
        self._intrinsic: dict[str, SkillBundle] = {}

    def load_all(self) -> None:
        self.user_root.mkdir(parents=True, exist_ok=True)
        self._rebuild_intrinsic()
        self._rebuild_contributed()

    def _scan_user_root(self) -> tuple[float, int, int]:
        """Cheap signature for "did the skills directory change on disk?".

        Entry count is included so a bundle added within the same mtime tick
        (or on a filesystem with coarse timestamps) is still noticed.
        """
        try:
            stat = self.user_root.stat()
            entries = len(list(self.user_root.iterdir()))
        except OSError:
            return (0.0, 0, 0)
        return (float(stat.st_mtime), int(stat.st_ino), entries)

    def refresh_if_stale(self) -> bool:
        """Re-scan when skills changed on disk behind the running process.

        Skills are installed by copying directories (or by a plugin), not
        only through create_skill, so both the prompt's skill list and
        activation must observe the directory rather than a startup
        snapshot. Returns True when the user layer was re-read.

        A full :meth:`load_all`, which rebuilds the intrinsic layers and replays the
        contributed ones.  It used to re-read only the user layer, because the plugin
        roots were attached by the loader *after* a load and a full reload would have
        dropped them; now the roots are records this class owns, so there is one reload
        path instead of two, and neither can lose the other's skills.
        """
        if self._scan_user_root() == self._user_root_signature:
            return False
        self.load_all()
        self.invalidate()
        return True

    # -- contributions -------------------------------------------------------

    def skill_count(self) -> int:
        """How many skills are loaded, for a contributor that reports what it added."""
        return len(self._skills)

    def scoped(self, ctx: Any) -> Any:
        """This catalog, for one plugin: what it attaches leaves with the plugin."""
        from agent.skills.scoped import ScopedSkillCatalog

        return ScopedSkillCatalog(self, ctx)

    def add_root(
        self,
        root: "Path | str",
        *,
        source: str = "",
        keep_existing: bool = False,
        skip_dirs: "tuple[str, ...]" = (),
    ) -> Any:
        """Attach a skill root, returning the undo that takes exactly it back.

        A contribution is a **record**, not a set of bundles, and any change replays
        the contributed layer over the intrinsic one (:meth:`_rebuild_contributed`).
        The alternative -- remembering the ids a read produced and putting them back on
        removal -- is a second rule that breaks as soon as two contributors touch one
        id: removing the first would restore a bundle from a plugin that has since
        unloaded, or delete the bundle of the one that is still there.  Replay asks the
        only question the undo can answer exactly: *which records are attached?*
        """
        record: dict[str, Any] = {
            "root": Path(root),
            "source": source,
            "keep_existing": keep_existing,
            "skip_dirs": tuple(skip_dirs),
        }
        self._roots.append(record)
        self._rebuild_contributed()

        def undo() -> None:
            # By **identity**, not equality.  Two contributors may attach records that
            # compare equal -- same root, same source, same flags -- and ``remove()``
            # takes the first equal one, so the second contributor's undo would detach
            # the first contributor's root.  Identity answers the only question the undo
            # can answer exactly: which record did *this* contribution attach?
            for index, item in enumerate(self._roots):
                if item is record:
                    del self._roots[index]
                    break
            self._rebuild_contributed()

        return undo

    def contributed_roots(self) -> "list[tuple[Path, str]]":
        """The roots other layers attached, with the source each was attached under.

        For the screens and reports that ask "which skills came from where", and for a
        test that wants to assert a contribution rather than a skill's contents.
        """
        return [(record["root"], record["source"]) for record in self._roots]

    def _rebuild_intrinsic(self) -> None:
        """The layers that are always there: the builtin root, then this agent's."""
        self._skills = {}
        self._aliases = {}
        self._load_root(self.builtin_root, source="builtin")
        self._load_root(self.user_root, source="user")
        self._intrinsic = dict(self._skills)
        self._user_root_signature = self._scan_user_root()

    def _rebuild_contributed(self) -> None:
        """Replay every contributed root over the intrinsic layers, in attach order.

        Contributed roots come last, which is what lets a plugin's skill win a name
        collision, and they are re-read rather than remembered -- so a reload keeps
        them, and one plugin's removal cannot take another's skills with it.
        """
        self._skills = dict(self._intrinsic)
        self._aliases = {}
        for record in list(self._roots):
            self._load_root(
                record["root"],
                source=record["source"],
                keep_existing=record["keep_existing"],
                skip_dirs=record["skip_dirs"],
            )
        self._rebuild_aliases()

    def _load_root(
        self,
        root: Path,
        *,
        source: str,
        keep_existing: bool = False,
        skip_dirs: tuple[str, ...] = (),
    ) -> None:
        """Read one root's skills.

        ``keep_existing`` makes the root a **fallback layer**: a skill id that is
        already loaded (the user's own, a plugin's) is not shadowed by this root.
        The skill roots other ecosystems keep are read that way -- a skill the user
        installed here outranks one this agent found in another tool's directory.

        ``skip_dirs`` names subtrees to leave alone, for the roots that keep the
        ecosystem's *own* bundled skills in one (Codex's ``.system``, which it
        rewrites from its embedded assets on every startup).
        """
        if not root.exists():
            return
        for skill_file in sorted(root.rglob("SKILL.md")):
            relative = skill_file.relative_to(root)
            if skip_dirs and any(part in skip_dirs for part in relative.parts[:-1]):
                continue
            bundle = self._read_bundle(skill_file, root=root, source=source)
            if bundle is None:
                continue
            if keep_existing and bundle.id in self._skills:
                continue
            self._skills[bundle.id] = bundle
        self._rebuild_aliases()

    def _read_bundle(
        self, skill_file: Path, *, root: Path, source: str
    ) -> Optional[SkillBundle]:
        try:
            raw_text = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            shared.CONSOLE.print(f"[yellow]Failed to read skill {skill_file}: {e}[/yellow]")
            return None

        metadata, body = parse_skill_markdown(raw_text)
        bundle_dir = skill_file.parent
        bundle_id = bundle_dir.relative_to(root).as_posix()
        if not bundle_id or bundle_id == ".":
            if source.startswith("plugin:") and metadata.get("name"):
                bundle_id = str(metadata.get("name"))
            else:
                bundle_id = bundle_dir.name
        if source.startswith("plugin:"):
            plugin_name = source.split(":", 1)[1]
            if plugin_name and not bundle_id.startswith(f"{plugin_name}:"):
                bundle_id = f"{plugin_name}:{bundle_id}"
        supporting_files: list[str] = []
        versions: list[str] = []
        for path in sorted(
            p.relative_to(bundle_dir).as_posix()
            for p in bundle_dir.rglob("*")
            if p.is_file()
        ):
            leaf = path.rsplit("/", 1)[-1]
            if leaf == "SKILL.md":
                continue
            if _is_skill_version(leaf):
                versions.append(path)
            else:
                supporting_files.append(path)
        return SkillBundle(
            id=bundle_id,
            name=str(metadata.get("name") or bundle_dir.name),
            description=str(metadata.get("description") or ""),
            path=bundle_dir,
            source=source,
            body=body,
            metadata=metadata,
            supporting_files=supporting_files,
            versions=versions,
            user_invocable=bool(metadata.get("user-invocable", True)),
            disable_model_invocation=bool(
                metadata.get("disable-model-invocation", False)
            ),
            agent_authored=str(metadata.get(_SKILL_ORIGIN_KEY) or "").strip()
            == _SKILL_ORIGIN_AGENT,
        )

    def _archive_skill_md(self, skill_file: Path) -> Optional[str]:
        """Copy the live SKILL.md aside so the coming update stays reversible.

        Returns the archive's name inside the bundle, or ``None`` if it could
        not be written.  The copy is the whole file -- frontmatter included --
        because that is what a restore has to put back.

        Failure is reported and tolerated rather than raised: the write that
        follows is atomic, so the skill itself cannot be corrupted, and
        refusing the edit would block a user from fixing their own skill over a
        missing backup.  ``versions`` in the result is where the caller sees
        that it did not happen.
        """
        try:
            stamp = _datestamp()
            archive = skill_file.with_name(f"{_SKILL_VERSION_PREFIX}{stamp}")
            # _datestamp() is second-resolution, so two edits within one second
            # would collide and the second would silently consume the first.
            suffix = 1
            while archive.exists():
                archive = skill_file.with_name(
                    f"{_SKILL_VERSION_PREFIX}{stamp}_{suffix}"
                )
                suffix += 1
            shutil.copy2(skill_file, archive)
            return archive.name
        except Exception as e:
            shared.CONSOLE.print(
                f"[yellow]Could not archive {skill_file.name} before update: {e}[/yellow]"
            )
            return None

    def _rebuild_aliases(self) -> None:
        self._aliases.clear()
        counts: dict[str, int] = {}
        for skill_id in self._skills:
            leaf = skill_id.rsplit("/", 1)[-1]
            counts[leaf] = counts.get(leaf, 0) + 1
        for skill_id, bundle in self._skills.items():
            self._aliases[skill_id] = skill_id
            leaf = skill_id.rsplit("/", 1)[-1]
            is_plugin_skill = bundle.source.startswith("plugin:")
            if counts.get(leaf, 0) == 1 and not is_plugin_skill:
                self._aliases[leaf] = skill_id
            if not is_plugin_skill:
                self._aliases[bundle.name] = skill_id

    def reload(self) -> None:
        self.load_all()
        self._dirty = True
        self._prompt_generation += 1

    def invalidate(self) -> None:
        """Signal that external mutations (e.g. plugin reload) require a prompt rebuild."""
        self._dirty = True
        self._prompt_generation += 1

    def consume_dirty(self) -> bool:
        """Return True and clear if the catalog was mutated since last check.

        Runs the on-disk staleness check first so a skill dropped into the
        skills directory during a session refreshes the prompt without a
        restart.
        """
        self.refresh_if_stale()
        if self._dirty:
            self._dirty = False
            return True
        return False

    def get(self, skill_ref: str) -> Optional[SkillBundle]:
        """The named bundle, unless it is switched off.

        Lookups answer for the *usable* set, so every caller -- activation,
        the tools that read a bundle's files, explicit requests -- refuses a
        disabled skill without having to remember to ask.  The bundle is still
        there; ``find_any`` is how a caller tells "there is no such skill"
        from "there is, and it is off", which are different sentences.
        """
        resolved = self.resolve_ref(skill_ref)
        if resolved is None:
            return None
        return self._skills.get(resolved)

    def find_any(self, skill_ref: str) -> Optional[SkillBundle]:
        """The named bundle whether or not it is enabled, or None."""
        ref = str(skill_ref or "").strip()
        if not ref:
            return None
        resolved = self._lookup_ref_any(ref)
        if resolved is None:
            self.refresh_if_stale()
            resolved = self._lookup_ref_any(ref)
        if resolved is None:
            return None
        return self._skills.get(resolved)

    def is_enabled(self, bundle_or_id: Any) -> bool:
        """Whether this bundle is switched on.

        Takes either a bundle or an id because the two callers come from
        different places: a listing walks bundles, a request has an id.
        """
        skill_id = (
            bundle_or_id.id
            if isinstance(bundle_or_id, SkillBundle)
            else str(bundle_or_id or "")
        )
        entry = self._skill_config.get(skill_id)
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        return True

    def set_enabled(self, skill_id: str, enabled: bool) -> None:
        """Switch a skill on or off, and make the change take effect now.

        The value is kept in memory as well as returned to whoever writes it
        to disk, so the running process answers with the new state before any
        restart -- and ``invalidate`` is what makes the next turn recompose
        the prompt instead of reusing the one built when the skill was off.
        """
        self._skill_config[skill_id] = {"enabled": bool(enabled)}
        self.invalidate()

    def forget(self, skill_id: str) -> None:
        """Drop the switch recorded for a skill that no longer exists.

        An id can come back -- a user recreates the skill, a plugin ships it
        -- and it would then arrive already switched off, with nothing on
        screen to explain why. The switch belongs to the bundle it was thrown
        for, so it goes when the bundle does.
        """
        if skill_id in self._skill_config:
            del self._skill_config[skill_id]
            self.invalidate()

    def resolve_ref(self, skill_ref: str) -> Optional[str]:
        ref = skill_ref.strip()
        if not ref:
            return None
        found = self._lookup_ref(ref)
        if found is not None:
            return found
        # A miss is exactly when a caller is about to report "not found";
        # the directory may have gained the skill since the last scan.
        if self.refresh_if_stale():
            return self._lookup_ref(ref)
        return None

    def _lookup_ref_any(self, ref: str) -> Optional[str]:
        """Resolve a ref against everything loaded, switch or no switch."""
        if ref in self._skills:
            return ref
        return self._aliases.get(ref)

    def _lookup_ref(self, ref: str) -> Optional[str]:
        """Resolve a ref against the skills that can be used right now."""
        found = self._lookup_ref_any(ref)
        if found is None or not self.is_enabled(found):
            return None
        return found

    def list_skills(self) -> list[SkillBundle]:
        """Every skill that can be used right now.

        Every discovery surface (slash-command routing, the prompt listing,
        the command list) goes through here, so the disk check belongs here
        rather than at each caller -- and so does the switch: a surface that
        had to remember to filter would be a surface that one day forgets.
        ``list_all_skills`` is the way to see the switched-off ones.
        """
        self.refresh_if_stale()
        return [
            self._skills[key]
            for key in sorted(self._skills)
            if self.is_enabled(self._skills[key])
        ]

    def list_all_skills(self) -> list[SkillBundle]:
        """Every skill on disk, switched off ones included.

        For the screens that manage skills rather than use them: a skill that
        cannot be found after being switched off could never be switched back
        on, which is how a switch turns into a one-way door.
        """
        self.refresh_if_stale()
        return [self._skills[key] for key in sorted(self._skills)]

    def summary_lines(self) -> list[str]:
        if not self._skills:
            return []
        lines = [
            "## Available Skills",
            "Available skills:",
            "Skills are instruction bundles loaded on demand. When a user's task matches a skill's description, use activate_skill to load it instead of reimplementing its functionality yourself. Never write manual scripts to replicate what an available skill already does.",
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

    def _unusable_reason(self, skill_name: str) -> str:
        """Why this name cannot be used, as a sentence for the caller.

        "not found" and "switched off" are different answers, and a refusal
        that reports the first when the second is true sends the reader
        hunting for a typo in a name that is spelled correctly.  Says which
        switch to turn when it can, since the person who turned it off may
        not be the person reading this.
        """
        bundle = self.find_any(skill_name)
        if bundle is None:
            return f"Skill '{skill_name}' not found"
        return (
            f"Skill '{bundle.id}' is switched off; "
            "turn it back on in the skills list to use it"
        )

    def register_tools(self, registry: ToolRegistry) -> None:
        self._registry = registry

        async def activate_skill(skill_name: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                # Only the skills that could actually be activated are worth
                # suggesting: offering a switched-off name answers the typo
                # with a dead end.
                available = [item.id for item in self.list_skills()]
                query = skill_name.strip()
                candidates = (
                    difflib.get_close_matches(query, available, n=8, cutoff=0.6)
                    if query
                    else []
                )
                error = self._unusable_reason(skill_name)
                if candidates:
                    error = f"{error}. Did you mean: {', '.join(candidates)}"
                return {
                    "ok": False,
                    "error": error,
                    "candidates": candidates,
                }
            if bundle.disable_model_invocation:
                return {
                    "ok": False,
                    "error": f"Skill '{bundle.id}' cannot be activated by the model",
                }
            return self._activation_payload(bundle, registry=registry)

        def list_skill_files(skill_name: str, path: str = "") -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": self._unusable_reason(skill_name)}
            filter_dir: str | None = None
            if path:
                rel_path = Path(path)
                if rel_path.is_absolute():
                    return {
                        "ok": False,
                        "error": "Skill file paths must be relative to the skill bundle",
                    }
                if rel_path.as_posix() not in (".", ""):
                    target = _resolve_within_bundle(bundle.path, rel_path)
                    if target is None:
                        return {
                            "ok": False,
                            "error": "Requested path escapes the skill bundle",
                        }
                    filter_dir = rel_path.as_posix().rstrip("/")

            def _visible(name: str) -> bool:
                return (
                    filter_dir is None
                    or name == filter_dir
                    or name.startswith(filter_dir + "/")
                )

            # Archived versions are reported separately from supporting files:
            # both are readable, but only one of them is material the skill
            # wants followed, and a caller listing a bundle should be able to
            # tell which is which.
            return {
                "ok": True,
                "skill": bundle.id,
                "bundle_root": self._bundle_root_label(bundle),
                "path": filter_dir or ".",
                "files": [name for name in bundle.supporting_files if _visible(name)],
                "versions": [name for name in bundle.versions if _visible(name)],
            }

        def read_skill_file(skill_name: str, path: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": self._unusable_reason(skill_name)}
            rel_path = Path(path)
            if rel_path.is_absolute():
                return {
                    "ok": False,
                    "error": "Skill file paths must be relative to the skill bundle",
                }
            target = _resolve_within_bundle(bundle.path, rel_path)
            if target is None:
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
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional bundle-relative subpath to list; omit for the whole bundle",
                        "default": "",
                    }
                },
                "required": ["skill_name"],
                "additionalProperties": False,
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
            created_by: str = "",
        ) -> str:
            """Render a SKILL.md, carrying *created_by* through unchanged.

            ``created_by`` is written verbatim and only when non-empty: updating a
            bundle must not touch who authored it.  An agent-created bundle stays
            the agent's after an edit, and -- the half that matters for the
            boundary -- editing a hand-written bundle (which the gate only allows
            with a stated intent) does not launder it into an agent-owned one that
            could then be rewritten silently.
            """
            lines = ["---"]
            lines.append(f"name: {name}")
            if description:
                lines.append(f"description: {description}")
            lines.append(f"user-invocable: {'true' if user_invocable else 'false'}")
            lines.append(
                f"disable-model-invocation: {'true' if disable_model_invocation else 'false'}"
            )
            if created_by:
                lines.append(f"{_SKILL_ORIGIN_KEY}: {created_by}")
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
                    # The persistent record of *why* this bundle may be rewritten
                    # without asking.  It has to be written here, at creation, or
                    # the next process would see an unmarked bundle in the user
                    # root -- indistinguishable from one a person typed by hand.
                    created_by=_SKILL_ORIGIN_AGENT,
                )
                # The digest covers the file as this write leaves it, so "the agent
                # owns this" means "and nobody has edited it since".  Stamped here
                # rather than checked later: a digest computed from something else
                # would be a guess about what was written.
                content = stamp_skill_digest(content)
                # Create-only (guarded by the exists() check above), but use the
                # durable primitive anyway so the rule needs no per-site reasoning.
                shared._atomic_write_text(skill_file, content)
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
            intent: str = "",
        ) -> dict[str, Any]:
            # ``intent`` is declared, not read: the gate that refuses an
            # unwarranted rewrite runs as this tool's authorizer, before the
            # handler is entered, and it is the only place that sees both the
            # call and the bundle's ownership.  Accepting the parameter here is
            # what stops a supplied intent from becoming a TypeError.
            #
            # Management, not use: a skill switched off is still a skill
            # somebody may want to edit, rename, or throw away.  Only the
            # tools that *use* a bundle ask whether it is on.
            bundle = self.find_any(skill_id)
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
                final_body = (
                    instructions
                    if instructions is not None and instructions.strip()
                    else bundle.body
                )
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
                    # Carried through from the bundle being edited, not set to
                    # "agent": an edit is not a claim of authorship.
                    created_by=str(bundle.metadata.get(_SKILL_ORIGIN_KEY) or ""),
                )
                if bundle.agent_authored:
                    # Re-stamped only for a bundle that is already the agent's: the
                    # new digest covers the text just written, which is what keeps
                    # the next edit free.  A user's bundle gets no digest at all --
                    # writing one into it would be claiming authorship we do not
                    # have, and the boundary would stop protecting it.
                    content = stamp_skill_digest(content)
                # Keep the version being replaced.  An edit is a hypothesis
                # about what the skill should say, and a hypothesis needs a way
                # back: without this, a change that makes the skill worse is
                # indistinguishable from one that makes it better, because the
                # evidence it was worse is gone.
                archived = self._archive_skill_md(skill_file)
                # Durable primitive: this overwrites a skill the user may have
                # authored, so a truncating write can destroy it outright.
                shared._atomic_write_text(skill_file, content)
                self.reload()
                # Reported back by the same rule the lookup used: this is the
                # management answer, so an edit to a switched-off skill still
                # describes the skill it just edited.
                updated = self.find_any(bundle.id)
                return {
                    "ok": True,
                    "skill_id": bundle.id,
                    "path": str(bundle.path),
                    "archived": archived,
                    "message": f"Skill '{bundle.id}' updated successfully",
                    "skill": {
                        "id": updated.id,
                        "name": updated.name,
                        "description": updated.description,
                        "versions": updated.versions,
                    }
                    if updated
                    else None,
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to update skill: {e}"}

        def delete_skill(skill_id: str, intent: str = "") -> dict[str, Any]:
            # ``intent``: declared, not read -- see ``update_skill``.
            bundle = self.find_any(skill_id)
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
                self.forget(bundle.id)
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
            skill_name: str, path: str, content: str, intent: str = ""
        ) -> dict[str, Any]:
            # ``intent``: declared, not read -- see ``update_skill``.
            bundle = self.find_any(skill_name)
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
            target = _resolve_within_bundle(bundle.path, rel_path)
            if target is None:
                return {"ok": False, "error": "Requested path escapes the skill bundle"}
            if target.name == "SKILL.md":
                return {
                    "ok": False,
                    "error": "Use update_skill to modify SKILL.md, not write_skill_file",
                }
            try:
                # Durable primitive: this path also overwrites existing resource
                # files, so it needs the same all-or-nothing replacement.
                shared._atomic_write_text(target, content)
                self.reload()
                return {
                    "ok": True,
                    "skill": bundle.id,
                    "path": rel_path.as_posix(),
                    "message": f"File '{rel_path.as_posix()}' written to skill '{bundle.id}'",
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to write skill file: {e}"}

        def _skill_write_authorizer(
            tool_input: dict[str, Any], registry: ToolRegistry
        ) -> Optional[dict[str, Any]]:
            """Refuse to rewrite a bundle the agent does not own, without an intent.

            The boundary this draws is on **ownership of the asset, not on the
            verb**: a bundle ``create_skill`` made is the agent's own scratchpad
            and stays freely editable -- that freedom is the point of a
            self-extending agent -- while a bundle a person wrote by hand, or
            copied in from somewhere, needs one sentence saying what is being
            changed and why before the agent replaces instructions that are
            pasted into its own system prompt.

            It has to be call-aware, which is why it is an authorizer and not a
            tool-level ``requires_intent`` capability.  The executor's capability
            check is unconditional per tool, so declaring it on ``update_skill``
            would demand a reason for editing the agent's *own* skills too and
            delete exactly the freedom this boundary exists to preserve;
            ownership is a property of the bundle named in the call, and only a
            check that can look at both can tell the two cases apart.

            ``registry`` is part of the authorizer contract and unused here: this
            decision is about a file, not about the turn's context.  An unknown
            id, a builtin/plugin bundle, and a bundle the agent authored are all
            allowed through -- the first two so the handlers can give their own,
            more specific answer ("not found", "built-in cannot be modified")
            rather than a policy sentence that hides the real one.
            """
            ref = tool_input.get("skill_id") or tool_input.get("skill_name") or ""
            bundle = self.find_any(str(ref))
            if bundle is None or bundle.source != "user" or skill_is_agent_owned(bundle):
                return None
            intent = str(tool_input.get("intent") or "").strip()
            if intent:
                # Borrowed rather than re-implemented: "specific enough to count
                # as a stated reason" already has one definition (the executor's
                # intent gate), and a second list of vague words here would be
                # two answers to one question.  Deferred because only this gate
                # needs the executor, and the catalog is loaded by paths -- the
                # prompt, the CLI, the settings screen -- that never call a tool.
                from agent.tools.executor import RegularToolExecutor

                if RegularToolExecutor._intent_text_is_specific(intent):
                    return None
            return {
                "ok": False,
                # Same vocabulary the executor uses when it refuses a call for
                # want of an intent, so the model reads one kind of refusal
                # rather than two.
                "intent_required": True,
                "skill": bundle.id,
                "error": (
                    f"Refused: skill '{bundle.id}' is the user's, not the agent's "
                    "-- it was written by hand (or installed from elsewhere), and "
                    "a SKILL.md is injected into this agent's own instructions, so "
                    "replacing it needs a stated reason on the record. If the user "
                    "asked for this change, call again with input.intent saying "
                    "what you are changing and why -- quote their words if you "
                    "have them. If nobody asked, leave this bundle alone and "
                    "propose the change in your reply. Bundles created with "
                    f"create_skill carry '{_SKILL_ORIGIN_KEY}: {_SKILL_ORIGIN_AGENT}' "
                    "plus a digest of what was written, and stay freely editable "
                    "for as long as nobody has edited them since -- a hand edit to "
                    "one of those makes it somebody's again, which is why this "
                    "bundle may now be one. A new skill of your own is also an "
                    "option."
                ),
            }

        # ``create_skill`` gets no authorizer on purpose: creating a *new* bundle
        # takes nothing from anyone, refuses an id that already exists, and is
        # itself the freedom this boundary protects.  The three tools that
        # overwrite or remove an existing bundle do get the ownership gate.
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
            (
                "Update an existing user skill's metadata or instructions. Only "
                "user skills can be modified. A bundle the agent created is "
                "freely editable; one the user wrote needs input.intent. The "
                "SKILL.md being replaced is archived inside the bundle first, so "
                "an edit can be undone by reading the archived version and "
                "passing its instructions back."
            ),
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
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required only when the skill was not created by the "
                            "agent (a hand-written or externally installed bundle): "
                            "say what you are changing and why, specifically. A "
                            "bundle the agent created needs no intent."
                        ),
                    },
                },
                "required": ["skill_id"],
            },
            update_skill,
            replace=True,
            source="runtime:skill",
            authorizer=_skill_write_authorizer,
        )

        registry.register(
            "delete_skill",
            "Delete a user skill bundle. Built-in skills cannot be deleted. A "
            "bundle the agent created is freely deletable; one the user wrote "
            "needs input.intent.",
            {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill ID, leaf name, or display name of the skill to delete",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required only when the skill was not created by the "
                            "agent (a hand-written or externally installed bundle): "
                            "why it is being deleted, and what it was for. A "
                            "bundle the agent created needs no intent."
                        ),
                    },
                },
                "required": ["skill_id"],
            },
            delete_skill,
            replace=True,
            source="runtime:skill",
            authorizer=_skill_write_authorizer,
        )

        registry.register(
            "write_skill_file",
            "Write or update a supporting file inside a user skill bundle. A "
            "bundle the agent created is freely writable; one the user wrote "
            "needs input.intent. SKILL.md itself is not writable here -- use "
            "update_skill.",
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
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required only when the skill was not created by the "
                            "agent (a hand-written or externally installed bundle): "
                            "what this file is for and why it is being written. A "
                            "bundle the agent created needs no intent."
                        ),
                    },
                },
                "required": ["skill_name", "path", "content"],
            },
            write_skill_file,
            replace=True,
            source="runtime:skill",
            authorizer=_skill_write_authorizer,
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
                "versions": bundle.versions,
                "metadata": bundle.metadata,
            },
            "hints": {
                "file_access": (
                    "Use `read_skill_file` (not `read_file`) to read files inside "
                    "the skill bundle. `read_file` only accesses the `workspace` and "
                    "`output_dir` roots, never the skills directory."
                ),
            },
        }
        if bundle.versions:
            payload["hints"]["rollback"] = (
                "Superseded versions of SKILL.md are kept in this bundle, oldest "
                f"first. Read one with `read_skill_file` (e.g. `{bundle.versions[-1]}`) "
                "and restore it by passing its instructions back to `update_skill`."
            )
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
                "Supporting files (use `read_skill_file` to read, NOT `read_file`, "
                "which cannot access the skills directory):"
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
            f"user tools live in {shared.TOOLS_DIR}, "
            f"user skills live in {shared.SKILLS_DIR}."
        )
        lines.append("")
        lines.append(bundle.body or "(No instructions in SKILL.md body)")
        return "\n".join(lines)
