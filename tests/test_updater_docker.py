"""Tests for the updater's docker wiring on top of ``code_memory._docker``.

Everything is mocked at the ``updater._run`` / resolution boundary — no
docker needed. Guards the WSL-fallback contract:

1. With a wsl resolution, compose gets the ``wsl -e docker`` prefix and the
   ``-f`` / ``--project-directory`` paths are translated.
2. With a native resolution, Windows paths pass through untouched.
3. A compose path read back from a container label is daemon-side already:
   passed verbatim (no re-translation), dirname computed posix-style.
4. No resolution → ``(False, <actionable detail>)`` without running compose.
5. ``_owning_compose_project`` / ``_running_compose_file`` parse plain-JSON
   ``docker inspect`` output (no Go templates).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_memory import updater
from code_memory._docker import DockerResolution

WSL_RES = DockerResolution(("wsl", "-e", "docker"), "wsl", "docker via WSL")
NATIVE_RES = DockerResolution(("docker",), "native", "docker (native daemon)")
NONE_RES = DockerResolution(None, "none", "no docker found — see README")


class RunRecorder:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        rc, out = self.responses.get(tuple(cmd), (0, ""))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolution: DockerResolution,
    run: RunRecorder,
    home: Path | None = None,
) -> None:
    monkeypatch.setattr(updater, "resolve_docker", lambda **kw: resolution)
    monkeypatch.setattr(
        updater, "docker_argv", lambda: list(resolution.argv) if resolution.argv else None
    )
    monkeypatch.setattr(updater, "_run", run)
    if home is not None:
        monkeypatch.setattr(updater, "CODEMEMORY_HOME", home)


def _make_home_compose(tmp_path: Path) -> Path:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")
    return tmp_path


# ---------------------------------------------------------------------------
# upgrade_docker_images
# ---------------------------------------------------------------------------


def test_compose_via_wsl_translates_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_home_compose(tmp_path)
    run = RunRecorder()
    _wire(monkeypatch, resolution=WSL_RES, run=run, home=home)
    translated: list[str] = []

    def fake_translate(p) -> str:
        translated.append(str(p))
        return "/mnt/fake/" + Path(p).name

    monkeypatch.setattr(updater, "to_docker_path", fake_translate)
    monkeypatch.setattr(updater, "_owning_compose_project", lambda: None)

    ok, msg = updater.upgrade_docker_images()

    assert ok, msg
    pull = run.calls[0]
    assert pull[:3] == ["wsl", "-e", "docker"]
    assert pull[3] == "compose"
    assert pull[pull.index("-f") + 1] == "/mnt/fake/docker-compose.yml"
    assert pull[pull.index("--project-directory") + 1] == "/mnt/fake/docker"
    assert len(translated) == 2  # -f and --project-directory, nothing else


def test_compose_native_keeps_windows_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_home_compose(tmp_path)
    run = RunRecorder()
    _wire(monkeypatch, resolution=NATIVE_RES, run=run, home=home)
    monkeypatch.setattr(updater, "to_docker_path", lambda p: str(p))
    monkeypatch.setattr(updater, "_owning_compose_project", lambda: "code-memory")

    ok, msg = updater.upgrade_docker_images()

    assert ok, msg
    pull = run.calls[0]
    assert pull[0] == "docker"
    assert pull[pull.index("-f") + 1] == str(home / "docker" / "docker-compose.yml")
    assert pull[pull.index("-p") + 1] == "code-memory"


def test_label_compose_path_passed_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    label_path = "/mnt/c/_git/code-memory/docker/docker-compose.yml"
    run = RunRecorder()
    _wire(monkeypatch, resolution=WSL_RES, run=run, home=tmp_path)  # no compose in home

    def must_not_translate(p) -> str:
        raise AssertionError(f"daemon-side path re-translated: {p}")

    monkeypatch.setattr(updater, "to_docker_path", must_not_translate)
    monkeypatch.setattr(updater, "_running_compose_file", lambda: label_path)
    monkeypatch.setattr(updater, "_owning_compose_project", lambda: "code-memory")

    ok, msg = updater.upgrade_docker_images()

    assert ok, msg
    pull = run.calls[0]
    assert pull[pull.index("-f") + 1] == label_path
    assert pull[pull.index("--project-directory") + 1] == "/mnt/c/_git/code-memory/docker"


def test_no_resolution_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = RunRecorder()
    _wire(monkeypatch, resolution=NONE_RES, run=run, home=tmp_path)

    ok, msg = updater.upgrade_docker_images()

    assert ok is False
    assert msg == NONE_RES.detail
    assert run.calls == []


def test_conflict_recovery_reuses_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_home_compose(tmp_path)

    class FlakyRun(RunRecorder):
        def __call__(self, cmd, **kwargs):
            self.calls.append(list(cmd))
            # First `up` hits the name conflict; the retry succeeds.
            if "up" in cmd and sum("up" in c for c in self.calls) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    run = FlakyRun()
    _wire(monkeypatch, resolution=WSL_RES, run=run, home=home)
    monkeypatch.setattr(updater, "to_docker_path", lambda p: str(p))
    monkeypatch.setattr(updater, "_owning_compose_project", lambda: None)

    ok, _ = updater.upgrade_docker_images()

    assert ok
    rm = next(c for c in run.calls if "rm" in c)
    assert rm[:3] == ["wsl", "-e", "docker"]
    assert set(updater.COMPOSE_CONTAINERS) <= set(rm)


# ---------------------------------------------------------------------------
# Plain-JSON inspect parsing
# ---------------------------------------------------------------------------


def _inspect_payload(labels: dict[str, str]) -> str:
    return json.dumps([{"Config": {"Labels": labels}}])


def test_owning_project_from_json_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _inspect_payload({"com.docker.compose.project": "my-proj"})
    run = RunRecorder({("wsl", "-e", "docker", "inspect", "cm-falkordb"): (0, payload)})
    _wire(monkeypatch, resolution=WSL_RES, run=run)

    assert updater._owning_compose_project() == "my-proj"
    # No Go-template flag anywhere in the invocation.
    assert all("-f" not in c for c in run.calls)


def test_owning_project_falls_through_to_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _inspect_payload({"com.docker.compose.project": "other"})
    run = RunRecorder(
        {
            ("docker", "inspect", "cm-falkordb"): (1, ""),
            ("docker", "inspect", "cm-qdrant"): (0, payload),
        }
    )
    _wire(monkeypatch, resolution=NATIVE_RES, run=run)

    assert updater._owning_compose_project() == "other"


def test_running_compose_file_checks_daemon_side(monkeypatch: pytest.MonkeyPatch) -> None:
    label_path = "/mnt/c/x/docker-compose.yml"
    payload = _inspect_payload({"com.docker.compose.project.config_files": label_path})
    run = RunRecorder({("wsl", "-e", "docker", "inspect", "cm-falkordb"): (0, payload)})
    _wire(monkeypatch, resolution=WSL_RES, run=run)
    checked: list[str] = []

    def fake_exists(p) -> bool:
        checked.append(str(p))
        return True

    monkeypatch.setattr(updater, "docker_path_exists", fake_exists)

    assert updater._running_compose_file() == label_path
    assert checked == [label_path]


def test_running_compose_file_none_when_label_path_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _inspect_payload({"com.docker.compose.project.config_files": "/mnt/c/gone.yml"})
    run = RunRecorder(
        {
            ("docker", "inspect", "cm-falkordb"): (0, payload),
            ("docker", "inspect", "cm-qdrant"): (0, payload),
        }
    )
    _wire(monkeypatch, resolution=NATIVE_RES, run=run)
    monkeypatch.setattr(updater, "docker_path_exists", lambda p: False)

    assert updater._running_compose_file() is None


def test_inspect_labels_bad_json_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    run = RunRecorder({("docker", "inspect", "cm-falkordb"): (0, "not json")})
    _wire(monkeypatch, resolution=NATIVE_RES, run=run)

    assert updater._inspect_labels("cm-falkordb") == {}
