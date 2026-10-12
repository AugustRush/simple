# Capability services and the shape of a contribution

**Status:** the shape is implemented for `channels`, `registry`, `skill_catalog` and
`contributed_mcp_servers`; `hooks`/`commands`/`agents` are analysed below.
**Context:** the plugin refactor's goal is that adding an ecosystem or replacing an
implementation is an *adapter* or a *contribution*, never an edit to the loader or the
composition root.  This note writes down the layering that makes that true, and the one
rule that turned out to be load-bearing.

## The layers

1. **Kernel** (`agent/kernel/`) -- `Plugin` (`name`/`inject`/`setup`), services,
   events, scopes.  A plugin declares what it needs (`inject`); the kernel holds it
   pending until those services exist and unwinds its registrations through its scope.
   Nothing in this layer knows what a skill, a channel or a tool is.
2. **Capability services** -- one service per *kind of contribution*: `channels`,
   `registry` (tools), `skill_catalog`, and (missing) `mcp_servers`, `hooks`,
   `commands`, `agents`.  A capability service is a **registry plus a per-plugin view**.
3. **Adapters** (`agent/plugins/formats.py`, and the loader's `_CatalogPlugin`) -- turn
   an ecosystem's directory layout into a **plugin object**.  This already happens:
   `_CatalogPlugin` is the plugin a discovered bundle becomes, and its `setup` runs the
   bundle's declared contributions.
4. **Plugins** -- code plugins and (via an adapter) declarative bundles.  They contribute
   only through injected services, in `setup`.

The important consequence: a *declarative* bundle is not a special case of the plugin
system; it is a plugin whose `setup` was written by an adapter.  What was missing was not
a plugin wrapper but **services for two of the kinds** -- so the loader collected the
paths into a list and the composition root attached them afterwards.

## The shape of a contribution

```
capability service        S            the registry: owns the records
service.scoped(ctx)    -> V            one plugin's view: attribution + undo
V.add_root(payload)    -> int          the contribution (view registers the undo)
S.add_root(payload)    -> undo         the primitive, for a caller holding S
```

Implemented three times now, with the same reasoning each time:

* `ChannelRegistry` + `ScopedChannels.add(channel)`;
* `ToolRegistry` + its view's `register(...)`;
* `SkillCatalog` + `ScopedSkillCatalog.add_root(root, ...)`;
* `MCPServerRegistry` + `ScopedMCPServers.add(config)`.

Two of them taught a rule about *when* the capability has to exist.  A service whose
consumer **computes a value from it in `setup`** (the MCP client builds the merged
server list) cannot be provided by that same consumer: its own `setup` would read the
registry before the contributors -- which are waiting for it -- have run.  So the
registry has a plugin of its own, provided before the consumer, and the consumer
declares `inject = (..., "capability", ...)`: the kernel activates the contributors when
the registry appears and the consumer last.  And a service whose name is also a **config
key** (`mcp_servers` is the MCP config's own field) makes every AST scan read the config
as a provision, so the capability is named for what it holds
(`contributed_mcp_servers`).

A contribution is therefore: attributed to the plugin that made it, undone when that
plugin's scope unwinds, and *visible to the ledger* (`CORE_SERVICES` names the owner and
its consumers, and `agent/runtime/core_services.py` is checked against the tree).

## The rule that turned out to be load-bearing

**A view is the service plus attribution, not a narrower service.** Reads pass through
(`ScopedSkillCatalog.__getattr__` -> the catalog); only the contribution calls the view
owns are attributed.

Measured cost of the alternative: the moment `skill_catalog` grew a `scoped()` view, the
kernel's `ctx.get("skill_catalog")` started returning the *view*, and `agent/ralph/plugin.py`
-- which only looks a skill up by id -- failed with
`AttributeError: 'ScopedSkillCatalog' object has no attribute 'get'`.  A capability
service that gains a view must not change what its existing readers see.  (A narrower
view is a legitimate design, but then the kernel needs a way to ask for *the service*,
and today it has none: `ctx.get` always returns the view.)

## What this removed

Moving one kind (skills) onto its service deleted, rather than added:

* `PluginCatalog._bundled_skills` and `_bundled_mcp` -- the loader's lists of what
  plugins' files declared, and their bookkeeping in the reload snapshot/rollback;
* `PluginCatalog.get_bundled_skills()`/`get_bundled_mcp()` and their
  `PluginCatalogService` members;
* `agent/bootstrap.py`'s three-line loop that attached a plugin's skill roots, and the
  `_rebuild_aliases()` call that went with it;
* the reload path's private reach into the skill catalog --
  `load_all()` + re-attach loop + `_rebuild_aliases()` + `invalidate()`-with-`_dirty`
  fallback -- now one call, `skill_catalog.reload()`.

And it made two things true that used to be luck:

* a contributed root **survives a reload**: the catalog keeps contributed roots as
  *records* and replays them over its intrinsic layers (`SkillCatalog._rebuild_intrinsic`
  / `_rebuild_contributed`), so `load_all`, `reload` and `refresh_if_stale` cannot drop a
  plugin's skills -- previously `refresh_if_stale` had to re-read *only* the user layer to
  avoid exactly that;
* a bundle that declares skills now **declares the dependency** (`_CatalogPlugin.inject`
  appends `skill_catalog`), so a composition without a skill catalog leaves it pending
  with a named reason in `pending_report()`, instead of a bundle silently contributing
  nothing.

## What is still outside the shape

| kind | today | what the shape needs |
|---|---|---|
| skills | **service** (this change) | -- |
| tools / channels | service | -- |
| MCP configs | **service** (this change: `contributed_mcp_servers`) | -- |
| hooks | `PluginMeta.hooks_config`, read by the loader's dispatchers | a `hooks` service, or an explicit statement that a hook is *loader* configuration rather than a contribution |
| commands / agents | registries provided **by the loader itself** (`catalog.py:1255-1256`) | providers as plugins, like `channels`, plus protocols |

The last row is the largest remaining asymmetry: two capability services are provided by
the loader because the loader's own declarative path writes into them at discovery time.
Once a bundle's contributions happen in its `setup` (as skills now do), the loader no
longer needs to own them, and they can be provided the way `channels` is.
