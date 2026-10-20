"""Closed DOCX review dialect: stories, basic structure and fixed formatting only.

Auxiliary parts (including settings, styles, fonts and numbering) are omitted.
Unknown elements are removed whole; content controls and revision wrappers retain
only their visible children. All names, relationships and namespace declarations
are generated, never copied. This deliberately sacrifices advanced formatting.
"""
from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from docx.oxml.ns import qn
from lxml import etree

from panda.limits import bounded, check_zip

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
PREFIX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.'
STORIES = {
    'document': ('document.main+xml', 'officeDocument'),
    'hdr': ('header+xml', 'header'),
    'ftr': ('footer+xml', 'footer'),
    'comments': ('comments+xml', 'comments'),
    'footnotes': ('footnotes+xml', 'footnotes'),
    'endnotes': ('endnotes+xml', 'endnotes'),
}
# Parent-specific grammar ensures every retained text node belongs to exactly one
# paragraph, so neither stray text nor nested paragraph duplication evades scanning.
BLOCK = {'p', 'tbl'}
GRAMMAR = {
    'document': {'body'}, 'body': BLOCK | {'sectPr'},
    'hdr': BLOCK, 'ftr': BLOCK, 'comments': {'comment'},
    'footnotes': {'footnote'}, 'endnotes': {'endnote'},
    'comment': BLOCK, 'footnote': BLOCK, 'endnote': BLOCK,
    'tbl': {'tr', 'tblGrid'}, 'tblGrid': {'gridCol'}, 'gridCol': set(),
    'tr': {'tc'}, 'tc': BLOCK,
    'p': {'r', 'pPr'}, 'pPr': {'sectPr'},
    'r': {'t', 'rPr', 'tab', 'br'},
    'rPr': {'b', 'i', 'u'}, 'b': set(), 'i': set(), 'u': set(),
    't': set(), 'tab': set(), 'br': set(),
    'sectPr': {'headerReference', 'footerReference'},
    'headerReference': set(), 'footerReference': set(),
}
WRAPPERS = {'sdt', 'sdtContent', 'ins', 'moveTo', 'customXml'}


def serialise(root):
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def project(root, references):
    """Build new XML using exclusively allowed names and bounded attribute values."""
    def copy(node, parent_name=None):
        if not isinstance(node.tag, str) or not node.tag.startswith('{' + W + '}'):
            return []
        name = etree.QName(node).localname
        if name in WRAPPERS:
            return [item for child in node for item in copy(child, parent_name)]
        if name not in GRAMMAR or (parent_name and name not in GRAMMAR[parent_name]):
            return []
        result = etree.Element(qn('w:' + name), nsmap={'w': W, 'r': R})
        if name == 't':
            result.text = node.text or None
            result.set(qn('xml:space'), 'preserve')
        elif name in {'b', 'i'}:
            value = node.get(qn('w:val'), 'true')
            if value not in {'true', 'false', '0', '1', 'on', 'off'}:
                return []
            result.set(qn('w:val'), value)
        elif name == 'u':
            value = node.get(qn('w:val'), 'single')
            if value not in {'single', 'double', 'none'}:
                return []
            result.set(qn('w:val'), value)
        elif name in {'comment', 'footnote', 'endnote'}:
            # These review stories are retained for reading, without source IDs.
            result.set(qn('w:id'), str(len(node.xpath('preceding-sibling::*'))))
        elif name in {'headerReference', 'footerReference'}:
            rid = node.get(qn('r:id'))
            kind = 'hdr' if name == 'headerReference' else 'ftr'
            if rid not in references or references[rid][1] != kind:
                return []
            result.set(qn('r:id'), references[rid][0])
            value = node.get(qn('w:type'), 'default')
            result.set(qn('w:type'), value if value in {'default', 'first', 'even'} else 'default')
        for child in node:
            result.extend(copy(child, name))
        return [result]
    roots = copy(root)
    if len(roots) != 1 or etree.QName(roots[0]).localname not in STORIES:
        raise ValueError('docx_unsupported_story_root')
    return roots[0]


