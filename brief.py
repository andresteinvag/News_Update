"""Økonomibrief: henter overskrifter, bruker Gemini gratisnivå, bygger docs/index.html."""
import html, json, os, re, sys, time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
import feedparser

OSLO = ZoneInfo("Europe/Oslo")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
EDITIONS = "docs/editions.json"
KEEP = 30
PAYWALLED = ["ft.com", "dn.no", "finansavisen.no", "wsj.com", "bloomberg.com"]
OPEN_COVERAGE = ["reuters.com", "apnews.com", "bbc.com", "nrk.no", "cnbc.com"]
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
- Kilder merket (betalingsmur) gir bare overskrift. For slike saker legges eventuelle funn fra åpne kilder (Reuters, AP, BBC, NRK, CNBC) inn i materialet ditt med merking som "Åpen dekning". Bruk den åpne dekningen når den faktisk beskriver samme sak. Finner du ingenting, skriv kun det overskriften faktisk sier, uten å gjette.
- Gi hver sak 2-4 punkter under "impact" om mulig påvirkning på de delene av samfunnet og økonomien som faktisk er relevante, f.eks. Aksjemarkedet, Renter, Kronekurs, Inflasjon, Bolig, Arbeidsmarked, Energi og råvarer, Politikk, Næringsliv. Angi retning (opp, ned, uklart) og begrunn kort. Bruk forbehold som "kan" og "trolig"; dette er vurderinger, ikke spådommer, og ikke investeringsråd.
- Skriv i egne ord, aldri lange sitater. Lenk bare til URL-er fra listen eller fra søkeresultater.
- Returner KUN JSON uten markdown-gjerder.
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


def open_coverage(lines):
    """Finn åpen omtale av betalingsmur-overskrifter via gratis Google News RSS."""
    results = []
    seen = set()
    paywall_lines = [ln for ln in lines if "(betalingsmur)" in ln]

    for ln in paywall_lines[:20]:
        try:
            headline = ln.split("] ", 1)[1].split(" | ", 1)[0].strip()
        except IndexError:
            continue
        key = headline.lower()
        if key in seen or not headline:
            continue
        seen.add(key)

        domain_query = " OR ".join(f"site:{d}" for d in OPEN_COVERAGE)
        query = f'"{headline[:180]}" ({domain_query}) when:2d'
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en&gl=US&ceid=US:en"

        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (okonomibrief)")
        except Exception as err:
            print(f"Åpen dekning: {err}", file=sys.stderr)
            continue

        for e in feed.entries[:3]:
            title = e.get("title", "").strip()
            link_url = e.get("link", "")
            source_data = e.get("source")
            source = source_data.get("title", "") if hasattr(source_data, "get") else ""
            if not title or not link_url:
                continue
            results.append(
                f"[Åpen dekning {source or 'åpen kilde'}] {title} | {link_url}"
            )
    return results[:40]


def summarize(lines, label):
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("Mangler GEMINI_API_KEY i GitHub Secrets.")

    client = genai.Client()
    material = "\n".join(lines)
    prompt = f"""{SYSTEM}

Utgave: {label}

Her er overskrifter og beskrivelser hentet fra nyhetskildene. Materiale som starter med
"[Åpen dekning" er funnet fra åpne kilder for å supplere betalingsmur-overskrifter.

MATERIALE:
{material}
"""

    # Gratismodeller med automatisk reserve dersom en modell er midlertidig utilgjengelig.
    models = [
        os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"),
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    ]

    last_error = None
    for model in dict.fromkeys(models):
        for attempt in range(4):
            try:
                print(f"Prøver Gemini-modell: {model} (forsøk {attempt + 1}/4)")
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                text = resp.text or ""
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    m = re.search(r"\{.*\}", text, re.S)
                    if not m:
                        raise RuntimeError("Ingen gyldig JSON i Gemini-svaret:\n" + text[:500])
                    return json.loads(m.group(0))
            except Exception as err:
                last_error = err
                print(f"{model} feilet: {err}", file=sys.stderr)
                if "503" in str(err) or "UNAVAILABLE" in str(err) or "429" in str(err):
                    time.sleep(2 ** attempt)
                    continue
                break

    raise RuntimeError(f"Alle Gemini-forsøk feilet. Siste feil: {last_error}")


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
    return f"""<!doctype html><html lang="no"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Økonomibrief</title><style>{CSS}</style></head><body><main><header><div class="time">{new["hour"]:02d}:00</div><div class="date">{label(new)}</div></header>{body(new)}{older}<footer>Generert automatisk av Gemini fra overskrifter og åpne kilder ({esc(new["generated"])}). Les originalsakene før du bruker innholdet i beslutninger.</footer></main></body></html>"""


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
    lines += open_coverage(lines)
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