import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REMOVED_ROOT_ARTIFACTS = {
    "calibrate.py",
    "decode_font.py",
    "download_novel.py",
    "font_decode_map.json",
    "main.py",
    "merge_final.py",
    "verify_parts.py",
}


def test_built_wheel_contains_cli_and_configs():
    wheels = sorted(Path("dist").glob("novel_crawler-*.whl"))
    assert wheels, "run python -m build before this test"
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
    assert "novel_crawler/cli.py" in names
    assert "novel_crawler/configs/example.json" in names
    assert "novel_crawler/configs/example.yaml" in names


def test_private_one_off_root_artifacts_are_not_distributed():
    present = sorted(name for name in REMOVED_ROOT_ARTIFACTS if (ROOT / name).exists())
    assert present == []


def test_dockerfile_installs_matching_chromium_and_runs_as_non_root():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    install_at = dockerfile.index("pip install --no-cache-dir .")
    assert dockerfile.index("COPY . .") < install_at
    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=" in dockerfile
    assert "useradd" in dockerfile
    assert "USER novel" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'VOLUME ["/app/data"]' in dockerfile
    assert 'ENTRYPOINT ["novel-crawler", "--data-dir", "/app/data"]' in dockerfile
    for ignored in (".git", ".worktrees", "data", "dist", "build", "htmlcov", ".coverage"):
        assert ignored in dockerignore


def test_build_workflow_builds_packages_and_smoke_tests_docker_browser():
    workflow_text = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert "branches: [main, master]" in workflow_text
    assert 'tags: ["v*"]' in workflow_text
    commands = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "run" in step
    ]
    joined = "\n".join(commands)
    assert "python -m build" in joined
    assert "dist/*.whl" in joined and "dist/*.tar.gz" in joined
    assert "docker build" in joined
    smoke_command = next(command for command in commands if "docker run" in command and " env" in command)
    assert "Runtime:" in smoke_command
    assert any("docker run" in command and "playwright" in command and "chromium.launch" in command for command in commands)


def test_ci_workflows_gate_supported_pythons_quality_coverage_and_privacy():
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(Path(".github/workflows").glob("*.yml"))
    )
    for version in ('"3.11"', '"3.12"', '"3.13"'):
        assert version in text
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in text
    for required in (
        "python -m ruff check",
        "python -m mypy",
        "--cov-fail-under=80",
        "--fail-under=85",
        "-W error",
        "RUN_PLAYWRIGHT_INTEGRATION=1",
        "-m release",
        "test -f LICENSE",
        "__version__",
        "REMOVED_ROOT_ARTIFACTS",
        "actions/download-artifact@v4",
    ):
        assert required in text
