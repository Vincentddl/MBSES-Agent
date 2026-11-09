"""Extract ordered text blocks and source locations without treating them as instructions."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
import zipfile
import time


@dataclass
class Block:
    text: str
    section: str
    locator: str


def parse_document(filename, raw):
    from docx.opc.exceptions import PackageNotFoundError
    from lxml.etree import XMLSyntaxError
    from pypdf.errors import PyPdfError, LimitReachedError
    try:
        return _parse_document(filename, raw)
    except LimitReachedError as exc:
        raise ValueError("PDF解析预算超限，请拆分或简化文档") from exc
    except (zipfile.BadZipFile, PackageNotFoundError, XMLSyntaxError, PyPdfError, KeyError) as exc:
        raise ValueError("文档格式损坏或内容不完整，请检查文件后重新上传") from exc


def _parse_document(filename, raw):
    ext = Path(filename).suffix.lower()
    blocks = []
    info = {"format": ext.lstrip("."), "warnings": []}
    if ext == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 512 or sum(x.file_size for x in members) > 32 * 1024 * 1024:
                raise ValueError("Word解压后体积超过限制")
            xml_members = [x for x in members if x.filename.endswith((".xml", ".rels"))]
            if sum(x.file_size for x in xml_members) > 4 * 1024 * 1024:
                raise ValueError("Word XML结构超过4MB限制，请拆分文档")
            for member in xml_members:
                data = archive.read(member)
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise ValueError("不支持包含DTD或实体声明的Word文件")
                if member.filename == "word/document.xml" and (
                    len(data) > 2 * 1024 * 1024 or
                    len(re.findall(rb"<(?:\w+:)?(?:p|tr|tc|r)(?:\s|>)", data)) > 20000):
                    raise ValueError("Word正文结构过于复杂，请拆分文档")
            info["embedded_images"] = sum(x.filename.startswith("word/media/") and not x.is_dir() for x in members)
        doc = Document(BytesIO(raw))
        levels = []
        pnum = tnum = 0
        for node in doc.element.body.iterchildren():
            if node.tag.endswith("}p"):
                pnum += 1
                paragraph = Paragraph(node, doc)
                text = paragraph.text.strip()
                if not text:
                    continue
                match = re.search(r"(?:Heading\s*|标题\s*)([1-9])", paragraph.style.name, re.I)
                if match:
                    level = int(match.group(1))
                    levels = levels[:level - 1]
                    levels.append(text)
                blocks.append(Block(text, " / ".join(levels) or "正文", f"P{pnum:03d}"))
            elif node.tag.endswith("}tbl"):
                tnum += 1
                for rnum, row in enumerate(Table(node, doc).rows, 1):
                    text = " | ".join(cell.text.strip() for cell in row.cells)
                    if text.strip(" |"):
                        blocks.append(Block(text, " / ".join(levels) or "表格", f"T{tnum}R{rnum}"))
        info["paragraphs"] = pnum
        info["tables"] = tnum
        if info["embedded_images"]:
            info["warnings"].append("图片中的需求树、拓扑和表格未做OCR，不能计入已提取事实。")
    elif ext == ".pdf":
        blocks, info["pages"] = _parse_pdf(raw)
        info["warnings"].append("PDF只提取文本层；扫描件需另行OCR，表格阅读顺序需人工核验。")
    elif ext in {".md", ".txt", ".markdown"}:
        text = raw.decode("utf-8-sig")
        section = "正文"
        for number, paragraph in enumerate(re.split(r"\n\s*\n", text), 1):
            match = re.match(r"^#{1,6}\s+(.+)", paragraph)
            if match:
                section = match.group(1).strip()
            if paragraph.strip():
                blocks.append(Block(paragraph.strip(), section, f"block:{number}"))
    else:
        raise ValueError("支持.docx、文本层.pdf、.md和UTF-8 .txt")
    if not blocks:
        raise ValueError("没有可解析正文；扫描件或图片需要OCR")
    info["text_blocks"] = len(blocks)
    return blocks, info


def _parse_pdf(raw):
    from pypdf import PdfReader, apply_configuration
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("PDF超过8MB解析限制，请拆分文档")
    # Context-local limits protect reader preflight and extraction without changing
    # limits for other threads or modifying pypdf's process-global legacy constants.
    with apply_configuration(
        maximum_declared_stream_length=4 * 1024 * 1024,
        array_based_stream_maximum_output_length=4 * 1024 * 1024,
        zlib_maximum_output_length=4 * 1024 * 1024,
        lzw_maximum_output_length=4 * 1024 * 1024,
        run_length_maximum_output_length=4 * 1024 * 1024,
        brotli_maximum_output_length=4 * 1024 * 1024,
        jbig2_maximum_output_length=4 * 1024 * 1024,
        array_maximum_element_count=10000, dictionary_maximum_element_count=10000,
        stream_filters_maximum_length=4, stream_decoding_work_maximum_length=8 * 1024 * 1024,
        stream_parsing_work_maximum_count=100000, page_tree_maximum_entries=1000,
        page_tree_maximum_depth=20, page_tree_maximum_page_entries=10000,
        xform_maximum_invocations_per_extraction=100,
    ):
        reader = PdfReader(BytesIO(raw), strict=True, root_object_recovery_limit=100,
                           xref_stream_maximum_entries=10000)
        if reader.is_encrypted:
            raise ValueError("不支持加密PDF")
        if len(reader.pages) > 100:
            raise ValueError("PDF超过100页，请拆分文档")
        if sum(len(items) for items in reader.xref.values()) + len(reader.xref_objStm) > 10000:
            raise ValueError("PDF对象过多，请简化文档")
        blocks, decoded_bytes, text_chars = [], 0, 0
        deadline = time.monotonic() + 15
        for number, page in enumerate(reader.pages, 1):
            if time.monotonic() > deadline:
                raise ValueError("PDF解析时间预算超限，请拆分文档")
            content = page.get_contents()
            decoded_bytes += len(content.get_data()) if content is not None else 0
            if decoded_bytes > 16 * 1024 * 1024:
                raise ValueError("PDF内容流总解压量超过16MB")
            extracted = page.extract_text() or ""
            text_chars += len(extracted)
            if text_chars > 500000:
                raise ValueError("PDF提取文本超过50万字符")
            for index, text in enumerate(extracted.split("\n\n"), 1):
                if text.strip():
                    blocks.append(Block(text.strip(), f"第{number}页", f"page:{number}:block:{index}"))
            if len(blocks) > 10000:
                raise ValueError("PDF文本块过多")
        return blocks, len(reader.pages)
