"""URL utilities shared by the crawler and checks (kept here to avoid an import
cycle between crawler and checks)."""

from __future__ import annotations

import os
import re
from urllib.parse import urldefrag, urlparse, urlunparse

# Extensions that are never HTML pages: don't crawl into them (still liveness-checked).
NON_HTML_EXTENSIONS = {
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".mp4", ".mp3", ".avi", ".mov", ".wav", ".woff", ".woff2", ".ttf", ".eot",
    ".css", ".js", ".json", ".xml", ".rss",
}

# Public suffixes with a meaningful third label (registrable = last 3).
# Small curated list — full PSL is out of scope for the deps.
_TWO_LEVEL_SUFFIXES = {
    ("co", "uk"), ("org", "uk"), ("gov", "uk"), ("ac", "uk"), ("me", "uk"),
    ("com", "au"), ("net", "au"), ("org", "au"), ("co", "nz"), ("co", "jp"),
    ("com", "br"), ("co", "in"), ("co", "za"),
}
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1. IPs and ``localhost`` return as-is."""
    host = (host or "").lower().strip(".")
    if not host or host == "localhost" or _IPV4_RE.match(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if tuple(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_url(url: str) -> str:
    """Canonical form for dedupe: drop fragment, lowercase scheme/host, strip a
    trailing slash from non-root paths."""
    url, _ = urldefrag(url)
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parts.params, parts.query, ""))


def looks_non_html(url: str) -> bool:
    """True if the URL's path has a known non-HTML file extension."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext in NON_HTML_EXTENSIONS


def path_key(url: str) -> str:
    """Scheme+host+path without the query — used to cap query-param explosion."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def same_registrable_domain(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    if pa.scheme == "file" or pb.scheme == "file":
        return pa.scheme == pb.scheme == "file"
    if pb.scheme not in ("http", "https"):
        return False
    return registrable_domain(pa.hostname or "") == registrable_domain(pb.hostname or "")


def is_third_party(page_url: str, resource_url: str | None) -> bool:
    """True if resource_url is on a different registrable domain than the page."""
    if not resource_url:
        return False
    rp = urlparse(resource_url)
    if rp.scheme not in ("http", "https"):
        return False
    return not same_registrable_domain(page_url, resource_url)
