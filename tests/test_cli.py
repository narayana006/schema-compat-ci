import io
import shutil
import subprocess
from pathlib import Path

import pytest

from schema_compat_ci.cli import main, run

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
V1 = EXAMPLES / "order-v1.avsc"
GOOD = EXAMPLES / "order-v2-compatible.avsc"
BAD = EXAMPLES / "order-v2-breaking.avsc"


def cli(*argv):
    out = io.StringIO()
    return run([str(a) for a in argv], out=out), out.getvalue()


def test_file_pair_pass_and_fail():
    code, text = cli("check", "--old", V1, "--new", GOOD, "--mode", "FULL")
    assert code == 0 and text.startswith("PASS") and "All 1 schema(s) compatible." in text
    code, text = cli("check", "--old", V1, "--new", BAD)  # BACKWARD by default
    assert code == 1 and "(BACKWARD)" in text
    assert "[BACKWARD] Order.customer_id:" in text and "[FORWARD]" not in text
    code, text = cli("check", "--old", V1, "--new", BAD, "--mode", "forward")
    assert code == 1 and "[FORWARD] Order.legacy_ref:" in text and "[BACKWARD]" not in text
    assert cli("check", "--old", V1, "--new", BAD, "--mode", "NONE")[0] == 0


def test_invalid_schema_is_an_error_not_a_pass(tmp_path):
    broken = tmp_path / "broken.avsc"
    broken.write_text('{"type": "record", "name": "Order"}')
    code, text = cli("check", "--old", V1, "--new", broken)
    assert code == 2 and text.startswith("ERROR") and "needs a list of fields" in text
    proto = tmp_path / "a.proto"
    proto.write_text('syntax = "proto3";')
    code, text = cli("check", "--old", proto, "--new", proto)
    assert code == 2 and "Avro (.avsc) only" in text


def test_github_annotations_only_inside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert "::error" not in cli("check", "--old", V1, "--new", BAD)[1]
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    text = cli("check", "--old", V1, "--new", BAD)[1]
    lines = [line for line in text.splitlines() if line.startswith("::error ")]
    assert len(lines) == 2 and all("title=Incompatible schema change::[BACKWARD] Order." in line for line in lines)
    assert f"file={BAD}," in lines[0]


@pytest.mark.parametrize(
    "argv",
    [["check"], ["check", "--old", str(V1)], ["check", "--old", str(V1), "--new", str(GOOD), "extra"],
     ["check", "--base-ref", "HEAD"], ["check", "--registry", "http://localhost:1"],
     ["check", "--registry", "http://localhost:1", "--mode", "FULL", str(V1)],
     ["check", "--registry", "http://localhost:1", "--subject-template", "fixed", str(V1)],
     ["check", "--old", str(V1), "--new", str(GOOD), "--base-ref", "HEAD"],
     ["check", "--old", "/nonexistent.avsc", "--new", str(GOOD)],
     ["check", "--base-ref", "HEAD", "/nonexistent-dir"]],
)
def test_usage_errors_exit_2(argv, capsys):
    assert main(argv) == 2
    assert "error:" in capsys.readouterr().err


def test_unknown_mode_is_rejected_by_the_parser():
    with pytest.raises(SystemExit) as exc:
        main(["check", "--old", str(V1), "--new", str(GOOD), "--mode", "SIDEWAYS"])
    assert exc.value.code == 2


@pytest.fixture
def repo(tmp_path):
    """A git repository with two committed schemas."""
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "schemas" / "nested").mkdir(parents=True)
    shutil.copy(V1, tmp_path / "schemas" / "orders-value.avsc")
    shutil.copy(V1, tmp_path / "schemas" / "nested" / "returns-value.avsc")
    (tmp_path / "schemas" / "notes.json").write_text("{}")
    git("add", ".")
    git("commit", "-q", "-m", "schemas")
    return tmp_path


def test_git_mode_unchanged_tree_passes(repo, monkeypatch):
    monkeypatch.chdir(repo)
    code, text = cli("check", "--base-ref", "HEAD", "schemas")
    assert code == 0 and text.count("PASS") == 2 and "notes.json" not in text


def test_git_mode_reports_changed_new_and_broken_files(repo, monkeypatch):
    monkeypatch.chdir(repo)
    shutil.copy(BAD, repo / "schemas" / "orders-value.avsc")
    shutil.copy(GOOD, repo / "schemas" / "nested" / "returns-value.avsc")
    shutil.copy(V1, repo / "schemas" / "brand-new.avsc")
    code, text = cli("check", "--base-ref", "main", "--mode", "FULL", "schemas")
    assert code == 1
    lines = text.splitlines()
    assert "NEW   schemas/brand-new.avsc (no earlier version to compare with)" in lines
    assert "PASS  schemas/nested/returns-value.avsc (FULL)" in lines
    assert "FAIL  schemas/orders-value.avsc (FULL)" in lines
    assert "1 incompatible, 0 error(s), 3 schema(s) checked." in lines
    # a single file given by absolute path, from another directory
    monkeypatch.chdir(repo / "schemas" / "nested")
    code, text = cli("check", "--base-ref", "HEAD", repo / "schemas" / "orders-value.avsc")
    assert code == 1 and "Order.customer_id" in text


def test_git_mode_errors(repo, tmp_path_factory, capsys):
    assert main(["check", "--base-ref", "no-such-branch", str(repo / "schemas")]) == 2
    assert "revision 'no-such-branch' not found" in capsys.readouterr().err
    outside = tmp_path_factory.mktemp("outside") / "x.avsc"
    shutil.copy(V1, outside)
    assert main(["check", "--base-ref", "HEAD", str(outside)]) == 2
    assert "not inside a git repository" in capsys.readouterr().err


def test_empty_directory_reports_no_files(repo, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / "empty").mkdir()
    code, text = cli("check", "--base-ref", "HEAD", "empty")
    assert code == 0 and "No schema files found." in text
