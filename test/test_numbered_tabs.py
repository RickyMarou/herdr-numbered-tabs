#!/usr/bin/env python3
"""
Test harness for numbered-tabs.

Runs the plugin's numbered_tabs.py against a FAKE `herdr` binary that serves
fixture tab lists and records rename calls, so we never touch the live Herdr
session. The fake binary is a tiny shell script that reads a fixture JSON file,
applies renames to it (so later `tab list` reflects them), and appends rename
commands to a log file.

Usage:
  python3 test/test_numbered_tabs.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

# tomli (Py<3.11) / tomllib (Py>=3.11): parse the plugin manifest in tests.
# No live Herdr session is touched.
try:
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as _toml  # type: ignore

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(PLUGIN_DIR, "numbered_tabs.py")

FAKE_HERDR_TEMPLATE = """#!/bin/sh
# fake herdr: reads fixture from HERDR_FIXTURE, logs renames to HERDR_RENAME_LOG
# and applies renames to the fixture so subsequent `tab list` reflects them.
if [ "$1" = "tab" ] && [ "$2" = "list" ]; then
  cat "$HERDR_FIXTURE"
  exit 0
fi
if [ "$1" = "tab" ] && [ "$2" = "rename" ]; then
  tab_id="$3"
  shift 3
  label="$*"
  printf '%s\\t%s\\n' "$tab_id" "$label" >> "$HERDR_RENAME_LOG"
  # Apply the rename to the fixture (JSON) so later `tab list` reflects it.
  HERDR_FIXTURE="$HERDR_FIXTURE" TAB_ID="$tab_id" NEW_LABEL="$label" python3 -c '
import json, os
path = os.environ["HERDR_FIXTURE"]
tab_id = os.environ["TAB_ID"]
label = os.environ["NEW_LABEL"]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
for t in data["result"]["tabs"]:
    if t.get("tab_id") == tab_id:
        t["label"] = label
        break
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
'
  exit 0
fi
exit 1
"""


def make_fake_herdr(tmpdir, fixture_path, rename_log):
    fake = os.path.join(tmpdir, "herdr")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write(FAKE_HERDR_TEMPLATE)
    os.chmod(fake, 0o755)
    return {
        "HERDR_BIN_PATH": fake,
        "HERDR_FIXTURE": fixture_path,
        "HERDR_RENAME_LOG": rename_log,
    }


def make_fixture(tmpdir, tabs):
    """tabs: list of dicts {tab_id, workspace_id, number, label, ...}"""
    path = os.path.join(tmpdir, "fixture.json")
    payload = {
        "id": "cli:tab:list",
        "result": {"type": "tab_list", "tabs": tabs},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def run_plugin(env, command="reconcile"):
    proc = subprocess.run(
        [sys.executable, PLUGIN, *command.split()],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        cwd=PLUGIN_DIR,
    )
    return proc


def read_renames(rename_log):
    if not os.path.exists(rename_log):
        return []
    out = []
    with open(rename_log, encoding="utf-8") as fh:
        for line in fh:
            tab_id, label = line.rstrip("\n").split("\t", 1)
            out.append((tab_id, label))
    return out


def tab(tab_id, ws, num, label, focused=False, pane_count=1, agent_status="idle"):
    return {
        "agent_status": agent_status,
        "focused": focused,
        "label": label,
        "number": num,
        "pane_count": pane_count,
        "tab_id": tab_id,
        "workspace_id": ws,
    }


PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name} {detail}")


def run_case(name, tabs, expect_renames, command="reconcile", pre_state=None):
    """Run reconcile/rollback against a fixture and assert the rename log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture = make_fixture(tmpdir, tabs)
        rename_log = os.path.join(tmpdir, "renames.log")
        env = make_fake_herdr(tmpdir, fixture, rename_log)
        state_file = os.path.join(tmpdir, "state.json")
        env["NUMBERED_TABS_STATE_FILE"] = state_file
        env["NUMBERED_TABS_LOCK_FILE"] = os.path.join(tmpdir, "lock")
        if pre_state is not None:
            with open(state_file, "w", encoding="utf-8") as fh:
                json.dump(pre_state, fh, ensure_ascii=False)
        proc = run_plugin(env, command)
        renames = read_renames(rename_log)
        state = json.load(open(state_file, encoding="utf-8")) if os.path.exists(state_file) else {}
        ok = proc.returncode == 0
        check(f"{name}: exit 0", ok, f"exit={proc.returncode}")
        check(
            f"{name}: renames match",
            renames == expect_renames,
            f"got={renames!r} want={expect_renames!r}",
        )
        return state, renames, proc


