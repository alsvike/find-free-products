#!/usr/bin/env python3
"""
Find produkter til 0 kr. / gratis på mange webshops.

Designet til CSV-filer med kolonnen "web_domain" (bl.a. e-mærket og Indexo).
- Respekterer robots.txt som standard.
- Bruger sitemap.xml, interne links og almindelige søge-URL'er.
- Finder især Schema.org Product/Offer med pris 0.
- Finder også synlig "0 kr." / "gratis"-tekst på sandsynlige produktsider.
- Filtrerer bl.a. gavekort, gratis fragt, kurv-0, variant-/tilvalgs-0 og 'Fra 0 kr.'.
- Gemmer fund løbende i CSV og en statusfil, så kørslen kan genoptages.

Eksempel:
    python find_free_products.py indexo-websites-20260819-190201.csv \
        --output gratis_produkter.csv \
        --status scannede_webshops.csv \
        --workers 8 \
        --max-pages-per-site 30 \
        --resume
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import requests
from bs4 import BeautifulSoup


USER_AGENT = "FreeProductFinder/1.4 (+local research; respectful crawler)"
DEFAULT_TIMEOUT = 12
MAX_SITEMAP_BYTES = 8_000_000
MAX_SITEMAP_DECOMPRESSED_BYTES = 16_000_000
MAX_HTML_BYTES = 4_000_000
MAX_CANDIDATE_ATTEMPT_FACTOR = 3

# Kun afsluttede, ikke-midlertidige statustyper springes over med --resume.
# Netværksfejl, rate limits og blokerede requests skal kunne prøves igen.
RESUME_COMPLETED_STATUSES = {
    "ok",
    "invalid_domain",
    "robots_disallowed",
    "external_redirect",
    "homepage_not_found",
}

# Tegn der ofte sniger sig med fra CSV/HTML-kopiering, men ikke bør stå
# yderst i et domæne eller en URL. Vi fjerner KUN tegn i enderne.
EDGE_JUNK_CHARS = " \t\r\n;,\'\\\"|<>[]{}()"
DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$",
    re.I,
)

FREE_WORD_RE = re.compile(
    r"\b(gratis|kostnadsfri|kostnadsfrit|uden\s+betaling)\b",
    re.I,
)
FREE_PRICE_WORD_RE = re.compile(r"^(?:gratis|free)$", re.I)
ZERO_PRICE_RE = re.compile(
    r"(?<!\d)(?:0(?:[.,]00)?\s*(?:kr\.?|dkk)|(?:kr\.?|dkk)\s*0(?:[.,]00)?)(?!\d)",
    re.I,
)

# Globale/lokale tekster der typisk betyder, at 0 kr. ikke er produktets pris.
EXCLUDE_RE = re.compile(
    r"\b("
    r"gavekort|gift\s*card|voucher|tilgodebevis|"
    r"gratis\s+fragt|fri\s+fragt|gratis\s+levering|fri\s+levering|"
    r"free\s+shipping|free\s+delivery|"
    r"gratis\s+returnering|fri\s+retur|free\s+returns|"
    r"gratis\s+afhentning|click\s*&\s*collect|"
    r"levering\s*(?:fra\s*)?(?:kr\.?\s*)?0(?:[.,]00)?|"
    r"fragt\s*(?:fra\s*)?(?:kr\.?\s*)?0(?:[.,]00)?|"
    r"shipping\s*(?:from\s*)?(?:dkk\s*)?0(?:[.,]00)?|"
    r"startgebyr|oprettelsesgebyr|depositum|pant|"
    r"gratis\s+(?:besøg|besoeg|opmåling|opmaaling|montering|installation|rådgivning|raadgivning)|"
    r"free\s+(?:consultation|installation|measurement|visit)|"
    r"serviceaftale|service\s+visit"
    r")\b",
    re.I,
)

# "Free" i et produktnavn betyder ofte noget andet end gratis pris.
FREE_FROM_RE = re.compile(
    r"\b("
    r"fragrance[-\s]?free|sugar[-\s]?free|alcohol[-\s]?free|gluten[-\s]?free|"
    r"lactose[-\s]?free|paraben[-\s]?free|plastic[-\s]?free|cruelty[-\s]?free|"
    r"dairy[-\s]?free|fat[-\s]?free|wireless[-\s]?free|hands[-\s]?free|"
    r"alkoholfri|sukkerfri|glutenfri|laktosefri|parfumefri|duftfri"
    r")\b",
    re.I,
)

NON_PRODUCT_RE = re.compile(
    r"\b(blog|artikel|nyhed|news|guide|inspiration|kundeservice|faq|"
    r"levering|shipping|returns?|retur|service|booking|book\s+tid)\b",
    re.I,
)

CONDITIONAL_FREE_RE = re.compile(
    r"\b(ved\s+køb|ved\s+koeb|køb\s+for|koeb\s+for|minimumskøb|"
    r"med\s+i\s+købet|gave\s+med\s+køb|gratis\s+gave\s+ved|"
    r"with\s+purchase|when\s+you\s+buy|spend\s+(?:at\s+least|over)|"
    r"minimum\s+purchase|free\s+gift\s+with)\b",
    re.I,
)

PRICE_ATTR_RE = re.compile(
    r"(?:^|[^a-z0-9])(product[-_\s]?price|current[-_\s]?price|sale[-_\s]?price|"
    r"sales[-_\s]?price|price[-_\s]?current|price[-_\s]?now|offer[-_\s]?price|"
    r"final[-_\s]?price|unit[-_\s]?price|price|amount)(?:$|[^a-z0-9])",
    re.I,
)

BAD_PRICE_ATTR_RE = re.compile(
    r"shipping|delivery|freight|fragt|levering|cart|basket|subtotal|total|"
    r"discount|saving|fee|gebyr|deposit|pant|install",
    re.I,
)

# Prisfelter i kurv/minikurv/header er ikke produktpriser.
CART_CONTEXT_RE = re.compile(
    r"(?:^|[^a-z0-9])(" 
    r"mini[-_\s]?cart|menu[-_\s]?cart|shopping[-_\s]?cart|header[-_\s]?cart|"
    r"cart[-_\s]?(?:contents?|total|subtotal|amount|count|toggle)|"
    r"basket[-_\s]?(?:contents?|total|subtotal|amount|count|toggle)|"
    r"woocommerce[-_\s]?(?:mini[-_\s]?cart|cart)|"
    r"shoptimizer[-_\s]?cart|kurv[-_\s]?(?:total|indhold|toggle)"
    r")(?:$|[^a-z0-9])",
    re.I,
)

# Købsknapper i relaterede produkter, navigation og footer må ikke validere
# hovedproduktets pris/availability.
AUXILIARY_BUY_CONTEXT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:header|footer|nav|menu|recommend(?:ed|ation)?|related|"
    r"upsell|up-sell|cross-sell|recently-viewed|also-bought)(?:$|[^a-z0-9])",
    re.I,
)

# + 0,00 kr. er typisk et tilvalg/variantens MERpris, ikke produktets pris.
ADDON_ZERO_RE = re.compile(
    r"^\s*[\(\[]?\s*\+\s*0(?:[.,]00)?\s*(?:kr\.?|dkk)?\s*[\)\]]?\s*$",
    re.I,
)

OPTION_CONTEXT_RE = re.compile(
    r"\b(additional|additionals|addon|add-on|option|variant|modifier|"
    r"tilvalg|merpris|valgmulighed|vælg\s+en|vaelg\s+en|"
    r"farve|størrelse|stoerrelse|attribute)\b",
    re.I,
)

# V5 strict: "Fra 0 kr." er ikke en sikker gratis produktpris.
# Det er typisk en startpris på specialmål/configurator-produkter.
STARTING_ZERO_RE = re.compile(
    r"\b(?:fra|from|starting\s+(?:at|from))\s*"
    r"(?:kr\.?\s*|dkk\s*)?0(?:[.,]00)?\s*(?:kr\.?|dkk)?\b",
    re.I,
)

# V5 strict: 0-priser i variant-/option-/configurator-kontekst er
# normalt en merpris/placeholder, ikke hovedproduktets pris.
OPTION_ZERO_CONTEXT_RE = re.compile(
    r"\b("
    r"variant(?:\s*pris)?|variant[-_\s]?option|variant[-_\s]?item|"
    r"option[-_\s]?(?:item|price|value|variant)?|"
    r"tilvalg|merpris|valgpris|prisforskel|"
    r"add[-_\s]?on|addon|modifier|price[-_\s]?adjustment|surcharge|extra[-_\s]?cost|"
    r"attribute[-_\s]?(?:option|value)?|"
    r"configurator|konfigurator|configuration|konfiguration|"
    r"specialmål|specialmal|målbestilt|maalbestilt|"
    r"vælg\s+(?:bredde|højde|hoejde|længde|laengde|mål|maal)|"
    r"choose\s+(?:width|height|length|size)"
    r")\b",
    re.I,
)

# Sider der meget sandsynligt er indhold frem for købbar vare.
NON_PRODUCT_PATH_RE = re.compile(
    r"/(?:blog|blogs|artikel|artikler|news|nyhed|nyheder|guide|guides|"
    r"inspiration|kundeservice|faq)(?:/|$)",
    re.I,
)

# Kategori-/arkivsider må ikke behandles som én produktdetaljeside alene
# fordi de indeholder Product-schema for deres produktkort.
CATEGORY_PATH_RE = re.compile(
    r"/(?:produkt-kategori|product-category|kategori|category|collections?)(?:/|$)",
    re.I,
)

POSITIVE_CURRENCY_RE = re.compile(
    r"(?<!\d)("
    r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:kr\.?|dkk)"
    r"|(?:kr\.?|dkk)\s*(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?"
    r")(?!\d)",
    re.I,
)

PRODUCT_URL_HINT_RE = re.compile(
    r"/(produkt|product|products|p|shop|vare|varer|item|catalog|collections?)/|"
    r"(produkt|product|item|sku|variant|gratis|free|0-?kr)",
    re.I,
)

SEARCH_PATHS = (
    "/search?q=gratis",
    "/search?query=gratis",
    "/search?q=0%20kr",
    "/soeg?q=gratis",
    "/sog?q=gratis",
    "/s%C3%B8g?q=gratis",
)

_thread_local = threading.local()


@dataclass
class Finding:
    domain: str
    url: str
    product_name: str
    match_type: str
    price: str
    matched_text: str
    source: str


@dataclass
class SiteStatus:
    domain: str
    status: str
    pages_checked: int
    findings: int
    note: str


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "da,en;q=0.8",
            }
        )
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=1)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def clean_url(value: str, *, keep_fragment: bool = False) -> str:
    """
    Rens en URL uden at ødelægge legitime querystrings.

    Fjerner bl.a. afsluttende:
      ; , | " ' < > [ ] { } ( )
    samt whitespace. Det løser fx:
      https://shop.dk/;  -> https://shop.dk/
      "https://shop.dk/" -> https://shop.dk/
    """
    value = (value or "").strip()
    if not value:
        return ""

    # Fjern typisk CSV-/kopi-junk i begge ender.
    value = value.strip(EDGE_JUNK_CHARS)

    # HTML entities kan forekomme i hrefs/sitemaps.
    value = value.replace("&amp;", "&")

    # Fjern igen trailing junk efter entity-rensning.
    value = value.rstrip(EDGE_JUNK_CHARS)

    # URL-defragmentering så samme side ikke scannes flere gange.
    if not keep_fragment:
        value, _fragment = urllib.parse.urldefrag(value)

    try:
        p = urllib.parse.urlparse(value)
    except Exception:
        return value.rstrip(EDGE_JUNK_CHARS)

    # Hvis det er en absolut HTTP(S)-URL, normaliser host og fjern trailing junk
    # fra path/query. Bevar ellers værdien, så urljoin kan håndtere relative links.
    if p.scheme.lower() in {"http", "https"} and p.netloc:
        scheme = p.scheme.lower()
        netloc = p.netloc.strip(EDGE_JUNK_CHARS)

        path = (p.path or "/").rstrip(EDGE_JUNK_CHARS)
        if not path:
            path = "/"

        # Et afsluttende semikolon ligger nogle gange i path params via urlparse.
        params = (p.params or "").rstrip(EDGE_JUNK_CHARS)
        query = (p.query or "").rstrip(EDGE_JUNK_CHARS)

        cleaned = urllib.parse.urlunparse(
            (scheme, netloc, path, params, query, "" if not keep_fragment else p.fragment)
        )
        return cleaned.rstrip(EDGE_JUNK_CHARS)

    return value.rstrip(EDGE_JUNK_CHARS)


def clean_domain(value: str) -> str:
    """
    Accepter både et rent domæne og en fuld URL fra CSV'en.
    Returnerer kun hostname uden www., port, path, query eller separator-junk.
    """
    value = (value or "").strip()
    if not value:
        return ""

    # Fjern wrapping/trailing tegn såsom ; og citationstegn.
    value = value.strip(EDGE_JUNK_CHARS)

    # Maskerede/ugyldige domæner må aldrig forsøges åbnet.
    if "*" in value:
        return ""

    # Gør et rent domæne parsebart som URL.
    probe = value if re.match(r"^https?://", value, re.I) else "https://" + value

    try:
        parsed = urllib.parse.urlparse(clean_url(probe))
        host = parsed.hostname or ""
    except Exception:
        host = ""

    host = host.strip().lower().strip(".").strip(EDGE_JUNK_CHARS)
    if host.startswith("www."):
        host = host[4:]

    # IDN/punycode: requests/urlparse arbejder fint med ASCII-punycode.
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        pass

    if not host or "*" in host or not DOMAIN_RE.fullmatch(host):
        return ""
    return host

def same_site(url: str, domain: str) -> bool:
    url = clean_url(url)
    domain = clean_domain(domain)
    if not url or not domain:
        return False
    try:
        host = clean_domain(urllib.parse.urlparse(url).hostname or "")
    except Exception:
        return False
    if not host:
        return False
    # Tillad det valgte domæne og dets underdomæner, men udvid ikke et
    # subdomæne til det bredere parent-domæne.
    return host == domain or host.endswith("." + domain)


def canonical_base(domain: str, timeout: int) -> tuple[Optional[str], str]:
    session = get_session()
    last_status = "unreachable"
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        try:
            # Context manageren lukker den streamede response, så forbindelser
            # ikke lækker ved scanning af mange tusinde domæner.
            with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as r:
                final = clean_url(r.url)
                if final and not same_site(final, domain):
                    return None, f"external_redirect:{final[:400]}"

                if 200 <= r.status_code < 400:
                    p = urllib.parse.urlparse(final)
                    if p.scheme and p.netloc:
                        base = clean_url(f"{p.scheme}://{p.netloc}/").rstrip("/")
                        return base, "ok"
                elif r.status_code in (401, 403):
                    last_status = "blocked"
                elif r.status_code == 429:
                    last_status = "rate_limited"
                elif r.status_code == 404:
                    last_status = "homepage_not_found"
                else:
                    last_status = f"http_{r.status_code}"
        except requests.RequestException:
            pass
    return None, last_status


def load_robots(
    base: str,
    domain: str,
    timeout: int,
) -> tuple[Optional[urllib.robotparser.RobotFileParser], list[str]]:
    robots_url = urllib.parse.urljoin(base + "/", "robots.txt")
    session = get_session()
    sitemaps: list[str] = []
    try:
        r = session.get(robots_url, timeout=timeout)
        if not same_site(r.url, domain):
            return None, sitemaps
        if r.status_code >= 400:
            return None, sitemaps
        text = r.text[:1_000_000]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(text.splitlines())
        for line in text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = clean_url(line.split(":", 1)[1].strip())
                if sm:
                    sitemaps.append(sm)
        return rp, sitemaps
    except requests.RequestException:
        return None, sitemaps


def allowed(rp: Optional[urllib.robotparser.RobotFileParser], url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def polite_sleep(delay: float) -> None:
    if delay <= 0:
        return
    time.sleep(delay + random.uniform(0, min(0.25, delay / 2)))


def fetch(
    url: str,
    timeout: int,
    rp: Optional[urllib.robotparser.RobotFileParser],
    delay: float,
    domain: Optional[str] = None,
) -> Optional[requests.Response]:
    url = clean_url(url)
    if not url or not allowed(rp, url):
        return None
    polite_sleep(delay)
    try:
        r = get_session().get(url, timeout=timeout, allow_redirects=True)
        if domain and not same_site(r.url, domain):
            r.close()
            return None
        if r.status_code in (401, 403, 429):
            return None
        if r.status_code >= 400:
            return None
        return r
    except requests.RequestException:
        return None


def parse_sitemap_content(content: bytes, url: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, nested_sitemap_urls)."""
    if len(content) > MAX_SITEMAP_BYTES:
        content = content[:MAX_SITEMAP_BYTES]

    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
                content = gz.read(MAX_SITEMAP_DECOMPRESSED_BYTES + 1)
            if len(content) > MAX_SITEMAP_DECOMPRESSED_BYTES:
                return [], []
        except OSError:
            return [], []

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], []

    tag = root.tag.lower()
    locs = [
        clean_url((el.text or "").strip())
        for el in root.iter()
        if el.tag.lower().endswith("loc") and (el.text or "").strip()
    ]
    locs = [u for u in locs if u]
    if tag.endswith("sitemapindex"):
        return [], locs
    return locs, []


