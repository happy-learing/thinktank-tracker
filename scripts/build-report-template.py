from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt


EAST_ASIA_FONT = "\u5b8b\u4f53"


def _set_fonts(font) -> None:  # type: ignore[no-untyped-def]
    font.name = "Times New Roman"
    font.size = Pt(10.5)
    font._element.rPr.rFonts.set(qn("w:ascii"), "Times New Roman")
    font._element.rPr.rFonts.set(qn("w:hAnsi"), "Times New Roman")
    font._element.rPr.rFonts.set(qn("w:cs"), "Times New Roman")
    font._element.rPr.rFonts.set(qn("w:eastAsia"), EAST_ASIA_FONT)


def build_template(reference: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(reference, output)
    document = Document(output)

    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)

    normal = document.styles["Normal"]
    _set_fonts(normal.font)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    heading = document.styles["Heading 1"]
    _set_fonts(heading.font)
    heading.font.bold = True
    heading.paragraph_format.line_spacing = 1.25
    heading.paragraph_format.space_before = Pt(0)
    heading.paragraph_format.space_after = Pt(0)
    heading.paragraph_format.keep_with_next = True
    heading.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    num_pr = heading._element.pPr.find(qn("w:numPr"))
    if num_pr is not None:
        heading._element.pPr.remove(num_pr)

    hyperlink = document.styles["Hyperlink"]
    _set_fonts(hyperlink.font)

    document.core_properties.title = "Think Tank Weekly Report Template"
    document.core_properties.subject = "Built from the approved 2026 weekly report reference"
    document.core_properties.comments = "Sample body removed; layout and typography normalized."
    document.save(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_template(args.reference.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
