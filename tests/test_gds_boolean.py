"""Tests for tools/gds_boolean.py (F2 M2.5 Boolean expression engine)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import gds_boolean as gb  # noqa: E402
from gds_boolean import (  # noqa: E402
    BooleanExprError,
    Ref, Not, Morph, BinOp,
    parse_expression,
    referenced_layers,
    rects_to_geometry,
    layer_geometry,
    evaluate,
    geometry_to_polygons,
    make_mask,
    compose,
    normalize_binding,
    recipe_dependency_order,
    resolve_expression,
)

shapely = pytest.importorskip("shapely")
from shapely import box  # noqa: E402


# ── Parser ───────────────────────────────────────────────────────────


class TestParse:

    def test_single_ref(self):
        name, ast = parse_expression("A")
        assert name is None
        assert ast == Ref("A")

    def test_assignment_prefix(self):
        name, ast = parse_expression("L0 = A & B")
        assert name == "L0"
        assert isinstance(ast, BinOp) and ast.op == "&"

    def test_and(self):
        _, ast = parse_expression("A & B")
        assert ast == BinOp("&", Ref("A"), Ref("B"))

    def test_or_and_diff(self):
        _, ast = parse_expression("A | B")
        assert ast == BinOp("|", Ref("A"), Ref("B"))
        _, ast2 = parse_expression("A - B")
        assert ast2 == BinOp("-", Ref("A"), Ref("B"))

    def test_complement(self):
        _, ast = parse_expression("~A")
        assert ast == Not(Ref("A"))

    def test_morph_grow_shrink(self):
        _, ast = parse_expression("A > W:10")
        assert isinstance(ast, Morph) and ast.sign == 1 and ast.amount == 10
        _, ast2 = parse_expression("A < H:5")
        assert isinstance(ast2, Morph) and ast2.sign == -1 and ast2.amount == 5

    def test_grouping_parens_and_brackets(self):
        _, ast = parse_expression("[(A > W:10) & B] < H:10")
        # Top node is the outer shrink.
        assert isinstance(ast, Morph) and ast.sign == -1 and ast.amount == 10
        inner = ast.child
        assert isinstance(inner, BinOp) and inner.op == "&"
        assert isinstance(inner.left, Morph) and inner.left.sign == 1

    def test_precedence_complement_over_morph(self):
        # ~A > W:5  parses as (~A) > W:5  (~ binds tighter than morph)
        _, ast = parse_expression("~A > W:5")
        assert isinstance(ast, Morph)
        assert isinstance(ast.child, Not)

    def test_precedence_and_over_or(self):
        # A | B & C  ->  A | (B & C)
        _, ast = parse_expression("A | B & C")
        assert isinstance(ast, BinOp) and ast.op == "|"
        assert isinstance(ast.right, BinOp) and ast.right.op == "&"

    def test_referenced_layers(self):
        _, ast = parse_expression("[(A > W:10) & B] - C")
        assert referenced_layers(ast) == {"A", "B", "C"}

    @pytest.mark.parametrize("bad", [
        "", "A &", "& A", "A B", "(A", "A]", "A > W", "A > W:", "A @ B",
        "A = ",
    ])
    def test_syntax_errors(self, bad):
        with pytest.raises(BooleanExprError):
            parse_expression(bad)


# ── Geometry builders ────────────────────────────────────────────────


class TestGeometryBuilders:

    def test_rects_to_geometry_area(self):
        g = rects_to_geometry(np.array([[0, 0, 10, 10]], dtype=float))
        assert g.area == pytest.approx(100.0)

    def test_rects_union_overlap(self):
        g = rects_to_geometry(np.array([[0, 0, 10, 10],
                                        [5, 0, 15, 10]], dtype=float))
        # Union of two 10x10 overlapping by 5 -> 150 area.
        assert g.area == pytest.approx(150.0)

    def test_empty_rects(self):
        g = rects_to_geometry(np.empty((0, 4)))
        assert g.is_empty

    def test_layer_geometry_combines(self):
        bb = np.array([[0, 0, 10, 10]], dtype=float)
        poly = [np.array([[20, 0], [30, 0], [30, 10], [20, 10]], dtype=float)]
        g = layer_geometry(bboxes=bb, polys=poly)
        assert g.area == pytest.approx(200.0)


# ── Evaluator ────────────────────────────────────────────────────────


class TestEvaluate:

    def _bind(self):
        A = box(0, 0, 10, 10)
        B = box(5, 0, 15, 10)
        return {"A": A, "B": B}

    def test_intersection(self):
        _, ast = parse_expression("A & B")
        g = evaluate(ast, self._bind())
        assert g.area == pytest.approx(50.0)   # 5x10 overlap

    def test_union(self):
        _, ast = parse_expression("A | B")
        g = evaluate(ast, self._bind())
        assert g.area == pytest.approx(150.0)

    def test_difference(self):
        _, ast = parse_expression("A - B")
        g = evaluate(ast, self._bind())
        assert g.area == pytest.approx(50.0)   # A minus overlap

    def test_complement(self):
        _, ast = parse_expression("~A")
        fov = box(0, 0, 20, 10)   # area 200
        g = evaluate(ast, self._bind(), fov_bbox=fov)
        assert g.area == pytest.approx(100.0)  # 200 - A(100)

    def test_complement_without_fov_raises(self):
        _, ast = parse_expression("~A")
        with pytest.raises(BooleanExprError):
            evaluate(ast, self._bind())

    def test_grow_width_is_directional(self):
        # > W:5 grows X only (5 per side): 10x10 -> 20x10 = 200 (NOT 400).
        _, ast = parse_expression("A > W:5")
        g = evaluate(ast, {"A": box(0, 0, 10, 10)})
        assert g.area == pytest.approx(200.0)
        x0, y0, x1, y1 = g.bounds
        assert (x0, y0, x1, y1) == pytest.approx((-5.0, 0.0, 15.0, 10.0))

    def test_grow_height_is_directional(self):
        # > H:5 grows Y only: 10x10 -> 10x20 = 200.
        _, ast = parse_expression("A > H:5")
        g = evaluate(ast, {"A": box(0, 0, 10, 10)})
        assert g.area == pytest.approx(200.0)
        assert g.bounds == pytest.approx((0.0, -5.0, 10.0, 15.0))

    def test_shrink_height_is_directional(self):
        # < H:2 shrinks Y only (2 per side): 10x10 -> 10x6 = 60.
        _, ast = parse_expression("A < H:2")
        g = evaluate(ast, {"A": box(0, 0, 10, 10)},
                     fov_bbox=box(-50, -50, 50, 50))
        assert g.area == pytest.approx(60.0)
        assert g.bounds == pytest.approx((0.0, 2.0, 10.0, 8.0))

    def test_shrink_width_is_directional(self):
        _, ast = parse_expression("A < W:2")
        g = evaluate(ast, {"A": box(0, 0, 10, 10)},
                     fov_bbox=box(-50, -50, 50, 50))
        assert g.area == pytest.approx(60.0)
        assert g.bounds == pytest.approx((2.0, 0.0, 8.0, 10.0))

    def test_shrink_without_fov_raises(self):
        _, ast = parse_expression("A < W:1")
        with pytest.raises(BooleanExprError):
            evaluate(ast, {"A": box(0, 0, 10, 10)})

    def test_full_example(self):
        # L0 = [(A > W:1) & B] < H:1
        A = box(0, 0, 10, 10)
        B = box(0, 0, 10, 10)
        _, ast = parse_expression("[(A > W:1) & B] < H:1")
        g = evaluate(ast, {"A": A, "B": B}, fov_bbox=box(-50, -50, 50, 50))
        # A grow X by1 -> 12x10; & B (10x10) -> 10x10; shrink Y by1 -> 10x8=80
        assert g.area == pytest.approx(80.0)

    def test_bad_axis_label_raises(self):
        with pytest.raises(BooleanExprError):
            parse_expression("A > Q:5")

    def test_missing_binding_raises(self):
        _, ast = parse_expression("A & Z")
        with pytest.raises(BooleanExprError, match="not bound"):
            evaluate(ast, self._bind())

    def test_complement_with_fov_box_helper(self):
        _, ast = parse_expression("~A")
        fov = gb.fov_box(10, 5, 20, 10)   # center (10,5), 20x10 -> area 200
        g = evaluate(ast, {"A": box(0, 0, 10, 10)}, fov_bbox=fov)
        assert g.area == pytest.approx(100.0)


# ── Output A: polygons ───────────────────────────────────────────────


class TestGeometryToPolygons:

    def test_single_box(self):
        polys = geometry_to_polygons(box(0, 0, 10, 10))
        assert len(polys) == 1
        assert polys[0].shape[1] == 2

    def test_empty(self):
        from shapely import Polygon
        assert geometry_to_polygons(Polygon()) == []

    def test_multipolygon(self):
        g = rects_to_geometry(np.array([[0, 0, 10, 10],
                                        [100, 100, 110, 110]], dtype=float))
        polys = geometry_to_polygons(g)
        assert len(polys) == 2


# ── Output B: mask ───────────────────────────────────────────────────


class TestMakeMask:

    def test_full_square_no_invert(self):
        # 10nm box, 1nm/px, FOV bottom-left (0,0), 10x10 image.
        g = box(0, 0, 10, 10)
        m = make_mask(g, width_px=10, height_px=10,
                      x_min_nm=0, y_min_nm=0, nm_per_px=1.0,
                      invert_y=False)
        assert m.shape == (10, 10)
        assert m.dtype == np.uint8
        assert (m == 255).all()

    def test_partial_box(self):
        # Box covers left half only.
        g = box(0, 0, 5, 10)
        m = make_mask(g, width_px=10, height_px=10,
                      x_min_nm=0, y_min_nm=0, nm_per_px=1.0,
                      invert_y=False)
        assert m[:, :5].mean() > 200   # left half white
        assert (m[:, 6:] == 0).all()   # right side black

    def test_empty_geometry(self):
        from shapely import Polygon
        m = make_mask(Polygon(), width_px=8, height_px=8,
                      x_min_nm=0, y_min_nm=0, nm_per_px=1.0)
        assert (m == 0).all()

    def test_hole_cut_out(self):
        outer = box(0, 0, 20, 20)
        inner = box(8, 8, 12, 12)
        ring = outer.difference(inner)
        m = make_mask(ring, width_px=20, height_px=20,
                      x_min_nm=0, y_min_nm=0, nm_per_px=1.0,
                      invert_y=False)
        # Center pixel is inside the hole -> black.
        assert m[10, 10] == 0
        # A corner pixel is material -> white.
        assert m[1, 1] == 255

    def test_invert_y_flips_rows(self):
        # Box in bottom half of GDS (y 0..5). With invert_y the white
        # band lands in the bottom rows of the image array.
        g = box(0, 0, 10, 5)
        m = make_mask(g, width_px=10, height_px=10,
                      x_min_nm=0, y_min_nm=0, nm_per_px=1.0,
                      invert_y=True)
        assert m[9, 5] == 255   # bottom row white
        assert m[0, 5] == 0     # top row black


# ── compose convenience ──────────────────────────────────────────────


class TestCompose:

    def test_compose_roundtrip(self):
        name, g = compose("L0 = A & B",
                           {"A": box(0, 0, 10, 10), "B": box(5, 0, 15, 10)})
        assert name == "L0"
        assert g.area == pytest.approx(50.0)


# ── F4: tagged bindings + nested recipes ─────────────────────────────


class TestNormalizeBinding:

    def test_legacy_pair_becomes_raw(self):
        assert normalize_binding((17, 101)) == ("raw", 17, 101)

    def test_tagged_raw_passes_through(self):
        assert normalize_binding(("raw", 3, 0)) == ("raw", 3, 0)

    def test_ref_passes_through(self):
        assert normalize_binding(("ref", "L0")) == ("ref", "L0")

    def test_ref_coerces_name_to_str(self):
        assert normalize_binding(["ref", "X"]) == ("ref", "X")

    def test_bad_binding_raises(self):
        with pytest.raises(BooleanExprError):
            normalize_binding(("nope", 1, 2, 3))


class TestRecipeDependencyOrder:

    def test_independent_recipes(self):
        order = recipe_dependency_order({"A": set(), "B": set()})
        assert set(order) == {"A", "B"}

    def test_dependency_before_dependent(self):
        order = recipe_dependency_order({"L0": set(), "L1": {"L0"}})
        assert order.index("L0") < order.index("L1")

    def test_chain(self):
        order = recipe_dependency_order(
            {"A": set(), "B": {"A"}, "C": {"B"}})
        assert order == ["A", "B", "C"]

    def test_cycle_raises(self):
        with pytest.raises(BooleanExprError):
            recipe_dependency_order({"A": {"B"}, "B": {"A"}})

    def test_self_cycle_raises(self):
        with pytest.raises(BooleanExprError):
            recipe_dependency_order({"A": {"A"}})

    def test_unknown_reference_raises(self):
        with pytest.raises(BooleanExprError):
            recipe_dependency_order({"A": {"ghost"}})


class TestResolveExpression:

    def _raw(self, layer, datatype):
        # Two unit-ish squares keyed by (layer, datatype).
        table = {
            (1, 0): box(0, 0, 10, 10),
            (2, 0): box(5, 0, 15, 10),
            (3, 0): box(0, 0, 4, 10),
        }
        return table[(layer, datatype)]

    def test_raw_only(self):
        g = resolve_expression(
            "A & B", {"A": ("raw", 1, 0), "B": ("raw", 2, 0)},
            raw_provider=self._raw, recipe_provider=lambda n: None)
        assert g.area == pytest.approx(50.0)

    def test_legacy_pair_binding(self):
        g = resolve_expression(
            "A", {"A": (1, 0)},
            raw_provider=self._raw, recipe_provider=lambda n: None)
        assert g.area == pytest.approx(100.0)

    def test_nested_reference(self):
        # L0 = A & B (area 50, x 5..15). L1 = L0 - C (C = x 0..4 -> no overlap).
        recipes = {"L0": ("A & B", {"A": ("raw", 1, 0), "B": ("raw", 2, 0)})}
        g = resolve_expression(
            "L0 - C", {"L0": ("ref", "L0"), "C": ("raw", 3, 0)},
            raw_provider=self._raw,
            recipe_provider=lambda n: recipes.get(n))
        assert g.area == pytest.approx(50.0)

    def test_circular_reference_raises(self):
        recipes = {
            "L0": ("L1", {"L1": ("ref", "L1")}),
            "L1": ("L0", {"L0": ("ref", "L0")}),
        }
        with pytest.raises(BooleanExprError):
            resolve_expression(
                "L0", {"L0": ("ref", "L0")},
                raw_provider=self._raw,
                recipe_provider=lambda n: recipes.get(n))

    def test_unknown_reference_raises(self):
        with pytest.raises(BooleanExprError):
            resolve_expression(
                "X", {"X": ("ref", "ghost")},
                raw_provider=self._raw, recipe_provider=lambda n: None)
