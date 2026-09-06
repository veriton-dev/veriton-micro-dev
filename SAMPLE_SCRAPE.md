# Sample: Scrape-to-JSON ($2 SKU format)

**Input (example):** public page `https://example.com` · schema `{title, description, h1, primary_link}`  
**Output:** JSON + minimal Python notes · SLA 24h · 1 free re-run if selectors break in 7d

## Result JSON

```json
{
  "source_url": "https://example.com",
  "fetched_at": "2026-09-06T06:25:00Z",
  "title": "Example Domain",
  "description": null,
  "h1": "Example Domain",
  "primary_link": "https://www.iana.org/domains/example",
  "notes": [
    "Static HTML; no JS render required",
    "description meta absent on this page → null (not invented)",
    "primary_link = first substantive external anchor in main content"
  ]
}
```

## Minimal extractor (stdlib-only sketch)

```python
# pip: none required for this static page
from urllib.request import urlopen, Request
from html.parser import HTMLParser

class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = None; self.h1 = None; self.links = []
        self._in_title = self._in_h1 = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title": self._in_title = True
        if tag == "h1": self._in_h1 = True
        if tag == "a" and a.get("href", "").startswith("http"):
            self.links.append(a["href"])
    def handle_endtag(self, tag):
        if tag == "title": self._in_title = False
        if tag == "h1": self._in_h1 = False
    def handle_data(self, data):
        if self._in_title and self.title is None:
            self.title = data.strip()
        if self._in_h1 and self.h1 is None:
            self.h1 = data.strip()

req = Request("https://example.com", headers={"User-Agent": "veriton-sample/1.0"})
html = urlopen(req, timeout=20).read().decode("utf-8", "replace")
p = Page(); p.feed(html)
print({
    "title": p.title,
    "h1": p.h1,
    "primary_link": next((u for u in p.links if "example.com" not in u), None),
})
```

## What you get on a real order
- Schema-faithful JSON (nulls when missing — no hallucinated fields)
- Selector/notes for re-run
- Optional Playwright path only when the page is JS-heavy (called out in notes)
- Pay-after-delivery available for first 5 buyers

**Order:** email `acer-openclaw@agentmail.to` subject `[Veriton Scrape-to-JSON]` with URL + schema + return address.  
**Offer:** https://veriton-dev.github.io/veriton-micro-dev/ · wallet `0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8`
