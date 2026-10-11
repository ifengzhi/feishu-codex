"""The audit must find deleted secrets without revealing their values."""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit-secrets.py"


def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    env = {**os.environ, "FEISHU_CODEX_CONFIG": str(config), "CODEX_HOME": str(tmp_path / "codex")}
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo, env


def audit(repo, env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True)


def test_audit_finds_deleted_known_secret_in_history_without_printing_it(tmp_path):
    repo, env = setup(tmp_path)
    secret = "audit-only-" + "sensitive-value-" * 2
    env["AUDIT_TEST_SECRET"] = secret
    path = repo / "old.txt"
    path.write_text(secret)
    subprocess.run(["git", "add", "old.txt"], cwd=repo, check=True)
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    identity = subprocess.run([*git, "var", "GIT_COMMITTER_IDENT"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.rsplit(">", 1)[0] + ">"
    message = ("test(audit): add a historical credential fixture\n\nBackground:\n"
               "1. The audit regression needs a secret removed from the worktree.\n\nChanges:\n"
               "1. Store a synthetic credential in this isolated temporary repository.\n\nValidation:\n"
               "1. The enclosing test validates detection after creating this fixture.\n\n"
               f"Signed-off-by: {identity}\n")
    subprocess.run([*git, "commit", "-q", "-F", "-"], input=message, text=True, cwd=repo, check=True)
    path.write_text("removed from worktree")
    assert audit(repo, env).returncode == 0
    result = audit(repo, env, "--history")
    assert result.returncode == 1 and "Git blob" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_audit_checks_new_untracked_files_and_patterns_without_printing_values(tmp_path):
    repo, env = setup(tmp_path)
    secret = "sk-" + "A1b2C3d4" * 5
    (repo / "new.txt").write_text(secret)
    result = audit(repo, env)
    assert result.returncode == 1 and "new.txt" in result.stdout and "pattern" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_audit_leaves_ignored_private_config_out_of_project_scan(tmp_path):
    repo, env = setup(tmp_path)
    (repo / ".gitignore").write_text("config.toml\n")
    (repo / "config.toml").write_text('app_secret = "' + "privatevalue" * 3 + '"')
    result = audit(repo, env)
    assert result.returncode == 0 and "no known credential" in result.stdout
