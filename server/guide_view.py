"""Safe HTML view of the repository-owned Markdown subset; never accepts raw HTML."""
import html
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit
import knowledge


def _href(target, document):
    parsed = urlsplit(target)
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        return target
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path:
        return '#' + quote(unquote(parsed.fragment))
    path = posixpath.normpath(posixpath.join(posixpath.dirname(document['path']), unquote(parsed.path)))
    source = knowledge.source_by_path(path)
    if not source:
        return None
    return source['id'] + '?sha=' + source['sha256'] + ('#' + quote(unquote(parsed.fragment)) if parsed.fragment else '')


def inline(text, document):
    pattern = r'(`[^`]+`|!?\[[^\]]*\]\([^\s)]+\)|\*\*[^*]+\*\*|\*[^*]+\*)'
    result = []
    for token in re.split(pattern, text):
        if token.startswith('`') and token.endswith('`'):
            result.append('<code>' + html.escape(token[1:-1]) + '</code>')
        elif re.fullmatch(r'!?\[[^\]]*\]\([^\s)]+\)', token):
            match = re.fullmatch(r'(!?)\[([^\]]*)\]\(([^\s)]+)\)', token)
            image, label, target = match.groups()
            href = _href(target, document)
            if image:
                result.append('<span class="figure-note">그림: ' + html.escape(label) + '</span>')
            elif href:
                result.append('<a href="' + html.escape(href, quote=True) + '" rel="noopener noreferrer">' + html.escape(label) + '</a>')
            else:
                result.append(html.escape(label))
        elif token.startswith('**') and token.endswith('**'):
            result.append('<strong>' + html.escape(token[2:-2]) + '</strong>')
        elif token.startswith('*') and token.endswith('*'):
            result.append('<em>' + html.escape(token[1:-1]) + '</em>')
        else:
            result.append(html.escape(token))
    return ''.join(result)


def body(document):
    lines = document['text'].splitlines()
    if document['kind'] == 'topology':
        return '<pre><code>' + '\n'.join('<span id="L%d">%s</span>' % (i, html.escape(line)) for i, line in enumerate(lines, 1)) + '</code></pre>'
    out, i = [], 0
    while i < len(lines):
        line, number = lines[i], i + 1
        if not line.strip():
            out.append('<span id="L%d"></span>' % number); i += 1; continue
        if line.startswith('```'):
            out.append('<pre id="L%d"><code>' % number); i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                out.append('<span id="L%d">%s</span>\n' % (i + 1, html.escape(lines[i]))); i += 1
            out.append('</code></pre>'); i += 1; continue
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading:
            level, title = len(heading[1]), heading[2]
            slug = re.sub(r'[^\w\s-]', '', title.lower()).strip().replace(' ', '-')
            out.append('<h%d id="L%d"><span id="%s"></span>%s</h%d>' % (level, number, html.escape(slug, quote=True), inline(title, document), level)); i += 1; continue
        if i + 1 < len(lines) and '|' in line and re.fullmatch(r'[\s|:-]+', lines[i + 1]) and '-' in lines[i + 1]:
            cells = lambda row: [cell.strip() for cell in row.strip().strip('|').split('|')]
            out.append('<div class="table-scroll"><table><thead><tr id="L%d">' % number + ''.join('<th>'+inline(cell, document)+'</th>' for cell in cells(line)) + '</tr></thead><tbody>'); i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                out.append('<tr id="L%d">' % (i + 1) + ''.join('<td>'+inline(cell, document)+'</td>' for cell in cells(lines[i])) + '</tr>'); i += 1
            out.append('</tbody></table></div>'); continue
        if re.match(r'^\s*([-*+] |\d+[.)] )', line):
            ordered = bool(re.match(r'^\s*\d', line)); tag = 'ol' if ordered else 'ul'; out.append('<'+tag+'>')
            while i < len(lines) and re.match(r'^\s*([-*+] |\d+[.)] )', lines[i]):
                text = re.sub(r'^\s*([-*+] |\d+[.)] )', '', lines[i])
                out.append('<li id="L%d">%s</li>' % (i + 1, inline(text, document))); i += 1
            out.append('</'+tag+'>'); continue
        if line.startswith('>'):
            out.append('<blockquote>')
            while i < len(lines) and lines[i].startswith('>'):
                out.append('<span id="L%d">%s</span> ' % (i + 1, inline(lines[i].lstrip('> '), document))); i += 1
            out.append('</blockquote>'); continue
        if re.fullmatch(r'\s*[-*_]{3,}\s*', line):
            out.append('<hr id="L%d">' % number); i += 1; continue
        # Line anchors are retained even when source paragraphs span many lines.
        out.append('<p>')
        while i < len(lines) and lines[i].strip():
            if i != number - 1 and re.match(r'^(#{1,6}\s|```|>|\s*[-*+] |\s*\d+[.)] )', lines[i]):
                break
            out.append('<span id="L%d">%s</span> ' % (i + 1, inline(lines[i], document))); i += 1
        out.append('</p>')
    return ''.join(out)


def render(document):
    title = knowledge.display_title(document)
    return '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>''' + html.escape(title) + '''</title><script src="../assets/theme.js"></script><style>
:root{color-scheme:light;--bg:#f6f8fa;--panel:#fff;--ink:#14202e;--muted:#52677b;--wire:#d2dce5;--sig:#0d6e86}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d1621;--panel:#131f2c;--ink:#dbe6f1;--muted:#a3b6c9;--wire:#2a3b4d;--sig:#3fc4dd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 system-ui,sans-serif}
main{max-width:940px;margin:auto;padding:28px 22px 60px}header{color:var(--muted);font-size:14px;border-bottom:1px solid var(--wire);padding-bottom:14px;margin-bottom:24px}
h1{font-size:27px;line-height:1.45}h2{margin-top:32px;font-size:22px}h3{font-size:18px}a{color:var(--sig)}
pre{background:var(--panel);border:1px solid var(--wire);border-radius:8px;padding:14px;overflow:auto;line-height:1.6}
code{font: .88em/1.6 ui-monospace,monospace}p code,li code,td code{background:var(--panel);padding:2px 4px;border-radius:4px}
blockquote{margin:18px 0;padding:10px 18px;border-left:3px solid var(--sig);color:var(--muted);background:var(--panel)}
.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%}td,th{padding:8px 12px;border:1px solid var(--wire);text-align:left}th{background:var(--panel)}
:target{scroll-margin-top:20px;background:color-mix(in srgb,var(--sig) 12%,transparent)}.figure-note,footer{color:var(--muted);font-size:13px}
</style></head><body><main><header>''' + html.escape(title) + ''' · 튜터 참고 문서</header><article>''' + body(document) + '''</article><footer>교재의 초기 설계와 실행 중인 실습 상태는 다를 수 있습니다.</footer></main></body></html>'''
