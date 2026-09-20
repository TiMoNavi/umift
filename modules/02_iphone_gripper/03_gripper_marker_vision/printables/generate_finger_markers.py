#!/usr/bin/env python3
import subprocess
from pathlib import Path

import cv2
import numpy as np


DICT_NAME = "DICT_4X4_50"
DPI = 300
MM_PER_INCH = 25.4
WHITE_MARGIN_MM = 2
OUTER_PADDING_MM = 4
MARKER_SIZES_MM = [16, 12]
A4_WIDTH_MM = 210
A4_HEIGHT_MM = 297
A4_MARGIN_MM = 10
GRID_GAP_MM = 5
HEADER_HEIGHT_MM = 18
SECTION_GAP_MM = 8


def mm_to_px(mm: float) -> int:
    return max(1, round(mm / MM_PER_INCH * DPI))


def make_marker_image(marker_id: int, marker_size_mm: int) -> np.ndarray:
    aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICT_NAME))
    marker_size_px = mm_to_px(marker_size_mm)
    white_margin_px = mm_to_px(WHITE_MARGIN_MM)
    outer_padding_px = mm_to_px(OUTER_PADDING_MM)

    marker = cv2.aruco.generateImageMarker(
        dictionary=aruco_dict,
        id=marker_id,
        sidePixels=marker_size_px,
        borderBits=1,
    )
    with_white_margin = cv2.copyMakeBorder(
        marker,
        white_margin_px,
        white_margin_px,
        white_margin_px,
        white_margin_px,
        cv2.BORDER_CONSTANT,
        value=255,
    )
    with_outer_padding = cv2.copyMakeBorder(
        with_white_margin,
        outer_padding_px,
        outer_padding_px,
        outer_padding_px,
        outer_padding_px,
        cv2.BORDER_CONSTANT,
        value=255,
    )
    return with_outer_padding


