"""swm ssh / run / upload / download — remote instance commands."""
from __future__ import annotations

import os

import click

from swm.commands._helpers import (
    console,
    _instance_for,
    complete_pod_id,
    pod_arg_callback,
    split_pod_and_command,
)


@click.command(name="ssh")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def ssh_connect(instance_id: str):
    """Open an interactive SSH session.

    Example: swm ssh runpod:abc123
    """
    from swm.remote.ssh import interactive_ssh

    inst = _instance_for(instance_id)
    console.print(
        f"[bold]Connecting to {inst.name or inst.id}[/bold] "
        f"({inst.provider}) via SSH…"
    )
    try:
        code = interactive_ssh(inst)
    except Exception as e:
        raise click.ClickException(str(e))
    raise SystemExit(code)


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("instance_id", required=False, shell_complete=complete_pod_id)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.option("--quiet", "-q", is_flag=True, help="Suppress real-time output")
def run(instance_id: str | None, command: tuple[str, ...], quiet: bool):
    """Run a command on a remote instance.

    \b
    Examples:
      swm run runpod:abc123 nvidia-smi       # explicit pod id
      swm run nvidia-smi                     # uses active pod (`swm use`)
      swm run runpod:abc123 -- ls -la /ws    # use -- to escape option parsing
    """
    from swm.remote.ssh import session_from_instance

    instance_id, command = split_pod_and_command(instance_id, command)
    if not command:
        raise click.UsageError("Missing command to run on the pod.")

    inst = _instance_for(instance_id)
    cmd_str = " ".join(command)

    if not quiet:
        console.print(
            f"[dim]>>> {inst.provider}:{inst.id}[/dim] $ {cmd_str}\n"
        )

    try:
        with session_from_instance(inst) as sess:
            code, _, _ = sess.exec(cmd_str, stream=not quiet)
    except Exception as e:
        raise click.ClickException(str(e))

    if not quiet:
        style = "green" if code == 0 else "red"
        console.print(f"\n[{style}]Exit code: {code}[/{style}]")
    raise SystemExit(code)


@click.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("local_path", type=click.Path(exists=True))
@click.argument("remote_path", default="")
@click.option("-r", "--recursive", is_flag=True, help="Upload a directory recursively")
def upload(instance_id: str, local_path: str, remote_path: str, recursive: bool):
    """Upload a file or directory to a running instance.

    \b
    Remote path defaults to /workspace/. If a relative path is given
    (no leading /), it is placed under /workspace/.

    \b
    Examples:
      swm upload runpod:abc123 ./model.safetensors
      swm upload runpod:abc123 ./model.safetensors models/
      swm upload runpod:abc123 ./loras/ models/loras -r
    """
    from pathlib import Path
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    if not remote_path:
        remote_path = f"/workspace/{Path(local_path).name}"
    elif not remote_path.startswith("/"):
        remote_path = f"/workspace/{remote_path}"
    if remote_path.endswith("/"):
        remote_path = remote_path + Path(local_path).name

    if os.path.isdir(local_path):
        recursive = True

    console.print(
        f"[bold]Uploading[/bold] {local_path} → "
        f"{inst.provider}:{inst.id}:{remote_path}"
    )

    try:
        with session_from_instance(inst) as sess, \
             console.status("Uploading…", spinner="dots"):
            sess.upload(local_path, remote_path, recursive=recursive)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print("[green]✓ Upload complete[/green]")


@click.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("remote_path")
@click.option("-d", "--dir", "local_dir", default=".", type=click.Path(), help="Local directory to save into (default: current dir)")
def download(instance_id: str, remote_path: str, local_dir: str):
    """Download a file or directory from a running instance.

    \b
    Directories are transferred via tar-over-SSH (compressed stream) which
    is significantly faster than scp -r for multi-file directories.
    If remote_path doesn't start with /, it is treated as relative to /workspace/.

    \b
    Examples:
      swm download runpod:abc123 output.mp4
      swm download runpod:abc123 output.mp4 -d ~/Downloads
      swm download runpod:abc123 ComfyUI/output/ -d ./results
    """
    from pathlib import Path
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    if not remote_path.startswith("/"):
        remote_path = f"/workspace/{remote_path}"

    remote_path = remote_path.rstrip("/")

    try:
        with session_from_instance(inst) as sess:
            with console.status("Checking remote path…", spinner="dots"):
                is_dir = sess.is_directory(remote_path)

            local_dir = str(Path(local_dir).expanduser())

            if is_dir:
                import tempfile
                base_name = Path(remote_path).name
                final_dest = Path(local_dir) / base_name
                if final_dest.exists():
                    n = 1
                    while (Path(local_dir) / f"{base_name}_{n}").exists():
                        n += 1
                    final_dest = Path(local_dir) / f"{base_name}_{n}"
                    console.print(
                        f"  [yellow]⚠ Destination already exists — saving to "
                        f"[bold]{final_dest.name}[/bold] instead[/yellow]"
                    )

                console.print(
                    f"[bold]Downloading directory[/bold] "
                    f"{inst.provider}:{inst.id}:{remote_path} → {final_dest}"
                )
                console.print("  [dim]Using tar stream (compressed)[/dim]")

                with console.status("Counting files…", spinner="dots"):
                    total = sess.file_count(remote_path)

                from rich.progress import (
                    Progress, SpinnerColumn, BarColumn,
                    TaskProgressColumn, TimeRemainingColumn, TextColumn,
                )

                Path(local_dir).mkdir(parents=True, exist_ok=True)

                tmpdir_obj = tempfile.TemporaryDirectory(dir=local_dir)
                tmpdir = tmpdir_obj.name
                try:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[bold]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        TextColumn("[dim]{task.completed}/{task.total} files"),
                        TimeRemainingColumn(),
                        console=console,
                        transient=True,
                    ) as progress:
                        task = progress.add_task("Streaming…", total=total or None)

                        def _on_member(name: str) -> None:
                            if not name.endswith("/"):
                                progress.advance(task)
                                progress.update(task, description=Path(name).name[:40])

                        sess.download_dir(remote_path, tmpdir, progress_callback=_on_member)

                    extracted = Path(tmpdir) / base_name
                    extracted.rename(final_dest)
                finally:
                    tmpdir_obj.cleanup()
            else:
                Path(local_dir).mkdir(parents=True, exist_ok=True)
                dest = str(Path(local_dir) / Path(remote_path).name)
                console.print(
                    f"[bold]Downloading[/bold] "
                    f"{inst.provider}:{inst.id}:{remote_path} → {dest}"
                )
                with console.status("Downloading…", spinner="dots"):
                    sess.download(remote_path, dest)

    except Exception as e:
        raise click.ClickException(str(e))

    console.print("[green]✓ Download complete[/green]")