def get_sitemap_urls(
    base: str,
    domain: str,
    rp: Optional[urllib.robotparser.RobotFileParser],
    discovered_sitemaps: list[str],
    timeout: int,
    delay: float,
    max_sitemap_urls: int,
) -> list[str]:
    queue = []
    seen_sm = set()

    for u in discovered_sitemaps + [
        urllib.parse.urljoin(base + "/", "sitemap.xml"),
        urllib.parse.urljoin(base + "/", "sitemap_index.xml"),
        urllib.parse.urljoin(base + "/", "sitemap-index.xml"),
    ]:
        if u and u not in queue:
            queue.append(u)

    pages: list[str] = []
    seen_pages = set()

    # Begræns også antal sitemap-filer for ikke at ramme enorme kataloger.
    while queue and len(seen_sm) < 25 and len(pages) < max_sitemap_urls:
        sm = queue.pop(0)
        if sm in seen_sm or not same_site(sm, domain):
            continue
        seen_sm.add(sm)
        r = fetch(sm, timeout, rp, delay, domain)
        if not r:
            continue
        page_urls, nested = parse_sitemap_content(r.content, sm)

        for nu in nested:
            if same_site(nu, domain) and nu not in seen_sm:
                queue.append(nu)

        for pu in page_urls:
            if not same_site(pu, domain):
                continue
            if pu in seen_pages:
                continue
            seen_pages.add(pu)
            pages.append(pu)
            if len(pages) >= max_sitemap_urls:
                break

    return pages


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def iter_json_objects(obj) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_json_objects(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_json_objects(item)


def jsonld_products(soup: BeautifulSoup) -> list[dict]:
    products = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text("", strip=True)
        if not raw:
            continue
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            # Nogle sider har flere JSON-objekter eller små syntaksfejl.
            continue
        for obj in iter_json_objects(data):
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(t).lower() == "product" for t in types if t):
                products.append(obj)
    return products