def add_label(image: np.ndarray, label: str) -> np.ndarray:
    canvas = np.full((image.shape[0] + 56, image.shape[1], 3), 255, dtype=np.uint8)
    rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    canvas[: image.shape[0], :, :] = rgb
    cv2.putText(
        canvas,
        label,
        (12, image.shape[0] + 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    return canvas


def write_png(path: Path, image: np.ndarray) -> None:
    cv2.imwrite(str(path), image)
    # OpenCV writes PNGs without print DPI metadata. Set it explicitly so
    # Preview / printer dialogs keep the intended physical size.
    subprocess.run(
        [
            "sips",
            "--setProperty",
            "dpiWidth",
            str(DPI),
            "--setProperty",
            "dpiHeight",
            str(DPI),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def write_pdf_from_png(path: Path) -> None:
    subprocess.run(
        [
            "sips",
            "-s",
            "format",
            "pdf",
            str(path),
            "--out",
            str(path.with_suffix(".pdf")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def draw_text(
    image: np.ndarray,
    text: str,
    origin: tuple[int, int],
    font_scale: float = 0.9,
    thickness: int = 2,
) -> None:
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        (0, 0, 0),
        thickness,
        cv2.LINE_AA,
    )


def make_a4_canvas(title: str, subtitle: str) -> tuple[np.ndarray, int]:
    width_px = mm_to_px(A4_WIDTH_MM)
    height_px = mm_to_px(A4_HEIGHT_MM)
    margin_px = mm_to_px(A4_MARGIN_MM)
    header_height_px = mm_to_px(HEADER_HEIGHT_MM)

    canvas = np.full((height_px, width_px, 3), 255, dtype=np.uint8)
    draw_text(canvas, title, (margin_px, margin_px + 36), font_scale=1.1, thickness=2)
    draw_text(
        canvas,
        subtitle,
        (margin_px, margin_px + 82),
        font_scale=0.72,
        thickness=2,
    )
    y0 = margin_px + header_height_px
    cv2.line(
        canvas,
        (margin_px, y0),
        (width_px - margin_px, y0),
        (0, 0, 0),
        1,
        cv2.LINE_AA,
    )
    return canvas, y0 + mm_to_px(4)


def tile_cards(
    canvas: np.ndarray,
    card: np.ndarray,
    top_y: int,
    cols: int,
    rows: int,
) -> int:
    margin_px = mm_to_px(A4_MARGIN_MM)
    gap_px = mm_to_px(GRID_GAP_MM)
    card_h, card_w = card.shape[:2]
    usable_width = canvas.shape[1] - 2 * margin_px
    total_width = cols * card_w + max(0, cols - 1) * gap_px
    start_x = margin_px + max(0, (usable_width - total_width) // 2)

    for row in range(rows):
        y = top_y + row * (card_h + gap_px)
        for col in range(cols):
            x = start_x + col * (card_w + gap_px)
            canvas[y : y + card_h, x : x + card_w] = card
    return top_y + rows * card_h + max(0, rows - 1) * gap_px


def max_rows_that_fit(canvas: np.ndarray, card: np.ndarray, top_y: int) -> int:
    margin_px = mm_to_px(A4_MARGIN_MM)
    gap_px = mm_to_px(GRID_GAP_MM)
    usable_height = canvas.shape[0] - top_y - margin_px
    card_h = card.shape[0]
    return max(1, (usable_height + gap_px) // (card_h + gap_px))


def build_single_size_sheet(marker_size_mm: int, pair: np.ndarray) -> np.ndarray:
    canvas, start_y = make_a4_canvas(
        title=f"A4 Gripper ArUco Sheet  {marker_size_mm} mm",
        subtitle="DICT_4X4_50   LEFT=id0   RIGHT=id1   Print at 100% scale / actual size",
    )
    margin_px = mm_to_px(A4_MARGIN_MM)
    gap_px = mm_to_px(GRID_GAP_MM)
    usable_width = canvas.shape[1] - 2 * margin_px
    usable_height = canvas.shape[0] - start_y - margin_px
    card_h, card_w = pair.shape[:2]
    cols = max(1, (usable_width + gap_px) // (card_w + gap_px))
    rows = max(1, (usable_height + gap_px) // (card_h + gap_px))
    tile_cards(canvas, pair, start_y, cols, rows)
    return canvas


def build_mixed_sheet(pairs: dict[int, np.ndarray]) -> np.ndarray:
    canvas, y = make_a4_canvas(
        title="A4 Gripper ArUco Mixed Sheet  16 mm + 12 mm",
        subtitle="Use 16 mm first. Fall back to 12 mm only if the designated mount points are too small.",
    )
    margin_px = mm_to_px(A4_MARGIN_MM)
    section_gap_px = mm_to_px(SECTION_GAP_MM)
    draw_text(canvas, "16 mm primary", (margin_px, y + 26), font_scale=0.8, thickness=2)
    y += mm_to_px(10)
    y = tile_cards(canvas, pairs[16], y, cols=2, rows=3)

    y += section_gap_px
    cv2.line(
        canvas,
        (margin_px, y),
        (canvas.shape[1] - margin_px, y),
        (0, 0, 0),
        1,
        cv2.LINE_AA,
    )
    y += mm_to_px(6)
    draw_text(canvas, "12 mm backup", (margin_px, y + 26), font_scale=0.8, thickness=2)
    y += mm_to_px(10)
    rows_12mm = min(4, max_rows_that_fit(canvas, pairs[12], y))
    tile_cards(canvas, pairs[12], y, cols=3, rows=rows_12mm)
    return canvas


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    pair_images: dict[int, np.ndarray] = {}
    for marker_size_mm in MARKER_SIZES_MM:
        left = add_label(
            make_marker_image(0, marker_size_mm),
            f"LEFT  ID 0   {marker_size_mm} mm",
        )
        right = add_label(
            make_marker_image(1, marker_size_mm),
            f"RIGHT ID 1   {marker_size_mm} mm",
        )

        write_png(out_dir / f"aruco_left_id0_{marker_size_mm}mm.png", left)
        write_png(out_dir / f"aruco_right_id1_{marker_size_mm}mm.png", right)

        gap = np.full((left.shape[0], 80, 3), 255, dtype=np.uint8)
        pair = np.concatenate([left, gap, right], axis=1)
        pair_path = out_dir / f"aruco_pair_id0_id1_{marker_size_mm}mm_300dpi.png"
        write_png(pair_path, pair)
        pair_images[marker_size_mm] = pair

    for marker_size_mm, pair in pair_images.items():
        a4_path = out_dir / f"a4_sheet_id0_id1_{marker_size_mm}mm_300dpi.png"
        write_png(a4_path, build_single_size_sheet(marker_size_mm, pair))
        write_pdf_from_png(a4_path)

    mixed_path = out_dir / "a4_sheet_mixed_12mm_16mm_300dpi.png"
    write_png(mixed_path, build_mixed_sheet(pair_images))
    write_pdf_from_png(mixed_path)


if __name__ == "__main__":
    main()