def package_parts(roots):
    """Canonical package envelope; no source-controlled ZIP names or edge data."""
    types = etree.Element(f'{{{CT}}}Types', nsmap={None: CT})
    etree.SubElement(types, f'{{{CT}}}Default', Extension='rels',
                     ContentType='application/vnd.openxmlformats-package.relationships+xml')
    package_rels = etree.Element(f'{{{PKG}}}Relationships', nsmap={None: PKG})
    document_rels = etree.Element(f'{{{PKG}}}Relationships', nsmap={None: PKG})
    parts = {}
    for index, root in enumerate(roots):
        kind = etree.QName(root).localname
        name = 'word/document.xml' if index == 0 else f'word/story-{index}.xml'
        if (index == 0) != (kind == 'document'):
            raise ValueError('docx_invalid_main_story')
        etree.SubElement(types, f'{{{CT}}}Override', PartName='/' + name,
                         ContentType=PREFIX + STORIES[kind][0])
        etree.SubElement(package_rels if index == 0 else document_rels,
                         f'{{{PKG}}}Relationship', Id=f'rId{index}',
                         Type=R + '/' + STORIES[kind][1],
                         Target=name if index == 0 else f'story-{index}.xml')
        parts[name] = serialise(root)
    parts['[Content_Types].xml'] = serialise(types)
    parts['_rels/.rels'] = serialise(package_rels)
    parts['word/_rels/document.xml.rels'] = serialise(document_rels)
    return parts


def write_package(path, roots):
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, data in package_parts(roots).items():
            archive.writestr(name, data)


@bounded
def verify_package(path):
    """Validate every ZIP entry without python-docx reachability or sanitiser calls."""
    with ZipFile(BytesIO(check_zip(path))) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names) or archive.comment or any(
            entry.comment or entry.extra for entry in entries
        ):
            raise ValueError('docx_unsupported_zip_metadata')
        count = len(names) - 3
        expected_names = ['word/document.xml'] + [f'word/story-{i}.xml' for i in range(1, count)]
        if count < 1 or set(names) != set(expected_names) | {
            '[Content_Types].xml', '_rels/.rels', 'word/_rels/document.xml.rels'
        }:
            raise ValueError('docx_unsupported_package_part')
        roots = []
        for name in expected_names:
            data = archive.read(name)
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            root = etree.fromstring(data, parser)
            if root.getroottree().docinfo.doctype:
                raise ValueError('docx_unsupported_doctype')
            roots.append(root)
        kinds = [etree.QName(root).localname for root in roots]
        references = {f'rId{i}': (f'rId{i}', kind) for i, kind in enumerate(kinds) if i}
        # Verification enforces the grammar directly; it does not call project().
        for root in roots:
            for node in root.iter():
                if not isinstance(node.tag, str) or node.tag not in {qn('w:' + n) for n in GRAMMAR}:
                    raise ValueError('docx_unsupported_element')
                name = etree.QName(node).localname
                if any(child.tag not in {qn('w:' + n) for n in GRAMMAR[name]} for child in node):
                    raise ValueError('docx_unsupported_structure')
                allowed = {}
                if name == 't':
                    allowed = {qn('xml:space'): {'preserve'}}
                elif name in {'b', 'i'}:
                    allowed = {qn('w:val'): {'true', 'false', '0', '1', 'on', 'off'}}
                elif name == 'u':
                    allowed = {qn('w:val'): {'single', 'double', 'none'}}
                elif name in {'comment', 'footnote', 'endnote'}:
                    allowed = {qn('w:id'): {str(len(node.xpath('preceding-sibling::*')))}}
                elif name in {'headerReference', 'footerReference'}:
                    kind = 'hdr' if name == 'headerReference' else 'ftr'
                    allowed = {qn('w:type'): {'default', 'first', 'even'},
                               qn('r:id'): {rid for rid, (_, k) in references.items() if k == kind}}
                if set(node.attrib) != set(allowed) or any(
                    value not in allowed[key] for key, value in node.attrib.items()
                ):
                    raise ValueError('docx_unsupported_attribute')
                if (name != 't' and node.text) or node.tail:
                    raise ValueError('docx_unscanned_text')
                if node.nsmap != {'w': W, 'r': R}:
                    raise ValueError('docx_unsupported_namespace')
        expected = package_parts(roots)
        if any(archive.read(name) != data for name, data in expected.items()):
            raise ValueError('docx_noncanonical_package')
        return roots