def normalize_price(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    s = str(value).strip().lower()
    s = s.replace("dkk", "").replace("kr.", "").replace("kr", "").strip()
    s = s.replace(" ", "")
    # Dansk format 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def offer_prices(product: dict) -> list[tuple[Optional[float], str, dict]]:
    offers = product.get("offers")
    if not offers:
        return []
    if not isinstance(offers, list):
        offers = [offers]
    out = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            if key in offer:
                raw = offer.get(key)
                out.append((normalize_price(raw), str(raw), offer))
    return out


def product_name_from_page(soup: BeautifulSoup, product: Optional[dict] = None) -> str:
    if product:
        name = product.get("name")
        if isinstance(name, str) and name.strip():
            return re.sub(r"\s+", " ", name).strip()[:300]
    h1 = soup.find("h1")
    if h1:
        txt = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()
        if txt:
            return txt[:300]
    if soup.title and soup.title.string:
        return re.sub(r"\s+", " ", soup.title.string).strip()[:300]
    return ""


def compact_text(node, limit: int = 800) -> str:
    if node is None:
        return ""
    try:
        txt = node.get_text(" ", strip=True)
    except Exception:
        txt = str(node)
    return re.sub(r"\s+", " ", txt).strip()[:limit]


def attrs_text(node) -> str:
    if node is None or not getattr(node, "attrs", None):
        return ""
    parts = []
    for k, v in node.attrs.items():
        if isinstance(v, (list, tuple)):
            v = " ".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    return " ".join(parts)


def is_excluded(text: str) -> bool:
    text = text or ""
    return bool(EXCLUDE_RE.search(text) or CONDITIONAL_FREE_RE.search(text))


def primary_product_text(soup: BeautifulSoup, limit: int = 12_000) -> str:
    """Returnér tekst fra hovedproduktets område uden header/footer-støj."""
    h1 = soup.find("h1")
    if h1 is None:
        return ""

    best = h1
    cur = h1
    for _ in range(10):
        cur = getattr(cur, "parent", None)
        if cur is None:
            break
        name = str(getattr(cur, "name", "") or "").lower()
        attrs = attrs_text(cur)
        if name in {"main", "article"} or re.search(
            r"(?:^|[^a-z0-9])(?:single[-_\s]?product|type[-_\s]?product|"
            r"product[-_\s]?(?:page|detail|main|content)|product)(?:$|[^a-z0-9])",
            attrs,
            re.I,
        ):
            best = cur
        if name == "main":
            break
    return compact_text(best, limit)


def has_conditional_free_product_offer(text: str) -> bool:
    """
    Find købskrav der står før den gratis ydelse i produktområdet.

    Retningen er vigtig: "ved køb over 8.000 kr. ... GRATIS et træ" er
    betinget, mens "gratis ringmåler ... ved køb under 400 betales fragt"
    stadig har en gratis produktpris og kun beskriver fragtomkostningen.
    """
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return False

    explicit = re.compile(
        r"\b(?:gratis\s+gave\s+ved|free\s+gift\s+with|gave\s+med\s+køb|"
        r"med\s+i\s+købet|with\s+purchase)\b",
        re.I,
    )
    if explicit.search(normalized):
        return True

    purchase_before_free = re.compile(
        r"\b(?:ved\s+køb|ved\s+koeb|køb\s+for|koeb\s+for|minimumskøb|"
        r"når\s+du\s+handler|naar\s+du\s+handler|when\s+you\s+buy|"
        r"spend\s+(?:at\s+least|over)|minimum\s+purchase)\b"
        r".{0,260}\b(?:gratis|free|kostnadsfri|uden\s+betaling)\b",
        re.I,
    )
    return bool(purchase_before_free.search(normalized))


def is_false_free_name(text: str) -> bool:
    return bool(FREE_FROM_RE.search(text or ""))


def node_is_hidden(node) -> bool:
    """Best-effort: ignorer priser der selv eller via en ancestor er skjult."""
    if node is None:
        return True

    cur = node
    # En normal prisnode når html/body på langt færre end 30 niveauer. Loftet
    # beskytter mod aparte eller programmatisk konstruerede DOM-træer.
    for _ in range(30):
        if cur is None:
            break
        name = str(getattr(cur, "name", "") or "").lower()
        if name == "meta":
            return True
        attrs = getattr(cur, "attrs", {}) or {}

        if "hidden" in attrs or "inert" in attrs:
            return True
        if str(attrs.get("aria-hidden", "")).lower() == "true":
            return True
        if name == "input" and str(attrs.get("type", "")).lower() == "hidden":
            return True

        cls = attrs.get("class", [])
        if isinstance(cls, str):
            cls = cls.split()
        cls_blob = " ".join(str(x) for x in cls)
        style = str(attrs.get("style", ""))
        blob = f"{cls_blob} {style}".lower()
        if (
            re.search(r"\b(d-none|hidden|visually-hidden|sr-only|screen-reader-text)\b", blob)
            or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", blob)
        ):
            return True
        cur = getattr(cur, "parent", None)
    return False


def node_in_cart_context(node, max_levels: int = 6) -> bool:
    """Afvis prisnoder der ligger i mini-cart, cart total, header cart osv."""
    cur = node
    for _ in range(max_levels + 1):
        if cur is None:
            break
        attrs = attrs_text(cur)
        if CART_CONTEXT_RE.search(attrs):
            return True

        # Kort lokal tekst kan afsløre fx "kr. 0 0 Se kurv".
        txt = compact_text(cur, 320)
        if len(txt) <= 320 and re.search(
            r"(?:se\s+kurv|view\s+cart|"
            r"\b0\s*kurv\b|\b0\s*cart\b|\bkurv\s*0\b|\bcart\s*0\b)",
            txt,
            re.I,
        ):
            return True
        cur = getattr(cur, "parent", None)
    return False


def is_addon_zero(raw_text: str, context: str = "") -> bool:
    raw = re.sub(r"\s+", " ", raw_text or "").strip()
    if ADDON_ZERO_RE.fullmatch(raw):
        return True
    return bool(
        re.search(r"\+\s*0(?:[.,]00)?\s*(?:kr\.?|dkk)", raw, re.I)
        and OPTION_CONTEXT_RE.search(context or "")
    )


def is_starting_zero(raw_text: str, context: str = "") -> bool:
    """
    Afvis fx:
      Fra 0,00 kr.
      From DKK 0
      Starting at 0 kr.

    En "fra"-pris er ikke bevis for, at en konkret købbar variant er gratis.
    """
    combined = re.sub(r"\s+", " ", f"{raw_text or ''} {context or ''}")
    return bool(STARTING_ZERO_RE.search(combined))


def node_in_option_context(node, max_levels: int = 5) -> bool:
    """
    Find variant/option/configurator-kontekst omkring en 0-pris.

    Vi vægter attributter højt, fordi fx:
      class="option variant-option-item"
    er et meget stærkere signal end almindelig produktsidetekst.
    """
    cur = node
    for _ in range(max_levels + 1):
        if cur is None:
            break

        attrs = attrs_text(cur)
        if OPTION_ZERO_CONTEXT_RE.search(attrs):
            return True

        # Kun kort lokal tekst. Vi vil ikke lade hele produktsiden gøre
        # hovedprisen til en "variantpris", bare fordi der findes varianter.
        txt = compact_text(cur, 500)
        if txt and len(txt) <= 500:
            if re.search(
                r"\b(?:variant\s*pris|tilvalg|merpris|valgpris|prisforskel|"
                r"specialmål|specialmal|konfigurator|configurator)\b",
                txt,
                re.I,
            ):
                return True

        cur = getattr(cur, "parent", None)

    return False


def is_option_or_configurator_zero(node, raw_text: str, context: str = "") -> bool:
    """
    V5 strict: en 0-pris afvises, hvis den er en variant/option/startpris.
    Kaldes kun på kandidater, der allerede ligner 0 kr./gratis.
    """
    if is_addon_zero(raw_text, context):
        return True

    if is_starting_zero(raw_text, context):
        return True

    if OPTION_ZERO_CONTEXT_RE.search(context or ""):
        return True

    if node_in_option_context(node):
        return True

    return False


def page_has_placeholder_zero(soup: BeautifulSoup) -> bool:
    """
    Bruges til structured/meta price=0.

    Hvis siden samtidig indeholder en synlig 0-pris, der tydeligt er
    "Fra 0", variantpris eller optionpris, så stoler vi ikke på skjult
    JSON-LD/meta price=0.
    """
    for node in price_node_candidates(soup):
        if node_is_hidden(node) or node_in_cart_context(node):
            continue

        raw = compact_text(node, 350)
        if not raw:
            raw = str(node.get("content", "")).strip()

        if not ZERO_PRICE_RE.search(raw):
            continue

        context = node_context(node)
        if is_option_or_configurator_zero(node, raw, context):
            return True

    return False

def positive_currency_values(text: str) -> list[float]:
    values = []
    for m in POSITIVE_CURRENCY_RE.finditer(text or ""):
        value = normalize_price(m.group(1))
        if value is not None and value > 0:
            values.append(value)
    return values


def node_has_positive_price(node) -> bool:
    if node_is_hidden(node) or node_in_cart_context(node):
        return False
    text = compact_text(node, 500)
    if not text:
        text = str(getattr(node, "get", lambda *_: "")("content", ""))
    return bool(positive_currency_values(text))


def node_in_auxiliary_buy_context(node, max_levels: int = 8) -> bool:
    """True for buttons in headers, footers and recommendation carousels."""
    cur = node
    for _ in range(max_levels + 1):
        if cur is None:
            break
        name = str(getattr(cur, "name", "") or "").lower()
        if name in {"header", "footer", "nav"}:
            return True
        if AUXILIARY_BUY_CONTEXT_RE.search(attrs_text(cur)):
            return True
        cur = getattr(cur, "parent", None)
    return False


def page_has_strong_buy_signal(soup: BeautifulSoup) -> bool:
    """
    0-priser fra JSON-LD/meta accepteres kun når siden faktisk ligner en
    købbar vare. Det fjerner blogindlæg med falsk Product/meta-price.
    """
    if has_buy_action(soup, reject_auxiliary=True):
        return True

    # Schema availability + produkt-formular er også et stærkt commerce-signal.
    htmlish = str(soup)[:1_500_000]
    if re.search(r"schema\.org/(?:InStock|PreOrder|LimitedAvailability)", htmlish, re.I):
        if soup.find("form", attrs={"class": re.compile(r"cart|product|purchase", re.I)}):
            return True
    return False


def obvious_non_product_page(soup: BeautifulSoup, url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    if NON_PRODUCT_PATH_RE.search(path):
        return True

    # Hvis siden eksplicit erklærer sig som Article/BlogPosting og ikke har
    # købsknap, skal Product/meta-støj ikke kunne gøre den til et fund.
    og_type = soup.find("meta", attrs={"property": "og:type"})
    if og_type and str(og_type.get("content", "")).lower() == "article":
        return not has_buy_action(soup)

    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}, limit=20):
        raw = tag.string or tag.get_text("", strip=True)
        if raw and re.search(r'"@type"\s*:\s*"(?:Article|BlogPosting|NewsArticle)"', raw, re.I):
            if not has_buy_action(soup):
                return True
    return False


