"""swm images — list curated Docker images per provider."""
from __future__ import annotations

import click
from rich.table import Table

from swm.commands._helpers import console
from swm.images import list_images
from swm.providers import PROVIDER_SLUGS


@click.group()
def images():
    """List Docker images available to swm pod create --image."""


@images.command(name="list")
@click.option(
    "--provider", "-p",
    type=click.Choice(list(PROVIDER_SLUGS), case_sensitive=False),
    default="runpod",
    help="Cloud provider (default: runpod).",
)
@click.option(
    "--cuda", "cuda_filter", default=None,
    help="Filter to a CUDA major.minor version (e.g. 12.8).",
)
@click.option("--refresh", is_flag=True, help="Bypass the local cache.")
@click.option("--limit", "-n", default=20, type=int, help="Max rows (default: 20).")
@click.option("--all", "show_all", is_flag=True, help="Show every image.")
def images_list(
    provider: str,
    cuda_filter: str | None,
    refresh: bool,
    limit: int,
    show_all: bool,
):
    """List Docker images discoverable for a provider, sorted newest-first."""
    with console.status(f"Fetching {provider} images…", spinner="dots"):
        try:
            imgs = list_images(provider, refresh=refresh)
        except Exception as exc:
            raise click.ClickException(f"Could not fetch images: {exc}")

    if not imgs:
        console.print(
            f"[yellow]No image catalog available for {provider}.[/yellow]\n"
            "[dim]swm only resolves images for providers that publish a "
            "Docker Hub catalog (currently: runpod). Pass --image directly "
            "for other providers.[/dim]"
        )
        return

    if cuda_filter:
        target = cuda_filter.strip()
        target_mm = ".".join(target.split(".")[:2])
        imgs = [
            i for i in imgs
            if ".".join(i.cuda.split(".")[:2]) == target_mm
        ]

    if not imgs:
        console.print(f"[yellow]No images matching --cuda {cuda_filter}.[/yellow]")
        return

    total = len(imgs)
    truncated = False
    if not show_all and total > limit:
        imgs = imgs[:limit]
        truncated = True

    title = f"{provider} images"
    if cuda_filter:
        title += f" (cuda={cuda_filter})"
    if truncated:
        title += f"  (top {limit} of {total})"

    table = Table(title=title, title_style="bold", show_lines=False)
    table.add_column("Tag", style="cyan", overflow="fold")
    table.add_column("CUDA", justify="right", no_wrap=True, min_width=6)
    table.add_column("Torch", justify="right", no_wrap=True, min_width=6)
    table.add_column("Ubuntu", justify="right", no_wrap=True, min_width=6)
    table.add_column("Updated", style="dim", no_wrap=True, min_width=10)

    for i in imgs:
        updated = i.last_updated.split("T", 1)[0] if i.last_updated else "—"
        table.add_row(
            i.tag,
            i.cuda,
            i.torch or "—",
            i.ubuntu,
            updated,
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[dim]Pick a tag and pass it to "
        "[bold]swm pod create --image <tag>[/bold], or use "
        "[bold]--cuda <X.Y>[/bold] to auto-pick the newest matching tag.[/dim]"
    )
    console.print(
        "[dim]Note: host driver may still constrain which CUDA toolkits "
        "actually run — verify with [bold]nvidia-smi[/bold] after the pod "
        "is up.[/dim]"
    )
