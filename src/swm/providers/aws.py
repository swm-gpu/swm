from __future__ import annotations

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)

GPU_INSTANCE_MAP: dict[str, dict] = {
    "h200": {"type": "p5en.48xlarge", "gpus": 8, "display": "H200 SXM", "vram": 141},
    "b200": {"type": "p6-b200.48xlarge", "gpus": 8, "display": "B200", "vram": 192},
    "h100": {"type": "p5.48xlarge", "gpus": 8, "display": "H100 SXM", "vram": 80},
}

_INSTANCE_TO_GPU = {v["type"]: k for k, v in GPU_INSTANCE_MAP.items()}

_STATUS = {
    "pending": InstanceStatus.PENDING,
    "running": InstanceStatus.RUNNING,
    "stopping": InstanceStatus.STOPPED,
    "stopped": InstanceStatus.STOPPED,
    "shutting-down": InstanceStatus.TERMINATED,
    "terminated": InstanceStatus.TERMINATED,
}

DEFAULT_AMI = "ami-0a0e5d9c7acc336f1"


def _boto3():
    try:
        import boto3
        return boto3
    except ImportError:
        raise RuntimeError(
            "boto3 required for AWS. Install with: pip install 'swm[aws]'"
        )


class AWSProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "AWS"

    @property
    def slug(self) -> str:
        return "aws"

    def _region(self) -> str:
        return str(cfg.get("aws.region", "us-east-1"))

    def _ec2(self):
        return _boto3().client("ec2", region_name=self._region())

    def is_configured(self) -> bool:
        try:
            _boto3().client("sts").get_caller_identity()
            return True
        except Exception:
            return False

    def list_instances(self) -> list[Instance]:
        ec2 = self._ec2()
        gpu_types = [v["type"] for v in GPU_INSTANCE_MAP.values()]
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "instance-type", "Values": gpu_types},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        return [
            self._to_instance(i)
            for r in resp["Reservations"]
            for i in r["Instances"]
        ]

    def create_instance(self, config: CreateConfig) -> Instance:
        spec = GPU_INSTANCE_MAP.get(config.gpu_type)
        if not spec:
            raise RuntimeError(
                f"Unknown GPU type '{config.gpu_type}' for AWS. "
                f"Available: {', '.join(GPU_INSTANCE_MAP)}"
            )

        params: dict = {
            "ImageId": config.image or str(cfg.get("aws.ami", DEFAULT_AMI)),
            "InstanceType": spec["type"],
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": config.name}],
                }
            ],
        }
        for cfg_key, param in [
            ("aws.key_name", "KeyName"),
            ("aws.subnet_id", "SubnetId"),
        ]:
            val = cfg.get(cfg_key)
            if val:
                params[param] = str(val)
        sg = cfg.get("aws.security_group")
        if sg:
            params["SecurityGroupIds"] = [str(sg)]

        resp = self._ec2().run_instances(**params)
        return self._to_instance(resp["Instances"][0])

    def start_instance(self, instance_id: str) -> Instance:
        ec2 = self._ec2()
        ec2.start_instances(InstanceIds=[instance_id])
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        return self._to_instance(resp["Reservations"][0]["Instances"][0])

    def stop_instance(self, instance_id: str) -> Instance:
        ec2 = self._ec2()
        ec2.stop_instances(InstanceIds=[instance_id])
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        return self._to_instance(resp["Reservations"][0]["Instances"][0])

    def terminate_instance(self, instance_id: str) -> bool:
        self._ec2().terminate_instances(InstanceIds=[instance_id])
        return True

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        from swm.pricing.providers import OFFERINGS

        return [
            GpuInfo(
                provider=self.slug,
                type_id=GPU_INSTANCE_MAP.get(o.gpu, {}).get("type", o.gpu),
                display_name=f"{o.gpu.upper()} ({GPU_INSTANCE_MAP.get(o.gpu, {}).get('type', '?')})",
                vram_gb=GPU_INSTANCE_MAP.get(o.gpu, {}).get("vram", 0),
                gpu_count=o.min_gpus,
                on_demand_price=o.on_demand,
                spot_price=o.spot,
                stock_level="",
                secure_cloud=True,
            )
            for o in OFFERINGS
            if o.provider == "AWS"
            and (gpu_count is None or o.min_gpus == gpu_count)
        ]

    def _to_instance(self, inst: dict) -> Instance:
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        itype = inst.get("InstanceType", "")
        gpu_key = _INSTANCE_TO_GPU.get(itype, "")
        spec = GPU_INSTANCE_MAP.get(gpu_key, {})

        return Instance(
            provider=self.slug,
            id=inst["InstanceId"],
            name=tags.get("Name", ""),
            gpu_type=spec.get("display", itype),
            gpu_count=spec.get("gpus", 1),
            status=_STATUS.get(inst["State"]["Name"], InstanceStatus.UNKNOWN),
            ip_address=inst.get("PublicIpAddress"),
            ssh_host=inst.get("PublicIpAddress"),
            ssh_port=22,
            region=inst.get("Placement", {}).get("AvailabilityZone"),
            image=inst.get("ImageId"),
        )
