#!/usr/bin/env python3
"""
numbered-tabs — prefix every Herdr tab label with its current displayed
position/shortcut number, e.g. `[1] 🚀launch-prep`, `[2] 🧪experiment-branch`.

The prefix is derived from TAB ORDER (display order within a workspace), NOT
Herdr's stable public `tab.number`. Closing, moving, or reordering tabs
renumbers the visible labels on the next reconcile.

Design goals (per user requirements):
  * Numbers ONLY. The plugin never takes over tab names — it only adds and
    removes a `[N] ` positional prefix. The user always controls the base name.
  * The `[N] ` prefix format is UNAMBIGUOUS: a user name like `5 things` or
    `[dev] setup` is never mistaken for our prefix, because we only strip a
    leading `[digits] ` badge. This makes reconciliation robust even if the
    state file is lost or a tab moved while the plugin was disabled.

Herdr 0.8.2 limitation (issue #3470): `tab.rename` permanently marks a tab as
custom-named (`custom_name = Some(...)`); there is no API to restore automatic
naming. This plugin therefore stores each tab's ORIGINAL base label in a state
file so it can re-prefix and roll back without ever losing the base name. It
cannot, however, turn a custom tab back into an auto-named tab.

Concurrency: plugin event hooks run asynchronously (up to 32 in flight). A
reconcile triggered by our own `tab.renamed` event would otherwise race with
the reconcile that caused it. We serialize all reconcile/rollback runs with an
exclusive `flock` on a lock file, and every reconcile is idempotent (it only
renames a tab when its current label differs from the desired one), which also
breaks the recursive `tab.renamed` → hook → rename → `tab.renamed` loop.
"""

import fcntl
import json
import os
import re
import subprocess
import sys

# --- Paths ---------------------------------------------------------------

def state_file_path():
    """Resolve the state file. Overridable for tests."""
    override = os.environ.get("NUMBERED_TABS_STATE_FILE")
    if override:
        return override
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "tabs.json")
    # Fallback for manual testing outside a plugin context.
    return os.path.join(
        os.path.expanduser("~/.config/herdr/plugins/state/numbered.tabs"),
        "tabs.json",
    )

def lock_file_path():
    override = os.environ.get("NUMBERED_TABS_LOCK_FILE")
    if override:
        return override
    return state_file_path() + ".lock"


def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH", "herdr")


# --- State ---------------------------------------------------------------

def load_state():
    try:
        with open(state_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"bases": {}, "disabled": False}
        bases = data.get("bases", {})
        if not isinstance(bases, dict):
            bases = {}
        return {
            "bases": {str(k): str(v) for k, v in bases.items()},
            "disabled": bool(data.get("disabled", False)),
        }
    except (OSError, ValueError):
        return {"bases": {}, "disabled": False}


def save_state(state):
    os.makedirs(os.path.dirname(state_file_path()), exist_ok=True)
    tmp = state_file_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, state_file_path())


# --- Herdr CLI helpers ---------------------------------------------------

