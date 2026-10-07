"""Escaped HTML rendering for Quant reports, independent of Agent protocols."""

import html
import re


def page(markdown, title):
    """A deliberately small, escaped renderer for the generated report syntax."""
    blocks, paragraph, rows = [], [], []
    in_code, code = False, []
    def flush():
        if paragraph:
            blocks.append("<p>" + "<br>".join(html.escape(v) for v in paragraph) + "</p>")
            paragraph.clear()
    def flush_table():
        if rows:
            visible = [r for r in rows if not all(re.fullmatch(r":?-+:?", c.strip()) for c in r)]
            body = []
            for i, row in enumerate(visible):
                tag = "th" if i == 0 else "td"
                body.append("<tr>" + "".join(f"<{tag}>" + html.escape(html.unescape(cell)) + f"</{tag}>" for cell in row) + "</tr>")
            blocks.append('<div class="table"><table>' + "".join(body) + "</table></div>")
            rows.clear()
    for line in markdown.splitlines():
        if line.startswith("```"):
            flush(); flush_table()
            if in_code:
                blocks.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
                code.clear()
            in_code = not in_code
        elif in_code:
            code.append(line)
        elif line.startswith("|") and line.endswith("|"):
            flush()
            rows.append([c.strip() for c in line[1:-1].split("|")])
        else:
            flush_table()
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                flush()
                level, label = len(heading[1]), heading[2]
                identifier = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-")
                blocks.append(f'<h{level} id="{html.escape(identifier)}">' + html.escape(label) + f"</h{level}>")
            elif not line.strip():
                flush()
            elif line.startswith("- "):
                flush()
                blocks.append("<p class=bullet>• " + html.escape(line[2:]) + "</p>")
            else:
                paragraph.append(line)
    flush(); flush_table()
    style = "body{max-width:1050px;margin:40px auto;padding:0 24px;background:#faf9f6;color:#17242d;font:16px/1.75 system-ui}h1{font-size:30px}h2{margin-top:40px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #d8dcd8;padding:10px;text-align:left;vertical-align:top}.table{overflow:auto;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#efefea;padding:18px;font:14px/1.6 ui-monospace}.bullet{margin:4px 0}"
    return ("<!doctype html><meta charset=utf-8><title>" + html.escape(title) + "</title><style>" + style + "</style>" + "\n".join(blocks) + "\n").encode()
