"""
psx_scraper.py — Async scrapers for the PSX Data Portal.

Sources (all public, all read-only):
- https://dps.psx.com.pk/                 Today's summary, indices
- https://dps.psx.com.pk/payouts          Upcoming dividends / book closures
- https://dps.psx.com.pk/announcements/companies   Corporate announcements
- https://dps.psx.com.pk/historical       Historical OHLC
- https://dps.psx.com.pk/company/{SYMBOL} Per-symbol page (quote + fundamentals)

PSX market data is licensed for personal/non-commercial use only.
For commercial redistribution, contact marketdatarequest@psx.com.pk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE = "https://dps.psx.com.pk"
TIMEOUT = 30.0


# ──────────────────────────── data classes ────────────────────────────

@dataclass
class Quote:
    symbol: str
    name: str
    last: float | None
    change: float | None
    change_pct: float | None
    volume: int | None
    high: float | None
    low: float | None
    open: float | None
    prev_close: float | None
    timestamp: str


@dataclass
class Dividend:
    symbol: str
    company: str
    bc_from: str          # book closure start
    bc_to: str            # book closure end
    agm_date: str
    agm_time: str
    type: str             # CASH DIVIDEND, BONUS, RIGHT SHARES, etc.
    payout: str           # raw, e.g. "150%" or "Rs 5/share"


@dataclass
class Announcement:
    date: str
    symbol: str
    title: str
    pdf_url: str | None


@dataclass
class IndexValue:
    name: str
    value: float
    change: float | None
    change_pct: float | None


# ──────────────────────────── http helper ────────────────────────────

async def _get(client: httpx.AsyncClient, path: str) -> str:
    r = await client.get(
        f"{BASE}{path}" if path.startswith("/") else path,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.text


async def _post(client: httpx.AsyncClient, path: str, data: dict) -> str:
    r = await client.post(
        f"{BASE}{path}" if path.startswith("/") else path,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.text


def _parse_float(s: str) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[,%\s]", "", s).replace("(", "-").replace(")", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(s: str) -> int | None:
    f = _parse_float(s)
    return int(f) if f is not None else None


# ──────────────────────────── quotes ────────────────────────────

async def fetch_quote(symbol: str) -> Quote | None:
    """Fetch the current quote for a single symbol."""
    symbol = symbol.upper().strip()
    async with httpx.AsyncClient() as client:
        try:
            html = await _get(client, f"/company/{symbol}")
        except httpx.HTTPError:
            return None

        soup = BeautifulSoup(html, "html.parser")

        name_el = soup.find("div", class_="quote__name")
        if name_el:
            for child in name_el.find_all("div"):
                child.decompose()
            name = name_el.get_text(strip=True)
        else:
            og = soup.find("meta", property="og:title")
            name = og["content"].split(" - ")[0].replace(symbol, "").strip(" -") if og else symbol

        close_el = soup.find("div", class_="quote__close")
        last = _parse_float(close_el.get_text(strip=True).replace("Rs.", "")) if close_el else None

        change_el = soup.find("div", class_="quote__change")
        change_text = change_el.get_text(" ", strip=True) if change_el else ""
        change_match = re.search(r"(-?[\d,.]+)\s+\((-?[\d,.]+)%\)", change_text)
        change = _parse_float(change_match.group(1)) if change_match else None
        change_pct = _parse_float(change_match.group(2)) if change_match else None

        def stat(label: str) -> str:
            el = soup.find("div", class_="stats_label", string=re.compile(f"^{label}$", re.I))
            if not el:
                return ""
            val = el.find_next_sibling("div", class_="stats_value")
            return val.get_text(strip=True) if val else ""

        volume = _parse_int(stat("Volume"))
        high = _parse_float(stat("High"))
        low = _parse_float(stat("Low"))
        open_ = _parse_float(stat("Open"))
        prev_close = _parse_float(stat("LDCP")) or _parse_float(stat("Previous Close"))

        return Quote(
            symbol=symbol,
            name=name,
            last=last,
            change=change,
            change_pct=change_pct,
            volume=volume,
            high=high,
            low=low,
            open=open_,
            prev_close=prev_close,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )


# ──────────────────────────── payouts / dividends ────────────────────────────

async def fetch_payouts() -> list[Dividend]:
    """Scrape the upcoming payouts table via POST (PSX SPA endpoint)."""
    page_size = 100
    out: list[Dividend] = []

    async with httpx.AsyncClient() as client:
        offset = 0
        while True:
            html = await _post(client, "/payouts", {"count": page_size, "offset": offset})
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            if not table:
                break

            rows_found = 0
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if not cells:
                    continue
                rows_found += 1
                symbol = cells[0].get_text(strip=True)
                company = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                payout_raw = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                bc_raw = cells[5].get_text(strip=True) if len(cells) > 5 else ""

                bc_from, bc_to = "", ""
                if "-" in bc_raw:
                    parts = [p.strip() for p in bc_raw.split("-", 1)]
                    bc_from = parts[0]
                    bc_to = parts[1] if len(parts) > 1 else ""

                payout_type = ""
                if "(D)" in payout_raw:
                    payout_type = "CASH DIVIDEND"
                elif "(R)" in payout_raw:
                    payout_type = "RIGHT SHARES"
                elif "(B)" in payout_raw:
                    payout_type = "BONUS SHARES"

                out.append(
                    Dividend(
                        symbol=symbol,
                        company=company,
                        bc_from=bc_from,
                        bc_to=bc_to,
                        agm_date="",
                        agm_time="",
                        type=payout_type,
                        payout=payout_raw,
                    )
                )

            if rows_found < page_size:
                break
            offset += page_size

    return [d for d in out if d.symbol]


async def fetch_dividend_history(symbol: str, years: int = 5) -> list[Dividend]:
    """
    Fetch historical dividends for a symbol via POST to /company/payouts.
    """
    symbol = symbol.upper().strip()
    cutoff = datetime.now() - timedelta(days=365 * years)
    out: list[Dividend] = []

    async with httpx.AsyncClient() as client:
        try:
            html = await _post(client, "/company/payouts", {"symbol": symbol})
        except httpx.HTTPError:
            return []

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 3:
            continue
        date_str = cells[0]
        year_match = re.search(r"\d{4}", date_str)
        if year_match and int(year_match.group()) < cutoff.year:
            continue

        payout_raw = cells[2] if len(cells) > 2 else ""
        bc_raw = cells[3] if len(cells) > 3 else ""

        bc_from, bc_to = "", ""
        if "-" in bc_raw:
            parts = [p.strip() for p in bc_raw.split("-", 1)]
            bc_from = parts[0]
            bc_to = parts[1] if len(parts) > 1 else ""

        payout_type = ""
        if "(D)" in payout_raw:
            payout_type = "CASH DIVIDEND"
        elif "(R)" in payout_raw:
            payout_type = "RIGHT SHARES"
        elif "(B)" in payout_raw:
            payout_type = "BONUS SHARES"

        out.append(
            Dividend(
                symbol=symbol,
                company="",
                bc_from=bc_from,
                bc_to=bc_to,
                agm_date="",
                agm_time="",
                type=payout_type,
                payout=payout_raw,
            )
        )
    return out


# ──────────────────────────── announcements ────────────────────────────

async def fetch_announcements(symbol: str | None = None, limit: int = 20) -> list[Announcement]:
    """Fetch recent company announcements via POST."""
    limit = max(1, min(limit, 100))
    out: list[Announcement] = []

    async with httpx.AsyncClient() as client:
        data = {
            "type": "C",
            "symbol": symbol.upper() if symbol else "",
            "query": "",
            "count": limit,
            "offset": 0,
            "date_from": "",
            "date_to": "",
            "page": "annc",
        }
        try:
            html = await _post(client, "/announcements", data)
        except httpx.HTTPError:
            return []

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        date_str = cells[0].get_text(strip=True)
        time_str = cells[1].get_text(strip=True)
        sym = cells[2].get_text(strip=True).upper()
        title = cells[4].get_text(strip=True)
        link_el = tr.find("a", href=re.compile(r"\.(pdf|gif)$", re.I))
        pdf = link_el["href"] if link_el else None
        if pdf and pdf.startswith("/"):
            pdf = BASE + pdf

        out.append(Announcement(
            date=f"{date_str} {time_str}",
            symbol=sym,
            title=title,
            pdf_url=pdf,
        ))

    return out


# ──────────────────────────── indices ────────────────────────────

INDEX_NAMES = {
    "KSE100", "KSE30", "ALLSHR", "KMI30", "KMIALLSHR",
    "PSXDIV20", "BKTI", "OGTI", "MII30",
}


async def fetch_indices() -> list[IndexValue]:
    """Fetch current values for all major PSX indices."""
    async with httpx.AsyncClient() as client:
        html = await _get(client, "/")

    soup = BeautifulSoup(html, "html.parser")
    out: list[IndexValue] = []

    # The home page header strip displays each index in a small block.
    # Pattern: index name + value + change + (pct%).
    text = soup.get_text(" ", strip=True)
    for name in INDEX_NAMES:
        # Match: NAME 12,345.67 -89.01 (-0.72%)
        pattern = rf"\b{name}\b\s+([\d,\.]+)\s+(-?[\d,\.]+)\s+\(\s*(-?[\d\.]+)%\s*\)"
        m = re.search(pattern, text)
        if m:
            out.append(IndexValue(
                name=name,
                value=_parse_float(m.group(1)) or 0,
                change=_parse_float(m.group(2)),
                change_pct=_parse_float(m.group(3)),
            ))
    return out


# ──────────────────────────── symbol search ────────────────────────────

_SYMBOL_CACHE: dict[str, str] = {}
_CACHE_AT: datetime | None = None


async def fetch_all_symbols() -> dict[str, str]:
    """Return {symbol: company_name} from the screener page. Cached for 24h."""
    global _SYMBOL_CACHE, _CACHE_AT
    if _CACHE_AT and (datetime.now() - _CACHE_AT) < timedelta(hours=24) and _SYMBOL_CACHE:
        return _SYMBOL_CACHE

    async with httpx.AsyncClient() as client:
        html = await _get(client, "/screener")

    soup = BeautifulSoup(html, "html.parser")
    mapping: dict[str, str] = {}
    for link in soup.find_all("a", class_="tbl__symbol", href=True):
        title = link.get("data-title", "")
        sym = link.get_text(strip=True)
        if sym:
            mapping[sym] = title or sym

    _SYMBOL_CACHE = mapping
    _CACHE_AT = datetime.now()
    return mapping


async def search_symbols(query: str, limit: int = 10) -> list[tuple[str, str]]:
    """Fuzzy match a query against symbols and company names."""
    q = query.upper().strip()
    if not q:
        return []
    all_syms = await fetch_all_symbols()
    matches: list[tuple[str, str, int]] = []
    for sym, name in all_syms.items():
        score = 0
        if sym == q:
            score = 100
        elif sym.startswith(q):
            score = 80
        elif q in sym:
            score = 60
        elif q in name.upper():
            score = 40
        if score > 0:
            matches.append((sym, name, score))
    matches.sort(key=lambda x: -x[2])
    return [(s, n) for s, n, _ in matches[:limit]]


# ──────────────────────────── market status ────────────────────────────

def market_status() -> dict:
    """Compute PSX market status. Trading hours: Mon–Fri 09:32–15:30 PKT."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Karachi"))
    weekday = now.weekday()  # 0=Mon .. 6=Sun
    is_weekday = weekday < 5

    open_t = now.replace(hour=9, minute=32, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if is_weekday and open_t <= now <= close_t:
        status = "OPEN"
    elif is_weekday and now < open_t:
        status = "PRE_OPEN"
    elif is_weekday and now > close_t:
        status = "CLOSED_TODAY"
    else:
        status = "WEEKEND"

    return {
        "status": status,
        "now_pkt": now.isoformat(),
        "next_open": _next_market_open(now).isoformat(),
        "trading_hours_pkt": "09:32 – 15:30 (Mon–Fri)",
    }


def _next_market_open(now) -> datetime:
    d = now.replace(hour=9, minute=32, second=0, microsecond=0)
    if now < d and now.weekday() < 5:
        return d
    # advance to next weekday
    while True:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            return d
