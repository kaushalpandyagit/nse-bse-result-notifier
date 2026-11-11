"""result_tag.py - the result tag and the two links shown beside a stock in Telegram alerts.

Data: result_tags.json, written by the local screener (export_result_tags.py) and pushed into this repository.
For a stock:
    1 S 📅13 Nov ⭐E▲▲        result still to come (⭐ = "High" in the screener's Likely good list)
    1 S Q2 S▲▲ NP▼▼ E▼▼       latest quarter declared: first arrow vs the previous quarter, second vs a year ago
    1 S                        nothing known
After the links comes the forensic verdict letter when the screener has one: 🟢C clean, 🟠W warn, 🔴R red, ⛔V veto.
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
_BOLD_SYMBOL = re.compile(r"<b>([A-Z0-9][A-Z0-9&\-]{1,19})</b>")


def _tags():
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8")).get("tags") or {}
        except Exception:
            _data = {}
    return _data


def _today_ist():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()


def tag(symbol: str) -> str:
    """HTML for one stock, or '' when the stock is not in the file."""
    sym = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", str(symbol or "").strip().upper().split(":")[-1])
    e = _tags().get(sym)
    if e is None:
        return ""
    out = [f'<a href="{LOCAL}{quote(sym)}">1</a> <a href="{SCREENER}{quote(sym)}/">S</a>']
    fz = {"C": "🟢C", "W": "🟠W", "R": "🔴R", "V": "⛔V"}.get(e.get("f"))      # forensic verdict: Clean, Warn, Red, Veto
    if fz:
        out.append(fz)
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
        t = tag(m.group(1))
        if not t:
            return text
        return first[:m.end()] + " " + t + first[m.end():] + sep + rest
    except Exception:
        return text
