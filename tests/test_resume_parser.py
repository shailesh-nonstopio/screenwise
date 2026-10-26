from datetime import date

import pytest

from app.demo import make_docx, make_pdf
from app.services.resume_parser import (
    ExtractionError,
    UploadError,
    extract_text,
    heuristic_parse,
    number_lines,
    safe_filename,
    validate_upload,
)
from tests.conftest import RESUME_LINES


def test_pdf_and_docx_text_is_extracted():
    for content, name in ((make_pdf(RESUME_LINES), "cv.pdf"), (make_docx(RESUME_LINES), "cv.docx")):
        vf = validate_upload(name, content)
        text = extract_text(content, vf.kind)
        assert "Northwind Pay" in text
        assert "FastAPI" in text


@pytest.mark.parametrize(
    "name,content,message",
    [
        ("cv.pdf", b"", "empty"),
        ("cv.exe", b"MZ\x90\x00binary", "Only PDF and DOCX"),
        ("cv.doc", b"\xd0\xcf\x11\xe0legacy", ".doc"),
        ("cv.docx", b"%PDF-1.4 fake", "don't match"),
        ("cv.pdf", b"PK\x03\x04not-a-real-zip", "Only PDF and DOCX"),
        ("cv.docx", b"PK\x03\x04broken", "damaged"),
        ("cv.txt", b"plain text resume", "Only PDF and DOCX"),
    ],
)
def test_invalid_files_are_rejected_safely(name, content, message):
    with pytest.raises(UploadError) as exc:
        validate_upload(name, content)
    assert message in str(exc.value)


def test_oversized_file_rejected(test_settings):
    big = b"%PDF-1.4\n" + b"0" * (test_settings.max_upload_bytes + 10)
    with pytest.raises(UploadError, match="larger than"):
        validate_upload("big.pdf", big)


def test_pdf_without_text_reports_extraction_failure():
    blank = make_pdf([])
    vf = validate_upload("scan.pdf", blank)
    with pytest.raises(ExtractionError, match="scanned image"):
        extract_text(blank, vf.kind)


def test_corrupt_pdf_reports_clear_error():
    bad = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> garbage"
    vf = validate_upload("bad.pdf", bad)
    with pytest.raises(ExtractionError):
        extract_text(bad, vf.kind)


def test_unsafe_filenames_are_sanitised():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("my <cv>.pdf") == "my _cv_.pdf"
    assert safe_filename("") == "resume"


def test_parser_preserves_uncertainty_and_unknowns():
    parsed = heuristic_parse(number_lines("\n".join(RESUME_LINES)), today=date(2026, 10, 1))
    roles = {r["organization"]: r for r in parsed["roles"]}
    assert roles["Northwind Pay"]["start_certain"] is True
    assert roles["Northwind Pay"]["end"] == "present"
    brightline = roles["Brightline Logistics"]
    assert brightline["start_certain"] is False and "approximate" in brightline["uncertain_note"]
    exp = parsed["experience_years"]
    assert exp["certain"] is False and exp["min"] < exp["max"]
    # Education has no year: it must be reported as unknown, never guessed.
    assert parsed["education"][0]["year"] is None
    assert "Graduation year" in parsed["unknowns"]
    assert "Python" in parsed["skills"] and "FastAPI" in parsed["skills"]


def test_parser_does_not_invent_roles_without_dates():
    parsed = heuristic_parse(number_lines("Jane Doe\nI like building things\nPython, SQL"))
    assert parsed["roles"] == []
    assert parsed["experience_years"] is None
    assert any("Work history" in u for u in parsed["unknowns"])
