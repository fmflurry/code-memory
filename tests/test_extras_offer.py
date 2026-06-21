"""Tests for the offer_missing_extras / resolve_extras_selection machinery.

All subprocess / install calls are mocked — no network, no pip, offline only.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from code_memory.updater import (
    EXTRAS,
    _install_selected_extras,
    offer_missing_extras,
    resolve_extras_selection,
    run_extras_wizard,
    run_update,
)

# ---------------------------------------------------------------------------
# resolve_extras_selection — pure, table-driven
# ---------------------------------------------------------------------------

ALL_MISSING: list[str] = list(EXTRAS.keys())  # ["dotnet", "hybrid"]


@pytest.mark.parametrize(
    "env_value,is_tty,missing,expected_selected,expected_mode",
    [
        # env "dotnet,hybrid" → both installed
        ("dotnet,hybrid", False, ALL_MISSING, ["dotnet", "hybrid"], "env"),
        # env "none" → nothing, env mode
        ("none", False, ALL_MISSING, [], "env"),
        # env "" → nothing, env mode
        ("", False, ALL_MISSING, [], "env"),
        # env "dotnet,bogus" → only dotnet (bogus filtered out)
        ("dotnet,bogus", False, ALL_MISSING, ["dotnet"], "env"),
        # env "dotnet" but dotnet already installed → empty
        ("dotnet", False, ["hybrid"], [], "env"),
        # env None + TTY → interactive mode, no pre-selected names
        (None, True, ALL_MISSING, [], "interactive"),
        # env None + no TTY → skip
        (None, False, ALL_MISSING, [], "skip"),
        # env "hybrid" + not in missing (already present) → empty
        ("hybrid", False, ["dotnet"], [], "env"),
    ],
)
def test_resolve_extras_selection(
    env_value: str | None,
    is_tty: bool,
    missing: list[str],
    expected_selected: list[str],
    expected_mode: str,
) -> None:
    selected, mode = resolve_extras_selection(
        env_value=env_value,
        is_tty=is_tty,
        missing=missing,
    )
    assert selected == expected_selected
    assert mode == expected_mode


# ---------------------------------------------------------------------------
# _install_selected_extras — rc aggregation
# ---------------------------------------------------------------------------


def test_install_selected_all_succeed(capsys: pytest.CaptureFixture[str]) -> None:
    """All installs succeed → rc 0."""
    with patch("code_memory.updater.install_extra", return_value=(True, "ok cmd")) as spy:
        rc = _install_selected_extras(["dotnet"], "pip")
    assert rc == 0
    spy.assert_called_once_with("dotnet", "pip")


def test_install_selected_one_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """One failure → nonzero rc."""
    results = {"dotnet": (False, "failed"), "hybrid": (True, "ok")}

    with patch("code_memory.updater.install_extra", side_effect=lambda n, m: results[n]):
        rc = _install_selected_extras(["dotnet", "hybrid"], "pip")
    assert rc != 0


def test_install_selected_all_fail() -> None:
    """All failures → nonzero rc."""
    with patch("code_memory.updater.install_extra", return_value=(False, "fail")):
        rc = _install_selected_extras(["dotnet", "hybrid"], "pip")
    assert rc != 0


# ---------------------------------------------------------------------------
# offer_missing_extras
# ---------------------------------------------------------------------------


def _patch_missing(names: list[str]) -> Any:
    """Return a patch ctx that makes only `names` appear as missing."""
    present = set(EXTRAS.keys()) - set(names)

    def _module_present(mod: str) -> bool:
        # Map module name → extra name to determine presence.
        for extra, info in EXTRAS.items():
            if info["module"] == mod:
                return extra in present
        return True

    return patch("code_memory.updater._python_module_present", side_effect=_module_present)


def test_offer_missing_non_tty_no_env_skips(capsys: pytest.CaptureFixture[str]) -> None:
    """non-TTY + no env → install spy NOT called, rc=0, hint printed."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras") as install_spy,
        patch.dict("os.environ", {}, clear=False),
    ):
        # Ensure CODEMEMORY_EXTRAS not present
        import os
        os.environ.pop("CODEMEMORY_EXTRAS", None)
        mock_sys.stdin.isatty.return_value = False
        rc = offer_missing_extras("pip")

    assert rc == 0
    install_spy.assert_not_called()
    out = capsys.readouterr().out
    assert "hint" in out or "CODEMEMORY_EXTRAS" in out


def test_offer_missing_isatty_raises_no_exception() -> None:
    """isatty() raising AttributeError → treated as False, no crash."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras"),
        patch.dict("os.environ", {}, clear=False),
    ):
        import os
        os.environ.pop("CODEMEMORY_EXTRAS", None)
        mock_sys.stdin.isatty.side_effect = AttributeError("no isatty")
        rc = offer_missing_extras("pip")
    # Should not raise, rc=0 (skip path)
    assert rc == 0


def test_offer_missing_env_dotnet_non_tty_calls_install(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CODEMEMORY_EXTRAS=dotnet, non-TTY → install called once with dotnet."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras", return_value=0) as install_spy,
        patch.dict("os.environ", {"CODEMEMORY_EXTRAS": "dotnet"}, clear=False),
    ):
        mock_sys.stdin.isatty.return_value = False
        rc = offer_missing_extras("pip")

    assert rc == 0
    install_spy.assert_called_once_with(["dotnet"], "pip")


def test_offer_missing_env_none_skips_install() -> None:
    """CODEMEMORY_EXTRAS=none → install never called."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras") as install_spy,
        patch.dict("os.environ", {"CODEMEMORY_EXTRAS": "none"}, clear=False),
    ):
        mock_sys.stdin.isatty.return_value = False
        rc = offer_missing_extras("pip")

    assert rc == 0
    install_spy.assert_not_called()


