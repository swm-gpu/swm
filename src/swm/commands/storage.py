"""swm storage — manage cloud storage buckets (GCS, Backblaze B2, S3)."""
from __future__ import annotations

import click
from rich.table import Table

from swm.commands._helpers import console


@click.group()
def storage():
    """Manage cloud storage buckets (GCS, Backblaze B2, S3)."""


@storage.command(name="list")
@click.option("--provider", "-p", default=None, type=click.Choice(["gcs", "b2", "s3"], case_sensitive=False), help="Filter to one provider")
def storage_list(provider: str | None):
    """List buckets across configured storage providers."""
    from swm.storage import get_configured_storage, get_storage

    sources = [get_storage(provider)] if provider else get_configured_storage()
    if not sources:
        console.print(
            "[yellow]No storage providers configured.[/yellow]\n"
            "  GCS:  swm config set gcp.project <id>\n"
            "  B2:   swm config set b2.key_id <id> && swm config set b2.app_key <key>\n"
            "  S3:   Configure AWS credentials (aws configure)"
        )
        return

    all_buckets = []
    with console.status("Fetching buckets…", spinner="dots") as spin:
        for s in sources:
            spin.update(f"Querying {s.name}…")
            try:
                buckets = s.list_buckets()
                all_buckets.extend(buckets)
                console.log(f"[green]✓[/green] {s.name} — {len(buckets)} buckets")
            except Exception as e:
                console.log(f"[red]✗[/red] {s.name}: {e}")

    if not all_buckets:
        console.print("[dim]No buckets found. Create one with: swm storage create <name> -p <provider>[/dim]")
        return

    table = Table(title="Storage Buckets", title_style="bold", show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Bucket", style="cyan")
    table.add_column("Location")
    table.add_column("Class")
    table.add_column("Created")
    table.add_column("Default", justify="center")

    from swm.storage import resolve_bucket
    try:
        _, default_name = resolve_bucket()
    except Exception:
        default_name = None

    for b in all_buckets:
        table.add_row(
            b.provider,
            b.name,
            b.location or "—",
            b.storage_class or "—",
            b.created or "—",
            "[green]✓[/green]" if b.name == default_name else "",
        )

    console.print()
    console.print(table)


@storage.command()
@click.argument("name")
@click.option("--provider", "-p", required=True, type=click.Choice(["gcs", "b2", "s3"], case_sensitive=False), help="Storage provider")
@click.option("--location", "-l", default="", help="Bucket location/region")
@click.option("--storage-class", "-c", default="", help="Storage class (STANDARD, NEARLINE, allPrivate, etc.)")
def create(name: str, provider: str, location: str, storage_class: str):
    """Create a storage bucket.

    \b
    Examples:
      swm storage create swm-models -p gcs
      swm storage create my-models -p b2
    """
    from swm.storage import get_storage

    s = get_storage(provider)
    try:
        with console.status(f"Creating bucket on {s.name}…", spinner="dots"):
            bucket = s.create_bucket(name, location=location, storage_class=storage_class)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Bucket created on {s.name}: {bucket.name}")
    if bucket.location:
        console.print(f"  Location:      {bucket.location}")
    if bucket.storage_class:
        console.print(f"  Storage class: {bucket.storage_class}")
    console.print(f"[green]✓[/green] Saved as default {provider} bucket in swm config")


@storage.command(name="ls")
@click.argument("path", default="")
@click.option("--bucket", "-b", default=None, help="Bucket name (default from config). Use 'provider:bucket' for explicit.")
def storage_ls(path: str, bucket: str | None):
    """List contents of a bucket.

    \b
    Examples:
      swm storage ls                      # default bucket root
      swm storage ls models/              # subdirectory
      swm storage ls -b gcs:swm-models    # explicit provider:bucket
      swm storage ls -b b2:my-backup      # backblaze bucket
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
    except Exception as e:
        raise click.ClickException(str(e))

    try:
        with console.status("Listing objects…", spinner="dots"):
            objects = provider.ls(bucket_name, prefix=path)
    except Exception as e:
        raise click.ClickException(str(e))

    label = f"{provider.slug}:{bucket_name}/{path}" if path else f"{provider.slug}:{bucket_name}/"
    console.print(f"\n[bold]{label}[/bold]")

    if not objects:
        console.print("[dim]  (empty)[/dim]")
        return

    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Name")
    table.add_column("Size", justify="right")
    table.add_column("Modified")

    for obj in objects:
        style = "bold" if obj.is_directory else ""
        table.add_row(
            f"[{style}]{obj.key}[/{style}]" if style else obj.key,
            obj.size_display,
            obj.modified or "—",
        )

    console.print(table)


@storage.command()
@click.argument("local_path", type=click.Path(exists=True))
@click.argument("remote_path")
@click.option("--bucket", "-b", default=None, help="Target bucket (provider:bucket)")
def upload(local_path: str, remote_path: str, bucket: str | None):
    """Upload a file to a bucket.

    Example: swm storage upload ./model.safetensors models/model.safetensors
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
        with console.status("Uploading…", spinner="dots"):
            provider.upload(local_path, bucket_name, remote_path)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Uploaded to {provider.slug}:{bucket_name}/{remote_path}")


@storage.command()
@click.argument("remote_path")
@click.argument("local_path", type=click.Path())
@click.option("--bucket", "-b", default=None, help="Source bucket (provider:bucket)")
def download(remote_path: str, local_path: str, bucket: str | None):
    """Download a file from a bucket.

    Example: swm storage download models/model.safetensors ./model.safetensors
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
        with console.status("Downloading…", spinner="dots"):
            provider.download(bucket_name, remote_path, local_path)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Downloaded {provider.slug}:{bucket_name}/{remote_path} → {local_path}")
