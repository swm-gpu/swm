"""Microsoft Azure cloud GPU provider.

Requires: pip install azure-identity azure-mgmt-compute azure-mgmt-network azure-mgmt-resource
"""

from __future__ import annotations

import re
import time

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
    resolve_gpu_type,
)

_DEFAULT_RG = "swm-resources"
_ADMIN_USER = "azureuser"
_DEFAULT_IMAGE = {
    "publisher": "Canonical",
    "offer": "0001-com-ubuntu-server-jammy",
    "sku": "22_04-lts-gen2",
    "version": "latest",
}

_POWER_STATUS = {
    "running": InstanceStatus.RUNNING,
    "deallocated": InstanceStatus.STOPPED,
    "deallocating": InstanceStatus.STOPPED,
    "stopped": InstanceStatus.STOPPED,
    "starting": InstanceStatus.PENDING,
}

_GPU_SIZES: dict[str, tuple[str, int, int]] = {
    "Standard_NC24ads_A100_v4": ("A100 80GB PCIe", 80, 1),
    "Standard_NC48ads_A100_v4": ("A100 80GB PCIe", 80, 2),
    "Standard_NC96ads_A100_v4": ("A100 80GB PCIe", 80, 4),
    "Standard_ND96asr_v4": ("A100 40GB NVLink", 40, 8),
    "Standard_ND96amsr_A100_v4": ("A100 80GB NVLink", 80, 8),
    "Standard_ND96isr_H100_v5": ("H100 80GB NVLink", 80, 8),
    "Standard_NC40ads_H100_v5": ("H100 NVL 94GB", 94, 1),
    "Standard_NC80adis_H100_v5": ("H100 NVL 94GB", 94, 2),
}


class AzureProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Azure"

    @property
    def slug(self) -> str:
        return "azure"

    def is_configured(self) -> bool:
        return all(
            cfg.get(f"azure.{k}") is not None
            for k in ("tenant_id", "client_id", "client_secret", "subscription_id")
        )

    def _creds(self) -> tuple[str, str, str, str]:
        vals = []
        for k in ("tenant_id", "client_id", "client_secret", "subscription_id"):
            v = cfg.get(f"azure.{k}")
            if not v:
                raise RuntimeError(
                    f"azure.{k} not configured. "
                    f"Run: swm config set azure.{k} <value>"
                )
            vals.append(str(v))
        return tuple(vals)  # type: ignore[return-value]

    def _clients(self):
        try:
            from azure.identity import ClientSecretCredential
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient
            from azure.mgmt.resource import ResourceManagementClient
        except ImportError:
            raise RuntimeError(
                "Azure SDK not installed. Run: "
                "pip install azure-identity azure-mgmt-compute "
                "azure-mgmt-network azure-mgmt-resource"
            )

        tenant, client_id, secret, sub = self._creds()
        cred = ClientSecretCredential(tenant, client_id, secret)
        return (
            ComputeManagementClient(cred, sub),
            NetworkManagementClient(cred, sub),
            ResourceManagementClient(cred, sub),
        )

    def _rg(self) -> str:
        return str(cfg.get("azure.resource_group") or _DEFAULT_RG)

    def _location(self) -> str:
        return str(cfg.get("azure.location") or "eastus")

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        compute, network, _ = self._clients()
        rg = self._rg()
        results: list[Instance] = []
        try:
            for vm in compute.virtual_machines.list(rg):
                results.append(self._vm_to_instance(compute, network, rg, vm))
        except Exception:
            pass
        return results

    def get_instance(self, instance_id: str) -> Instance:
        compute, network, _ = self._clients()
        rg = self._rg()
        vm = compute.virtual_machines.get(rg, instance_id, expand="instanceView")
        return self._vm_to_instance(compute, network, rg, vm)

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        results: list[GpuInfo] = []
        for size_name, (display, vram, n) in _GPU_SIZES.items():
            if gpu_count is not None and n != gpu_count:
                continue
            results.append(GpuInfo(
                provider=self.slug,
                type_id=size_name,
                display_name=display,
                vram_gb=vram,
                gpu_count=n,
                stock_level="available",
                secure_cloud=True,
            ))
        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        compute, network, resource = self._clients()
        rg = self._rg()
        loc = self._location()
        vm_name = re.sub(r"[^a-zA-Z0-9-]", "-", config.name)[:64]

        candidates = list(_GPU_SIZES.keys())
        vm_size = resolve_gpu_type(config.gpu_type, candidates)

        resource.resource_groups.create_or_update(rg, {"location": loc})

        ip_result = network.public_ip_addresses.begin_create_or_update(
            rg, f"{vm_name}-ip",
            {"location": loc, "sku": {"name": "Standard"},
             "public_ip_allocation_method": "Static"},
        ).result()

        vnet = network.virtual_networks.begin_create_or_update(
            rg, f"{vm_name}-vnet",
            {"location": loc, "address_space": {"address_prefixes": ["10.0.0.0/16"]},
             "subnets": [{"name": "default",
                          "properties": {"address_prefix": "10.0.0.0/24"}}]},
        ).result()

        nic = network.network_interfaces.begin_create_or_update(
            rg, f"{vm_name}-nic",
            {"location": loc,
             "ip_configurations": [{
                 "name": "ipconfig1",
                 "subnet": {"id": vnet.subnets[0].id},
                 "public_ip_address": {"id": ip_result.id},
             }]},
        ).result()

        ssh_key = _read_ssh_pubkey()

        vm_params = {
            "location": loc,
            "hardware_profile": {"vm_size": vm_size},
            "storage_profile": {
                "image_reference": _DEFAULT_IMAGE,
                "os_disk": {
                    "create_option": "FromImage",
                    "disk_size_gb": max(config.volume_gb, 128),
                    "managed_disk": {"storage_account_type": "Premium_LRS"},
                },
            },
            "os_profile": {
                "computer_name": vm_name,
                "admin_username": _ADMIN_USER,
                "linux_configuration": {
                    "disable_password_authentication": True,
                    "ssh": {"public_keys": [{
                        "path": f"/home/{_ADMIN_USER}/.ssh/authorized_keys",
                        "key_data": ssh_key,
                    }]},
                },
            },
            "network_profile": {"network_interfaces": [{"id": nic.id}]},
        }

        compute.virtual_machines.begin_create_or_update(rg, vm_name, vm_params).result()
        return self.get_instance(vm_name)

    def start_instance(self, instance_id: str) -> Instance:
        compute, _, _ = self._clients()
        compute.virtual_machines.begin_start(self._rg(), instance_id).result()
        return self.get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        compute, _, _ = self._clients()
        compute.virtual_machines.begin_deallocate(self._rg(), instance_id).result()
        return self.get_instance(instance_id)

    def terminate_instance(self, instance_id: str) -> bool:
        compute, network, _ = self._clients()
        rg = self._rg()

        try:
            vm = compute.virtual_machines.get(rg, instance_id)
        except Exception:
            return False

        nic_ids = [n.id for n in (vm.network_profile.network_interfaces or [])]
        compute.virtual_machines.begin_delete(rg, instance_id).result()

        for nic_id in nic_ids:
            nic_name = nic_id.split("/")[-1]
            try:
                nic_obj = network.network_interfaces.get(rg, nic_name)
                pip_ids = [
                    ip.public_ip_address.id
                    for ip in (nic_obj.ip_configurations or [])
                    if ip.public_ip_address
                ]
                network.network_interfaces.begin_delete(rg, nic_name).result()
                for pip_id in pip_ids:
                    pip_name = pip_id.split("/")[-1]
                    network.public_ip_addresses.begin_delete(rg, pip_name).result()
            except Exception:
                pass

        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _vm_to_instance(self, compute, network, rg: str, vm) -> Instance:
        status = InstanceStatus.UNKNOWN
        if hasattr(vm, "instance_view") and vm.instance_view:
            for s in (vm.instance_view.statuses or []):
                if s.code.startswith("PowerState/"):
                    power = s.code.split("/", 1)[1]
                    status = _POWER_STATUS.get(power, InstanceStatus.UNKNOWN)

        if status == InstanceStatus.UNKNOWN:
            prov_state = (vm.provisioning_state or "").lower()
            if prov_state == "creating":
                status = InstanceStatus.PENDING
            elif prov_state == "succeeded":
                status = InstanceStatus.RUNNING

        ip = self._resolve_ip(network, rg, vm)
        size = vm.hardware_profile.vm_size if vm.hardware_profile else ""
        gpu_info = _GPU_SIZES.get(size, ("", 0, 0))

        return Instance(
            provider=self.slug,
            id=vm.name,
            name=vm.name,
            gpu_type=gpu_info[0] or size,
            gpu_count=gpu_info[2],
            status=status,
            region=vm.location,
            ip_address=ip,
            ssh_host=ip,
            ssh_port=22,
            ssh_user=_ADMIN_USER,
        )

    @staticmethod
    def _resolve_ip(network, rg: str, vm) -> str | None:
        try:
            nics = vm.network_profile.network_interfaces or []
            if not nics:
                return None
            nic_name = nics[0].id.split("/")[-1]
            nic = network.network_interfaces.get(rg, nic_name)
            pip_ref = nic.ip_configurations[0].public_ip_address
            if not pip_ref:
                return None
            pip_name = pip_ref.id.split("/")[-1]
            pip = network.public_ip_addresses.get(rg, pip_name)
            return pip.ip_address
        except Exception:
            return None


def _read_ssh_pubkey() -> str:
    import pathlib
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = pathlib.Path.home() / ".ssh" / name
        if path.exists():
            return path.read_text().strip()
    raise RuntimeError(
        "No SSH public key found in ~/.ssh/. "
        "Generate one with: ssh-keygen -t ed25519"
    )