def test_offer_missing_all_present_returns_zero() -> None:
    """All extras already installed → rc=0, no prompt."""
    with (
        _patch_missing([]),  # nothing missing
        patch("code_memory.updater._install_selected_extras") as install_spy,
    ):
        rc = offer_missing_extras("pip")

    assert rc == 0
    install_spy.assert_not_called()


def test_offer_missing_unknown_method_returns_zero_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """method='unknown' → rc=0, hint printed, no install."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater._install_selected_extras") as install_spy,
    ):
        rc = offer_missing_extras("unknown")

    assert rc == 0
    install_spy.assert_not_called()
    out = capsys.readouterr().out
    assert "hint" in out


def test_offer_missing_cli_override_beats_env(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """extras_override='dotnet' beats CODEMEMORY_EXTRAS='hybrid'."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras", return_value=0) as install_spy,
        patch.dict("os.environ", {"CODEMEMORY_EXTRAS": "hybrid"}, clear=False),
    ):
        mock_sys.stdin.isatty.return_value = False
        rc = offer_missing_extras("pip", extras_override="dotnet")

    assert rc == 0
    install_spy.assert_called_once_with(["dotnet"], "pip")


def test_offer_missing_env_bogus_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """CODEMEMORY_EXTRAS=dotnet,bogus → install dotnet, warn bogus."""
    with (
        _patch_missing(ALL_MISSING),
        patch("code_memory.updater.sys") as mock_sys,
        patch("code_memory.updater._install_selected_extras", return_value=0) as install_spy,
        patch.dict("os.environ", {"CODEMEMORY_EXTRAS": "dotnet,bogus"}, clear=False),
    ):
        mock_sys.stdin.isatty.return_value = False
        rc = offer_missing_extras("pip")

    assert rc == 0
    install_spy.assert_called_once_with(["dotnet"], "pip")
    out = capsys.readouterr().out
    assert "bogus" in out


# ---------------------------------------------------------------------------
# run_extras_wizard regression — must still work unchanged
# ---------------------------------------------------------------------------


def test_run_extras_wizard_all_no_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_extras_wizard with user answering 'n' → 'Nothing to install', rc=0."""
    # Pretend nothing is installed.
    monkeypatch.setattr(
        "code_memory.updater._python_module_present", lambda mod: False
    )
    monkeypatch.setattr("code_memory.updater.detect_install_method", lambda: "pip")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    rc = run_extras_wizard()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to install" in out


# ---------------------------------------------------------------------------
# run_update integration — offer_missing_extras wiring
# ---------------------------------------------------------------------------


def _mock_build_plan_and_upgrades() -> tuple[Any, Any, Any, Any]:
    """Return patches for build_plan, upgrade_cli, upgrade_docker_images."""
    from code_memory.updater import UpdatePlan, ComponentState

    plan = UpdatePlan(
        install_method="pip",
        cli_current="0.1.0",
        cli_latest="0.1.0",  # already current → no upgrade
        components=[],
    )

    build_plan_mock = patch("code_memory.updater.build_plan", return_value=plan)
    upgrade_cli_mock = patch("code_memory.updater.upgrade_cli", return_value=(True, "ok"))
    upgrade_docker_mock = patch(
        "code_memory.updater.upgrade_docker_images", return_value=(True, "ok")
    )
    offer_mock = patch("code_memory.updater.offer_missing_extras", return_value=0)
    return build_plan_mock, upgrade_cli_mock, upgrade_docker_mock, offer_mock


def test_run_update_smart_path_calls_offer(capsys: pytest.CaptureFixture[str]) -> None:
    """Smart path (check_only=False, full=False) must call offer_missing_extras."""
    bpm, ucm, udm, om = _mock_build_plan_and_upgrades()
    with bpm, ucm, udm, om as offer_spy:
        rc = run_update(check_only=False, full=False, bleeding=False)
    assert rc == 0
    offer_spy.assert_called_once()


def test_run_update_check_only_does_not_call_offer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--check path must NOT call offer_missing_extras."""
    bpm, ucm, udm, om = _mock_build_plan_and_upgrades()
    with bpm, ucm, udm, om as offer_spy:
        run_update(check_only=True, full=False, bleeding=False)
    offer_spy.assert_not_called()


def test_run_update_full_does_not_call_offer(capsys: pytest.CaptureFixture[str]) -> None:
    """--full path must NOT call offer_missing_extras."""
    bpm, ucm, udm, om = _mock_build_plan_and_upgrades()
    with bpm, ucm, udm, om as offer_spy, patch(
        "code_memory.updater._run_full_installer", return_value=0
    ):
        run_update(check_only=False, full=True, bleeding=False)
    offer_spy.assert_not_called()


def test_run_update_threads_extras_override(capsys: pytest.CaptureFixture[str]) -> None:
    """--extras flag is forwarded to offer_missing_extras as extras_override."""
    bpm, ucm, udm, om = _mock_build_plan_and_upgrades()
    with bpm, ucm, udm, om as offer_spy:
        run_update(check_only=False, full=False, bleeding=False, extras_override="dotnet")
    offer_spy.assert_called_once_with("pip", extras_override="dotnet")
