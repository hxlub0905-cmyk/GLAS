"""User-facing layer cache for the GDS align tool (F2 M2.1).

A *user-facing project cache*: after geometry is loaded the user clicks
"Export Cache (.npz)" and picks a path. The file stores the selected layers'
geometry **plus the alignment metadata** the user set up -- chip-corner offset
and FOV size -- so the next launch opens instantly and auto-populates those
settings (plan Q13). It is a plain ``.npz`` at an arbitrary path.

Format inside the ``.npz``::

    meta        uint8 1-D : JSON manifest (see LayerCacheMeta + layer_index)
    L{i}_pts    f32 (M,2) : all polygons of layer i concatenated
    L{i}_offs   int64 (P+1,) : slice offsets into L{i}_pts
    L{i}_bbs    f32 (P,4)  : per-polygon bbox

Only layers the user selected (passed to :func:`cache_save`) are stored
(plan M2.1: "不存未選取的 layer").
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

SCHEMA_VERSION = 4   # v4: RFL Chip-offset params (chip corner/size/GDS off, um)

# Layer tuple shape:
#   (layer:int, datatype:int, polys:list[ndarray(n,2) f32], bboxes:ndarray(P,4) f32)
LayerTuple = tuple


# ── Metadata ─────────────────────────────────────────────────────────


@dataclass
class LayerCacheMeta:
    """Everything the GUI restores from a cache besides the geometry.

    The alignment fields (``chip_corner_*`` / ``fov_*``) are what the
    Coordinate Setup panel (M3) auto-fills on load; ``source_*`` drives
    the staleness check in :func:`check_source`.
    """
    source_oas: str          # basename of the source OASIS file
    source_mtime: float
    source_size: int
    # Derived chip-corner offset (nm) -- kept for convenience / debugging;
    # the source of truth is the six RFL params below (plan Q15).
    chip_corner_x: float = 0.0
    chip_corner_y: float = 0.0
    # RFL "Chip offset" table (um): chip corner (DieX/DieY) rel die corner,
    # chip size (SizeW/SizeH), and the GDS default origin offset.
    chip_x_um: float = 0.0
    chip_y_um: float = 0.0
    chip_w_um: float = 0.0
    chip_h_um: float = 0.0
    gds_off_x_um: float = 0.0
    gds_off_y_um: float = 0.0
    fov_w: float = 0.0
    fov_h: float = 0.0
    # Origin correction delta (nm): the constant KLARF(P)->GDS(G) offset the
    # user finds by dragging the SEM/GDS overlay (plan M4a). nm_per_px is the
    # overlay scale (0 = derive from FOV / image width).
    origin_dx: float = 0.0
    origin_dy: float = 0.0
    nm_per_px: float = 0.0
    top_cell_name: str = ""
    nm_units: float = 1.0
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION


@dataclass
class LayerCacheData:
    """Loaded cache contents: metadata + geometry layers."""
    meta: LayerCacheMeta
    layers: list   # list[(layer, datatype, polys, bboxes)]


# ── Staleness ────────────────────────────────────────────────────────


def check_source(meta: LayerCacheMeta,
                 source_path: Optional[str | Path]) -> str:
    """Compare a loaded cache's source metadata against the current
    OASIS file. Returns one of:

    * ``"ok"``            -- name + mtime match (cache is fresh)
    * ``"no_source"``     -- caller passed no path to check against
    * ``"missing"``       -- the source file no longer exists
    * ``"name_mismatch"`` -- basename differs from what the cache stored
    * ``"stale_mtime"``   -- same name but the file was modified

    The GUI uses anything other than ``"ok"`` / ``"no_source"`` to warn
    the user to regenerate the cache (plan M2.1: "cache 過期時提示重新
    產生").
    """
    if source_path is None:
        return "no_source"
    p = Path(source_path)
    if not p.exists():
        return "missing"
    if p.name != meta.source_oas:
        return "name_mismatch"
    if int(p.stat().st_size) != int(meta.source_size):
        return "stale_mtime"
    # mtime compared at whole-second granularity to dodge float jitter
    # between filesystems.
    if int(p.stat().st_mtime) != int(meta.source_mtime):
        return "stale_mtime"
    return "ok"


# ── Save ─────────────────────────────────────────────────────────────


def cache_save(path: str | Path,
               layers: list,
               meta: LayerCacheMeta) -> Path:
    """Atomically write the selected ``layers`` + ``meta`` to ``path``.

    ``layers`` is a list of ``(layer, datatype, polys, bboxes)`` tuples
    -- the GUI builds it from the currently selected LayerEntry list, so
    unselected layers are simply never passed in (plan M2.1).

    Returns the final path. Raises ``ValueError`` if a layer's polygon
    count doesn't match its bbox count.
    """
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(final.suffix + ".tmp")

    npz_kwargs: dict = {}
    layer_index: list = []
    for i, (layer, datatype, polys, bboxes) in enumerate(layers):
        n_polys = len(polys)
        if n_polys == 0:
            pts = np.empty((0, 2), dtype=np.float32)
            offs = np.zeros(1, dtype=np.int64)
            bbs = np.empty((0, 4), dtype=np.float32)
        else:
            pts = np.concatenate(
                [np.asarray(p, dtype=np.float32) for p in polys], axis=0)
            lengths = np.asarray([len(p) for p in polys], dtype=np.int64)
            offs = np.concatenate(([0], np.cumsum(lengths))).astype(np.int64)
            bbs = np.asarray(bboxes, dtype=np.float32)
            if bbs.shape != (n_polys, 4):
                raise ValueError(
                    f"layer {layer}/{datatype}: {n_polys} polys but bboxes "
                    f"shape {bbs.shape}; expected ({n_polys}, 4)")
        npz_kwargs[f"L{i}_pts"] = pts
        npz_kwargs[f"L{i}_offs"] = offs
        npz_kwargs[f"L{i}_bbs"] = bbs
        layer_index.append({
            "layer": int(layer),
            "datatype": int(datatype),
            "n_polys": int(n_polys),
        })

    meta.schema_version = SCHEMA_VERSION
    manifest = asdict(meta)
    manifest["layer_index"] = layer_index
    npz_kwargs["meta"] = np.frombuffer(
        json.dumps(manifest).encode("utf-8"), dtype=np.uint8)

    try:
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, **npz_kwargs)
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return final


# ── Load ─────────────────────────────────────────────────────────────


def cache_load(path: str | Path) -> Optional[LayerCacheData]:
    """Load a layer cache. Returns ``None`` on missing file, schema
    mismatch, or corruption (never raises -- the GUI treats a bad cache
    as "no cache" and offers to regenerate)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with np.load(p, allow_pickle=False) as npz:
            manifest = json.loads(bytes(npz["meta"]).decode("utf-8"))
            if int(manifest.get("schema_version", -1)) != SCHEMA_VERSION:
                return None
            layer_index = manifest.pop("layer_index", [])
            layers: list = []
            for i, entry in enumerate(layer_index):
                layer = int(entry["layer"])
                datatype = int(entry["datatype"])
                pts = np.asarray(npz[f"L{i}_pts"], dtype=np.float32)
                offs = np.asarray(npz[f"L{i}_offs"], dtype=np.int64)
                bbs = np.asarray(npz[f"L{i}_bbs"], dtype=np.float32)
                polys = [pts[offs[j]:offs[j + 1]] for j in range(len(offs) - 1)]
                layers.append((layer, datatype, polys, bbs))
            # Drop unknown keys so a future schema field doesn't crash
            # this loader; the version gate above is the real guard.
            known = {f.name for f in LayerCacheMeta.__dataclass_fields__.values()}
            meta = LayerCacheMeta(**{k: v for k, v in manifest.items()
                                     if k in known})
            return LayerCacheData(meta=meta, layers=layers)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def make_meta(source_path: str | Path,
              *,
              chip_corner_x: float = 0.0,
              chip_corner_y: float = 0.0,
              chip_x_um: float = 0.0,
              chip_y_um: float = 0.0,
              chip_w_um: float = 0.0,
              chip_h_um: float = 0.0,
              gds_off_x_um: float = 0.0,
              gds_off_y_um: float = 0.0,
              fov_w: float = 0.0,
              fov_h: float = 0.0,
              origin_dx: float = 0.0,
              origin_dy: float = 0.0,
              nm_per_px: float = 0.0,
              top_cell_name: str = "",
              nm_units: float = 1.0) -> LayerCacheMeta:
    """Build a :class:`LayerCacheMeta` from the source file + the
    current alignment settings. Convenience for the GUI's Export
    Cache handler."""
    sp = Path(source_path)
    st = sp.stat()
    return LayerCacheMeta(
        source_oas=sp.name,
        source_mtime=st.st_mtime,
        source_size=st.st_size,
        chip_corner_x=chip_corner_x,
        chip_corner_y=chip_corner_y,
        chip_x_um=chip_x_um,
        chip_y_um=chip_y_um,
        chip_w_um=chip_w_um,
        chip_h_um=chip_h_um,
        gds_off_x_um=gds_off_x_um,
        gds_off_y_um=gds_off_y_um,
        fov_w=fov_w,
        fov_h=fov_h,
        origin_dx=origin_dx,
        origin_dy=origin_dy,
        nm_per_px=nm_per_px,
        top_cell_name=top_cell_name,
        nm_units=nm_units,
    )