def product_schema_matches_page(product: dict, page_url: str) -> bool:
    """Hvis schema har url/@id, skal den pege på den aktuelle produktside."""
    candidates = []
    for key in ("url", "@id"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    if not candidates:
        return True  # kan stadig valideres af buy-signal nedenfor

    page = clean_url(page_url).rstrip("/")
    for value in candidates:
        candidate = clean_url(urllib.parse.urljoin(page_url, value)).rstrip("/")
        if candidate == page:
            return True
    return False


def visible_positive_product_price(soup: BeautifulSoup, page_name: str = "") -> bool:
    """
    Find en synlig positiv pris, som sandsynligvis tilhører hovedproduktet.
    Bruges til at afvise skjult/meta price=0 når siden synligt koster > 0.
    """
    page_name_norm = re.sub(r"\s+", " ", page_name or "").strip().lower()
    for node in price_node_candidates(soup):
        if node_is_hidden(node) or node_in_cart_context(node):
            continue
        raw = compact_text(node, 500) or str(node.get("content", ""))
        if not positive_currency_values(raw):
            continue

        container = nearest_product_container(node)
        ctext = compact_text(container, 1200).lower()
        cattrs = attrs_text(container)

        # Hovedprodukt: H1 i samme container, navn i samme container eller
        # typiske detail-product attributter.
        has_h1 = bool(getattr(container, "find", lambda *a, **k: None)("h1"))
        name_match = bool(page_name_norm and page_name_norm[:80] in ctext)
        primary_attrs = bool(
            re.search(
                r"product[-_\s]?(?:summary|detail|info|main|single)|"
                r"entry[-_\s]?summary|product_page|product-page",
                cattrs,
                re.I,
            )
        )
        if has_h1 or name_match or primary_attrs:
            return True

    return False


def offer_is_expired(offer: dict) -> bool:
    """Best-effort filter for stale structured offers."""
    for key in ("priceValidUntil", "validThrough", "endDate"):
        raw = offer.get(key)
        if not raw:
            continue
        value = str(raw).strip()
        try:
            # Handles YYYY-MM-DD and most ISO timestamps.
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            dt = datetime.fromisoformat(value)
            if dt.date() < date.today():
                return True
        except ValueError:
            try:
                if date.fromisoformat(value[:10]) < date.today():
                    return True
            except ValueError:
                pass
    return False


def offer_is_unavailable(offer: dict) -> bool:
    unavailable = {"outofstock", "discontinued", "soldout"}
    raw = str(offer.get("availability", "") or "").strip().lower().rstrip("/")
    token = re.split(r"[/#]", raw)[-1]
    return token in unavailable


def has_buy_action(soup: BeautifulSoup, *, reject_auxiliary: bool = False) -> bool:
    action_re = re.compile(
        r"(læg\s+i\s+kurv|laeg\s+i\s+kurv|tilføj\s+til\s+kurv|tilfoej\s+til\s+kurv|"
        r"køb\s+nu|koeb\s+nu|add\s+to\s+(?:cart|bag|basket)|buy\s+now|bestil)",
        re.I,
    )
    for node in soup.find_all(["button", "a", "input"], limit=250):
        if node_is_hidden(node):
            continue
        if reject_auxiliary and node_in_auxiliary_buy_context(node):
            continue
        txt = compact_text(node, 180)
        if node.name == "input":
            txt += " " + str(node.get("value", ""))
        txt += " " + attrs_text(node)
        if action_re.search(txt):
            return True
    return False


def page_is_product_detail(soup: BeautifulSoup, url: str, products: list[dict]) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    searchish = re.search(r"/(search|soeg|sog|søg)(?:/|$)|[?&](q|query|search)=", url, re.I)
    if searchish or CATEGORY_PATH_RE.search(path) or obvious_non_product_page(soup, url):
        return False

    og_type = soup.find("meta", attrs={"property": "og:type"})
    if og_type and "product" in str(og_type.get("content", "")).lower():
        return True

    # Product-schema alene er ikke nok: kategori/blog-sider kan indeholde det.
    if products and PRODUCT_URL_HINT_RE.search(path) and page_has_strong_buy_signal(soup):
        return True

    item_product = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)})
    if item_product and PRODUCT_URL_HINT_RE.search(path) and page_has_strong_buy_signal(soup):
        return True

    return bool(
        soup.find("h1")
        and page_has_strong_buy_signal(soup)
        and PRODUCT_URL_HINT_RE.search(path)
    )

