import zipfile
from pathlib import Path

import yaml


def test_built_wheel_contains_cli_and_configs():
    wheels = sorted(Path("dist").glob("novel_crawler-*.whl"))
    assert wheels, "run python -m build before this test"
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
    assert "novel_crawler/cli.py" in names
    assert "novel_crawler/configs/example.json" in names
    assert "novel_crawler/configs/example.yaml" in names


def test_dockerfile_installs_project_after_copying_build_inputs():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    install_at = dockerfile.index("RUN pip install --no-cache-dir -r requirements.txt")
    for build_input in ("pyproject.toml", "README.md", "novel_crawler"):
        assert dockerfile.index(f"COPY {build_input}") < install_at
    assert 'VOLUME ["/app/data"]' in dockerfile
    assert 'ENTRYPOINT ["python", "main.py", "--data-dir", "/app/data"]' in dockerfile
    assert "!requirements.txt" in dockerignore


def test_build_workflow_builds_and_smoke_tests_docker_image():
    workflow = yaml.safe_load(Path(".github/workflows/build.yml").read_text(encoding="utf-8"))
    commands = [step["run"] for step in workflow["jobs"]["build"]["steps"] if "run" in step]
    assert "docker build -t novel-crawler ." in commands
    smoke_command = next(command for command in commands if "docker run --rm novel-crawler env" in command)
    assert "Runtime:" in smoke_command
