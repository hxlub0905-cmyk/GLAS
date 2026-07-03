"""Live Boolean expression engine for layer composition (F2 M2.5).

Replaces the old "precompute + store synthetic layer" design (see plan
Q9). The user types an HMI-style expression such as::

    L0 = [(A > W:10) & B] < H:10

bound to raw layers (``A = (17, 101)``, ``B = (25, 0)``), and the engine
evaluates it *live* on whatever polygons fall in the current FOV. Two
outputs come back (plan Q12):

* **A. shapely polygons** -- drawn on the GDS canvas so the user can
  confirm the ROI definition is right.
* **B. uint8 mask** (same size as the SEM image) -- the SEM measurement
  only looks for defects where the mask is white.

Grammar (precedence high -> low, plan M2.4)::

    1. ~              complement                  (unary prefix, highest)
    2. > W/H:n / < W/H:n  grow / shrink (nm)       (postfix morphology)
    3. &              intersection
    4. | / -          union / difference          (lowest)

    ( ... ) and [ ... ] both group.

The parser is a ~80-line hand-written recursive-descent (plan Q10): the
grammar is small and fixed, and hand-rolling keeps error messages sharp
and avoids a pyparsing dependency.

Operator semantics (shapely >= 2.0)::

    A & B     -> A.intersection(B)
    A | B     -> A.union(B)
    A - B     -> A.difference(B)
    ~A        -> fov_bbox.difference(A)
    A > W:n   -> grow width  (X) by n nm per side  (anisotropic, F4)
    A > H:n   -> grow height (Y) by n nm per side
    A < W:n   -> shrink width  (X) by n nm per side
    A < H:n   -> shrink height (Y) by n nm per side

``n`` is in nanometres (GDS coords are already nm). ``W``/``H`` pick the
axis; ``>``/``<`` pick grow/shrink. The bias is **directional** (F4): each
side along the chosen axis moves by ``n`` (so ``A > W:5`` on a 10×10 box
gives 20×10). Grow is the exact Minkowski sum with the axis segment;
shrink is the morphological erosion (complement-dilate-complement, so it
needs ``fov_bbox``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Union

import numpy as np

try:
    import shapely
    from shapely import Polygon, MultiPolygon, box, unary_union
    from shapely.affinity import translate
    from shapely.geometry.base import BaseGeometry
    _SHAPELY_OK = True
except Exception as exc:  # pragma: no cover - import guard
    _SHAPELY_OK = False
    _SHAPELY_ERR = exc

try:
    import cv2
    _CV2_OK = True
except Exception:  # pragma: no cover - import guard
    _CV2_OK = False


class BooleanExprError(ValueError):
    """Raised for any syntax or evaluation error in a layer expression."""


# ── AST nodes ────────────────────────────────────────────────────────


@dataclass
class Ref:
    """A bound layer reference, e.g. ``A``."""
    name: str


@dataclass
class Not:
    """Complement against the FOV bounding box: ``~child``."""
    child: object


@dataclass
class Morph:
    """Grow (``sign=+1``) or shrink (``sign=-1``) by ``amount`` nm."""
    child: object
    sign: int
    amount: float
    label: str   # 'W' / 'H' as written; informational only


@dataclass
class BinOp:
    """Binary set op: ``op`` in ``{'&', '|', '-'}``."""
    op: str
    left: object
    right: object


# ── Tokenizer ────────────────────────────────────────────────────────


_PUNCT = set("&|-~><:()[]=")


def _tokenize(text: str) -> list[tuple[str, object]]:
    """Split an expression into ``(kind, value)`` tokens.

    Kinds: ``IDENT`` (value=str), ``NUM`` (value=float), ``OP``
    (value=the punctuation char). Whitespace is ignored.
    """
    toks: list[tuple[str, object]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(("IDENT", text[i:j]))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            num = text[i:j]
            try:
                val = float(num)
            except ValueError as e:
                raise BooleanExprError(f"bad number {num!r}") from e
            toks.append(("NUM", val))
            i = j
            continue
        if c in _PUNCT:
            toks.append(("OP", c))
            i += 1
            continue
        raise BooleanExprError(f"unexpected character {c!r} at index {i}")
    return toks


# ── Recursive-descent parser ─────────────────────────────────────────


class _Parser:
    def __init__(self, toks: list[tuple[str, object]]) -> None:
        self._toks = toks
        self._pos = 0

    def _peek(self) -> Optional[tuple[str, object]]:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _next(self) -> tuple[str, object]:
        t = self._peek()
        if t is None:
            raise BooleanExprError("unexpected end of expression")
        self._pos += 1
        return t

    def _expect_op(self, ch: str) -> None:
        t = self._next()
        if t != ("OP", ch):
            raise BooleanExprError(f"expected {ch!r}, got {t!r}")

    def parse(self) -> object:
        node = self._parse_or()
        if self._peek() is not None:
            raise BooleanExprError(f"trailing tokens: {self._toks[self._pos:]}")
        return node

    # | and - (lowest precedence)
    def _parse_or(self) -> object:
        node = self._parse_and()
        while self._peek() in (("OP", "|"), ("OP", "-")):
            op = self._next()[1]
            node = BinOp(op, node, self._parse_and())
        return node

    # &
    def _parse_and(self) -> object:
        node = self._parse_morph()
        while self._peek() == ("OP", "&"):
            self._next()
            node = BinOp("&", node, self._parse_morph())
        return node

    # > W:n / < H:n  (postfix)
    def _parse_morph(self) -> object:
        node = self._parse_unary()
        while self._peek() in (("OP", ">"), ("OP", "<")):
            op = self._next()[1]
            sign = 1 if op == ">" else -1
            label_t = self._next()
            if label_t[0] != "IDENT":
                raise BooleanExprError(
                    f"expected W/H label after {op!r}, got {label_t!r}")
            label = str(label_t[1]).upper()
            if label not in ("W", "H"):
                raise BooleanExprError(
                    f"expected axis W or H after {op!r}, got {label_t[1]!r}")
            self._expect_op(":")
            num_t = self._next()
            if num_t[0] != "NUM":
                raise BooleanExprError(
                    f"expected nm amount after {op!r} {label}:, "
                    f"got {num_t!r}")
            node = Morph(node, sign, float(num_t[1]), label)
        return node

    # ~ (highest)
    def _parse_unary(self) -> object:
        if self._peek() == ("OP", "~"):
            self._next()
            return Not(self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> object:
        t = self._next()
        if t == ("OP", "("):
            node = self._parse_or()
            self._expect_op(")")
            return node
        if t == ("OP", "["):
            node = self._parse_or()
            self._expect_op("]")
            return node
        if t[0] == "IDENT":
            return Ref(str(t[1]))
        raise BooleanExprError(f"unexpected token {t!r}")


def parse_expression(text: str) -> tuple[Optional[str], object]:
    """Parse an expression string into ``(name, ast)``.

    Supports an optional ``NAME = ...`` assignment prefix (the ``L0 =``
    in ``L0 = [(A > W:10) & B] < H:10``). When absent, ``name`` is
    None. Raises :class:`BooleanExprError` on any syntax problem.
    """
    toks = _tokenize(text)
    if not toks:
        raise BooleanExprError("empty expression")
    name: Optional[str] = None
    # Detect "IDENT = ..." assignment prefix.
    if (len(toks) >= 2 and toks[0][0] == "IDENT" and toks[1] == ("OP", "=")):
        name = str(toks[0][1])
        toks = toks[2:]
        if not toks:
            raise BooleanExprError("expression after '=' is empty")
    ast = _Parser(toks).parse()
    return name, ast


def referenced_layers(ast: object) -> set[str]:
    """Return the set of layer-reference names used in ``ast``."""
    out: set[str] = set()

    def walk(n: object) -> None:
        if isinstance(n, Ref):
            out.add(n.name)
        elif isinstance(n, Not):
            walk(n.child)
        elif isinstance(n, Morph):
            walk(n.child)
        elif isinstance(n, BinOp):
            walk(n.left)
            walk(n.right)

    walk(ast)
    return out


# ── Bindings + nested-recipe resolution (F4) ─────────────────────────
#
# A binding maps an expression letter to a source of geometry. Two forms:
#
#   ("raw", layer, datatype)   -- a raw layout layer
#   ("ref", name)              -- another synthetic (expression) layer,
#                                 enabling nested composition L1 = L0 - C
#
# The legacy form ``(layer, datatype)`` (a 2-tuple) is read as ``("raw",
# layer, datatype)`` so older caches / sidecars keep working.


def normalize_binding(val) -> tuple:
    """Coerce a binding value into tagged form (see module note).

    ``(layer, datatype)`` -> ``("raw", layer, datatype)``; tagged tuples
    pass through unchanged. Raises :class:`BooleanExprError` otherwise."""
    t = tuple(val)
    if len(t) == 2 and t[0] not in ("raw", "ref"):
        return ("raw", int(t[0]), int(t[1]))
    if t and t[0] == "raw" and len(t) == 3:
        return ("raw", int(t[1]), int(t[2]))
    if t and t[0] == "ref" and len(t) == 2:
        return ("ref", str(t[1]))
    raise BooleanExprError(f"bad binding {val!r}")


def recipe_dependency_order(ref_map: Mapping[str, set]) -> list[str]:
    """Topologically order synthetic recipes so dependencies come first.

    ``ref_map`` maps each recipe name to the set of OTHER recipe names it
    references (via ``("ref", name)`` bindings). Returns the names in an
    order safe to evaluate in. Raises :class:`BooleanExprError` on a
    circular reference or a reference to an unknown recipe."""
    order: list[str] = []
    state: dict[str, int] = {}   # name -> 0 (on stack) / 1 (done)

    def visit(n: str, stack: list[str]) -> None:
        st = state.get(n)
        if st == 1:
            return
        if st == 0:
            cyc = " -> ".join(stack[stack.index(n):] + [n])
            raise BooleanExprError(f"circular reference: {cyc}")
        state[n] = 0
        for dep in sorted(ref_map.get(n, ())):
            if dep not in ref_map:
                raise BooleanExprError(
                    f"binding references unknown synthetic layer {dep!r}")
            visit(dep, stack + [n])
        state[n] = 1
        order.append(n)

    for name in ref_map:
        visit(name, [])
    return order


def resolve_expression(expr: str,
                       bindings: Mapping[str, tuple],
                       *,
                       raw_provider,
                       recipe_provider,
                       fov_bbox: Optional["BaseGeometry"] = None,
                       _cache: Optional[dict] = None,
                       _visiting: Optional[set] = None) -> "BaseGeometry":
    """Evaluate ``expr`` whose ``bindings`` may reference raw layers AND
    other synthetic recipes (nested composition).

    ``raw_provider(layer, datatype) -> geometry`` supplies a raw layer's
    geometry; ``recipe_provider(name) -> (expr, bindings) | None`` supplies
    a referenced recipe's definition. Referenced recipes are evaluated
    recursively and memoized in ``_cache``; cycles raise
    :class:`BooleanExprError`."""
    cache = {} if _cache is None else _cache
    visiting = set() if _visiting is None else _visiting
    _, ast = parse_expression(expr)
    geoms: dict[str, "BaseGeometry"] = {}
    for letter, raw_val in bindings.items():
        val = normalize_binding(raw_val)
        if val[0] == "raw":
            geoms[letter] = raw_provider(val[1], val[2])
            continue
        name = val[1]
        if name in cache:
            geoms[letter] = cache[name]
            continue
        if name in visiting:
            raise BooleanExprError(f"circular reference to {name!r}")
        rec = recipe_provider(name)
        if rec is None:
            raise BooleanExprError(
                f"binding references unknown synthetic layer {name!r}")
        visiting.add(name)
        g = resolve_expression(rec[0], rec[1], raw_provider=raw_provider,
                               recipe_provider=recipe_provider,
                               fov_bbox=fov_bbox, _cache=cache,
                               _visiting=visiting)
        visiting.discard(name)
        cache[name] = g
        geoms[letter] = g
    return evaluate(ast, geoms, fov_bbox=fov_bbox)


# ── Geometry helpers ─────────────────────────────────────────────────


def _require_shapely() -> None:
    if not _SHAPELY_OK:  # pragma: no cover - import guard
        raise BooleanExprError(
            f"shapely >= 2.0 is required for Boolean evaluation "
            f"(install: pip install 'shapely>=2.0'); import error: "
            f"{_SHAPELY_ERR}")


def rects_to_geometry(bboxes: np.ndarray) -> "BaseGeometry":
    """Union of axis-aligned boxes from an ``(N, 4)`` bbox array
    (rows ``x1, y1, x2, y2``). Empty input -> empty geometry."""
    _require_shapely()
    arr = np.asarray(bboxes, dtype=float)
    if arr.size == 0:
        return Polygon()
    boxes = [box(min(r[0], r[2]), min(r[1], r[3]),
                 max(r[0], r[2]), max(r[1], r[3])) for r in arr]
    return unary_union(boxes)


def polys_to_geometry(polys: list[np.ndarray]) -> "BaseGeometry":
    """Union of polygons from a list of ``(n, 2)`` point arrays. Rings
    with < 3 vertices are skipped. Empty input -> empty geometry."""
    _require_shapely()
    shapes = []
    for p in polys:
        pa = np.asarray(p, dtype=float)
        if pa.ndim == 2 and pa.shape[0] >= 3:
            shapes.append(Polygon(pa))
    if not shapes:
        return Polygon()
    return unary_union(shapes)


def union_geometries(geoms) -> "BaseGeometry":
    """Union an iterable of geometries into one, **preserving interior holes**
    (unlike re-unioning exterior-ring polygons). Empty / all-empty -> empty."""
    _require_shapely()
    parts = [g for g in geoms if g is not None and not g.is_empty]
    if not parts:
        return Polygon()
    return unary_union(parts)


def layer_geometry(bboxes: Optional[np.ndarray] = None,
                   polys: Optional[list[np.ndarray]] = None) -> "BaseGeometry":
    """Combine a layer's rectangles and polygons into one geometry.

    Either argument may be omitted/empty. Buffer(0) is applied to heal
    any self-touching boundaries from the union so downstream set ops
    behave."""
    _require_shapely()
    parts = []
    if bboxes is not None and np.asarray(bboxes).size:
        parts.append(rects_to_geometry(bboxes))
    if polys:
        parts.append(polys_to_geometry(polys))
    if not parts:
        return Polygon()
    g = unary_union(parts)
    return g if g.is_valid else g.buffer(0)


# ── Evaluator ────────────────────────────────────────────────────────


def _iter_rings(geom: "BaseGeometry"):
    """Yield every exterior + interior LinearRing of a (multi)polygon."""
    for g in getattr(geom, "geoms", [geom]):
        ext = getattr(g, "exterior", None)
        if ext is None:
            continue
        yield ext
        for ring in getattr(g, "interiors", []):
            yield ring


def _dilate_axis(geom: "BaseGeometry", n: float, axis: str) -> "BaseGeometry":
    """Exact axis-aligned dilation by ``n`` nm per side (``axis`` 'W'->X,
    'H'->Y): the Minkowski sum of ``geom`` with the centred segment
    ``[-n, n]`` on that axis. Computed as the union of ``geom``, its
    translated copy, and each boundary edge swept into a parallelogram —
    exact for arbitrary polygons (F4)."""
    if geom.is_empty or n <= 0:
        return geom
    vx, vy = (2.0 * n, 0.0) if axis == "W" else (0.0, 2.0 * n)
    base = translate(geom, -vx / 2.0, -vy / 2.0)   # centre the bias
    parts = [base, translate(base, vx, vy)]
    for ring in _iter_rings(base):
        coords = list(ring.coords)
        for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
            parts.append(Polygon([(x0, y0), (x1, y1),
                                   (x1 + vx, y1 + vy), (x0 + vx, y0 + vy)]))
    g = unary_union(parts)
    return g if g.is_valid else g.buffer(0)


def _morph_axis(geom: "BaseGeometry", sign: int, n: float, axis: str,
                fov_bbox: Optional["BaseGeometry"]) -> "BaseGeometry":
    """Directional grow (``sign>0``) / shrink (``sign<0``) of ``geom`` by
    ``n`` nm per side along ``axis`` ('W'/'H'). Shrink is morphological
    erosion (complement-dilate-complement) and needs ``fov_bbox``."""
    if sign > 0:
        return _dilate_axis(geom, n, axis)
    if fov_bbox is None:
        raise BooleanExprError(
            "shrink '< W/H:n' needs a FOV bounding box to bound the "
            "erosion; pass fov_bbox=")
    comp = fov_bbox.difference(geom)
    return fov_bbox.difference(_dilate_axis(comp, n, axis))


def fov_box(cx: float, cy: float, fov_w: float, fov_h: float) -> "BaseGeometry":
    """Build the FOV rectangle as a shapely polygon (centre + size, nm).
    Pass this as ``fov_bbox`` to :func:`evaluate` when the expression
    uses complement (``~``)."""
    _require_shapely()
    return box(cx - fov_w / 2.0, cy - fov_h / 2.0,
               cx + fov_w / 2.0, cy + fov_h / 2.0)


def evaluate(ast: object,
             bindings: Mapping[str, "BaseGeometry"],
             fov_bbox: Optional["BaseGeometry"] = None) -> "BaseGeometry":
    """Evaluate ``ast`` against shapely geometries bound per layer name.

    Args:
        ast: parse tree from :func:`parse_expression`.
        bindings: ``{ref_name: shapely geometry}`` for each layer letter.
        fov_bbox: the FOV rectangle as a shapely polygon, required only
            if the expression uses complement (``~``); it bounds the
            complement so it isn't infinite.

    Returns a shapely geometry (possibly empty / MultiPolygon).
    """
    _require_shapely()

    def ev(n: object) -> "BaseGeometry":
        if isinstance(n, Ref):
            g = bindings.get(n.name)
            if g is None:
                raise BooleanExprError(
                    f"layer {n.name!r} is not bound (bind it to a "
                    f"(layer, datatype) before evaluating)")
            return g
        if isinstance(n, Not):
            if fov_bbox is None:
                raise BooleanExprError(
                    "complement '~' needs a FOV bounding box to bound "
                    "the result; pass fov_bbox=")
            return fov_bbox.difference(ev(n.child))
        if isinstance(n, Morph):
            g = ev(n.child)
            # Directional bias (F4): W -> X axis, H -> Y axis; > grows and
            # < shrinks, n nm per side along that axis.
            return _morph_axis(g, n.sign, n.amount, n.label, fov_bbox)
        if isinstance(n, BinOp):
            a, b = ev(n.left), ev(n.right)
            if n.op == "&":
                return a.intersection(b)
            if n.op == "|":
                return a.union(b)
            if n.op == "-":
                return a.difference(b)
            raise BooleanExprError(f"unknown binary op {n.op!r}")
        raise BooleanExprError(f"unknown AST node {n!r}")

    return ev(ast)


# ── Output A: polygon list (for canvas) ──────────────────────────────


def geometry_to_polygons(geom: "BaseGeometry") -> list[np.ndarray]:
    """Flatten a shapely geometry into a list of exterior-ring point
    arrays ``(n, 2)`` float64 for the GDS canvas. Interior holes are
    dropped (display-only output; the mask in :func:`make_mask` handles
    holes correctly)."""
    _require_shapely()
    out: list[np.ndarray] = []
    if geom is None or geom.is_empty:
        return out
    geoms = list(getattr(geom, "geoms", [geom]))
    for g in geoms:
        ext = getattr(g, "exterior", None)
        if ext is None:
            continue
        out.append(np.asarray(ext.coords, dtype=np.float64))
    return out


# ── Output B: uint8 mask (for SEM measurement) ───────────────────────


def make_mask(
    geom: "BaseGeometry",
    *,
    width_px: int,
    height_px: int,
    x_min_nm: float,
    y_min_nm: float,
    nm_per_px: float,
    invert_y: bool = True,
    fill: int = 255,
) -> np.ndarray:
    """Rasterize a geometry to a ``uint8`` mask matching the SEM image.

    White (``fill``) = ROI, black (0) = ignore. Holes in the geometry
    are cut back out so a ring of material reads as ROI while its
    interior reads as ignore.

    Coordinate mapping (plan M2.5: "需輸入 nm_per_pixel 換算")::

        col = (x_nm - x_min_nm) / nm_per_px
        row = (y_nm - y_min_nm) / nm_per_px            (invert_y=False)
        row = height_px - 1 - that                     (invert_y=True)

    ``(x_min_nm, y_min_nm)`` is the GDS coordinate of the FOV's
    bottom-left corner. ``invert_y=True`` (default) accounts for SEM
    image row 0 being at the top while GDS Y increases upward.
    """
    if not _CV2_OK:  # pragma: no cover - import guard
        raise BooleanExprError(
            "opencv (cv2) is required for mask rasterization; "
            "install opencv-python")
    _require_shapely()
    mask = np.zeros((height_px, width_px), dtype=np.uint8)
    if geom is None or geom.is_empty:
        return mask

    def to_px(coords: np.ndarray) -> np.ndarray:
        cols = (coords[:, 0] - x_min_nm) / nm_per_px
        rows = (coords[:, 1] - y_min_nm) / nm_per_px
        if invert_y:
            rows = (height_px - 1) - rows
        return np.column_stack([cols, rows]).round().astype(np.int32)

    geoms = list(getattr(geom, "geoms", [geom]))
    exteriors: list[np.ndarray] = []
    holes: list[np.ndarray] = []
    for g in geoms:
        ext = getattr(g, "exterior", None)
        if ext is None:
            continue
        exteriors.append(to_px(np.asarray(ext.coords, dtype=float)))
        for ring in getattr(g, "interiors", []):
            holes.append(to_px(np.asarray(ring.coords, dtype=float)))

    if exteriors:
        cv2.fillPoly(mask, exteriors, int(fill))
    if holes:
        cv2.fillPoly(mask, holes, 0)
    return mask


# ── Raster Boolean (F27 M5): evaluate the whole expression in pixel space ────
#
# The export product IS a uint8 raster (gray/label via make_mask). The shapely
# path unions the raw rects then does directional morphology (grow/shrink) in
# VECTOR space — the Minkowski-sum-of-every-edge dilation is O(edges) + a giant
# unary_union, ~15 s on a dense FOV. In raster space the same ops are cv2 calls:
# grow/shrink -> dilate/erode with an axis kernel, & | - ~ -> bitwise ops, a raw
# layer -> fillPoly. ~10 ms total. The result differs from the vector-then-
# rasterize mask only at the sub-pixel boundary (morphology is quantized to the
# pixel grid), which is exactly the resolution the product is emitted at anyway.


def polys_to_mask(polys, *, width_px: int, height_px: int,
                  x_min_nm: float, y_min_nm: float, nm_per_px: float,
                  invert_y: bool = True, fill: int = 255) -> np.ndarray:
    """Rasterize a list of ``(n, 2)`` nm polygon rings into a uint8 mask using
    the SAME coordinate mapping as :func:`make_mask` (so a raster-evaluated
    expression lands pixel-aligned with the gray/label products). Overlapping
    polys fill idempotently (union); raw-layer rings are filled solid."""
    if not _CV2_OK:  # pragma: no cover
        raise BooleanExprError("opencv (cv2) is required for raster Boolean")
    mask = np.zeros((height_px, width_px), dtype=np.uint8)
    if not polys:
        return mask
    parts = []
    for p in polys:
        a = np.asarray(p, dtype=float)
        if a.shape[0] < 3:
            continue
        cols = (a[:, 0] - x_min_nm) / nm_per_px
        rows = (a[:, 1] - y_min_nm) / nm_per_px
        if invert_y:
            rows = (height_px - 1) - rows
        parts.append(np.column_stack([cols, rows]).round().astype(np.int32))
    # NOTE: cv2.fillPoly on a LIST applies the even-odd rule, so overlapping
    # raw rects would cancel into holes. Fill each polygon on its own so the
    # result is their UNION (idempotent over-paint) — this is the raw layer's
    # geometry. (Batched into one call per polygon; a 4-pt rect fill is ~µs.)
    for part in parts:
        cv2.fillPoly(mask, [part], int(fill))
    return mask


def raster_layer_mask(rects, polys, *, width_px: int, height_px: int,
                      x_min_nm: float, y_min_nm: float, nm_per_px: float,
                      invert_y: bool = True, fill: int = 255) -> np.ndarray:
    """Rasterize a raw layer's geometry to a uint8 mask, fast. Axis-aligned
    rects (the bulk of production geometry) are filled by numpy SLICING
    (``mask[r0:r1, c0:c1] = fill``) instead of ``cv2.fillPoly`` — ~10x cheaper on
    a dense FOV and it unions idempotently. Non-rectangular polys (if any) still
    go through fillPoly (each on its own, so overlaps union). Same coordinate
    mapping as :func:`make_mask` / :func:`polys_to_mask`."""
    if not _CV2_OK:  # pragma: no cover
        raise BooleanExprError("opencv (cv2) is required for raster Boolean")
    mask = np.zeros((height_px, width_px), dtype=np.uint8)
    arr = None if rects is None else np.asarray(rects, dtype=np.float64)
    if arr is not None and arr.size:
        x1 = np.minimum(arr[:, 0], arr[:, 2]); x2 = np.maximum(arr[:, 0], arr[:, 2])
        y1 = np.minimum(arr[:, 1], arr[:, 3]); y2 = np.maximum(arr[:, 1], arr[:, 3])
        c0 = np.round((x1 - x_min_nm) / nm_per_px).astype(np.int64)
        c1 = np.round((x2 - x_min_nm) / nm_per_px).astype(np.int64)
        if invert_y:
            r0 = np.round((height_px - 1) - (y2 - y_min_nm) / nm_per_px).astype(np.int64)
            r1 = np.round((height_px - 1) - (y1 - y_min_nm) / nm_per_px).astype(np.int64)
        else:
            r0 = np.round((y1 - y_min_nm) / nm_per_px).astype(np.int64)
            r1 = np.round((y2 - y_min_nm) / nm_per_px).astype(np.int64)
        c0 = np.clip(c0, 0, width_px); c1 = np.clip(c1 + 1, 0, width_px)
        r0 = np.clip(r0, 0, height_px); r1 = np.clip(r1 + 1, 0, height_px)
        for i in range(arr.shape[0]):
            if c1[i] > c0[i] and r1[i] > r0[i]:
                mask[r0[i]:r1[i], c0[i]:c1[i]] = fill
    if polys:
        for p in polys:
            a = np.asarray(p, dtype=float)
            if a.shape[0] < 3:
                continue
            cols = (a[:, 0] - x_min_nm) / nm_per_px
            rows = (a[:, 1] - y_min_nm) / nm_per_px
            if invert_y:
                rows = (height_px - 1) - rows
            pts = np.column_stack([cols, rows]).round().astype(np.int32)
            cv2.fillPoly(mask, [pts], int(fill))
    return mask


def evaluate_raster(ast: object, masks: Mapping[str, np.ndarray], *,
                    nm_per_px: float) -> np.ndarray:
    """Evaluate ``ast`` against per-layer uint8 masks (255=inside), returning a
    uint8 mask. Byte-mirror of :func:`evaluate` in pixel space: ``~`` ->
    bitwise_not (the mask IS the FOV), ``& | -`` -> bitwise ops, grow/shrink ->
    cv2.dilate/erode with an axis-aligned kernel sized ``round(amount/nm_per_px)``
    pixels per side (W -> X/columns, H -> Y/rows)."""
    if not _CV2_OK:  # pragma: no cover
        raise BooleanExprError("opencv (cv2) is required for raster Boolean")

    def ev(n: object) -> np.ndarray:
        if isinstance(n, Ref):
            m = masks.get(n.name)
            if m is None:
                raise BooleanExprError(f"layer {n.name!r} is not bound")
            return m
        if isinstance(n, Not):
            return cv2.bitwise_not(ev(n.child))
        if isinstance(n, Morph):
            m = ev(n.child)
            npx = int(round(n.amount / nm_per_px)) if nm_per_px > 0 else 0
            if npx <= 0:
                return m
            ksize = (1, 2 * npx + 1) if n.label == "W" else (2 * npx + 1, 1)
            kernel = np.ones(ksize, dtype=np.uint8)
            return (cv2.dilate(m, kernel) if n.sign > 0
                    else cv2.erode(m, kernel))
        if isinstance(n, BinOp):
            a, b = ev(n.left), ev(n.right)
            if n.op == "&":
                return cv2.bitwise_and(a, b)
            if n.op == "|":
                return cv2.bitwise_or(a, b)
            if n.op == "-":
                return cv2.bitwise_and(a, cv2.bitwise_not(b))
            raise BooleanExprError(f"unknown binary op {n.op!r}")
        raise BooleanExprError(f"unknown AST node {n!r}")

    return ev(ast)


def resolve_expression_raster(expr: str, bindings: Mapping[str, tuple], *,
                              raw_mask_provider, recipe_provider,
                              nm_per_px: float,
                              _cache: Optional[dict] = None,
                              _visiting: Optional[set] = None) -> np.ndarray:
    """Raster analogue of :func:`resolve_expression`: evaluate ``expr`` directly
    into a uint8 mask. ``raw_mask_provider(layer, datatype) -> uint8 mask``
    supplies a raw layer's rasterized geometry (the caller builds it — usually
    via :func:`raster_layer_mask` on the walked rects/polys, so all masks share
    one FOV pixel grid); ``recipe_provider(name) -> (expr, bindings)`` handles
    nested synthetic layers (evaluated recursively into masks, memoized)."""
    cache = {} if _cache is None else _cache
    visiting = set() if _visiting is None else _visiting
    _, ast = parse_expression(expr)
    masks: dict[str, np.ndarray] = {}
    for letter, raw_val in bindings.items():
        val = normalize_binding(raw_val)
        if val[0] == "raw":
            masks[letter] = raw_mask_provider(val[1], val[2])
            continue
        name = val[1]
        if name in cache:
            masks[letter] = cache[name]
            continue
        if name in visiting:
            raise BooleanExprError(f"circular reference to {name!r}")
        rec = recipe_provider(name)
        if rec is None:
            raise BooleanExprError(
                f"binding references unknown synthetic layer {name!r}")
        visiting.add(name)
        m = resolve_expression_raster(
            rec[0], rec[1], raw_mask_provider=raw_mask_provider,
            recipe_provider=recipe_provider, nm_per_px=nm_per_px,
            _cache=cache, _visiting=visiting)
        visiting.discard(name)
        cache[name] = m
        masks[letter] = m
    return evaluate_raster(ast, masks, nm_per_px=nm_per_px)


def mask_to_geometry(mask: np.ndarray, *, x_min_nm: float, y_min_nm: float,
                     nm_per_px: float, invert_y: bool = False) -> "BaseGeometry":
    """Vectorize a uint8 mask back into a shapely geometry (in nm root coords),
    preserving holes. Inverse of :func:`polys_to_mask` — lets the raster Boolean
    result re-enter the existing geom-based export pipeline (make_mask / overlay
    / template) unchanged. Uses ``cv2.findContours`` with a 2-level hierarchy so
    an outer boundary keeps its interior holes (Boolean subtraction / shrink)."""
    _require_shapely()
    if not _CV2_OK:  # pragma: no cover
        raise BooleanExprError("opencv (cv2) is required for raster Boolean")
    res = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    cnts, hier = (res[0], res[1]) if len(res) == 2 else (res[1], res[2])
    if not cnts or hier is None:
        return Polygon()
    hier = hier[0]
    h_px = mask.shape[0]

    def to_nm(c: np.ndarray) -> np.ndarray:
        pts = c.reshape(-1, 2).astype(float)
        xs = x_min_nm + pts[:, 0] * nm_per_px
        rows = (h_px - 1 - pts[:, 1]) if invert_y else pts[:, 1]
        ys = y_min_nm + rows * nm_per_px
        return np.column_stack([xs, ys])

    polys = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1 or len(c) < 3:      # a hole (has parent) / degenerate
            continue
        holes = []
        ch = hier[i][2]                          # first child = first hole
        while ch != -1:
            if len(cnts[ch]) >= 3:
                holes.append(to_nm(cnts[ch]))
            ch = hier[ch][0]                     # next sibling hole
        polys.append(Polygon(to_nm(c), holes))
    if not polys:
        return Polygon()
    g = polys[0] if len(polys) == 1 else unary_union(polys)
    return g if g.is_valid else g.buffer(0)


# ── One-shot convenience ─────────────────────────────────────────────


def compose(
    expr: str,
    layer_geoms: Mapping[str, "BaseGeometry"],
    fov_bbox: Optional["BaseGeometry"] = None,
) -> tuple[Optional[str], "BaseGeometry"]:
    """Parse + evaluate in one call. Returns ``(name, geometry)``."""
    name, ast = parse_expression(expr)
    geom = evaluate(ast, layer_geoms, fov_bbox=fov_bbox)
    return name, geom