def price_node_candidates(soup: BeautifulSoup):
    """Yield DOM nodes that look like actual, visible product price fields."""
    seen = set()

    # Explicit semantic nodes, men IKKE skjulte/meta/cart-priser.
    for node in soup.select('[itemprop="price"], [data-price], [data-product-price]'):
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node_is_hidden(node) or node_in_cart_context(node):
            continue
        yield node

    # Common class/id/data-* price markers.
    for node in soup.find_all(True):
        if id(node) in seen:
            continue
        blob = attrs_text(node)
        if not blob or not PRICE_ATTR_RE.search(blob):
            continue
        if BAD_PRICE_ATTR_RE.search(blob):
            continue
        if node_is_hidden(node) or node_in_cart_context(node):
            continue

        txt = compact_text(node, 1000)
        if not txt or len(txt) > 500:
            continue

        seen.add(id(node))
        yield node

def node_context(node, max_levels: int = 3) -> str:
    parts = [compact_text(node, 450), attrs_text(node)]
    parent = getattr(node, "parent", None)
    levels = 0
    while parent is not None and levels < max_levels:
        ptxt = compact_text(parent, 700)
        # Avoid pulling the entire page into the context.
        if ptxt and len(ptxt) <= 700:
            parts.append(ptxt)
            parts.append(attrs_text(parent))
        parent = getattr(parent, "parent", None)
        levels += 1
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:1800]


def nearest_product_container(node):
    """Find a small product card/detail wrapper around a price node."""
    cur = node
    for _ in range(6):
        cur = getattr(cur, "parent", None)
        if cur is None:
            break
        blob = attrs_text(cur)
        tag = getattr(cur, "name", "")
        txt = compact_text(cur, 1200)
        if len(txt) > 1200:
            continue
        if (
            tag in {"article", "li"}
            or re.search(r"product|item|card|tile|result", blob, re.I)
        ):
            return cur
    return node.parent if getattr(node, "parent", None) else node


def product_info_from_container(container, page_url: str, fallback_name: str) -> tuple[str, str]:
    name = ""
    for selector in ["[itemprop='name']", "h1", "h2", "h3", ".product-name", ".product-title"]:
        try:
            n = container.select_one(selector)
        except Exception:
            n = None
        if n:
            candidate = compact_text(n, 300)
            if candidate:
                name = candidate
                break
    if not name:
        name = fallback_name

    result_url = page_url
    try:
        links = container.find_all("a", href=True, limit=15)
    except Exception:
        links = []
    for a in links:
        raw_href = a.get("href", "")
        href = clean_url(urllib.parse.urljoin(page_url, raw_href))
        if href and (PRODUCT_URL_HINT_RE.search(href) or compact_text(a, 300) == name):
            result_url = href
            break
    return name[:300], clean_url(result_url)


