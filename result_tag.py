"""result_tag.py - the result tag and the two links shown beside a stock in Telegram alerts.

Data: result_tags.json, written by the local screener (export_result_tags.py) and pushed into this repository.
For a stock:
    1 S 📅13 Nov ⭐E▲▲        result still to come (⭐ = "High" in the screener's Likely good list)
    1 S Q2 S▲▲ NP▼▼ E▼▼       latest quarter declared: first arrow vs the previous quarter, second vs a year ago
    1 S                        nothing known (also for a stock that is not in the file at all: the links alone)
After the links comes the forensic verdict letter when the screener has one: 🟢C clean, 🟠W warn, 🔴R red, ⛔V veto.
Then the alignment score when the screener has one: A+41 (from -100 to +100; outside facts in favour minus against).
"1" opens the chart in the local screener (works on the computer where it runs), "S" opens Screener.in.
A missing or old file never breaks an alert: the text is then sent as it is.
"""
import datetime
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

FILE = Path(__file__).parent / "result_tags.json"
LOCAL = "http://127.0.0.1:8000/?chart="
SCREENER = "https://www.screener.in/company/"
_data = None
_codes = {}
# "<b>TCS</b> ..." and "<b>TCS — Unusual Volume Low Breach!</b>" (the name inside a longer bold title)
_BOLD_SYMBOL = re.compile(r"<b>([A-Z0-9][A-Z0-9&\-]{1,19})(?:</b>| — [^<\n]*</b>)")


def _tags():
    global _data, _codes
    if _data is None:
        try:
            j = json.loads(FILE.read_text(encoding="utf-8"))
            _data, _codes = j.get("tags") or {}, j.get("codes") or {}
        except Exception:
            _data, _codes = {}, {}
    return _data


def _today_ist():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()


# bold words in alert titles that look like a symbol but are not a stock
_NOT_A_STOCK = {"VCP", "NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "NSE", "BSE", "ETF", "FNO", "IPO", "RSI", "IT", "FMCG", "PSU",
                "AUTO", "PHARMA", "METAL", "REALTY", "ENERGY", "MEDIA", "INFRA", "TEST"}
_SYMBOL_OK = re.compile(r"[A-Z0-9][A-Z0-9&\-]{1,19}")


def tag(symbol: str, known_only: bool = False, name_link: bool = True) -> str:
    """HTML for one stock. A stock that is not in the file gets the two links alone ('' with known_only, or when the
    text is not a symbol)."""
    sym = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", str(symbol or "").strip().upper().split(":")[-1])
    e = _tags().get(sym)
    if e is None and sym in _codes:          # a BSE filing gives only the scrip code
        sym = _codes[sym]
        e = _tags().get(sym)
    if e is None:
        if known_only or not _SYMBOL_OK.fullmatch(sym) or sym in _NOT_A_STOCK:
            return ""
        e = {}                               # not in the screener's lists: still give the chart and Screener links
    # The stock name is the chart link (no more "1"). name_link=False: the caller has made the bold name the link itself.
    out = [(f'<a href="{LOCAL}{quote(sym)}">{html.escape(sym)}</a> ' if name_link else "") + f'<a href="{SCREENER}{quote(sym)}/">S</a>']
    fz = {"C": "🟢C", "W": "🟠W", "R": "🔴R", "V": "⛔V"}.get(e.get("f"))      # forensic verdict: Clean, Warn, Red, Veto
    if fz:
        out.append(fz)
    a = e.get("a")                           # alignment score from the screener, -100 to +100
    if isinstance(a, (int, float)):
        out.append(f"A{int(a):+d}")
    d = e.get("d")
    shown = False
    if d:
        try:
            day = datetime.date.fromisoformat(d)
            if day >= _today_ist():
                out.append("📅" + day.strftime("%d %b").lstrip("0") + (" ⭐E▲▲" if e.get("star") else ""))
                shown = True
        except ValueError:
            pass
    if not shown and e.get("t"):
        q, _, rest = e["t"].partition(" ")
        out.append(f"<b>{html.escape(q)}</b> {html.escape(rest)}")
    return " ".join(out)


def decorate(text: str) -> str:
    """Put the tag after the first bold stock symbol on the first line of an alert. Other texts come back unchanged."""
    try:
        first, sep, rest = text.partition("\n")
        m = _BOLD_SYMBOL.search(first)
        if not m or "result_tag" in first:
            return text
        t = tag(m.group(1), name_link=False)
        if not t:
            return text
        sym = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", m.group(1).strip().upper())
        sym = _codes.get(sym, sym) if _tags().get(sym) is None else sym
        named = first[:m.start(1)] + f'<a href="{LOCAL}{quote(sym)}">{m.group(1)}</a>' + first[m.end(1):m.end()]      # the bold name opens the chart
        return named + " " + t + first[m.end():] + sep + rest
    except Exception:
        return text
