"""Vast.ai geolocation.eq contract.

The live bundles feed emits ``City, CC`` / ``, CC``. The search predicate
only matches an uppercase ISO country code. These tests pin the helper and
both call sites (search + create) so a ``regions[0]`` round-trip cannot
send ``OREGON, US`` again.

Create is stubbed through ``_put``; nothing is rented. Live tests are
search-only and skip when no key is configured.
"""
from __future__ import annotations

import pytest

from swm.providers.base import CreateConfig, GpuSearchQuery
from swm.providers.vastai import VastAIProvider, geolocation_eq


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("US", "US"),
        ("us", "US"),
        ("  us  ", "US"),
        ("Oregon, US", "US"),
        ("oregon, us", "US"),
        ("OREGON, US", "US"),
        (", US", "US"),
        (",US", "US"),
        ("United States, US", "US"),
        ("South Korea, KR", "KR"),
        ("France, FR", "FR"),
        ("Quebec, CA", "CA"),
        ("United Kingdom, GB", "GB"),
        ("UK", "UK"),  # valid alpha-2 shape; Vast wants GB, not our job here
        ("europe", None),
        ("us-east", None),
        ("Oregon", None),
        ("OR", "OR"),
        ("US-CA-2", None),
        ("Virginia, US, extra", None),  # tail is not a 2-letter code
    ],
)
def test_geolocation_eq(raw, expected):
    assert geolocation_eq(raw) == expected


def _search_bodies(region: str | None) -> list[dict]:
    bodies: list[dict] = []
    p = VastAIProvider()
    p._post = lambda path, body=None: (bodies.append(body), {"offers": []})[1]
    p._get = lambda path, params=None: {"gpu_names": ["B200"]}
    p._search_gpus(GpuSearchQuery(gpu="B200", gpu_count=1, region=region))
    return bodies


class TestSearchSendsCountryCode:
    def test_city_string_becomes_uppercase_cc(self):
        bodies = _search_bodies("Oregon, US")
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_empty_city_becomes_uppercase_cc(self):
        bodies = _search_bodies(", US")
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_lowercase_cc(self):
        bodies = _search_bodies("us")
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_does_not_uppercase_the_whole_city_string(self):
        bodies = _search_bodies("Oregon, US")
        assert bodies[0]["geolocation"] != {"eq": "OREGON, US"}
        assert bodies[0]["geolocation"] != {"eq": "Oregon, US"}

    def test_unparseable_region_omits_native_filter(self):
        bodies = _search_bodies("europe")
        assert "geolocation" not in bodies[0]

    def test_no_region_omits_native_filter(self):
        bodies = _search_bodies(None)
        assert "geolocation" not in bodies[0]


def _create(region: str | None, *, offers=None, sleep=lambda *_: None):
    bodies: list[dict] = []
    p = VastAIProvider()
    p._get = lambda path, params=None: {"gpu_names": ["B200"]}
    p._post = lambda path, body=None: (
        bodies.append(body),
        {"offers": offers if offers is not None else [
            {"id": 11, "machine_id": "m1", "geolocation": "Oregon, US"},
        ]},
    )[1]
    p._put = lambda path, body=None: {"new_contract": "42"}
    p.list_instances = lambda *a, **k: []
    import swm.providers.vastai as vastai_mod
    orig_sleep = vastai_mod.time.sleep
    vastai_mod.time.sleep = sleep
    try:
        inst = p.create_instance(CreateConfig(
            name="t", gpu_type="B200", gpu_count=1, region=region,
            cloud_type="COMMUNITY",
        ))
    finally:
        vastai_mod.time.sleep = orig_sleep
    return inst, bodies


class TestCreateSendsCountryCode:
    def test_city_string_becomes_uppercase_cc(self):
        inst, bodies = _create("Oregon, US")
        assert inst.id == "42"
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_empty_city_becomes_uppercase_cc(self):
        _, bodies = _create(", US")
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_lowercase_cc(self):
        _, bodies = _create("us")
        assert bodies[0]["geolocation"] == {"eq": "US"}

    def test_does_not_uppercase_the_whole_city_string(self):
        _, bodies = _create("Virginia, US")
        assert bodies[0]["geolocation"] == {"eq": "US"}
        assert bodies[0]["geolocation"] != {"eq": "VIRGINIA, US"}

    def test_no_region_omits_filter(self):
        _, bodies = _create(None)
        assert "geolocation" not in bodies[0]

    def test_unparseable_region_raises_before_search(self):
        with pytest.raises(RuntimeError, match="City, CC"):
            _create("europe")

    def test_empty_offers_still_mentions_original_region(self):
        with pytest.raises(RuntimeError, match="Oregon, US"):
            _create("Oregon, US", offers=[])

    def test_does_not_call_put_when_region_is_unparseable(self):
        p = VastAIProvider()
        p._get = lambda path, params=None: {"gpu_names": ["B200"]}
        p._post = lambda path, body=None: (_ for _ in ()).throw(
            AssertionError("search must not run"))
        p._put = lambda path, body=None: (_ for _ in ()).throw(
            AssertionError("rent must not run"))
        with pytest.raises(RuntimeError, match="City, CC"):
            p.create_instance(CreateConfig(
                name="t", gpu_type="B200", region="us-east",
                cloud_type="COMMUNITY",
            ))


