from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import FAKE_AGENT
from ghostlab.cli import main


def agent_file(tmp_path: Path) -> Path:
    path = tmp_path / "fake.yaml"
    path.write_text(json.dumps({
        "harness": "command", "options": {"command": [sys.executable, str(FAKE_AGENT)]},
        "sandbox": {"backend": "local"},
    }))
    return path


def test_exec_prints_the_answer(tmp_path: Path, capsys) -> None:
    code = main(["exec", "--agent", str(agent_file(tmp_path)), "hello there",
                 "--run-dir", str(tmp_path / "run")])
    out, err = capsys.readouterr()
    assert code == 0 and out.strip() == "echo: hello there"
    assert "[completed]" in err
    assert main(["exec", "--agent", str(agent_file(tmp_path)), "exit:4",
                 "--run-dir", str(tmp_path / "r2"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "agent_error"


def test_exec_reports_config_errors(tmp_path: Path, capsys) -> None:
    assert main(["exec", "--agent", str(tmp_path / "missing.yaml"), "hi"]) == 2
    assert "agent config not found" in capsys.readouterr().err


def test_run_resume_and_status(tmp_path: Path, capsys) -> None:
    pipeline = tmp_path / "pipe.py"
    pipeline.write_text(
        "from ghostlab import Pipeline, stage\n"
        "@stage\n"
        "def scale(ctx, item):\n"
        "    return item['n'] * ctx.params['factor']\n"
        "pipeline = Pipeline('scale', [scale], params={'factor': 2})\n"
    )
    inputs = tmp_path / "items.jsonl"
    inputs.write_text('{"id": "a", "n": 1}\n{"id": "b", "n": 5}\n')
    run_dir = tmp_path / "run"
    args = ["run", str(pipeline), "--run-dir", str(run_dir)]
    assert main([*args, "--input", str(inputs), "--param", "factor=3"]) == 0
    assert (run_dir / "results.jsonl").read_text().split() == ["3", "15"]
    assert main([*args, "--param", "factor=3"]) == 0  # resume with recorded inputs
    capsys.readouterr()
    assert main(["status", str(run_dir)]) == 0
    assert " 2 cached" in capsys.readouterr().out
    assert main(args) == 0  # a changed param recomputes
    assert (run_dir / "results.jsonl").read_text().split() == ["2", "10"]
    assert main(["run", f"{pipeline}:nope", "--run-dir", str(run_dir)]) == 2
