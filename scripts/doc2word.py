#!/usr/bin/env python3
"""TECH_DOC.md -> Word"""
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import re

def shade(cell, hex_color):
    tcPr = cell._element.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), hex_color)
    tcPr.append(shd)

def runs(p, text, size=None):
    for part in re.split(r'(\*\*.*?\*\*)', text):
        if part.startswith('**') and part.endswith('**') and len(part) > 4:
            r = p.add_run(part[2:-2]); r.bold = True
            if size: r.font.size = Pt(size)
        else:
            for cp in re.split(r'(`[^`]+`)', part):
                if cp.startswith('`') and cp.endswith('`') and len(cp) > 2:
                    r = p.add_run(cp[1:-1]); r.font.name = 'Consolas'; r.font.size = Pt(9)
                    r.font.color.rgb = RGBColor(0xC7, 0x25, 0x4E)
                elif cp:
                    r = p.add_run(cp)
                    if size: r.font.size = Pt(size)

def table_rows(lines, i):
    rows = []
    while i < len(lines) and lines[i].strip().startswith('|'):
        cells = [c.strip() for c in lines[i].strip().split('|')[1:-1]]
        if not all(re.match(r'^[-:]+$', c) for c in cells):
            rows.append(cells)
        i += 1
    return rows, i

def convert(md_path, docx_path):
    lines = open(md_path, encoding='utf-8').read().split('\n')
    doc = Document()
    st = doc.styles['Normal']; st.font.name = 'Microsoft YaHei'; st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(0.75)
        s.left_margin = s.right_margin = Inches(0.85)
    i = 0; in_code = False; code = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('```'):
            if in_code:
                txt = '\n'.join(code)
                if txt.strip():
                    p = doc.add_paragraph(); p.paragraph_format.left_indent = Inches(0.15)
                    r = p.add_run(txt); r.font.name = 'Consolas'; r.font.size = Pt(8.5)
                    r.font.color.rgb = RGBColor(0x2A, 0x2A, 0x2A)
                    shd = OxmlElement('w:shd')
                    shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), 'F4F6F8')
                    p._element.get_or_add_pPr().append(shd)
                code = []; in_code = False
            else:
                in_code = True; code = []
            i += 1; continue
        if in_code:
            code.append(line); i += 1; continue
        s = line.strip()
        if not s: i += 1; continue
        if s == '---':
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(3); p.paragraph_format.space_after = Pt(3)
            pPr = p._element.get_or_add_pPr(); pBdr = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            bottom.set(qn('w:val'), 'single'); bottom.set(qn('w:sz'), '6')
            bottom.set(qn('w:space'), '1'); bottom.set(qn('w:color'), 'D0D0D0')
            pBdr.append(bottom)
            pPr.append(pBdr); i += 1; continue
        if s.startswith('# ') and not s.startswith('## '):
            p = doc.add_heading(s[2:], level=0); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in p.runs: r.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
            i += 1; continue
        if s.startswith('## '):
            p = doc.add_heading(s[3:], level=1)
            for r in p.runs: r.font.color.rgb = RGBColor(0x2B, 0x57, 0x9A)
            i += 1; continue
        if s.startswith('### '):
            p = doc.add_heading(s[4:], level=2)
            for r in p.runs: r.font.color.rgb = RGBColor(0x33, 0x66, 0x99)
            i += 1; continue
        if s.startswith('> '):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.25)
            p.paragraph_format.space_before = Pt(4); p.paragraph_format.space_after = Pt(4)
            runs(p, s[2:])
            pPr = p._element.get_or_add_pPr()
            shd = OxmlElement('w:shd')
            shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), 'EAF3FB')
            pPr.append(shd)
            pBdr = pPr.makeelement(qn('w:pBdr'), {})
            left = OxmlElement('w:left')
            left.set(qn('w:val'), 'single'); left.set(qn('w:sz'), '14')
            left.set(qn('w:space'), '5'); left.set(qn('w:color'), '4A90D9')
            pBdr.append(left)
            pPr.append(pBdr); i += 1; continue
        if s.startswith('|'):
            rows, nxt = table_rows(lines, i)
            if rows:
                cols = max(len(r) for r in rows)
                t = doc.add_table(rows=len(rows), cols=cols); t.style = 'Table Grid'
                t.alignment = WD_TABLE_ALIGNMENT.CENTER
                for ri, row in enumerate(rows):
                    for ci in range(cols):
                        cell = t.rows[ri].cells[ci]; cell.text = ''
                        p = cell.paragraphs[0]
                        p.paragraph_format.space_before = Pt(2); p.paragraph_format.space_after = Pt(2)
                        runs(p, row[ci] if ci < len(row) else '', size=9)
                        if ri == 0:
                            shade(cell, '4472C4')
                            for r in p.runs: r.bold = True; r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                        elif ri % 2 == 0:
                            shade(cell, 'E8F0F8')
                doc.add_paragraph().paragraph_format.space_after = Pt(2)
            i = nxt; continue
        m = re.match(r'^(\d+)\.\s+(.*)', s)
        if m:
            p = doc.add_paragraph(style='List Number'); runs(p, m.group(2)); i += 1; continue
        if s.startswith('- ') or s.startswith('* '):
            p = doc.add_paragraph(style='List Bullet'); runs(p, s[2:]); i += 1; continue
        p = doc.add_paragraph(); runs(p, s); i += 1
    doc.save(docx_path)
    print(f'Saved: {docx_path}')

if __name__ == '__main__':
    import sys
    md = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\Vincent Lu\Desktop\mbses-agent\TECH_DOC.md'
    out = sys.argv[2] if len(sys.argv) > 2 else r'C:\Users\Vincent Lu\Desktop\mbses-agent\mbses-agent技术文档.docx'
    convert(md, out)