class TestListGpusKeepsLiveCityStrings:
    def test_city_rows_stay_distinct(self):
        p = VastAIProvider()
        p._post = lambda path, body=None: {"offers": [
            {"gpu_name": "B200", "num_gpus": 1, "dph_total": 5.0,
             "geolocation": "Oregon, US", "gpu_ram": 180000,
             "hosting_type": 0, "verification": "unverified"},
            {"gpu_name": "B200", "num_gpus": 1, "dph_total": 5.5,
             "geolocation": "Virginia, US", "gpu_ram": 180000,
             "hosting_type": 0, "verification": "unverified"},
            {"gpu_name": "B200", "num_gpus": 1, "dph_total": 4.8,
             "geolocation": ", US", "gpu_ram": 180000,
             "hosting_type": 0, "verification": "unverified"},
        ]}
        rows = p.list_gpus()
        geos = {tuple(r.regions) for r in rows}
        assert geos == {("Oregon, US",), ("Virginia, US",), (", US",)}


def _live_provider():
    p = VastAIProvider()
    return p if p.is_configured() else None


def _bundles(p: VastAIProvider, body: dict) -> list[dict]:
    import time

    import httpx

    last = None
    for attempt in range(4):
        try:
            return p._post("bundles/", body).get("offers") or []
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code != 429 or attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last  # pragma: no cover


@pytest.mark.skipif(_live_provider() is None, reason="no vastai.api_key configured")
class TestLiveBundlesContract:
    """Lock the API behaviour the helper is written against. Search only."""

    def test_country_code_hits_city_rows_city_string_does_not(self):
        p = VastAIProvider()
        feed = _bundles(p, {
            "gpu_name": {"eq": "B200"},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "limit": 20,
        })
        if not feed:
            pytest.skip("no rentable B200 x1 right now")
        geos = [o.get("geolocation") or "" for o in feed]
        assert any("," in g for g in geos), geos
        assert not any(len(g) == 2 and g.isalpha() for g in geos), geos

        sample = geos[0]
        code = geolocation_eq(sample)
        assert code, f"could not parse live geo {sample!r}"

        by_code = _bundles(p, {
            "gpu_name": {"eq": "B200"},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "geolocation": {"eq": code},
            "limit": 20,
        })
        assert by_code
        assert any(o.get("geolocation") == sample for o in by_code)
        assert all(geolocation_eq(o.get("geolocation")) == code for o in by_code)

        # The 0.3.0 create path: uppercase the display string and find nothing.
        if "," in sample:
            broken = _bundles(p, {
                "gpu_name": {"eq": "B200"},
                "num_gpus": {"eq": 1},
                "rentable": {"eq": True},
                "geolocation": {"eq": sample.strip().upper()},
                "limit": 10,
            })
            assert broken == []
        lowercase = _bundles(p, {
            "gpu_name": {"eq": "B200"},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "geolocation": {"eq": code.lower()},
            "limit": 10,
        })
        assert lowercase == []


class TestSearchKeepsClickedCity:
    def test_local_filter_still_sees_city_string(self):
        p = VastAIProvider()
        p._post = lambda path, body=None: {"offers": [
            {"gpu_name": "B200", "num_gpus": 1, "dph_total": 5.0,
             "geolocation": "Oregon, US", "gpu_ram": 180000,
             "hosting_type": 0, "verification": "unverified"},
            {"gpu_name": "B200", "num_gpus": 1, "dph_total": 5.5,
             "geolocation": "Virginia, US", "gpu_ram": 180000,
             "hosting_type": 0, "verification": "unverified"},
        ]}
        p._get = lambda path, params=None: {"gpu_names": ["B200"]}
        rows = p.search_gpus(GpuSearchQuery(gpu="B200", region="Oregon, US"))
        assert [r.regions for r in rows] == [["Oregon, US"]]