def run_herdr(*args):
    """Run a herdr CLI command, returning parsed JSON or None on failure."""
    try:
        proc = subprocess.run(
            [herdr_bin(), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def list_tabs():
    """Return all tabs across all workspaces in display order."""
    data = run_herdr("tab", "list")
    if not data:
        return []
    return data.get("result", {}).get("tabs", [])


def rename_tab(tab_id, label):
    """Rename a tab. Returns True on success."""
    # `herdr tab rename <tab_id> <label...>` joins remaining args with spaces.
    return run_herdr("tab", "rename", tab_id, label) is not None


# --- Label logic ---------------------------------------------------------

# Our managed prefix is `[N] ` — unambiguous: only a bracketed all-digit badge
# (optionally followed by whitespace + text) is treated as ours. A user name
# like `5 things` or `[dev] setup` is never stripped.
PREFIX_RE = re.compile(r"^\[(\d+)\](?:\s+(.*))?$", re.DOTALL)
# Herdr's automatic naming is a bare integer label (its live position).
AUTO_NAME_RE = re.compile(r"^\d+$")


def is_auto_named(label):
    """A pure-integer label is Herdr's automatic naming (position)."""
    return bool(AUTO_NAME_RE.match(label))


def strip_managed_prefix(label):
    """Remove our `[N] ` prefix if present; otherwise return the label as-is."""
    m = PREFIX_RE.match(label)
    if m:
        return m.group(2) or ""
    return label


def desired_label(pos, base):
    if base:
        return f"[{pos}] {base}"
    return f"[{pos}]"


def compute_plan(tabs, state):
    """
    Build a list of (tab_id, new_label) renames needed to make every custom tab
    show `[<display_pos>] <base>`. Returns (renames, new_bases).

    The base label is the user-controlled name. We adopt a tab's current label
    (minus our own `[N] ` prefix) as its base the first time we see it, and
    re-derive it whenever the user changes the name.
    """
    renames = []
    new_bases = dict(state.get("bases", {}))
    # Group tabs by workspace, preserving display order.
    by_ws = {}
    for tab in tabs:
        by_ws.setdefault(tab.get("workspace_id"), []).append(tab)

    for ws_id, ws_tabs in by_ws.items():
        for pos, tab in enumerate(ws_tabs, start=1):
            tab_id = tab.get("tab_id")
            current = tab.get("label", "")
            if not tab_id:
                continue
            if is_auto_named(current):
                # Herdr's auto-named placeholder (bare position number). Adopt
                # it with an EMPTY base so it renders `[N]` like every other
                # tab, instead of leaving a bare `3` (the pre-filled index the
                # user wants removed). Once adopted, the tab is custom-named
                # and we manage its prefix from here on.
                base = ""
                new_bases[tab_id] = base
                new_label = desired_label(pos, base)
                if new_label != current:
                    renames.append((tab_id, new_label))
                continue

            stored = new_bases.get(tab_id)
            if stored is not None:
                # We manage this tab. Determine whether the user renamed it.
                if current == desired_label(pos, stored):
                    continue  # already correct (idempotent)
                stripped = strip_managed_prefix(current)
                if stripped == stored:
                    # Our own prefix from a previous position → re-prefix.
                    new_label = desired_label(pos, stored)
                else:
                    # The user changed the name. Adopt the new name as the base
                    # (minus our own prefix if they kept it). This preserves
                    # digit-leading names because strip_managed_prefix only
                    # removes `[N] ` badges.
                    base = stripped
                    new_bases[tab_id] = base
                    new_label = desired_label(pos, base)
            else:
                # New tab we haven't seen before. Adopt its current label as the
                # base (minus our own `[N] ` prefix if present, e.g. after a
                # state-file loss).
                base = strip_managed_prefix(current)
                new_bases[tab_id] = base
                new_label = desired_label(pos, base)

            if new_label != current:
                renames.append((tab_id, new_label))

    return renames, new_bases


# --- Reconcile / rollback ------------------------------------------------

def reconcile(dry_run=False, force=False):
    state = load_state()
    if state.get("disabled") and not force:
        return 0
    # An explicit reconcile (action) re-enables the plugin after a rollback.
    if force and state.get("disabled"):
        state["disabled"] = False

    tabs = list_tabs()
    if not tabs:
        if force:
            save_state(state)
        return 0

    renames, new_bases = compute_plan(tabs, state)
    # Always record the derived bases, even when no rename is needed, so the
    # state file is complete for rollback and future reconciliation.
    state["bases"] = new_bases

    if not renames:
        if force:
            save_state(state)
        return 0

    if dry_run:
        for tab_id, label in renames:
            print(f"{tab_id}\t{label}")
        return 0

    # Apply renames. Each rename fires a tab.renamed event → another reconcile
    # hook, which will be serialized by the lock and find nothing to do.
    for tab_id, label in renames:
        if not rename_tab(tab_id, label):
            sys.stderr.write(f"numbered-tabs: rename failed for {tab_id}\n")

    save_state(state)
    return 0


def rollback():
    state = load_state()
    bases = state.get("bases", {})
    tabs = list_tabs()
    by_id = {t.get("tab_id"): t.get("label", "") for t in tabs}

    # Restore every managed tab to its stored base label (no prefix).
    for tab_id, base in bases.items():
        if tab_id not in by_id:
            continue
        if not base:
            # Adopted auto-named tab (empty base). Leave it as `[N]`; there is
            # no meaningful name to restore, and renaming to empty is invalid.
            continue
        current = by_id[tab_id]
        if current == base:
            continue
        if not rename_tab(tab_id, base):
            sys.stderr.write(f"numbered-tabs: rollback rename failed for {tab_id}\n")

    # Disable future reconcile until the user re-enables via the reconcile action.
    state["disabled"] = True
    save_state(state)
    return 0


def main():
    args = sys.argv[1:]
    command = args[0] if args else "reconcile"
    dry_run = "--dry-run" in args
    force = "--force" in args
    # Serialize concurrent event-hook invocations.
    try:
        lock_fh = open(lock_file_path(), "a+", encoding="utf-8")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
    except OSError as err:
        sys.stderr.write(f"numbered-tabs: could not acquire lock: {err}\n")
        return 1

    try:
        if command == "reconcile":
            return reconcile(dry_run=dry_run, force=force)
        if command == "rollback":
            return rollback()
        sys.stderr.write(f"numbered-tabs: unknown command {command!r}\n")
        return 2
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        finally:
            lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
