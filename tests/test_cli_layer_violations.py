"""Wiring test for the ``layer-violations`` CLI command.

New symbol under test (does not exist yet — RED until implemented):
``code_memory.cli.layer_violations`` registered as the Typer command
``layer-violations``, dispatching to ``FalkorStore.layer_violations()``
via the module's existing ``_graph_for(project)`` helper — mirroring
the ``injectors`` command (cli.py:753-760).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from code_memory.cli import app

runner = CliRunner()


def test_layer_violations_command_is_registered() -> None:
    result = runner.invoke(app, ["layer-violations", "--help"])
    assert result.exit_code == 0


def test_layer_violations_command_dispatches_to_graph_store(monkeypatch) -> None:
    from code_memory import cli as cli_mod

    class _FakeGraph:
        def layer_violations(self) -> list[dict]:
            return [
                {
                    "file": "/repo/domain/UseCase.cs",
                    "layer": "domain",
                    "depends_on_file": "/repo/infrastructure/Impl.cs",
                    "depends_on_layer": "infrastructure",
                    "symbol": "DoWork",
                }
            ]

    monkeypatch.setattr(cli_mod, "_graph_for", lambda project: _FakeGraph())

    result = runner.invoke(app, ["layer-violations", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["violations"] == [
        {
            "file": "/repo/domain/UseCase.cs",
            "layer": "domain",
            "depends_on_file": "/repo/infrastructure/Impl.cs",
            "depends_on_layer": "infrastructure",
            "symbol": "DoWork",
        }
    ]
