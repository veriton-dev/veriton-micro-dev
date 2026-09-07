"""HTML → structured JSON (no network fetch)."""
from __future__ import annotations
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

class _H(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._intitle = False
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.headings: list[dict[str, str]] = []
        self._h = None
        self._hbuf = ""
        self.base = ""
        self.canonical = ""
        self._in_a = False
        self._a_href = ""
        self._a_buf = ""

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._intitle = True
        elif tag == "base" and d.get("href"):
            self.base = d["href"]
        elif tag == "meta":
            k = d.get("name") or d.get("property") or d.get("http-equiv")
            v = d.get("content")
            if k and v:
                self.meta[k] = v
        elif tag == "link" and d.get("rel") == "canonical" and d.get("href"):
            self.canonical = d["href"]
        elif tag == "a" and d.get("href"):
            self._in_a = True
            self._a_href = d["href"]
            self._a_buf = ""
        elif tag == "img" and d.get("src"):
            self.images.append({"src": d["src"], "alt": d.get("alt") or ""})
        elif tag in ("h1", "h2", "h3"):
            self._h = tag
            self._hbuf = ""

    def handle_endtag(self, tag):
        if tag == "title":
            self._intitle = False
        elif tag == "a" and self._in_a:
            self.links.append({"href": self._a_href, "text": " ".join(self._a_buf.split())[:200]})
            self._in_a = False
        elif tag in ("h1", "h2", "h3") and self._h == tag:
            self.headings.append({"level": tag, "text": " ".join(self._hbuf.split())[:300]})
            self._h = None

    def handle_data(self, data):
        if self._intitle:
            self.title += data
        if self._in_a:
            self._a_buf += data
        if self._h:
            self._hbuf += data


def extract_html(html: str, *, base_url: str | None = None, limit: int = 50) -> dict[str, Any]:
    p = _H()
    p.feed(html or "")
    p.close()
    base = base_url or p.base or ""

    def abs_u(u: str) -> str:
        if not u:
            return u
        try:
            return urljoin(base or "https://example.invalid/", u)
        except Exception:
            return u

    links = [{"href": abs_u(x["href"]), "text": x["text"]} for x in p.links[:limit]]
    images = [{"src": abs_u(x["src"]), "alt": x["alt"]} for x in p.images[:limit]]
    return {
        "title": " ".join(p.title.split()),
        "canonical": abs_u(p.canonical) if p.canonical else None,
        "meta": p.meta,
        "headings": p.headings[:limit],
        "links": links,
        "images": images,
        "stats": {
            "meta": len(p.meta),
            "headings": len(p.headings),
            "links": len(p.links),
            "images": len(p.images),
        },
    }
