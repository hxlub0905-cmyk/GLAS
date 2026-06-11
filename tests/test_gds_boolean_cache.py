"""P1 sub-expression dedup cache tests (gds_boolean.evaluate / resolve_expression).

Confirms the cache (a) preserves results (value-identical to no-cache) and
(b) actually avoids recomputing shared sub-trees / re-fetching raw layers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import gds_boolean as gb  # noqa: E402

pytest.importorskip("shapely", reason="shapely required")

from shapely.geometry import box  # noqa: E402


def _raw(geoms_by_ld, counter=None):
    """A raw_provider over a dict {(layer,datatype): geometry}; counts calls
    per (layer,datatype) into ``counter`` if given."""
    def provider(layer, datatype):
        if counter is not None:
            counter[(layer, datatype)] = counter.get((layer, datatype), 0) + 1
        return geoms_by_ld.get((layer, datatype), gb.polys_to_geometry([]))
    return provider


def _no_recipe(_name):
    return None


class TestRawProviderMemo:
    def test_repeated_raw_layer_fetched_once_with_cache(self):
        geoms = {(1, 0): box(0, 0, 10, 10)}
        counter = {}
        # A & A references layer A (1,0) twice.
        cache = {}
        gb.resolve_expression(
            "A & A", {"A": (1, 0)},
            raw_provider=_raw(geoms, counter), recipe_provider=_no_recipe,
            _eval_cache=cache)
        # raw_provider is called once per *letter* in resolve_expression's
        # binding loop (A is one letter) — the node cache then reuses A's geom
        # for both Ref occurrences (no second compute path).
        assert counter[(1, 0)] == 1


class TestSharedSubexpr:
    def test_shared_subtree_evaluated_once(self, monkeypatch):
        geoms = {(1, 0): box(0, 0, 10, 10), (2, 0): box(5, 5, 20, 20)}
        calls = {"morph": 0}
        real_morph = gb._morph_axis

        def counting_morph(*a, **k):
            calls["morph"] += 1
            return real_morph(*a, **k)

        monkeypatch.setattr(gb, "_morph_axis", counting_morph)

        # Two expressions sharing the sub-tree (A>W:2), evaluated with one
        # shared cache → the morph runs once, not twice.
        cache: dict = {}
        prov = _raw(geoms)
        gb.resolve_expression("(A>W:2) & B", {"A": (1, 0), "B": (2, 0)},
                              raw_provider=prov, recipe_provider=_no_recipe,
                              _eval_cache=cache)
        gb.resolve_expression("(A>W:2) | B", {"A": (1, 0), "B": (2, 0)},
                              raw_provider=prov, recipe_provider=_no_recipe,
                              _eval_cache=cache)
        assert calls["morph"] == 1

    def test_no_cache_recomputes(self, monkeypatch):
        geoms = {(1, 0): box(0, 0, 10, 10), (2, 0): box(5, 5, 20, 20)}
        calls = {"morph": 0}
        real_morph = gb._morph_axis

        def counting_morph(*a, **k):
            calls["morph"] += 1
            return real_morph(*a, **k)

        monkeypatch.setattr(gb, "_morph_axis", counting_morph)
        prov = _raw(geoms)
        # Separate calls, no shared cache → morph runs once per expression.
        gb.resolve_expression("(A>W:2) & B", {"A": (1, 0), "B": (2, 0)},
                              raw_provider=prov, recipe_provider=_no_recipe)
        gb.resolve_expression("(A>W:2) | B", {"A": (1, 0), "B": (2, 0)},
                              raw_provider=prov, recipe_provider=_no_recipe)
        assert calls["morph"] == 2


class TestEquivalence:
    @pytest.mark.parametrize("expr", [
        "A & B", "A | B", "A - B", "(A>W:3) - B", "(A>W:2<W:2) - (B>H:1)",
        "~A & B", "(A & B) | (A - B)",
    ])
    def test_cache_value_identical(self, expr):
        geoms = {(1, 0): box(0, 0, 30, 30), (2, 0): box(10, 10, 40, 40)}
        fov = gb.fov_box(20, 20, 100, 100)
        bindings = {"A": (1, 0), "B": (2, 0)}
        g_plain = gb.resolve_expression(
            expr, bindings, raw_provider=_raw(geoms),
            recipe_provider=_no_recipe, fov_bbox=fov)
        g_cached = gb.resolve_expression(
            expr, bindings, raw_provider=_raw(geoms),
            recipe_provider=_no_recipe, fov_bbox=fov, _eval_cache={})
        # Geometrically equal (symmetric difference area ~ 0).
        assert g_plain.equals(g_cached) or g_plain.symmetric_difference(
            g_cached).area < 1e-6


class TestDifferentLayersNotConfused:
    def test_same_letter_different_layer_distinct_keys(self):
        # Two expressions where 'A' means different raw layers must NOT share a
        # cached Ref result.
        geoms = {(1, 0): box(0, 0, 10, 10), (9, 0): box(100, 100, 110, 110)}
        cache: dict = {}
        g1 = gb.resolve_expression("A", {"A": (1, 0)},
                                   raw_provider=_raw(geoms),
                                   recipe_provider=_no_recipe, _eval_cache=cache)
        g2 = gb.resolve_expression("A", {"A": (9, 0)},
                                   raw_provider=_raw(geoms),
                                   recipe_provider=_no_recipe, _eval_cache=cache)
        assert g1.bounds[0] == 0
        assert g2.bounds[0] == 100     # not the cached (1,0) geometry


class TestMorphVectorizedEquivalence:
    """P4: the vectorized _dilate_axis (degenerate-edge skip + batched
    shapely.polygons) must be geometrically exact vs the per-edge reference,
    including non-rectilinear (diagonal) polygons where edge/sweep angles vary.
    """
    @staticmethod
    def _old_dilate(geom, n, axis):
        from shapely.affinity import translate as _tr
        from shapely import Polygon as _P
        from shapely.ops import unary_union as _uu
        vx, vy = (2.0 * n, 0.0) if axis == "W" else (0.0, 2.0 * n)
        base = _tr(geom, -vx / 2.0, -vy / 2.0)
        parts = [base, _tr(base, vx, vy)]
        for g in getattr(base, "geoms", [base]):
            for ring in [g.exterior] + list(g.interiors):
                cc = list(ring.coords)
                for (x0, y0), (x1, y1) in zip(cc, cc[1:]):
                    parts.append(_P([(x0, y0), (x1, y1),
                                     (x1 + vx, y1 + vy), (x0 + vx, y0 + vy)]))
        return _uu(parts)

    @pytest.mark.parametrize("axis", ["W", "H"])
    def test_rectilinear(self, axis):
        from shapely.geometry import box as _box
        from shapely.ops import unary_union as _uu
        geom = _uu([_box(0, 0, 10, 10), _box(20, 5, 35, 25)])
        new = gb._dilate_axis(geom, 3.0, axis)
        ref = self._old_dilate(geom, 3.0, axis)
        assert new.symmetric_difference(ref).area < 1e-6

    @pytest.mark.parametrize("axis", ["W", "H"])
    def test_diagonal_polygon(self, axis):
        from shapely.geometry import Polygon as _P
        tri = _P([(0, 0), (20, 5), (8, 20)])      # all edges oblique
        new = gb._dilate_axis(tri, 2.0, axis)
        ref = self._old_dilate(tri, 2.0, axis)
        assert new.symmetric_difference(ref).area < 1e-6

    def test_polygon_with_hole(self):
        from shapely.geometry import Polygon as _P
        ring = _P([(0, 0), (30, 0), (30, 30), (0, 30)],
                  [[(10, 10), (20, 10), (20, 20), (10, 20)]])
        new = gb._dilate_axis(ring, 2.0, "W")
        ref = self._old_dilate(ring, 2.0, "W")
        assert new.symmetric_difference(ref).area < 1e-6


class TestFineAlignCrossSpecShare:
    """P1 batch path: sharing walk_memo / raw_geom_memo / eval_cache across an
    image's POI specs walks each raw layer once and is result-equivalent to the
    unshared path."""

    def _reader(self, tmp_path):
        import subprocess
        oas = tmp_path / "s.oas"
        subprocess.run([sys.executable, "tools/bench/_make_sample_oasis.py",
                        str(oas), "300"], check=True,
                       cwd=Path(__file__).resolve().parents[1])
        import oasis_random as orx
        return orx.RandomAccessReader(
            str(oas), wanted_layers={(17, 0)},
            bbox_layer=orx.DEFAULT_BBOX_LAYER), orx

    def test_shared_walks_once_and_equivalent(self, tmp_path, monkeypatch):
        import fine_align as fa
        rar, orx = self._reader(tmp_path)
        roi = (180000.0, 160000.0, 220000.0, 200000.0)
        specs = [("expr", "A>W:7", {"A": (17, 0)}),
                 ("expr", "A>W:10", {"A": (17, 0)}),
                 ("expr", "A>W:7", {"A": (17, 0)})]
        calls = {"n": 0}
        real = fa._walk_roi_polys

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(fa, "_walk_roi_polys", counting)
        calls["n"] = 0
        unshared = [fa.poi_polys_for_roi(rar, "TOP", roi, s) for s in specs]
        n_unshared = calls["n"]
        calls["n"] = 0
        wm, rgm, ec = {}, {}, {}
        shared = [fa.poi_polys_for_roi(rar, "TOP", roi, s, walk_memo=wm,
                                       raw_geom_memo=rgm, eval_cache=ec)
                  for s in specs]
        n_shared = calls["n"]
        assert n_unshared == 3 and n_shared == 1   # layer 17 walked once
        # same polygon count per spec
        assert [len(x) for x in unshared] == [len(x) for x in shared]
