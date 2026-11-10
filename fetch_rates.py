#!/usr/bin/env python3
"""Fetch allow-listed public rate series into the static site. No API keys. Stdlib only.

Publishers, and only these:
  - Freddie Mac PMMS history CSV (30-year and 15-year fixed)
  - Federal Reserve H.15 Data Download Program CSV, series RIFSPFF_N.B
  - The latest FOMC statement (target range)
  - U.S. Treasury daily par yield curve CSV (10-year)

A missing observation is left missing. Nothing is interpolated, carried onto a
later date, or filled. If a publisher request fails, that series keeps its last
stored good value only when that value still has its real as-of date; otherwise
the series is unavailable. One failure does not drop the other series.
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "site" / "data"
RAW = ROOT / "raw"
UA = "rate-tracker/1.0 (+https://github.com/nardthefox/rate-tracker; public data; no API key)"
MAX_BYTES = 20_000_000

try:
    CHICAGO = ZoneInfo("America/Chicago")
    TZ_NAME = "America/Chicago"
except ZoneInfoNotFoundError:
    CHICAGO = timezone.utc
    TZ_NAME = "UTC"

DISCLAIMER = (
    "National averages for information only. Not a rate quote, offer, or commitment to lend. "
    "Your rate depends on credit, down payment, property, and lender. "
    "Todd Renard is a real estate agent, not a mortgage lender or loan originator."
)
BROKERAGE = "Harbor Realty & Management Group"

ENDPOINTS = {
    "pmms": {
        "url": "https://www.freddiemac.com/pmms/docs/PMMS_history.csv",
        "page": "https://www.freddiemac.com/pmms",
        "terms": "Use with attribution. Values are unaltered. No Freddie Mac logo or endorsement.",
    },
    "effr": {
        "url": (
            "https://www.federalreserve.gov/datadownload/Output.aspx"
            "?rel=H15&series=646250c87b1afd04cc6774796fc0cec8"
            "&lastobs=280&filetype=csv&label=include&layout=seriescolumn&type=package"
        ),
        "page": "https://www.federalreserve.gov/releases/h15/",
        "terms": "Board of Governors public domain. Cite the Board. No Federal Reserve seal or affiliation.",
    },
    "treasury10y": {
        "url_year": (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            "daily-treasury-rates.csv/{year}/all?_format=csv&type=daily_treasury_yield_curve"
        ),
        "page": (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            "TextView?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
        ),
        "terms": "U.S. government work, public domain. Cite the U.S. Department of the Treasury.",
    },
    "fomc": {
        "calendars": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "terms": "Board of Governors public domain. Cite the Board. No Federal Reserve seal or affiliation.",
    },
}

SERIES_META: dict[str, dict[str, str]] = {
    "pmms30": {
        "label": "30-year fixed (PMMS)",
        "source_name": "Freddie Mac Primary Mortgage Market Survey®",
        "source_url": ENDPOINTS["pmms"]["page"],
        "data_url": ENDPOINTS["pmms"]["url"],
        "license_note": ENDPOINTS["pmms"]["terms"],
        "frequency": "weekly",
        "unit": "percent",
    },
    "pmms15": {
        "label": "15-year fixed (PMMS)",
        "source_name": "Freddie Mac Primary Mortgage Market Survey®",
        "source_url": ENDPOINTS["pmms"]["page"],
        "data_url": ENDPOINTS["pmms"]["url"],
        "license_note": ENDPOINTS["pmms"]["terms"],
        "frequency": "weekly",
        "unit": "percent",
    },
    "effr": {
        "label": "Effective federal funds rate",
        "source_name": "Board of Governors of the Federal Reserve System, H.15",
        "source_url": ENDPOINTS["effr"]["page"],
        "data_url": ENDPOINTS["effr"]["url"],
        "license_note": ENDPOINTS["effr"]["terms"],
        "frequency": "daily",
        "unit": "percent",
    },
    "treasury10y": {
        "label": "10-year Treasury par yield",
        "source_name": "U.S. Department of the Treasury",
        "source_url": ENDPOINTS["treasury10y"]["page"].format(year=date.today().year),
        "data_url": ENDPOINTS["treasury10y"]["url_year"].format(year=date.today().year),
        "license_note": ENDPOINTS["treasury10y"]["terms"],
        "frequency": "daily",
        "unit": "percent",
    },
    "fomc_target": {
        "label": "FOMC federal funds target range",
        "source_name": "Board of Governors of the Federal Reserve System, FOMC statement",
        "source_url": ENDPOINTS["fomc"]["calendars"],
        "data_url": ENDPOINTS["fomc"]["calendars"],
        "license_note": ENDPOINTS["fomc"]["terms"],
        "frequency": "meeting",
        "unit": "percent_range",
    },
}

SERIES_IDS = ("pmms30", "pmms15", "effr", "treasury10y", "fomc_target")
_MISSING = {"", "ND", "N/A", "NA", "NULL", ".", "-", "—", "–"}
_RANGE_RE = re.compile(
    r"target range for the federal funds rate (?:at|to)\s+"
    r"(\d+(?:-\d+/\d+)?(?:\.\d+)?)\s+to\s+"
    r"(\d+(?:-\d+/\d+)?(?:\.\d+)?)\s+percent",
    re.I,
)
_HYPHENS = ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212")


class SourceError(Exception):
    """A publisher file was missing, unreadable, or did not contain this series."""


def now_chicago() -> datetime:
    current = datetime.now(CHICAGO).replace(microsecond=0)
    return current


def now_iso() -> str:
    return now_chicago().isoformat()


def today_chicago() -> date:
    return now_chicago().date()


def http_get(url: str, dest: Path | None = None, timeout: int = 60, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            body = _http_get_once(url, timeout)
            if not body.strip():
                raise SourceError(f"empty response from {url}")
            if len(body) > MAX_BYTES:
                raise SourceError(f"response from {url} exceeds {MAX_BYTES} bytes")
            if dest is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
            return body
        except Exception as exc:  # noqa: BLE001 — retry transient publisher failures
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"GET failed for {url}: {last}")


def _http_get_once(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"request failed for {url}: {exc.reason}") from exc


def parse_mdy(raw: str | None) -> date | None:
    text = (raw or "").strip().strip('"')
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def accept_number(raw: str | None) -> Decimal | None:
    """Parse a published percent. Skip blanks and non-numeric marks. Do not round extra precision."""
    if raw is None:
        return None
    text = str(raw).strip().strip('"')
    if text.upper() in _MISSING or text in _MISSING:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    hundredth = number.quantize(Decimal("0.01"))
    if hundredth != number:
        return None
    return hundredth


def to_number(number: Decimal) -> float:
    hundredth = number.quantize(Decimal("0.01"))
    if hundredth != number:
        raise SourceError(f"value {number} is not published to a hundredth")
    return float(hundredth)


def load_prev(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def valid_as_of(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def good_observation(prev: dict[str, Any] | None) -> bool:
    if not isinstance(prev, dict):
        return False
    value = prev.get("value")
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            return False
    elif isinstance(value, str):
        if not value.strip():
            return False
    else:
        return False
    return valid_as_of(prev.get("as_of"))


def series_meta(series_id: str) -> dict[str, str]:
    meta = dict(SERIES_META[series_id])
    if series_id == "treasury10y":
        year = today_chicago().year
        meta["source_url"] = ENDPOINTS["treasury10y"]["page"].format(year=year)
        meta["data_url"] = ENDPOINTS["treasury10y"]["url_year"].format(year=year)
    return meta


def unavailable(series_id: str, err: str) -> dict[str, Any]:
    meta = series_meta(series_id)
    return {
        "series_id": series_id,
        "label": meta["label"],
        "value": None,
        "as_of": None,
        "prior_value": None,
        "prior_as_of": None,
        "change": None,
        "retrieved_at": now_iso(),
        "source_name": meta["source_name"],
        "source_url": meta["source_url"],
        "data_url": meta["data_url"],
        "license_note": meta["license_note"],
        "status": "unavailable",
        "error": err,
        "frequency": meta["frequency"],
        "unit": meta["unit"],
        "history": [],
    }


def mark_delayed(prev: dict[str, Any] | None, series_id: str, err: str) -> dict[str, Any]:
    """Keep a prior good value only with the as-of date it was published under."""
    if good_observation(prev) and prev is not None:
        out = dict(prev)
        history = prev.get("history")
        out["history"] = list(history) if isinstance(history, list) else []
        out["series_id"] = series_id
        out["status"] = "delayed"
        out["error"] = err
        out["retrieved_at"] = now_iso()
        return out
    return unavailable(series_id, err)


def business_days_between(start: date, end: date) -> int:
    """Weekdays strictly after start through end."""
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def annotate_freshness(snap: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    out = dict(snap)
    today = today or today_chicago()
    if out.get("value") is not None and not valid_as_of(out.get("as_of")):
        out["value"] = None
        out["as_of"] = None
        out["history"] = []
        out["status"] = "unavailable"
        out["stale"] = False
        out["freshness"] = "unavailable"
        return out
    if out.get("value") is None or not valid_as_of(out.get("as_of")):
        out["stale"] = False
        out["freshness"] = "unavailable"
        return out
    as_of = date.fromisoformat(out["as_of"])
    age_days = (today - as_of).days
    freq = out.get("frequency")
    if freq == "weekly":
        stale = age_days > 10
        threshold = 10
    elif freq == "meeting":
        stale = age_days > 60
        threshold = 60
    elif freq == "daily":
        stale = business_days_between(as_of, today) > 5
        threshold = 5
    else:
        stale = False
        threshold = None
    out["age_days"] = age_days
    out["stale"] = stale
    out["stale_threshold_days"] = threshold
    out["freshness"] = "stale" if stale else "current"
    if out.get("status") == "ok" and stale:
        out["status"] = "stale"
    return out


def _dedupe(rows: list[dict[str, Any]]) -> list[tuple[date, Decimal]]:
    """Last published print wins for a repeated date. Values are never averaged."""
    by_date: dict[date, Decimal] = {}
    for row in rows:
        by_date[row["date"]] = row["value"]
    return [(day, by_date[day]) for day in sorted(by_date)]


def build_rate_snapshot(
    *,
    series_id: str,
    rows: list[dict[str, Any]],
    today: date,
    source_url: str | None = None,
    data_url: str | None = None,
) -> dict[str, Any]:
    if not rows:
        raise SourceError(f"{series_id} had no published values")
    points = _dedupe(rows)
    cur_day, cur_value = points[-1]
    prev_day, prev_value = points[-2] if len(points) > 1 else (None, None)
    change: float | None = None
    if prev_value is not None:
        change = float((cur_value - prev_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if change == 0:
            change = 0.0
    cut = today - timedelta(days=370)
    history = [
        {"date": day.isoformat(), "value": to_number(value)}
        for day, value in points
        if day >= cut or day == cur_day
    ]
    meta = series_meta(series_id)
    return {
        "series_id": series_id,
        "label": meta["label"],
        "value": to_number(cur_value),
        "as_of": cur_day.isoformat(),
        "prior_value": to_number(prev_value) if prev_value is not None else None,
        "prior_as_of": prev_day.isoformat() if prev_day else None,
        "change": change,
        "retrieved_at": now_iso(),
        "source_name": meta["source_name"],
        "source_url": source_url or meta["source_url"],
        "data_url": data_url or meta["data_url"],
        "license_note": meta["license_note"],
        "status": "ok",
        "frequency": meta["frequency"],
        "unit": meta["unit"],
        "history": history,
    }


def parse_pmms_csv(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(text))
    fields = [name.strip() for name in (reader.fieldnames or [])]
    if "pmms30" not in fields and "pmms15" not in fields:
        raise SourceError(f"PMMS CSV missing pmms30 and pmms15 columns: {reader.fieldnames}")
    rows30: list[dict[str, Any]] = []
    rows15: list[dict[str, Any]] = []
    for row in reader:
        day = parse_mdy(row.get("date") or "")
        if day is None:
            continue
        value30 = accept_number(row.get("pmms30"))
        value15 = accept_number(row.get("pmms15"))
        if value30 is not None:
            rows30.append({"date": day, "value": value30})
        if value15 is not None:
            rows15.append({"date": day, "value": value15})
    return rows30, rows15


def parse_effr_csv(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith('"Time Period"') or line.startswith("Time Period"):
            start = index
            break
    if start is None:
        raise SourceError("H.15 CSV missing Time Period header")
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    header = next(reader, None)
    if not header or len(header) < 2 or header[1].strip() != "RIFSPFF_N.B":
        found = header[1].strip() if header and len(header) > 1 else ""
        raise SourceError(f"H.15 series column is {found!r}, expected RIFSPFF_N.B")
    rows: list[dict[str, Any]] = []
    for parts in reader:
        if len(parts) < 2:
            continue
        day = parse_mdy(parts[0])
        value = accept_number(parts[1])
        if day is None or value is None:
            continue
        rows.append({"date": day, "value": value})
    return rows


def parse_treasury_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SourceError("Treasury CSV missing header")
    field = None
    for name in reader.fieldnames:
        normalized = (name or "").strip().strip('"').lower().replace(" ", "")
        if normalized in {"10yr", "10year"}:
            field = name
            break
    if not field:
        raise SourceError(f"Treasury CSV missing 10 Yr column: {reader.fieldnames}")
    rows: list[dict[str, Any]] = []
    for row in reader:
        day = parse_mdy(row.get("Date") or row.get("date") or "")
        value = accept_number(row.get(field))
        if day is None or value is None:
            continue
        rows.append({"date": day, "value": value})
    return rows


def html_to_text(page: str) -> str:
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", page)
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    for dash in _HYPHENS:
        text = text.replace(dash, "-")
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def frac_to_decimal(token: str) -> Decimal:
    match = re.fullmatch(r"(\d+)-(\d+)/(\d+)", token.strip())
    if match:
        whole = int(match.group(1))
        numerator = int(match.group(2))
        denominator = int(match.group(3))
        if denominator == 0:
            raise SourceError(f"bad fraction {token}")
        return Decimal(whole) + (Decimal(numerator) / Decimal(denominator))
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise SourceError(f"bad range token {token}") from exc


def format_bound(number: Decimal) -> str:
    hundredth = number.quantize(Decimal("0.01"))
    if hundredth != number:
        raise SourceError(f"target bound {number} is not a hundredth")
    return f"{hundredth:.2f}"


def format_range(low: Decimal, high: Decimal) -> str:
    return f"{format_bound(low)}\u2013{format_bound(high)}"


def parse_target_range(text: str) -> tuple[Decimal, Decimal, str]:
    match = _RANGE_RE.search(text)
    if not match:
        raise SourceError("could not parse a federal funds target range")
    low = frac_to_decimal(match.group(1))
    high = frac_to_decimal(match.group(2))
    if low >= high:
        raise SourceError(f"parsed range {low} to {high} is not ordered")
    return low, high, match.group(0)


def fetch_pmms(today: date | None = None) -> tuple[Any, Any]:
    today = today or today_chicago()
    body = http_get(ENDPOINTS["pmms"]["url"], RAW / "pmms_history.csv")
    rows30, rows15 = parse_pmms_csv(body.decode("utf-8-sig"))
    if not rows30 and not rows15:
        raise SourceError("PMMS CSV had no 30-year or 15-year values")
    left: Any
    right: Any
    if rows30:
        left = build_rate_snapshot(series_id="pmms30", rows=rows30, today=today)
    else:
        left = SourceError("PMMS CSV had no pmms30 values")
    if rows15:
        right = build_rate_snapshot(series_id="pmms15", rows=rows15, today=today)
    else:
        right = SourceError("PMMS CSV had no pmms15 values")
    return left, right


def fetch_effr(today: date | None = None) -> dict[str, Any]:
    today = today or today_chicago()
    body = http_get(ENDPOINTS["effr"]["url"], RAW / "h15_effr.csv")
    rows = parse_effr_csv(body.decode("utf-8-sig"))
    return build_rate_snapshot(series_id="effr", rows=rows, today=today)


def fetch_treasury(today: date | None = None) -> dict[str, Any]:
    today = today or today_chicago()
    rows: list[dict[str, Any]] = []
    used: list[str] = []
    errors: list[str] = []
    for year in (today.year - 1, today.year):
        url = ENDPOINTS["treasury10y"]["url_year"].format(year=year)
        try:
            body = http_get(url, RAW / f"treasury_{year}.csv")
            part = parse_treasury_csv(body.decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001 — one year can fail without dropping the other
            errors.append(f"{year}: {exc}")
            continue
        if part:
            used.append(url)
            rows.extend(part)
    if not rows:
        detail = "; ".join(errors) or "no rows"
        raise SourceError(f"Treasury CSV had no 10-year values. {detail}")
    points = _dedupe(rows)
    as_of_year = points[-1][0].year
    page = ENDPOINTS["treasury10y"]["page"].format(year=as_of_year)
    data_url = next((url for url in used if f"/{as_of_year}/" in url), used[-1])
    return build_rate_snapshot(
        series_id="treasury10y",
        rows=rows,
        today=today,
        source_url=page,
        data_url=data_url,
    )


def fetch_fomc(today: date | None = None) -> dict[str, Any]:
    today = today or today_chicago()
    calendars = ENDPOINTS["fomc"]["calendars"]
    body = http_get(calendars, RAW / "fomc_calendars.html")
    page = body.decode("utf-8")
    published = sorted(set(re.findall(r"/newsevents/pressreleases/monetary(\d{8})a\.htm", page)), reverse=True)
    if not published:
        raise SourceError("no FOMC statement links on the calendar")
    last_error = "no statement checked"
    checked = 0
    for ymd in published:
        statement_day = datetime.strptime(ymd, "%Y%m%d").date()
        if statement_day > today:
            continue
        checked += 1
        if checked > 6:
            break
        url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
        try:
            statement = http_get(url, RAW / f"fomc_{ymd}.html")
            plain = html_to_text(statement.decode("utf-8"))
            low, high, quote = parse_target_range(plain)
        except Exception as exc:  # noqa: BLE001 — try the next older statement
            last_error = str(exc)
            continue
        return {
            "series_id": "fomc_target",
            "label": SERIES_META["fomc_target"]["label"],
            "value": format_range(low, high),
            "value_low": to_number(low),
            "value_high": to_number(high),
            "as_of": statement_day.isoformat(),
            "prior_value": None,
            "prior_as_of": None,
            "change": None,
            "retrieved_at": now_iso(),
            "source_name": SERIES_META["fomc_target"]["source_name"],
            "source_url": url,
            "data_url": calendars,
            "license_note": SERIES_META["fomc_target"]["license_note"],
            "status": "ok",
            "frequency": "meeting",
            "unit": "percent_range",
            "history": [],
            "statement_quote": quote,
        }
    raise SourceError(f"could not read a target range from an FOMC statement: {last_error}")


def choose_previous(local: dict[str, Any] | None, remote: dict[str, Any] | None) -> dict[str, Any] | None:
    local_ok = good_observation(local)
    remote_ok = good_observation(remote)
    if remote_ok and not local_ok:
        return remote
    if local_ok and not remote_ok:
        return local
    if not local_ok and not remote_ok:
        return None
    assert local is not None and remote is not None
    if str(remote.get("as_of")) > str(local.get("as_of")):
        return remote
    if str(remote.get("as_of")) < str(local.get("as_of")):
        return local
    if str(remote.get("retrieved_at") or "") > str(local.get("retrieved_at") or ""):
        return remote
    return local


def seed_previous(data_dir: Path, url: str, getter: Callable[..., bytes] | None = None) -> None:
    """Load the last published snapshot so a failed fetch can keep that as-of date."""
    if not url.startswith("https://"):
        print("previous URL ignored (https required)")
        return
    getter = getter or http_get
    try:
        try:
            body = getter(url, attempts=2)
        except TypeError:
            body = getter(url)
    except Exception as exc:  # noqa: BLE001 — a missing Pages snapshot must not block publishers
        print(f"previous published snapshot not used: {exc}")
        return
    if not isinstance(body, (bytes, bytearray)):
        print("previous published snapshot not used: unexpected body")
        return
    if len(body) > 5_000_000:
        print("previous published snapshot not used: too large")
        return
    try:
        bundle = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        print(f"previous published snapshot not used: {exc}")
        return
    series = bundle.get("series") if isinstance(bundle, dict) else None
    if not isinstance(series, dict):
        print("previous published snapshot not used: no series object")
        return
    for series_id in SERIES_IDS:
        remote = series.get(series_id)
        if not isinstance(remote, dict) or not good_observation(remote):
            continue
        if remote.get("series_id") not in (None, series_id):
            continue
        local = load_prev(data_dir / f"{series_id}.json")
        chosen = choose_previous(local, remote)
        if chosen is remote:
            write_json(data_dir / f"{series_id}.json", remote)
            print(f"seeded {series_id} from published snapshot as_of={remote.get('as_of')}")


def _finish(data_dir: Path, series_id: str, snap: Any, today: date) -> dict[str, Any]:
    if isinstance(snap, Exception):
        print(f"{series_id} FAILED: {snap}")
        snap = mark_delayed(load_prev(data_dir / f"{series_id}.json"), series_id, str(snap))
    if not isinstance(snap, dict):
        snap = unavailable(series_id, "fetcher returned nothing")
    return annotate_freshness(snap, today)


def collect(
    data_dir: Path,
    *,
    today: date | None = None,
    pmms: Callable[[], tuple[Any, Any]] | None = None,
    effr: Callable[[], Any] | None = None,
    treasury: Callable[[], Any] | None = None,
    fomc: Callable[[], Any] | None = None,
) -> dict[str, dict[str, Any]]:
    today = today or today_chicago()
    pmms = pmms or fetch_pmms
    effr = effr or fetch_effr
    treasury = treasury or fetch_treasury
    fomc = fomc or fetch_fomc
    results: dict[str, dict[str, Any]] = {}
    try:
        left, right = pmms()
    except Exception as exc:  # noqa: BLE001 — PMMS failure must not stop the other publishers
        left = SourceError(str(exc))
        right = SourceError(str(exc))
    results["pmms30"] = _finish(data_dir, "pmms30", left, today)
    results["pmms15"] = _finish(data_dir, "pmms15", right, today)
    for series_id, fetcher in (("effr", effr), ("treasury10y", treasury), ("fomc_target", fomc)):
        try:
            snap = fetcher()
        except Exception as exc:  # noqa: BLE001
            snap = exc
        results[series_id] = _finish(data_dir, series_id, snap, today)
    return results


def write_outputs(data_dir: Path, results: dict[str, dict[str, Any]]) -> None:
    for series_id in SERIES_IDS:
        write_json(data_dir / f"{series_id}.json", results[series_id])
    bundle = {
        "generated_at": now_iso(),
        "timezone": TZ_NAME,
        "disclaimer": DISCLAIMER,
        "brokerage": BROKERAGE,
        "series": {series_id: results[series_id] for series_id in SERIES_IDS},
    }
    write_json(data_dir / "all.json", bundle)
    (data_dir / "all.js").write_text(
        "window.RATE_TRACKER_DATA = " + json.dumps(bundle, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("RATE_TRACKER_PREVIOUS_URL", "").strip()
    if previous:
        seed_previous(DATA, previous)
    results = collect(DATA)
    write_outputs(DATA, results)
    for series_id in SERIES_IDS:
        snap = results[series_id]
        print(
            f"{series_id}: value={snap.get('value')} as_of={snap.get('as_of')} status={snap.get('status')}"
        )
    print(f"Wrote {DATA / 'all.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
