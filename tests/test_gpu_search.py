"""Regression tests for provider-aware GPU search (PR #18).

All provider I/O is stubbed; no network, no credentials. Payloads are
modelled on the documented Vast.ai ``bundles/`` and RunPod ``gpuTypes``
responses.
"""

from __future__ import annotations

import pytest

from swm.providers.base import (
    CloudProvider,
    GpuInfo,
    GpuSearchQuery,
    UnsupportedSearchFilters,
)
from swm.providers.runpod import RunPodProvider
from swm.providers.vastai import VastAIProvider


def _vast_offers():
    offers = []
    for name, ram_mib in (("RTX 4090", 24564), ("H100 SXM", 81920 - 100)):
        for geo in ("US", "VN"):
            for hosting, verif, price in (
                (1, "verified", 0.50),    # certified datacenter: Secure
                (0, "verified", 0.30),    # verified community host
                (0, "unverified", 0.20),  # plain community host
            ):
                offers.append({
                    "gpu_name": name, "num_gpus": 1, "dph_total": price,
                    "geolocation": geo, "gpu_ram": ram_mib,
                    "hosting_type": hosting, "verification": verif,
                    "inet_up": 500.0, "inet_down": 900.0,
                })
    return offers


@pytest.fixture
def vast():
    p = VastAIProvider()
    p._post = lambda path, body=None: {"offers": _vast_offers()}
    p._get = lambda path, params=None: {"gpu_names": ["RTX 4090", "H100 SXM"]}
    return p


class TestVastSearch:
    def test_rows_split_by_region_and_tier(self, vast):
        rows = vast.list_gpus()
        # 2 GPUs x 2 regions x 2 tiers (datacenter-verified vs community)
        assert len(rows) == 8
        r4090_us = [r for r in rows
                    if r.display_name == "RTX 4090" and r.regions == ["US"]]
        assert len(r4090_us) == 2
        secure = next(r for r in r4090_us if r.secure_cloud)
        community = next(r for r in r4090_us if not r.secure_cloud)
        # Cheapest offer within each tier, never mixed across tiers.
        assert secure.on_demand_price == 0.50
        assert community.on_demand_price == 0.20

    def test_verified_community_is_not_secure(self, vast):
        rows = vast.list_gpus()
        community = [r for r in rows if not r.secure_cloud]
        # The $0.30 verified community offer must not carry a Secure marker.
        assert any(r.on_demand_price == 0.20 for r in community)
        assert all(r.on_demand_price != 0.30 or not r.secure_cloud
                   for r in rows)

    def test_hosting_type_above_one_is_secure(self):
        p = VastAIProvider()
        p._post = lambda path, body=None: {"offers": [{
            "gpu_name": "RTX 4090", "num_gpus": 1, "dph_total": 0.60,
            "geolocation": "US", "gpu_ram": 24564,
            "hosting_type": 2, "verification": "verified",
        }]}
        p._get = lambda path, params=None: {"gpu_names": ["RTX 4090"]}
        rows = p.list_gpus()
        assert len(rows) == 1 and rows[0].secure_cloud

    def test_vram_rounded_and_min_vram_local(self, vast):
        rows = vast.list_gpus()
        r4090 = next(r for r in rows if r.display_name == "RTX 4090")
        # 24564 MiB displays as 24 GB, not 23.
        assert r4090.vram_gb == 24
        # ...and therefore passes a 24 GB floor even though raw MiB < 24576.
        kept = vast.search_gpus(GpuSearchQuery(min_vram_gb=24))
        assert any(r.display_name == "RTX 4090" for r in kept)

    def test_secure_query_pushes_datacenter_and_verified(self):
        bodies = []
        p = VastAIProvider()
        p._post = lambda path, body=None: (
            bodies.append(body), {"offers": []})[1]
        p._get = lambda path, params=None: {"gpu_names": []}
        p.search_gpus(GpuSearchQuery(secure_only=True))
        assert bodies[0]["datacenter"] == {"eq": True}
        assert bodies[0]["verified"] == {"eq": True}

    def test_min_vram_not_sent_natively(self):
        bodies = []
        p = VastAIProvider()
        p._post = lambda path, body=None: (
            bodies.append(body), {"offers": []})[1]
        p._get = lambda path, params=None: {"gpu_names": []}
        p.search_gpus(GpuSearchQuery(min_vram_gb=24))
        assert "gpu_ram" not in bodies[0]

    def test_bandwidth_fields_populated(self, vast):
        row = vast.list_gpus()[0]
        assert row.upload_mbps == 500.0
        assert row.download_mbps == 900.0

    def test_gpu_names_falls_back_to_bundles(self):
        import httpx

        p = VastAIProvider()
        def fail_get(path, params=None):
            raise httpx.ConnectError("endpoint gone")
        p._get = fail_get
        p._post = lambda path, body=None: {"offers": [
            {"gpu_name": "RTX 4090"}, {"gpu_name": "RTX 4090"}, {},
        ]}
        assert p._gpu_names() == ["RTX 4090"]


_RUNPOD_TYPES = [
    {"id": "NVIDIA L40", "displayName": "L40", "memoryInGb": 48,
     "secureCloud": True, "communityCloud": True,
     "securePrice": {"minimumBidPrice": 0.40, "uninterruptablePrice": 0.82,
                     "stockStatus": "High"},
     "communityPrice": {"minimumBidPrice": 0.30, "uninterruptablePrice": 0.69,
                        "stockStatus": "High"}},
    {"id": "NVIDIA A100", "displayName": "A100 80GB", "memoryInGb": 80,
     "secureCloud": True, "communityCloud": False,
     "securePrice": {"minimumBidPrice": 1.00, "uninterruptablePrice": 1.89,
                     "stockStatus": "Low"},
     "communityPrice": {"minimumBidPrice": None, "uninterruptablePrice": None,
                        "stockStatus": None}},
]


