"""Workspace size vs pod disk preflight check (runs locally, no SSH)."""

from __future__ import annotations

from dataclasses import dataclass, field

from swm.bootstrap import SAFETY_MARGIN, _humanize, console


@dataclass
class DiskCheck:
    """Result of a workspace-vs-disk size comparison."""

    workspace_bytes: int = 0
    available_bytes: int = 0
    dir_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.workspace_bytes <= int(self.available_bytes * SAFETY_MARGIN)

    @property
    def overshoot(self) -> int:
        limit = int(self.available_bytes * SAFETY_MARGIN)
        return max(0, self.workspace_bytes - limit)


def _workspace_info_s3(
    storage_slug: str, bucket: str, workspace: str,
) -> tuple[int, int, dict[str, int]]:
    """Return (total_bytes, file_count, dir_sizes) via S3 ListObjectsV2."""
    from swm.storage import get_storage

    provider = get_storage(storage_slug)
    client = provider.s3
    paginator = client.get_paginator("list_objects_v2")
    prefix = f"{workspace}/"
    total = 0
    count = 0
    dir_sizes: dict[str, int] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sz = obj["Size"]
            total += sz
            count += 1
            rel = obj["Key"][len(prefix):]
            top = rel.split("/", 1)[0] if "/" in rel else ""
            if top:
                dir_sizes[top] = dir_sizes.get(top, 0) + sz
    return total, count, dir_sizes


def preflight_check(
    storage_slug: str,
    bucket: str,
    workspace: str,
    volume_gb: int,
) -> DiskCheck:
    """Check whether a workspace fits on the pod's disk.

    Runs entirely locally — queries the bucket via S3-compatible API
    and uses *volume_gb* from the provider API.  No SSH required.
    """
    console.print("\n[bold cyan]▸ Checking workspace size (local)[/bold cyan]")

    avail = int(volume_gb) * 1_073_741_824

    try:
        total, count, dir_sizes = _workspace_info_s3(storage_slug, bucket, workspace)
    except Exception as exc:
        console.print(f"  [yellow]⚠ Could not query bucket: {exc}[/yellow]")
        total, count, dir_sizes = 0, 0, {}

    console.print(
        f"  Workspace: [bold]{_humanize(total)}[/bold] ({count:,} files)  "
        f"Volume: [bold]{_humanize(avail)}[/bold]"
    )

    check = DiskCheck(workspace_bytes=total, available_bytes=avail)

    if check.fits:
        console.print("  [green]✓ Fits on disk[/green]")
        return check

    console.print(
        f"  [yellow]⚠ Workspace exceeds usable disk by "
        f"{_humanize(check.overshoot)}[/yellow]"
    )
    check.dir_sizes = dir_sizes
    return check
