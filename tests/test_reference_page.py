"""The offline reference must never send readers to missing checkout files."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs/reference.html"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "href" in attrs:
            self.hrefs.append(attrs["href"])
        if "id" in attrs:
            self.ids.add(attrs["id"])


def test_reference_page_relative_links_resolve():
    assert PAGE.is_file(), "docs/reference.html is missing"
    document = Links()
    document.feed(PAGE.read_text(encoding="utf-8"))
    assert document.hrefs, "The reference page must link to the documentation"
    file_links = []
    for href in document.hrefs:
        url = urlsplit(href)
        if url.scheme or url.netloc:
            continue
        assert not url.path.startswith("/"), f"Not checkout-relative: {href}"
        target = (PAGE.parent / unquote(url.path)).resolve() if url.path else PAGE
        assert target.is_relative_to(ROOT), f"Link leaves the checkout: {href}"
        assert target.is_file(), f"Missing relative link target: {href}"
        if url.fragment and target == PAGE:
            assert unquote(url.fragment) in document.ids, f"Missing anchor: {href}"
        if url.path:
            file_links.append(href)
    assert file_links, "The reference must include links to files, not only anchors"


class PreBlocks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_pre = False
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.in_pre = True
            self.blocks.append("")

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False

    def handle_data(self, data):
        if self.in_pre:
            self.blocks[-1] += data


@pytest.mark.parametrize("source,heading", [
    ("docs/USING-AI.md", "### 2. Test my Striim application"),
    ("examples/acme-retail/PROMPTS.md", "## 2. Add the orders application and test"),
])
def test_reference_prompt_matches_markdown(source, heading):
    markdown = (ROOT / source).read_text(encoding="utf-8")
    section = markdown.split(heading + "\n", 1)[1]
    prompt = re.search(r"```text\n(.*?)\n```", section, re.S)
    assert prompt, f"Missing prompt under {heading} in {source}"
    page = PreBlocks()
    page.feed(PAGE.read_text(encoding="utf-8"))
    assert prompt.group(1) in page.blocks, f"Reference prompt drifted from {source}"
