"""Zarr writing helpers for UMI-FT replay-buffer export."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import cumulative_int64


def ensure_numpy_zarr_compat(np_module: Any) -> None:
    # zarr 2.x still calls np.product when guessing chunks; NumPy 2 removed it.
    if not hasattr(np_module, "product") and hasattr(np_module, "prod"):
        setattr(np_module, "product", np_module.prod)


def first_dim_chunks(shape: tuple[int, ...], preferred: int) -> tuple[int, ...]:
    if not shape:
        return ()
    first = max(1, min(preferred, max(1, int(shape[0]))))
    return (first, *shape[1:])


def create_array(group: Any, name: str, data: Any, *, chunks: tuple[int, ...], compressor: Any) -> Any:
    return group.create_dataset(
        name,
        data=data,
        shape=data.shape,
        chunks=chunks,
        dtype=data.dtype,
        compressor=compressor,
        overwrite=True,
    )


def write_episode_group(
    episode_group: Any,
    arrays: dict[str, Any],
    *,
    image_compressor: Any,
    numeric_compressor: Any,
    depth_compressor: Any,
) -> None:
    for name, data in arrays.items():
        if data.ndim == 4 and (name == "rgb_0" or name.startswith("rgb_global_")):
            chunks = first_dim_chunks(data.shape, 8)
            compressor = image_compressor
        elif name == "depth_0" or name == "iphone_depth_0":
            chunks = first_dim_chunks(data.shape, 8)
            compressor = depth_compressor
        elif name.startswith("wrench"):
            chunks = first_dim_chunks(data.shape, 4096)
            compressor = numeric_compressor
        elif name.startswith("ts_pose") or name.startswith("robot_time") or name.startswith("rgb_time"):
            chunks = first_dim_chunks(data.shape, 4096)
            compressor = numeric_compressor
        else:
            chunks = first_dim_chunks(data.shape, 4096)
            compressor = numeric_compressor
        create_array(episode_group, name, data, chunks=chunks, compressor=compressor)


def write_meta_lengths(
    meta_group: Any,
    *,
    lengths: dict[str, list[int]],
    np: Any,
    numeric_compressor: Any,
) -> None:
    for name, values in lengths.items():
        if not values:
            continue
        create_array(
            meta_group,
            name,
            cumulative_int64(values, np),
            chunks=(max(1, min(1024, len(values))),),
            compressor=numeric_compressor,
        )