@pytest.fixture
def runpod():
    p = RunPodProvider()
    p._gql = lambda query: {"gpuTypes": _RUNPOD_TYPES}
    return p


class TestRunPodSearch:
    def test_tier_split(self, runpod):
        rows = runpod.list_gpus()
        assert len(rows) == 3  # L40 secure + L40 community + A100 secure
        l40 = [r for r in rows if r.type_id == "NVIDIA L40"]
        assert {r.secure_cloud for r in l40} == {True, False}
        assert next(r for r in l40 if r.secure_cloud).on_demand_price == 0.82
        assert next(r for r in l40
                    if not r.secure_cloud).on_demand_price == 0.69

    def test_null_tier_skipped(self, runpod):
        rows = runpod.list_gpus()
        a100 = [r for r in rows if r.type_id == "NVIDIA A100"]
        # communityCloud is False and communityPrice is null: no community row.
        assert len(a100) == 1 and a100[0].secure_cloud

        # The flag check above runs first; exercise the null-price guard
        # directly with a tier that is advertised but has no current offer.
        p = RunPodProvider()
        p._gql = lambda q: {"gpuTypes": [{
            "id": "NVIDIA RTX 4090", "displayName": "RTX 4090",
            "memoryInGb": 24, "secureCloud": False, "communityCloud": True,
            "securePrice": None,
            "communityPrice": {"minimumBidPrice": None,
                               "uninterruptablePrice": None,
                               "stockStatus": None},
        }]}
        assert p.list_gpus() == []

    def test_prices_are_totals_for_gpu_count(self, runpod):
        rows = runpod.list_gpus(gpu_count=4)
        l40 = next(r for r in rows
                   if r.type_id == "NVIDIA L40" and r.secure_cloud)
        assert l40.on_demand_price == pytest.approx(0.82 * 4)
        assert l40.spot_price == pytest.approx(0.40 * 4)

    def test_secure_only_queries_secure_tier(self):
        queries = []
        p = RunPodProvider()
        p._gql = lambda q: (queries.append(q), {"gpuTypes": _RUNPOD_TYPES})[1]
        rows = p.search_gpus(GpuSearchQuery(secure_only=True))
        assert "communityPrice" not in queries[0]
        assert all(r.secure_cloud for r in rows)

    def test_region_refused_not_silently_empty(self, runpod):
        with pytest.raises(UnsupportedSearchFilters, match="region"):
            runpod.search_gpus(GpuSearchQuery(region="us-east"))


class TestBaseSearch:
    class _Stub(RunPodProvider):
        """Two rows, base-default search field sets (RunPod drops REGION,
        which would break the region case here)."""

        native_search_fields = CloudProvider.native_search_fields
        local_search_fields = CloudProvider.local_search_fields

        _rows = [
            GpuInfo(provider="stub", type_id="rtx-4090",
                    display_name="RTX 4090", vram_gb=24,
                    gpu_count=1, on_demand_price=0.50,
                    secure_cloud=True, regions=["US"]),
            GpuInfo(provider="stub", type_id="a100-80gb",
                    display_name="A100 80GB", vram_gb=80,
                    gpu_count=1, on_demand_price=1.89,
                    secure_cloud=False, regions=["EU"]),
        ]

        def __init__(self):
            pass

        @property
        def slug(self):
            return "stub"

        def _search_gpus(self, query):
            return list(self._rows)

    def test_local_filters(self):
        p = self._Stub()
        assert len(p.search_gpus(GpuSearchQuery(gpu="4090"))) == 1
        assert len(p.search_gpus(GpuSearchQuery(max_price=1.0))) == 1
        assert len(p.search_gpus(GpuSearchQuery(region="eu"))) == 1
        assert len(p.search_gpus(GpuSearchQuery(secure_only=True))) == 1
        assert len(p.search_gpus(GpuSearchQuery(min_vram_gb=48))) == 1
        assert len(p.search_gpus(GpuSearchQuery())) == 2

    def test_gpu_filter_is_punctuation_insensitive(self):
        p = self._Stub()
        assert len(p.search_gpus(GpuSearchQuery(gpu="rtx4090"))) == 1
        assert len(p.search_gpus(GpuSearchQuery(gpu="A100 80GB"))) == 1

    def test_unsupported_field_raises(self):
        p = self._Stub()
        with pytest.raises(UnsupportedSearchFilters,
                           match="min_download"):
            p.search_gpus(GpuSearchQuery(min_download_mbps=100))


class TestCostTrackerRate:
    def test_lookup_rate_takes_cheapest_tier(self, monkeypatch):
        import swm.providers
        from swm.costs import tracker

        monkeypatch.setattr(
            swm.providers, "get_provider", lambda slug: RunPodProvider())
        monkeypatch.setattr(
            RunPodProvider, "_gql",
            lambda self, q: {"gpuTypes": _RUNPOD_TYPES})
        # L40 has a $0.82 secure row and a $0.69 community row.
        assert tracker._lookup_rate("runpod", "NVIDIA L40", 1) == 0.69
