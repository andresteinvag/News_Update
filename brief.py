"""Økonomibrief: henter overskrifter, lar Claude oppsummere, bygger docs/index.html."""
import html, json, os, re, sys
from calendar import timegm
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import anthropic
import feedparser

OSLO = ZoneInfo("Europe/Oslo")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
EDITIONS = "docs/editions.json"
KEEP = 30
PAYWALLED = ["ft.com", "dn.no", "finansavisen.no", "wsj.com", "bloomberg.com"]
PW = " (betalingsmur)"

# (navn, domene, direkte RSS eller None, betalingsmur). Uten RSS, eller hvis den feiler,
# brukes Google News RSS filtrert på domenet (kun overskrifter).
SOURCES = [
    ("E24", "e24.no", "https://e24.no/rss2/", True),
    ("VG", "vg.no", "https://www.vg.no/rss/feed/", False),
    ("NRK", "nrk.no", "https://www.nrk.no/toppsaker.rss", False),
    ("CNBC International", "cnbc.com", "https://www.cnbc.com/id/20910258/device/rss/rss.html", False),
    ("Reuters", "reuters.com", None, False),
    ("Dagens Næringsliv", "dn.no", None, True),
    ("Finansavisen", "finansavisen.no", None, True),
    ("Financial Times", "ft.com", None, True),
]

SYSTEM = """Du er økonomiredaktør og skriver en kort brief på norsk (bokmål) for en økonomistudent.
Fokus: norsk økonomi (Norges Bank, rente, krone, olje og gass, Oljefondet, Oslo Børs, bolig, statsbudsjett) og verdensøkonomi (sentralbanker, inflasjon, handel og toll, markeder, geopolitikk med økonomisk effekt). Hopp over sport, kjendis og lokalstoff.
Regler:
- Velg de 6-8 viktigste sakene og slå sammen dubletter.
- Kilder merket (betalingsmur) gir bare overskrift. Der en sak kun dekkes av slike kilder, bruk web_search til å finne åpen dekning (Reuters, AP, BBC, NRK, CNBC m.fl.) og bygg oppsummeringen på den. Finner du ingenting, skriv kun det overskriften faktisk sier, uten å gjette.
- Gi hver sak 2-4 punkter under "impact" om mulig påvirkning på de delene av samfunnet og økonomien som faktisk er relevante, f.eks. Aksjemarkedet, Renter, Kronekurs, Inflasjon, Bolig, Arbeidsmarked, Energi og råvarer, Politikk, Næringsliv. Angi retning (opp, ned, uklart) og begrunn kort. Bruk forbehold som "kan" og "trolig"; dette er vurderinger, ikke spådommer, og ikke investeringsråd.
- Skriv i egne ord, aldri lange sitater. Lenk bare til URL-er fra listen eller fra søkeresultater.
- Svar KUN med JSON:
{"overview": "2 setninger om dagens bilde", "stories": [{"title": "...", "summary": "2-3 setninger", "why_it_matters": "1 setning om betydning for Norge eller verdensøkonomien", "region": "Norge eller Verden", "impact": [{"area": "Aksjemarkedet", "effect": "1 setning"}], "sources": [{"name": "...", "url": "https://...", "paywall": true}]}]}"""


def fetch(name, domain, url, paywall):
    gnews = f"https://news.google.com/rss/search?q={quote(f'site:{domain} when:1d')}&hl=no&gl=NO&ceid=NO:no"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=16)
    for u in [x for x in (url, gnews) if x]:
        try:
            feed = feedparser.parse(u, agent="Mozilla/5.0 (okonomibrief)")
        except Exception as err:
            print(f"{name}: {err}", file=sys.stderr)
            continue
        items = []
        for e in feed.entries[:40]:
            t = e.get("published_parsed")
            if t and datetime.fromtimestamp(timegm(t), timezone.utc) < cutoff:
                continue
            desc = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("summary", "")))).strip()[:220]
            tag = name + (PW if paywall else "")
            items.append(f"[{tag}] {e.get('title', '').strip()} | {desc} | {e.get('link', '')}")
        if items:
            return items[:25]
    print(f"{name}: ingen treff", file=sys.stderr)
    return []