def detect_findings(domain: str, url: str, html: str, source: str) -> list[Finding]:
    domain = clean_domain(domain)
    url = clean_url(url)
    soup = BeautifulSoup(html, "html.parser")
    findings: list[Finding] = []
    products = jsonld_products(soup)
    detail_page = page_is_product_detail(soup, url, products)

    page_name = product_name_from_page(soup, products[0] if products else None)
    title = soup.title.string if soup.title and soup.title.string else ""
    page_context = re.sub(r"\s+", " ", f"{page_name} {title}").strip()
    non_product_page = obvious_non_product_page(soup, url)
    buy_signal = page_has_strong_buy_signal(soup)
    conditional_product_offer = has_conditional_free_product_offer(
        primary_product_text(soup)
    )

    # 1) Schema.org Product/Offer price=0.
    # Kræv reel detail-/købskontekst; schema på forsider/kategorier/blogs tæller ikke.
    if (
        detail_page
        and buy_signal
        and not non_product_page
        and not conditional_product_offer
        and not page_has_placeholder_zero(soup)
    ):
        for product in products:
            name = product_name_from_page(soup, product)
            context = " ".join(
                str(product.get(k, ""))
                for k in ("name", "description", "category", "sku")
            )
            full_context = f"{name} {context}"
            if (
                is_excluded(full_context)
                or is_false_free_name(name)
                or not product_schema_matches_page(product, url)
            ):
                continue

            prices = offer_prices(product)
            # Hvis samme Product/Offer-data både siger 0 og en positiv pris,
            # er 0 typisk placeholder/low-level metadata.
            if any(p is not None and p > 0 for p, _raw, _offer in prices):
                continue

            # Hvis hovedproduktet synligt har en positiv pris, tro ikke på skjult 0.
            if visible_positive_product_price(soup, name or page_name):
                continue

            for price, raw, offer in prices:
                if (
                    offer_is_expired(offer)
                    or offer_is_unavailable(offer)
                    or offer_is_unavailable(product)
                ):
                    continue
                if price is not None and abs(price) < 1e-12:
                    findings.append(
                        Finding(
                            domain=domain,
                            url=url,
                            product_name=name,
                            match_type="structured_price_zero",
                            price=raw,
                            matched_text=f"Schema.org Product/Offer price={raw}"[:500],
                            source=source,
                        )
                    )
                    break

    # 2) Meta/itemprop price=0.
    # Meta er skjult af natur, så kræv detail + købssignal + ingen synlig positiv pris.
    if (
        detail_page
        and buy_signal
        and not non_product_page
        and not conditional_product_offer
        and not is_excluded(page_context)
        and not is_false_free_name(page_name)
        and not visible_positive_product_price(soup, page_name)
        and not page_has_placeholder_zero(soup)
    ):
        meta_price_selectors = [
            ('meta[itemprop="price"]', "content"),
            ('meta[property="product:price:amount"]', "content"),
            ('meta[property="og:price:amount"]', "content"),
        ]
        for selector, attr in meta_price_selectors:
            for node in soup.select(selector):
                raw = str(node.get(attr, "")).strip()
                price = normalize_price(raw)
                if price is not None and abs(price) < 1e-12:
                    findings.append(
                        Finding(
                            domain=domain,
                            url=url,
                            product_name=page_name,
                            match_type="meta_price_zero",
                            price=raw,
                            matched_text=f"{selector}={raw}"[:500],
                            source=source,
                        )
                    )

    # 3) Synlige produktpriser. Oversigtskort bruges kun til at opdage links;
    # selve fundet skal bekræftes på en købbar produktdetaljeside.
    visible_nodes = (
        price_node_candidates(soup)
        if detail_page and buy_signal and not non_product_page and not conditional_product_offer
        else ()
    )
    for node in visible_nodes:
        if node_is_hidden(node) or node_in_cart_context(node):
            continue

        raw_text = compact_text(node, 300)
        if not raw_text:
            raw_text = str(node.get("content", "")).strip()

        context = node_context(node)
        if is_excluded(context):
            continue

        zero_match = ZERO_PRICE_RE.search(raw_text)
        free_price = bool(FREE_PRICE_WORD_RE.fullmatch(raw_text.strip()))
        if not zero_match and not free_price:
            continue

        # V5 strict: variant/option/"Fra 0"-priser er ikke hovedproduktpriser.
        if zero_match and is_option_or_configurator_zero(node, raw_text, context):
            continue

        container = nearest_product_container(node)
        container_text = compact_text(container, 1200)
        if (
            node_in_cart_context(container)
            or is_excluded(container_text)
            or is_false_free_name(container_text)
            or (
                zero_match
                and is_option_or_configurator_zero(
                    node,
                    raw_text,
                    container_text + " " + attrs_text(container),
                )
            )
        ):
            continue

        product_name, finding_url = product_info_from_container(container, url, page_name)
        if is_false_free_name(product_name):
            continue

        # På oversigter/søgesider skal prisfeltet være inde i et ægte produktkort.
        if not detail_page:
            container_blob = attrs_text(container)
            has_productish_wrapper = bool(
                getattr(container, "name", "") in {"article", "li"}
                or re.search(r"product|item|card|tile|result", container_blob, re.I)
            )
            has_link = bool(getattr(container, "find", lambda *a, **k: None)("a", href=True))
            if not (has_productish_wrapper and has_link):
                continue

        # Hvis samme produktcontainer har en synlig positiv pris, er 0 typisk
        # kurv/tilvalg/placeholder og skal ikke rapporteres.
        contradictory_positive = False
        try:
            for other in container.find_all(True, limit=250):
                if other is node or node_is_hidden(other) or node_in_cart_context(other):
                    continue
                other_blob = attrs_text(other)
                if not (
                    PRICE_ATTR_RE.search(other_blob)
                    or other.get("itemprop") == "price"
                    or other.get("data-price") is not None
                ):
                    continue
                other_text = compact_text(other, 400)
                if positive_currency_values(other_text):
                    contradictory_positive = True
                    break
        except Exception:
            pass
        if contradictory_positive:
            continue

        value = zero_match.group(0) if zero_match else raw_text.strip()
        findings.append(
            Finding(
                domain=domain,
                url=finding_url,
                product_name=product_name,
                match_type="visible_product_price_zero" if zero_match else "visible_product_price_free",
                price=value,
                matched_text=context[:500],
                source=source,
            )
        )

    # 4) "Gratis" i H1: kun rigtig produktside med købssignal.
    if detail_page and buy_signal and not non_product_page and not conditional_product_offer:
        heading = soup.find("h1")
        heading_text = compact_text(heading, 400) if heading else page_name
        heading_context = f"{heading_text} {page_context}"
        if (
            FREE_WORD_RE.search(heading_text)
            and not is_excluded(heading_context)
            and not is_false_free_name(heading_context)
            and not NON_PRODUCT_RE.search(urllib.parse.urlparse(url).path)
        ):
            findings.append(
                Finding(
                    domain=domain,
                    url=url,
                    product_name=page_name,
                    match_type="free_product_heading_with_buy_action",
                    price="gratis",
                    matched_text=heading_text[:500],
                    source=source,
                )
            )

    # Deduplicér og foretræk stærkere signaler.
    priority = {
        "structured_price_zero": 4,
        "meta_price_zero": 3,
        "visible_product_price_zero": 2,
        "visible_product_price_free": 2,
        "free_product_heading_with_buy_action": 1,
    }
    unique: dict[tuple[str, str], Finding] = {}
    for f in findings:
        key = (f.url, f.product_name)
        old = unique.get(key)
        if old is None or priority.get(f.match_type, 0) > priority.get(old.match_type, 0):
            unique[key] = f
    return list(unique.values())