def main():
    print("=== numbered-tabs test suite ===\n")

    # 1. Basic: two custom tabs in one workspace, positions 1 and 2.
    state, renames, _ = run_case(
        "basic",
        [
            tab("w1:t1", "w1", 1, "launch-prep"),
            tab("w1:t2", "w1", 2, "experiment-branch"),
        ],
        [
            ("w1:t1", "[1] launch-prep"),
            ("w1:t2", "[2] experiment-branch"),
        ],
    )
    check("basic: state records bases", state["bases"] == {
        "w1:t1": "launch-prep",
        "w1:t2": "experiment-branch",
    }, f"state={state}")

    # 2. Idempotent: running reconcile again with already-prefixed labels does nothing.
    state, renames, _ = run_case(
        "idempotent",
        [
            tab("w1:t1", "w1", 1, "[1] launch-prep"),
            tab("w1:t2", "w1", 2, "[2] experiment-branch"),
        ],
        [],
        pre_state={"bases": {
            "w1:t1": "launch-prep",
            "w1:t2": "experiment-branch",
        }, "disabled": False},
    )
    check("idempotent: no renames", renames == [], f"renames={renames}")

    # 3. Auto-named tabs (pure integers) are now ADOPTED and prefixed `[N]`
    #  (removing Herdr's pre-filled bare index).
    state, renames, _ = run_case(
        "auto_named",
        [
            tab("w1:t1", "w1", 1, "1"),
            tab("w1:t2", "w1", 2, "2"),
            tab("w1:t3", "w1", 3, "3"),
        ],
        [
            ("w1:t1", "[1]"),
            ("w1:t2", "[2]"),
            ("w1:t3", "[3]"),
        ],
    )
    check("auto_named: adopted and prefixed [N]", renames == [
        ("w1:t1", "[1]"),
        ("w1:t2", "[2]"),
        ("w1:t3", "[3]"),
    ], f"renames={renames}")
    check("auto_named: empty bases recorded", state["bases"] == {
        "w1:t1": "", "w1:t2": "", "w1:t3": "",
    }, f"state={state}")

    # 4. Mixed: auto-named + custom. Both get prefixed; auto-named with empty base.
    state, renames, _ = run_case(
        "mixed",
        [
            tab("w1:t1", "w1", 1, "1"),
            tab("w1:t2", "w1", 2, "🤖zyg"),
        ],
        [
            ("w1:t1", "[1]"),
            ("w1:t2", "[2] 🤖zyg"),
        ],
    )
    check("mixed: both prefixed (auto empty, custom named)", renames == [
        ("w1:t1", "[1]"),
        ("w1:t2", "[2] 🤖zyg"),
    ], f"renames={renames}")

    # 4b. NEW-TAB ADOPTION: a tab created with Herdr's pre-filled bare number
    #  (e.g. `3`) is adopted and prefixed `[3]` — removing the pre-filled index.
    state, renames, _ = run_case(
        "new_tab_adoption",
        [
            tab("w1:t1", "w1", 1, "[1] alpha"),
            tab("w1:t2", "w1", 2, "[2] beta"),
            tab("w1:t3", "w1", 3, "3"),  # pre-filled bare number
        ],
        [
            ("w1:t3", "[3]"),
        ],
        pre_state={"bases": {
            "w1:t1": "alpha",
            "w1:t2": "beta",
        }, "disabled": False},
    )
    check("new_tab_adoption: pre-filled bare number replaced by [3]", renames == [
        ("w1:t3", "[3]"),
    ], f"renames={renames}")
    check("new_tab_adoption: empty base recorded", state["bases"].get("w1:t3") == "", f"state={state}")

    # 5. Multi-digit positions (10+ tabs).
    tabs = [tab(f"w1:t{i}", "w1", i, f"tab{i}") for i in range(1, 12)]
    expect = [(f"w1:t{i}", f"[{i}] tab{i}") for i in range(1, 12)]
    state, renames, _ = run_case("multi_digit", tabs, expect)
    check("multi_digit: 11 tabs prefixed [1]..[11]", renames == expect, f"renames={renames}")

    # 6. Label with spaces preserved.
    state, renames, _ = run_case(
        "spaces",
        [
            tab("w1:t1", "w1", 1, "my project"),
            tab("w1:t2", "w1", 2, "another  task"),
        ],
        [
            ("w1:t1", "[1] my project"),
            ("w1:t2", "[2] another  task"),
        ],
    )
    check("spaces: labels with spaces preserved", renames == [
        ("w1:t1", "[1] my project"),
        ("w1:t2", "[2] another  task"),
    ], f"renames={renames}")

    # 7. Emoji labels preserved.
    state, renames, _ = run_case(
        "emoji",
        [
            tab("w1:t1", "w1", 1, "🤖robot"),
            tab("w1:t2", "w1", 2, "🧩puzzle"),
        ],
        [
            ("w1:t1", "[1] 🤖robot"),
            ("w1:t2", "[2] 🧩puzzle"),
        ],
    )
    check("emoji: emoji preserved", renames == [
        ("w1:t1", "[1] 🤖robot"),
        ("w1:t2", "[2] 🧩puzzle"),
    ], f"renames={renames}")

    # 8. Names that naturally start with digits are preserved (not stripped).
    state, renames, _ = run_case(
        "digit_leading",
        [
            tab("w1:t1", "w1", 1, "5 things"),
            tab("w1:t2", "w1", 2, "42 answers"),
        ],
        [
            ("w1:t1", "[1] 5 things"),
            ("w1:t2", "[2] 42 answers"),
        ],
    )
    check("digit_leading: names starting with digits preserved", renames == [
        ("w1:t1", "[1] 5 things"),
        ("w1:t2", "[2] 42 answers"),
    ], f"renames={renames}")

    # 9. Bracket-style user names are preserved (not stripped).
    state, renames, _ = run_case(
        "bracket_user_name",
        [
            tab("w1:t1", "w1", 1, "[dev] setup"),
            tab("w1:t2", "w1", 2, "[] empty"),
        ],
        [
            ("w1:t1", "[1] [dev] setup"),
            ("w1:t2", "[2] [] empty"),
        ],
    )
    check("bracket_user_name: [dev] and [] preserved", renames == [
        ("w1:t1", "[1] [dev] setup"),
        ("w1:t2", "[2] [] empty"),
    ], f"renames={renames}")

    # 10. Close/reorder: after removing a tab, positions shift and renumber.
    state, renames, _ = run_case(
        "close_renumber",
        [
            tab("w1:t2", "w1", 2, "[2] 🤖zyg"),
            tab("w1:t3", "w1", 3, "[3] 🤖ryan"),
        ],
        [
            ("w1:t2", "[1] 🤖zyg"),
            ("w1:t3", "[2] 🤖ryan"),
        ],
        pre_state={"bases": {
            "w1:t2": "🤖zyg",
            "w1:t3": "🤖ryan",
        }, "disabled": False},
    )
    check("close_renumber: positions renumber after close", renames == [
        ("w1:t2", "[1] 🤖zyg"),
        ("w1:t3", "[2] 🤖ryan"),
    ], f"renames={renames}")

    # 11. Move/reorder: tab moved to a different position renumbers.
    state, renames, _ = run_case(
        "move_renumber",
        [
            tab("w1:t1", "w1", 1, "[1] alpha"),
            tab("w1:t3", "w1", 3, "[3] gamma"),
            tab("w1:t2", "w1", 2, "[2] beta"),
        ],
        [
            ("w1:t3", "[2] gamma"),
            ("w1:t2", "[3] beta"),
        ],
        pre_state={"bases": {
            "w1:t1": "alpha",
            "w1:t2": "beta",
            "w1:t3": "gamma",
        }, "disabled": False},
    )
    check("move_renumber: moved tab renumbers", renames == [
        ("w1:t3", "[2] gamma"),
        ("w1:t2", "[3] beta"),
    ], f"renames={renames}")

    # 12. User rename detected: user renamed a managed tab to a new name.
    state, renames, _ = run_case(
        "user_rename",
        [
            tab("w1:t1", "w1", 1, "[1] alpha"),
            tab("w1:t2", "w1", 2, "new name"),
        ],
        [
            ("w1:t2", "[2] new name"),
        ],
        pre_state={"bases": {
            "w1:t1": "alpha",
            "w1:t2": "beta",
        }, "disabled": False},
    )
    check("user_rename: user's new name adopted as base", renames == [
        ("w1:t2", "[2] new name"),
    ], f"renames={renames}")
    check("user_rename: state updated", state["bases"]["w1:t2"] == "new name", f"state={state}")

    # 13. STATE-LOSS RECOVERY (the bug): a tab with a stale prefix at a new
    #  position and NO stored base must NOT double-prefix. `[4] yes not bad` at
    #  position 3 → `[3] yes not bad` (not `[3] [4] yes not bad`).
    state, renames, _ = run_case(
        "state_loss_recovery",
        [
            tab("w1:t1", "w1", 1, "[1] alpha"),
            tab("w1:t2", "w1", 2, "[2] beta"),
            tab("w1:t5", "w1", 5, "[4] yes not bad"),
        ],
        [
            ("w1:t5", "[3] yes not bad"),
        ],
        pre_state={"bases": {
            "w1:t1": "alpha",
            "w1:t2": "beta",
        }, "disabled": False},
    )
    check("state_loss_recovery: no double-prefix", renames == [
        ("w1:t5", "[3] yes not bad"),
    ], f"renames={renames}")
    check("state_loss_recovery: base recorded", state["bases"]["w1:t5"] == "yes not bad", f"state={state}")

    # 14. Rollback restores base labels and disables reconcile.
    state, renames, _ = run_case(
        "rollback",
        [
            tab("w1:t1", "w1", 1, "[1] alpha"),
            tab("w1:t2", "w1", 2, "[2] beta"),
        ],
        [
            ("w1:t1", "alpha"),
            ("w1:t2", "beta"),
        ],
        command="rollback",
        pre_state={"bases": {
            "w1:t1": "alpha",
            "w1:t2": "beta",
        }, "disabled": False},
    )
    check("rollback: bases restored", renames == [
        ("w1:t1", "alpha"),
        ("w1:t2", "beta"),
    ], f"renames={renames}")
    check("rollback: disabled flag set", state.get("disabled") is True, f"state={state}")

    # 15. Disabled state: reconcile is a no-op.
    state, renames, _ = run_case(
        "disabled_noop",
        [
            tab("w1:t1", "w1", 1, "alpha"),
            tab("w1:t2", "w1", 2, "beta"),
        ],
        [],
        pre_state={"bases": {}, "disabled": True},
    )
    check("disabled_noop: no renames when disabled", renames == [], f"renames={renames}")

    # 16. Concurrency: two reconciles run back-to-back. The lock serializes;
    #  second run finds nothing to do.
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture = make_fixture(tmpdir, [
            tab("w1:t1", "w1", 1, "alpha"),
            tab("w1:t2", "w1", 2, "beta"),
        ])
        rename_log = os.path.join(tmpdir, "renames.log")
        env = make_fake_herdr(tmpdir, fixture, rename_log)
        env["NUMBERED_TABS_STATE_FILE"] = os.path.join(tmpdir, "state.json")
        env["NUMBERED_TABS_LOCK_FILE"] = os.path.join(tmpdir, "lock")
        p1 = run_plugin(env, "reconcile")
        p2 = run_plugin(env, "reconcile")
        renames = read_renames(rename_log)
        check("concurrency: first reconcile renames", renames == [
            ("w1:t1", "[1] alpha"),
            ("w1:t2", "[2] beta"),
        ], f"renames={renames}")
        check("concurrency: second reconcile no-op", p2.returncode == 0, f"exit={p2.returncode}")

    # 17. Malformed fixture (empty tabs) → no crash, no renames.
    state, renames, _ = run_case("empty", [], [])
    check("empty: no tabs → no renames", renames == [], f"renames={renames}")

    # 18. Recursion prevention: a reconcile that renames tabs is followed by
    #  another reconcile (simulating the tab.renamed event hooks it triggered).
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture = make_fixture(tmpdir, [
            tab("w1:t1", "w1", 1, "alpha"),
            tab("w1:t2", "w1", 2, "beta"),
            tab("w1:t3", "w1", 3, "gamma"),
        ])
        rename_log = os.path.join(tmpdir, "renames.log")
        env = make_fake_herdr(tmpdir, fixture, rename_log)
        env["NUMBERED_TABS_STATE_FILE"] = os.path.join(tmpdir, "state.json")
        env["NUMBERED_TABS_LOCK_FILE"] = os.path.join(tmpdir, "lock")
        p1 = run_plugin(env, "reconcile")
        first = read_renames(rename_log)
        p2 = run_plugin(env, "reconcile")
        p3 = run_plugin(env, "reconcile")
        total = read_renames(rename_log)
        check("recursion: first reconcile prefixes all", first == [
            ("w1:t1", "[1] alpha"),
            ("w1:t2", "[2] beta"),
            ("w1:t3", "[3] gamma"),
        ], f"first={first}")
        check("recursion: follow-up reconciles add no renames (no loop)",
              len(total) == 3, f"total={total}")
        check("recursion: all exits 0", p1.returncode == p2.returncode == p3.returncode == 0,
              f"{p1.returncode},{p2.returncode},{p3.returncode}")

    # 19. Full event-driven loop: after each rename the fake herdr applies it to
    #  the fixture, and we fire a reconcile hook per rename (like Herdr's
    #  tab.renamed event). The loop must converge and never double-prefix.
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture = make_fixture(tmpdir, [
            tab("w1:t1", "w1", 1, "alpha"),
            tab("w1:t2", "w1", 2, "beta"),
        ])
        rename_log = os.path.join(tmpdir, "renames.log")
        env = make_fake_herdr(tmpdir, fixture, rename_log)
        env["NUMBERED_TABS_STATE_FILE"] = os.path.join(tmpdir, "state.json")
        env["NUMBERED_TABS_LOCK_FILE"] = os.path.join(tmpdir, "lock")
        run_plugin(env, "reconcile")
        for _ in range(5):
            count = len(read_renames(rename_log))
            run_plugin(env, "reconcile")
            if len(read_renames(rename_log)) == count:
                break
        final = read_renames(rename_log)
        check("event_loop: converges to exactly 2 renames", len(final) == 2, f"final={final}")
        check("event_loop: labels correct", final == [
            ("w1:t1", "[1] alpha"),
            ("w1:t2", "[2] beta"),
        ], f"final={final}")

    # 20. Re-enable: after rollback (disabled=true), a plain reconcile is a no-op
    #  but a `reconcile --force` clears disabled and renumbers.
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture = make_fixture(tmpdir, [
            tab("w1:t1", "w1", 1, "alpha"),
            tab("w1:t2", "w1", 2, "beta"),
        ])
        rename_log = os.path.join(tmpdir, "renames.log")
        env = make_fake_herdr(tmpdir, fixture, rename_log)
        env["NUMBERED_TABS_STATE_FILE"] = os.path.join(tmpdir, "state.json")
        env["NUMBERED_TABS_LOCK_FILE"] = os.path.join(tmpdir, "lock")
        with open(env["NUMBERED_TABS_STATE_FILE"], "w", encoding="utf-8") as fh:
            json.dump({"bases": {"w1:t1": "alpha", "w1:t2": "beta"}, "disabled": True}, fh)
        p_plain = run_plugin(env, "reconcile")
        check("reenable: plain reconcile while disabled is no-op",
              read_renames(rename_log) == [], f"renames={read_renames(rename_log)}")
        p_force = run_plugin(env, "reconcile --force")
        renames = read_renames(rename_log)
        state = json.load(open(env["NUMBERED_TABS_STATE_FILE"], encoding="utf-8"))
        check("reenable: force reconcile renumbers", renames == [
            ("w1:t1", "[1] alpha"),
            ("w1:t2", "[2] beta"),
        ], f"renames={renames}")
        check("reenable: disabled cleared", state.get("disabled") is False, f"state={state}")
        check("reenable: exits 0", p_plain.returncode == p_force.returncode == 0,
              f"{p_plain.returncode},{p_force.returncode}")

    # 21. REGRESSION (middle-close reindex bug): the plugin must react to the
    #  Herdr 0.9.0 pane-close cascade. Closing a tab by removing its LAST pane
    #  (TUI prefix+x on a single-pane tab; `herdr pane close`) removes the tab
    #  but emits ONLY `pane.closed` -- no `tab.closed`, no focus event. Without
    #  a `pane.closed` hook no reconcile runs and surviving tabs keep stale
    #  numbers (`[1] main / [3] agent-b`). Parse the MANIFEST and require the
    #  hook. This test fails before the fix (manifest had no `pane.closed`).
    manifest_path = os.path.join(PLUGIN_DIR, "herdr-plugin.toml")
    with open(manifest_path, "rb") as fh:
        manifest = _toml.load(fh)
    hook_events = [hook.get("on") for hook in manifest.get("events", [])]
    check("manifest: hooks include pane.closed (pane-close cascade fix)",
          "pane.closed" in hook_events, f"events={hook_events}")
    # Every hook name must also be valid for Herdr (dot-named event kinds the
    # plugin registry accepts; mirrored from Herdr 0.9.0 PLUGIN_HOOK_EVENT_KINDS).
    known_events = {
        "workspace.created", "workspace.updated", "workspace.closed",
        "workspace.renamed", "workspace.moved", "workspace.reordered",
        "workspace.focused", "worktree.created", "worktree.opened",
        "worktree.removed", "tab.created", "tab.closed", "tab.renamed",
        "tab.moved", "tab.focused", "pane.created", "pane.closed",
        "pane.focused", "pane.moved", "pane.exited", "pane.agent_detected",
        "pane.agent_status_changed",
    }
    check("manifest: all hook names valid for Herdr 0.9.0",
          all(name in known_events for name in hook_events),
          f"invalid={[e for e in hook_events if e not in known_events]}")
    # tab.closed must also be hooked (the dedicated `tab.close` path).
    check("manifest: hooks include tab.closed",
          "tab.closed" in hook_events, f"events={hook_events}")

    # 22. REGRESSION (middle-close reindex logic): the exact reported scenario
    #  `[1] main / [2] agent-a / [3] agent-b`, middle tab closed -> surviving
    #  tab must renumber `[3] agent-b` -> `[2] agent-b` (not stay stale). The
    #  fixture models the post-close tab list a `pane.closed`-triggered
    #  reconcile would see.
    state, renames, _ = run_case(
        "middle_close_renumber",
        [
            tab("w1:t1", "w1", 1, "[1] main"),
            tab("w1:t3", "w1", 3, "[3] agent-b"),
        ],
        [
            ("w1:t3", "[2] agent-b"),
        ],
        pre_state={"bases": {
            "w1:t1": "main",
            "w1:t2": "agent-a",
            "w1:t3": "agent-b",
        }, "disabled": False},
    )
    check("middle_close_renumber: surviving tab renumbers to [2]", renames == [
        ("w1:t3", "[2] agent-b"),
    ], f"renames={renames}")
    check("middle_close_renumber: bases preserved", state["bases"]["w1:t3"] == "agent-b",
          f"state={state}")

    # 23. User-controlled names are never double-prefixed or clobbered: a tab
    #  the user renames to a digit-leading name keeps it as the base.
    state, renames, _ = run_case(
        "user_controlled_names",
        [
            tab("w1:t1", "w1", 1, "[1] main"),
            tab("w1:t2", "w1", 2, "7 secrets"),
            tab("w1:t3", "w1", 3, "[3] agent-b"),
        ],
        [
            ("w1:t2", "[2] 7 secrets"),
        ],
        pre_state={"bases": {
            "w1:t1": "main",
            "w1:t2": "7 secrets",
            "w1:t3": "agent-b",
        }, "disabled": False},
    )
    check("user_controlled_names: digit-leading user name preserved (no double prefix)", renames == [
        ("w1:t2", "[2] 7 secrets"),
    ], f"renames={renames}")
    check("user_controlled_names: base intact", state["bases"]["w1:t2"] == "7 secrets",
          f"state={state}")

    print("\n=== summary ===")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("  FAILED:", ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