def summarize(lines, label):
    resp = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=7000,
        system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8, "blocked_domains": PAYWALLED}],
        messages=[{"role": "user", "content": f"Utgave: {label}. Overskrifter siste 16 timer:\n\n" + "\n".join(lines)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Ingen JSON i svaret:\n" + text[:500])
    return json.loads(m.group(0))


DAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"]
CSS = """:root{--bg:#eef2f4;--ink:#14222b;--mute:#586873;--rule:#c7d2d8;--acc:#0e5a6b}
@media(prefers-color-scheme:dark){:root{--bg:#10191e;--ink:#e6edf0;--mute:#93a4ad;--rule:#2a3a43;--acc:#6cc3d5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 Georgia,'Times New Roman',serif}
main{max-width:42rem;margin:0 auto;padding:2rem 1.25rem 4rem}
.time{font:700 clamp(4.5rem,18vw,8rem)/.9 system-ui,sans-serif;letter-spacing:-.04em;color:var(--acc)}
.date{font:600 1.1rem system-ui,sans-serif;margin-top:.5rem}
.lead{font-size:1.15rem;border-top:1px solid var(--rule);padding-top:1rem;margin:1.5rem 0 0}
article{border-top:1px solid var(--rule);padding:1.25rem 0}
h3{font:700 1.25rem/1.3 system-ui,sans-serif;margin:0 0 .4rem}
p{margin:.4rem 0}
.why{color:var(--mute)}
.impact{margin:.7rem 0;padding:0 0 0 1.1rem;font:.95rem/1.5 system-ui,sans-serif}
.impact li{margin:.3rem 0}
.impact b{color:var(--acc)}
.src{font:.85rem system-ui,sans-serif}
.src a{color:var(--acc);margin-right:1rem}
details{border-top:1px solid var(--rule);padding:1rem 0}
summary{cursor:pointer;font:600 1rem system-ui,sans-serif}
a:focus-visible,summary:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
footer{color:var(--mute);font:.8rem system-ui,sans-serif;margin-top:2rem}"""


def esc(s):
    return html.escape(str(s or ""))


def link(u):
    return u if str(u).startswith(("https://", "http://")) else "#"


def story(s):
    items = "".join(
        f'<li><b>{esc(i.get("area"))}:</b> {esc(i.get("effect"))}</li>'
        for i in s.get("impact", []) if isinstance(i, dict)
    )
    impact = f'<ul class="impact">{items}</ul>' if items else ""
    links = " ".join(
        f'<a href="{esc(link(x.get("url")))}" rel="noopener">{esc(x.get("name"))}{PW if x.get("paywall") else ""}</a>'
        for x in s.get("sources", [])
    )
    return (f'<article><h3>{esc(s.get("title"))}</h3><p>{esc(s.get("summary"))}</p>'
            f'<p class="why"><b>{esc(s.get("region"))}:</b> {esc(s.get("why_it_matters"))}</p>'
            f'{impact}<p class="src">{links}</p></article>')


def body(e):
    return f'<p class="lead">{esc(e.get("overview"))}</p>' + "".join(story(s) for s in e.get("stories", []))


def label(e):
    d = datetime.strptime(e["date"], "%Y-%m-%d")
    return f"{DAYS[d.weekday()]} {d.day}. {MONTHS[d.month - 1]}"


def render(editions):
    new, old = editions[0], editions[1:]
    older = "".join(
        f'<details><summary>{label(e)} kl. {e["hour"]:02d}:00</summary>{body(e)}</details>' for e in old
    )
    return f"""<!doctype html><html lang="no"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Økonomibrief</title><style>{CSS}</style></head><body><main><header><div class="time">{new["hour"]:02d}:00</div><div class="date">{label(new)}</div></header>{body(new)}{older}<footer>Generert automatisk av Claude fra overskrifter og åpne kilder ({esc(new["generated"])}). Les originalsakene før du bruker innholdet i beslutninger.</footer></main></body></html>"""


def main():
    now = datetime.now(OSLO)
    force = os.environ.get("FORCE") == "1"
    if now.hour not in (8, 20) and not force:
        print("Ikke utgavetid, hopper over.")
        return
    hour = 8 if now.hour < 14 else 20
    ed_id = f"{now:%Y-%m-%d}-{hour:02d}"
    editions = json.load(open(EDITIONS, encoding="utf-8")) if os.path.exists(EDITIONS) else []
    if any(e["id"] == ed_id for e in editions) and not force:
        print("Utgaven finnes allerede.")
        return
    lines = [ln for s in SOURCES for ln in fetch(*s)]
    if len(lines) < 10:
        sys.exit("For få overskrifter hentet, avbryter.")
    data = summarize(lines, f"{now:%d.%m.%Y} kl. {hour:02d}:00")
    ed = {"id": ed_id, "hour": hour, "date": f"{now:%Y-%m-%d}", "generated": now.strftime("%d.%m.%Y %H:%M"), **data}
    editions = sorted([e for e in editions if e["id"] != ed_id] + [ed], key=lambda e: e["id"], reverse=True)[:KEEP]
    os.makedirs("docs", exist_ok=True)
    json.dump(editions, open(EDITIONS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open("docs/index.html", "w", encoding="utf-8").write(render(editions))
    if out := os.environ.get("GITHUB_OUTPUT"):
        open(out, "a").write("new=true\n")


if __name__ == "__main__":
    main()