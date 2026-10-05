"""Render recorded model prose without turning it into active HTML or requests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]


def render(text: str) -> BeautifulSoup:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the dashboard Markdown renderer")
    result = subprocess.run(
        [node, "-e", """
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {markdownit: require('./refund_eval/dashboard/static/markdown-it.min.js')}};
vm.runInNewContext(fs.readFileSync('./refund_eval/dashboard/static/markdown.js', 'utf8'), context);
process.stdout.write(context.window.RefundMarkdown.render(JSON.parse(fs.readFileSync(0, 'utf8'))));
"""],
        input=json.dumps(text), capture_output=True, text=True, cwd=ROOT, check=True, timeout=10,
    )
    return BeautifulSoup(result.stdout, "html.parser")


def test_model_markdown_preserves_structure_and_literal_code():
    page = render('''## Next steps

Read **the policy** and `booking_ref`.

1. Check the ticket
   - Confirm the date
   - Confirm the fare
2. Send the request

> Keep the reference.

```html
<button onclick="send()">Send</button>
```

| Route | Status |
| --- | --- |
| Trainline | Pending |
''')
    assert page.h2.get_text() == "Next steps"
    assert page.strong.get_text() == "the policy"
    assert len(page.select("ol > li")) == 2
    assert len(page.select("ol ul > li")) == 2
    assert page.blockquote.get_text(strip=True) == "Keep the reference."
    assert page.select_one("pre code").get_text() == '<button onclick="send()">Send</button>\n'
    assert page.button is None
    assert page.select_one(".markdown-table table td").get_text() == "Trainline"


@pytest.mark.parametrize("source", [
    '<script>alert(1)</script><img src="/api/runs" onerror="alert(1)">',
    '[execute](javascript:alert%281%29) [encoded](jav&#x61;script:alert%281%29)',
    '[payload](data:text/html;base64,PHNjcmlwdD4=) [local](file:///etc/passwd)',
    '![tracking](https://tracking.invalid/pixel) ![local](/api/runs)',
    '<iframe src="/api/runs"></iframe><svg onload="alert(1)"></svg>',
])
def test_model_content_cannot_create_executable_html_or_load_images(source):
    page = render(source)
    assert not page.select("script, img, iframe, svg, object, embed, style")
    assert not page.select("[src], [onload], [onerror]")
    assert not page.select("a[href]")
    assert page.get_text().strip()


def test_public_links_are_explicit_clicks_with_no_opener_or_referrer():
    page = render('[Policy](https://example.com/policy) and [Email](mailto:help@example.com)')
    links = page.select("a[href]")
    assert [link["href"] for link in links] == ["https://example.com/policy", "mailto:help@example.com"]
    assert all(link["target"] == "_blank" and set(link["rel"]) == {"noopener", "noreferrer"} for link in links)
