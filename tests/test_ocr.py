"""OCR helpers that don't need the OCR model."""

from types import SimpleNamespace as NS

from core.ocr import _is_sideways, _to_lines


def box(x, y, w, h):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def test_upright_page_is_not_sideways():
    out = NS(boxes=[box(0, i * 30, 200, 20) for i in range(5)])
    assert not _is_sideways(out)


def test_rotated_page_is_sideways():
    out = NS(boxes=[box(i * 30, 0, 20, 200) for i in range(5)])
    assert _is_sideways(out)


def test_boxes_on_the_same_row_become_one_line():
    boxes = [box(300, 100, 60, 20), box(10, 102, 80, 20), box(10, 150, 80, 20)]
    assert _to_lines(boxes, ["9.50", "TOTAL", "CASH"]) == "TOTAL  9.50\nCASH"