def discover_internal_links(html: str, current_url: str, domain: str, limit: int = 100) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    scored = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = clean_url(urllib.parse.urljoin(current_url, a["href"]))
        if not href or href in seen or not same_site(href, domain):
            continue
        seen.add(href)
        text = a.get_text(" ", strip=True)
        score = 0
        if re.search(r"\b(gratis|free|0\s*kr)\b", href + " " + text, re.I):
            score += 100
        if PRODUCT_URL_HINT_RE.search(href):
            score += 20
        if re.search(r"(tilbud|sale|outlet|kampagne|deals)", href + " " + text, re.I):
            score += 10
        if score:
            scored.append((score, href))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [u for _s, u in scored[:limit]]


def build_candidate_urls(
    base: str,
    domain: str,
    sitemap_urls: list[str],
    homepage_links: list[str],
    max_candidates: int,
    use_search_pages: bool,
) -> list[tuple[str, str]]:
    scored: list[tuple[int, str, str]] = []
    seen = set()

    def add(url: str, source: str, score: int):
        url = clean_url(url)
        if not url or url in seen or not same_site(url, domain):
            return
        seen.add(url)
        scored.append((score, url, source))

    # Søgesider prioriteres højt, fordi de kan pege direkte på gratis varer.
    if use_search_pages:
        for p in SEARCH_PATHS:
            add(urllib.parse.urljoin(base + "/", p.lstrip("/")), "site_search", 95)

    for u in homepage_links:
        score = 80 if re.search(r"(gratis|free|0-?kr)", u, re.I) else 60
        add(u, "homepage_link", score)

    for u in sitemap_urls:
        if re.search(r"(gratis|free|0-?kr)", u, re.I):
            score = 90
        elif PRODUCT_URL_HINT_RE.search(u):
            score = 50
        else:
            score = 10
        add(u, "sitemap", score)

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(url, source) for _score, url, source in scored[:max_candidates]]


