"""swm use — set (or clear) the active pod used by commands."""
from __future__ import annotations

import click

from swm import config as cfg
from swm.commands._helpers import (
    _ACTIVE_POD_ENV,
    clear_active_pod,
    complete_pod_id,
    console,
    get_active_pod,
    set_active_pod,
)


@click.command(name="use")
@click.argument(
    "instance_id",
    required=False,
    shell_complete=complete_pod_id,
)
@click.option("--clear", "do_clear", is_flag=True, help="Unset the active pod.")
@click.option("--show", "do_show", is_flag=True, help="Print the currently active pod.")
def use(instance_id: str | None, do_clear: bool, do_show: bool) -> None:
    """Select which pod other commands should target by default.

    \b
    Subsequent commands (swm ssh, swm run, swm sync push, swm setup install, …)
    may omit the pod id and will fall back to this selection.

    \b
    Resolution order for pod id arguments:
      1. explicit CLI argument
      2. $SWM_POD environment variable
      3. active pod set via `swm use`

    \b
    Examples:
      swm use vastai:12345678         # set active pod
      swm use 12345678                # bare id also works
      swm use --show                  # print active pod
      swm use --clear                 # unset active pod

    \b
    Tip: enable shell completion once so TAB suggests known pod ids:
      eval "$(_SWM_COMPLETE=bash_source swm)"     # bash
      eval "$(_SWM_COMPLETE=zsh_source swm)"      # zsh
      eval (env _SWM_COMPLETE=fish_source swm)    # fish
    """
    import os

    if do_clear:
        clear_active_pod()
        console.print("[green]✓[/green] Cleared active pod.")
        return

    if do_show or (instance_id is None and not do_clear):
        active = get_active_pod()
        env_val = os.environ.get(_ACTIVE_POD_ENV)
        if env_val:
            console.print(f"Active pod: [bold]{env_val}[/bold] [dim](from ${_ACTIVE_POD_ENV})[/dim]")
        elif active:
            console.print(f"Active pod: [bold]{active}[/bold] [dim](from config)[/dim]")
        else:
            console.print("[dim]No active pod set.[/dim]")
            pods = cfg.get("pods") or {}
            if isinstance(pods, dict) and pods:
                console.print("\n[bold]Known pods:[/bold]")
                for pid, meta in pods.items():
                    name = (meta or {}).get("name", "")
                    prov = (meta or {}).get("provider", "")
                    label = f"{prov}:{pid}" if prov else pid
                    console.print(f"  {label}  [dim]{name}[/dim]")
                console.print("\nSet one with: [bold]swm use <pod_id>[/bold]")
        return

    set_active_pod(instance_id)
    console.print(f"[green]✓[/green] Active pod: [bold]{instance_id}[/bold]")
