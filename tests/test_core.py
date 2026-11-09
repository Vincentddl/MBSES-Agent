import copy
import json
from io import BytesIO
from unittest.mock import Mock

import pytest
from docx import Document

from app.cache import MemoryCache, get_cache
from app.chunking import chunk_blocks
from app.embedding import MockEmbedder, QwenEmbedder
from app.parsing import Block, parse_document
from app.reliability import calculate_reliability, calculation_from_draft
from app.validation import check_draft, validate_llm_output


def test_valid_draft_preserves_numeric_constraint(draft):
    value, errors, _ = check_draft(draft, ["USER_INPUT"])
    assert not errors
    assert value["model_elements"][1]["properties"] == {"value": 200, "unit": "g", "operator": "<="}


@pytest.mark.parametrize("change,expected", [
    (lambda d: d["model_elements"][1].update(localId="root"), "LOCAL_ID_DUPLICATE"),
    (lambda d: d["model_elements"][1].update(name="系统需求"), "NAME_CONFLICT"),
    (lambda d: d["model_elements"][1].update(evidence_chunk_ids=["FAKE"]), "EVIDENCE_UNKNOWN"),
    (lambda d: d["model_elements"][1].update(evidence_chunk_ids=[]), "EVIDENCE_MISSING"),
    (lambda d: d["sysml_relationships"][0].update(target="missing"), "ENDPOINT_MISSING"),
    (lambda d: d["sysml_relationships"][0].update(target="root"), "SELF_REFERENCE"),
    (lambda d: d["sysml_relationships"][0].update(relType="SATISFIES"), "RELATION_TYPE_MISMATCH"),
    (lambda d: d.update(evidence=[]), "EVIDENCE_INCOMPLETE"),
])
def test_rule_rejections(draft, change, expected):
    change(draft)
    assert any(expected in x for x in check_draft(draft, ["USER_INPUT"])[1])


def test_cycle(draft):
    draft["sysml_relationships"].append({"source": "child", "relType": "CONTAINS", "target": "root"})
    assert any("OWNERSHIP_CYCLE" in x for x in check_draft(draft, ["USER_INPUT"])[1])


@pytest.mark.parametrize("relations", [{}, "", None])
def test_wrong_relation_shape_not_silently_removed(draft, relations):
    draft["sysml_relationships"] = relations
    assert check_draft(draft, ["USER_INPUT"])[0] is None


def test_reject_nested_nan(draft):
    draft["model_elements"][0]["properties"] = {"nested": [float("nan")]}
    assert check_draft(draft, ["USER_INPUT"])[0] is None
    assert validate_llm_output("{bad json}")[0] is None


def test_docx_order_and_source_locations():
    document = Document()
    document.add_heading("第一章", level=1)
    document.add_paragraph("需求A")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "表格值"
    document.add_paragraph("需求B")
    output = BytesIO()
    document.save(output)
    blocks, info = parse_document("test.docx", output.getvalue())
    assert [b.text for b in blocks] == ["第一章", "需求A", "表格值", "需求B"]
    assert [b.locator for b in blocks] == ["P001", "P002", "T1R1", "P003"]
    assert info["tables"] == 1


@pytest.mark.parametrize("entries", [
    {"word/document.xml": b"x" * (4 * 1024 * 1024 + 1)},
    {"word/document.xml": b'<!DOCTYPE document [<!ENTITY x "y">]><document/>'},
    {f"word/part{i}.xml": b"<x/>" for i in range(513)},
    {"word/document.xml": b"<w:p>" * 20001},
])
def test_docx_resource_preflight(entries):
    import zipfile
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    with pytest.raises(ValueError):
        parse_document("untrusted.docx", stream.getvalue())


def test_pdf_file_budget():
    with pytest.raises(ValueError, match="8MB"):
        parse_document("large.pdf", b"x" * (8 * 1024 * 1024 + 1))


def test_pdf_page_budget():
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(101):
        writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="100页"):
        parse_document("many.pdf", output.getvalue())


def test_pdf_decompression_budget():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b" " * (4 * 1024 * 1024 + 1))
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="PDF解析预算"):
        parse_document("compressed.pdf", output.getvalue())


def test_pdf_normal_text_still_parses():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 10 Td (Requirement text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    blocks, info = parse_document("normal.pdf", output.getvalue())
    assert blocks[0].text == "Requirement text" and info["pages"] == 1


def test_chunk_integrity():
    fact = "需求编号1.2.1；重量不超过200克；必须保留单位和比较符。"
    chunks = chunk_blocks([Block(fact, "需求", "P091"), Block("x" * 2000, "附录", "P100")])
    assert chunks[0].content == fact and chunks[0].locator == "P091"
    assert len(chunks) > 2
    assert all(c.title for c in chunks)


def test_cache_shared_and_copy_isolation():
    assert get_cache() is get_cache()
    cache = MemoryCache()
    cache.set("x", {"items": [1]}, 60)
    cache.get("x")["items"].append(2)
    assert cache.get("x") == {"items": [1]}
    cache.set("expired", 1, -1)
    assert cache.get("expired") is None


def test_mock_embedding_is_deterministic():
    embedder = MockEmbedder()
    first, second = embedder.embed(["质量要求", "质量要求"])
    assert first == second and len(first) == 1024
    assert abs(sum(v*v for v in first) - 1) < 1e-6


def test_qwen_embedding_restores_provider_index(monkeypatch):
    import app.embedding as module
    monkeypatch.setattr(module.settings, "EMBED_DIM", 2)
    monkeypatch.setattr(module, "post_json", lambda *args: {"data": [
        {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})
    assert QwenEmbedder().embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_401_does_not_retry(monkeypatch):
    import app.http_client as module
    monkeypatch.setattr(module.settings, "QWEN_API_KEY", "test")
    monkeypatch.setattr(module.settings, "ALLOW_PAID_MODEL_CALLS", True)
    response = Mock(status_code=401, text="unauthorized")
    post = Mock(return_value=response)
    monkeypatch.setattr(module.requests, "post", post)
    with pytest.raises(RuntimeError):
        module.post_json("/chat/completions", {})
    assert post.call_count == 1


def test_429_has_finite_retry_limit(monkeypatch):
    import app.http_client as module
    monkeypatch.setattr(module.settings, "QWEN_API_KEY", "test")
    monkeypatch.setattr(module.settings, "ALLOW_PAID_MODEL_CALLS", True)
    post = Mock(return_value=Mock(status_code=429, headers={}, text="rate limited"))
    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError):
        module.post_json("/chat/completions", {})
    assert post.call_count == 3


def test_reliability_case_and_strict_threshold():
    components = [{"name": str(i), "mtbf_hours": value} for i, value in enumerate([60000, 65000, 58000, 48000, 50000])]
    value = calculate_reliability(1000, components)
    assert value["system_reliability"] == pytest.approx(0.9138160420441955)
    assert value["passed"]
    assert not calculate_reliability(1000, components, value["system_reliability"])["passed"]


@pytest.mark.parametrize("invalid", [0, -10, True, float("nan"), float("inf"), "60000"])
def test_invalid_mtbf(invalid):
    with pytest.raises(ValueError):
        calculate_reliability(1000, [{"name": "A", "mtbf_hours": invalid}])


def test_missing_parameter_not_silently_computed(draft):
    assert calculation_from_draft(draft) is None
    draft["model_elements"][0]["properties"] = {"parameter_role": "mtbf", "value": 60000, "unit": "h"}
    with pytest.raises(ValueError):
        calculation_from_draft(draft)