def scan_site(
    domain: str,
    timeout: int,
    delay: float,
    max_pages_per_site: int,
    max_sitemap_urls: int,
    use_search_pages: bool,
) -> tuple[list[Finding], SiteStatus]:
    domain = clean_domain(domain)
    if not domain or "." not in domain:
        return [], SiteStatus(domain, "invalid_domain", 0, 0, "Ugyldigt domæne")

    base, base_status = canonical_base(domain, timeout)
    if not base:
        if base_status.startswith("external_redirect:"):
            redirect_url = base_status.split(":", 1)[1]
            return [], SiteStatus(domain, "external_redirect", 0, 0, redirect_url)
        notes = {
            "blocked": "Forsiden afviste crawleren (HTTP 401/403)",
            "rate_limited": "Forsiden svarede HTTP 429",
            "homepage_not_found": "Forsiden svarede HTTP 404",
            "unreachable": "Kunne ikke åbne HTTP/HTTPS",
        }
        return [], SiteStatus(domain, base_status, 0, 0, notes.get(base_status, base_status))

    final_domain = clean_domain(urllib.parse.urlparse(base).hostname or domain) or domain
    rp, robot_sitemaps = load_robots(base, final_domain, timeout)

    homepage = clean_url(urllib.parse.urljoin(base + "/", "/"))
    if not allowed(rp, homepage):
        return [], SiteStatus(domain, "robots_disallowed", 0, 0, "Forsiden er blokeret af robots.txt")

    findings: list[Finding] = []
    pages_checked = 0
    homepage_links: list[str] = []

    r = fetch(homepage, timeout, rp, delay, final_domain)
    if r and "text/html" in r.headers.get("Content-Type", "").lower():
        html = r.text[:MAX_HTML_BYTES]
        pages_checked += 1
        findings.extend(detect_findings(domain, r.url, html, "homepage"))
        homepage_links = discover_internal_links(html, r.url, final_domain)

    sitemap_urls = get_sitemap_urls(
        base=base,
        domain=final_domain,
        rp=rp,
        discovered_sitemaps=robot_sitemaps,
        timeout=timeout,
        delay=delay,
        max_sitemap_urls=max_sitemap_urls,
    )

    max_attempts = max(
        max_pages_per_site,
        max_pages_per_site * MAX_CANDIDATE_ATTEMPT_FACTOR,
    )
    candidates = build_candidate_urls(
        base=base,
        domain=final_domain,
        sitemap_urls=sitemap_urls,
        homepage_links=homepage_links,
        max_candidates=max(0, max_attempts),
        use_search_pages=use_search_pages,
    )

    visited = {homepage}
    attempts = 0
    search_result_pages = 0
    search_result_budget = max(2, max_pages_per_site // 3)
    for url, source in candidates:
        if pages_checked >= max_pages_per_site or attempts >= max_attempts:
            break
        if url in visited or not allowed(rp, url):
            continue
        visited.add(url)
        attempts += 1
        rr = fetch(url, timeout, rp, delay, final_domain)
        if not rr:
            continue
        ctype = rr.headers.get("Content-Type", "").lower()
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            continue
        html = rr.text[:MAX_HTML_BYTES]
        pages_checked += 1
        findings.extend(detect_findings(domain, rr.url, html, source))

        # Hvis en søgeside viser links med "gratis" / produktmønstre,
        # skan også de bedste af dem, hvis der er plads.
        if (
            source == "site_search"
            and pages_checked < max_pages_per_site
            and search_result_pages < search_result_budget
        ):
            for link in discover_internal_links(html, rr.url, final_domain, limit=12):
                if (
                    pages_checked >= max_pages_per_site
                    or attempts >= max_attempts
                    or search_result_pages >= search_result_budget
                ):
                    break
                if link in visited or not allowed(rp, link):
                    continue
                visited.add(link)
                attempts += 1
                rrr = fetch(link, timeout, rp, delay, final_domain)
                if not rrr:
                    continue
                ctype2 = rrr.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype2 and "application/xhtml" not in ctype2:
                    continue
                html2 = rrr.text[:MAX_HTML_BYTES]
                pages_checked += 1
                search_result_pages += 1
                findings.extend(detect_findings(domain, rrr.url, html2, "site_search_result"))

    # Dedup på domæne/url/type/navn/pris
    unique = {}
    for f in findings:
        key = (f.domain, f.url, f.match_type, f.product_name, f.price)
        unique[key] = f
    findings = list(unique.values())

    status_name = "ok" if pages_checked else "no_html_pages"
    status = SiteStatus(
        domain=domain,
        status=status_name,
        pages_checked=pages_checked,
        findings=len(findings),
        note=(
            f"base={base}; sitemap_urls={len(sitemap_urls)}; attempts={attempts}"
            if pages_checked
            else f"Ingen HTML-sider kunne hentes; base={base}; sitemap_urls={len(sitemap_urls)}; attempts={attempts}"
        ),
    )
    return findings, status


def detect_domain_column(fieldnames: list[str]) -> str:
    preferred = ["web_domain", "domain", "website", "webshop", "url", "site"]
    lower_map = {f.lower(): f for f in fieldnames}
    for name in preferred:
        if name in lower_map:
            return lower_map[name]
    raise ValueError(
        "Kunne ikke finde domænekolonne. Forventede fx web_domain, domain, website eller url."
    )


def load_domains(input_csv: Path, limit: Optional[int]) -> list[str]:
    """
    Læs bl.a. e-mærkets CSV:
        web_domain;url;kategorier;source_url

    Foretrækker web_domain. Hvis den mangler/er ugyldig, prøves url-kolonnen.
    Semikolon er CSV-delimiter og bliver aldrig en del af domænet.
    """
    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            # e-mærkets crawler skriver semikolon-separeret CSV.
            class SemiDialect(csv.excel):
                delimiter = ";"
            dialect = SemiDialect

        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CSV-filen har ingen header.")

        col = detect_domain_column(reader.fieldnames)
        lower_map = {name.lower(): name for name in reader.fieldnames}
        url_col = lower_map.get("url")

        domains: list[str] = []
        seen: set[str] = set()

        for row in reader:
            candidates = [row.get(col, "")]
            if url_col and url_col != col:
                candidates.append(row.get(url_col, ""))

            d = ""
            for candidate in candidates:
                d = clean_domain(candidate)
                if d:
                    break

            if not d or d in seen:
                continue

            seen.add(d)
            domains.append(d)

            # Limit gælder kun BRUGBARE, unikke domæner.
            if limit and len(domains) >= limit:
                break

        return domains

def load_completed(status_file: Path) -> set[str]:
    if not status_file.exists():
        return set()
    completed = set()
    try:
        with status_file.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                d = clean_domain(row.get("domain", ""))
                status = str(row.get("status", "") or "").strip().lower()
                if d and status in RESUME_COMPLETED_STATUSES:
                    completed.add(d)
    except Exception:
        pass
    return completed


def finding_key(value) -> tuple[str, str, str, str, str]:
    if isinstance(value, Finding):
        return (
            value.domain,
            value.url,
            value.product_name,
            value.match_type,
            value.price,
        )
    return tuple(
        str(value.get(name, "") or "")
        for name in ("domain", "url", "product_name", "match_type", "price")
    )


def load_existing_finding_keys(output_file: Path) -> set[tuple[str, str, str, str, str]]:
    if not output_file.exists():
        return set()
    keys = set()
    try:
        with output_file.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                keys.add(finding_key(row))
    except Exception:
        pass
    return keys


def ensure_writer(path: Path, fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    f = path.open("a", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
    if not exists:
        writer.writeheader()
        f.flush()
    return f, writer


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find 0 kr./gratis produkter på webshops fra en CSV."
    )
    ap.add_argument("input_csv", type=Path, help="Input CSV. e-mærket/Indexo: kolonnen web_domain bruges automatisk; url bruges som fallback.")
    ap.add_argument("--output", type=Path, default=Path("gratis_produkter.csv"))
    ap.add_argument("--status", type=Path, default=Path("scannede_webshops.csv"))
    ap.add_argument("--workers", type=int, default=6, help="Webshops scannet parallelt. Standard: 6")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--delay", type=float, default=0.35, help="Minimum pause mellem requests til samme site.")
    ap.add_argument("--max-pages-per-site", type=int, default=25)
    ap.add_argument("--max-sitemap-urls", type=int, default=4000)
    ap.add_argument("--limit-sites", type=int, default=None, help="Test kun de første N webshops.")
    ap.add_argument(
        "--max-runtime-minutes",
        type=float,
        default=None,
        help="Stop med at starte nye webshops efter N minutter; aktive webshops afsluttes.",
    )
    ap.add_argument("--resume", action="store_true", help="Spring domæner over der allerede står i statusfilen.")
    ap.add_argument(
        "--no-search-pages",
        action="store_true",
        help="Prøv ikke almindelige interne søge-URL'er som /search?q=gratis.",
    )
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers skal være mindst 1")
    if args.timeout < 1:
        ap.error("--timeout skal være mindst 1 sekund")
    if args.delay < 0:
        ap.error("--delay må ikke være negativ")
    if args.max_pages_per_site < 1:
        ap.error("--max-pages-per-site skal være mindst 1")
    if args.max_sitemap_urls < 1:
        ap.error("--max-sitemap-urls skal være mindst 1")
    if args.limit_sites is not None and args.limit_sites < 1:
        ap.error("--limit-sites skal være mindst 1")
    if args.max_runtime_minutes is not None and args.max_runtime_minutes <= 0:
        ap.error("--max-runtime-minutes skal være større end 0")

    if not args.input_csv.exists():
        print(f"Inputfil findes ikke: {args.input_csv}", file=sys.stderr)
        return 2

    domains = load_domains(args.input_csv, args.limit_sites)
    if args.resume:
        completed = load_completed(args.status)
        domains = [d for d in domains if d not in completed]
    else:
        completed = set()

    if not domains:
        print("Ingen nye domæner at scanne.")
        return 0

    finding_fields = [f.name for f in Finding.__dataclass_fields__.values()]
    status_fields = [f.name for f in SiteStatus.__dataclass_fields__.values()]
    existing_finding_keys = load_existing_finding_keys(args.output)
    fout, finding_writer = ensure_writer(args.output, finding_fields)
    fstatus, status_writer = ensure_writer(args.status, status_fields)

    total = len(domains)
    print(f"Scanner {total} webshops...")
    print(f"Fund -> {args.output}")
    print(f"Status -> {args.status}")

    done = 0
    total_findings = 0
    deadline = (
        time.monotonic() + args.max_runtime_minutes * 60
        if args.max_runtime_minutes is not None
        else None
    )
    runtime_reached = False
    submitted = 0

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            domain_iter = iter(domains)
            future_map = {}

            def submit_next() -> bool:
                nonlocal submitted
                try:
                    domain_to_scan = next(domain_iter)
                except StopIteration:
                    return False
                future = executor.submit(
                    scan_site,
                    domain_to_scan,
                    args.timeout,
                    args.delay,
                    args.max_pages_per_site,
                    args.max_sitemap_urls,
                    not args.no_search_pages,
                )
                future_map[future] = domain_to_scan
                submitted += 1
                return True

            for _ in range(min(args.workers, total)):
                submit_next()

            while future_map:
                if deadline is not None and time.monotonic() >= deadline:
                    runtime_reached = True

                timeout_for_wait = None
                if deadline is not None and not runtime_reached:
                    timeout_for_wait = max(0.0, deadline - time.monotonic())

                completed_futures, _pending = wait(
                    future_map,
                    timeout=timeout_for_wait,
                    return_when=FIRST_COMPLETED,
                )
                if not completed_futures:
                    runtime_reached = True
                    continue

                for future in completed_futures:
                    d = future_map.pop(future)
                    try:
                        findings, status = future.result()
                    except Exception as exc:
                        findings = []
                        status = SiteStatus(
                            d,
                            "error",
                            0,
                            0,
                            f"{type(exc).__name__}: {exc}"[:500],
                        )

                    new_findings = 0
                    for finding in findings:
                        key = finding_key(finding)
                        if key in existing_finding_keys:
                            continue
                        finding_writer.writerow(asdict(finding))
                        existing_finding_keys.add(key)
                        new_findings += 1
                    fout.flush()

                    status_writer.writerow(asdict(status))
                    fstatus.flush()

                    done += 1
                    total_findings += new_findings
                    print(
                        f"[{done}/{total}] {status.domain}: "
                        f"{status.status}, {status.pages_checked} sider, "
                        f"{new_findings} nye fund | total={total_findings}",
                        flush=True,
                    )

                    if deadline is not None and time.monotonic() >= deadline:
                        runtime_reached = True
                    if not runtime_reached:
                        submit_next()
    except KeyboardInterrupt:
        print("\nAfbrudt. Kør igen med --resume for at fortsætte.", file=sys.stderr)
        return 130
    finally:
        fout.close()
        fstatus.close()

    remaining = total - submitted
    if runtime_reached and remaining:
        print(
            f"Tidsgrænsen er nået. {remaining} webshops venter til næste --resume-kørsel."
        )
    print(f"Færdig. Nye fund i denne kørsel: {total_findings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
