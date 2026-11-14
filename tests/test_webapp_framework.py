"""Tests for the Custom Web UI (webapp) framework definition and launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path

from swm import bootstrap_frameworks as bf
from swm.frameworks import get_framework, list_frameworks, webapp


def test_webapp_registered_in_framework_registry():
    fw = get_framework("webapp")
    assert fw.name == "webapp"
    assert fw.label == "Custom Web UI"
    assert fw.repo == ""
    assert fw.install_dir == "/workspace/app"
    assert fw.venv == "/workspace/app/venv"
    assert fw.ports == {7860: "http"}
    assert fw.ready_timeout == 600
    assert fw.category == "inference"
    assert fw.access == "ui"
    assert fw.installed_check == "[ -f /workspace/app/start.sh ]"
    assert fw.process_pattern == "/workspace/app/"
    assert bf._self_safe(fw.process_pattern) == "/[w]orkspace/app/"
    assert "webapp" in [f.name for f in list_frameworks()]


def test_webapp_launch_replaces_default_port_with_custom_port():
    fw = get_framework("webapp")
    launched: list[tuple[str, str | None]] = []

    class FakeSession:
        host = "pod"

        def exec_background(self, command, logfile="/dev/null", workdir=None, env_setup=""):
            launched.append((command, workdir))

    class_con = type("QuietConsole", (), {"print": lambda *a, **k: None})()
    runner = bf._Runner(FakeSession(), fw, class_con, None)
    runner.launch(8088, "--share")

    assert len(launched) == 1
    cmd, workdir = launched[0]
    assert cmd == "PORT=8088 bash /workspace/app/start.sh --port 8088 --share"
    assert workdir == "/workspace/app"


def test_webapp_existing_start_sh_is_preserved_and_made_executable(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    launcher = app_dir / "start.sh"
    custom_script = "#!/bin/bash\necho CUSTOM_CREATOR_LAUNCHER\n"
    launcher.write_text(custom_script)
    launcher.chmod(0o644)

    write_cmd = (
        webapp._WRITE_LAUNCHER
        .replace(webapp._LAUNCHER, str(launcher))
        .replace(webapp._INSTALL_DIR, str(app_dir))
    )
    check_cmd = f"[ -x {launcher} ]"

    # Before running _WRITE_LAUNCHER, non-executable custom start.sh fails [ -x ] check.
    assert subprocess.run(["bash", "-c", check_cmd], check=False).returncode != 0

    # Running _WRITE_LAUNCHER preserves the custom start.sh content and adds +x.
    assert subprocess.run(["bash", "-c", write_cmd], check=False).returncode == 0
    assert subprocess.run(["bash", "-c", check_cmd], check=False).returncode == 0
    assert launcher.read_text() == custom_script


def test_webapp_default_launcher_and_default_app_generation(tmp_path: Path):
    app_dir = tmp_path / "app"
    venv_bin = app_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        'printf "PORT=%s GRADIO=%s ARGS=%s\\n" "$PORT" "$GRADIO_SERVER_PORT" "$*"\n'
    )
    fake_python.chmod(0o755)
    launcher = app_dir / "start.sh"

    write_app_cmd = webapp._WRITE_DEFAULT_APP.replace(webapp._INSTALL_DIR, str(app_dir))
    write_launcher_cmd = (
        webapp._WRITE_LAUNCHER
        .replace(webapp._LAUNCHER, str(launcher))
        .replace(webapp._PYTHON, str(fake_python))
        .replace(webapp._VENV, str(app_dir / "venv"))
        .replace(webapp._INSTALL_DIR, str(app_dir))
    )

    assert subprocess.run(["bash", "-c", write_app_cmd], check=False).returncode == 0
    assert (app_dir / "app.py").is_file()
    compile((app_dir / "app.py").read_text(), str(app_dir / "app.py"), "exec")

    assert subprocess.run(["bash", "-c", write_launcher_cmd], check=False).returncode == 0
    proc = subprocess.run(
        ["bash", str(launcher), "--port", "9001", "--extra-flag"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"PORT=9001 GRADIO=9001 ARGS={app_dir / 'app.py'} --extra-flag" in proc.stdout


def test_webapp_default_app_not_created_when_user_entrypoint_exists(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "server.py").write_text("print('custom server')\n")

    write_app_cmd = webapp._WRITE_DEFAULT_APP.replace(webapp._INSTALL_DIR, str(app_dir))
    check_cmd = webapp._HAS_USER_APP_CHECK.replace(webapp._INSTALL_DIR, str(app_dir))

    assert subprocess.run(["bash", "-c", check_cmd], check=False).returncode == 0
    assert subprocess.run(["bash", "-c", write_app_cmd], check=False).returncode == 0
    assert not (app_dir / "app.py").exists()
