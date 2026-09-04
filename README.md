# Herdr Numbered Tabs

A [Herdr](https://herdr.dev) plugin that prefixes every tab label with its
**current displayed position/shortcut number**, derived from **tab order** (not
Herdr's stable public `tab.number`).

```
[1] 🚀launch-prep    [2] 🧪experiment-branch    [3] 📦release-candidate
```

Closing, moving, or reordering tabs renumbers the visible labels automatically,
because the prefix follows display order, not the stable per-workspace
`tab.number` counter.

## What it does

- On startup, and on every `tab.created`, `tab.closed`, `tab.moved`, and
  `tab.renamed` event, reconciles all tabs in every workspace.
- For each tab, computes `desired = [<display position>] <base label>` and
  renames only when the current label differs (idempotent).
- **Numbers only.** The plugin never takes over tab names — it only adds and
  removes a `[N] ` positional prefix. You always control the base name.
- Stores each tab's **original base label** in a state file, so renumbering never
  corrupts the underlying name and rollback can restore it.
- **Adopts new auto-named tabs**: when a new tab is created with Herdr's
  pre-filled bare index (e.g. `3`), the plugin immediately renames it to `[3]`
  so it's consistent with every other tab (removes the pre-filled index).
  Once adopted, the tab is custom-named and the plugin manages its prefix.
- Handles emoji, spaces, multi-byte characters, names that naturally start with
  digits, and multi-digit positions (10, 11, …).

## Files

| File | Purpose |
| --- | --- |
| `herdr-plugin.toml` | Plugin v1 manifest (`id = "numbered.tabs"`, startup hook, event hooks, actions) |
| `numbered_tabs.py` | Implementation: reconcile + rollback (Python 3, stdlib only) |
| `test/test_numbered_tabs.py` | Fixture-based test suite (no live Herdr session needed) |

## Install / link

```bash
# Local development:
herdr plugin link /path/to/numbered-tabs

# From GitHub (once published):
herdr plugin install owner/repo
```

## How the prefix is derived

The plugin calls `herdr tab list` (all workspaces), groups tabs by workspace,
and assigns each tab its **1-based index within the workspace's display order**.
This is deliberately NOT `tab.number`: that value is a stable public id that
increments per workspace and is never reused on close/move (confirmed in Herdr
0.8.2 source: `next_public_tab_number`). Using display order means a tab moved
from position 3 to position 2 immediately shows `[2] …` instead of keeping `[3] …`.

## Why `[N] ` and not `N `

The prefix format is deliberately `[N] <base>` (a bracketed all-digit badge).
This makes our prefix **unambiguous**: a user name like `5 things` or `[dev] setup`
is never mistaken for our prefix, because we only ever strip a leading `[digits] `
badge. That means:

- A user's digit-leading name (`5 things`) is preserved as the base → `[3] 5 things`.
- Even if the state file is lost, a tab that already carries our `[N] ` prefix is
  correctly re-derived (no double-prefix like `[3] [4] yes not bad`).

## Base-label preservation

The plugin keeps a state file (`HERDR_PLUGIN_STATE_DIR/tabs.json`) mapping
`tab_id → base_label`. Reconciliation:

1. **Managed tab (has stored base):** if the current label is already
   `[<pos>] <base>` → skip. If the current label is `[<N>] <base>` for a
   different `N` (a move) → re-prefix to the current position. Otherwise the user
   renamed it → adopt the new label (minus our `[N] ` prefix) as the base.
2. **New tab (no stored base):** a pure-integer label (Herdr's auto-named
   placeholder) → adopt with an empty base → `[<pos>]`. A label with our `[N] `
   prefix (e.g. after a state-file loss) → strip it and adopt the rest as the
   base. Otherwise the whole label is the base.
3. Only renames a tab when `desired != current` (idempotent).

## Recursion and race protection

- **Recursive `tab.renamed` loops:** renaming a tab fires a `tab.renamed` event,
  which triggers the plugin's `tab.renamed` hook. Because every reconcile is
  idempotent (it only renames when the label differs), the follow-up reconcile
  finds nothing to do and the loop terminates. Verified by tests 18–19.
- **Overlapping event-hook races:** Herdr runs plugin event hooks asynchronously
  (up to 32 in flight). All reconcile/rollback runs serialize on an exclusive
  `flock` (via `fcntl`) on a lock file, so two overlapping hooks cannot both
  read stale state and double-rename. Verified by test 16.

## Actions

- **`numbered.tabs.reconcile`** — re-run reconciliation and **re-enable** the
  plugin (clears the `disabled` flag set by a rollback). Runs with `--force` so
  it works even after a rollback.
- **`numbered.tabs.rollback`** — restore every managed tab to its stored base
  label (no prefix) and set a `disabled` flag so event hooks stop until the
  reconcile action is invoked again.

## Enable / disable lifecycle

- **Rollback** sets `disabled = true`: event hooks (`tab.created`, `tab.closed`,
  `tab.moved`, `tab.renamed`) become no-ops, so tabs stay unnumbered.
- **Reconcile action** runs with `--force`: it clears `disabled` and renumbers
  everything again.
- This lets you safely turn the plugin off (rollback), inspect, and turn it
  back on (reconcile) without losing the stored base labels.

## Herdr 0.8.2 limitation (#3470)

`tab.rename` permanently sets a tab's `custom_name`; there is **no API in Herdr
0.8.2 to restore automatic naming** for a tab that has been renamed. Once a tab
has been renamed (by this plugin or by hand), it stays custom-named and the tab
bar renders it bold. This plugin therefore:

- stores the original base label so it can re-prefix and roll back **without
  losing the name**, and
- documents that **rollback restores the base label as a custom name** — it
  cannot turn a custom tab back into an auto-named (non-bold) tab. That is a
  Herdr limitation, not a plugin one.

## Validate

```bash
# Manifest:
python3 -c "import tomli; tomli.load(open('herdr-plugin.toml','rb')); print('manifest: ok')"

# Tests (fixture-based, no live session touched):
python3 test/test_numbered_tabs.py

# Dry-run against a live session (read-only — no renames, no state writes):
NUMBERED_TABS_STATE_FILE=/tmp/nt-state.json HERDR_BIN_PATH=$(which herdr) \
  python3 numbered_tabs.py reconcile --dry-run
```

## Rollback

```bash
# Via the plugin action (restores base labels + disables reconcile):
herdr plugin action invoke numbered.tabs.rollback

# Or directly:
python3 numbered_tabs.py rollback
```

## Notes / limitations

- Requires Python 3 (stdlib only: `fcntl`, `json`, `re`, `subprocess`). On
  macOS and Linux `/usr/bin/python3` is used by the manifest command.
- `platforms = ["linux", "macos"]` (Windows uses `cmd.exe /d /c` and lacks the
  same `fcntl` semantics).
- The plugin only renames tabs it has a stored base for (or can infer one for).
  Auto-named tabs (bare integers) are adopted with an empty base so they render
  `[N]` like every other tab.
- The public Herdr API does not expose whether a tab is auto-named vs custom
  (`custom_label` is client-protocol-only). A pure-integer label is therefore
  treated as auto-named and adopted with an empty base. If a user manually
  renames a tab to a bare integer like `7`, the plugin will treat it as
  auto-named and render it as `[N]` (losing the `7`).
