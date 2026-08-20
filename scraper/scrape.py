#!/usr/bin/env python3
"""
Otra Coronación de Gloria — scraper.

Busca en Google News RSS noticias de argentinos/as saliendo 1°, 2° o 3° en
competencias mundiales. Sin dependencias externas (stdlib solamente).

Modos:
  python3 scraper/scrape.py                 # modo normal: ventana 3 días, actualiza data/
  python3 scraper/scrape.py --dry-run --days 30   # calibración: no toca estado, escribe candidates.json

Salidas (modo normal):
  data/podios.json    — feed público de eventos confirmados (lo lee la landing)
  data/seen.json      — estado de deduplicación
  new_events.json     — SOLO si hay eventos nuevos de alta confianza (dispara el email)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PODIOS_PATH = os.path.join(DATA_DIR, "podios.json")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
NEW_EVENTS_PATH = os.path.join(ROOT, "new_events.json")
CANDIDATES_PATH = os.path.join(ROOT, "candidates.json")

UA = "Mozilla/5.0 (compatible; OtraCoronacionDeGloria/1.0; +https://github.com/otracoronacion/otracoronacion.github.io)"

# Medios que reciclan notas viejas con fecha fresca (fuente de falsos positivos).
# Se compara contra el nombre de fuente normalizado (minúsculas, sin acentos).
BLOCKED_SOURCES = {
    "la propuesta digital",
}

# ---------------------------------------------------------------- queries ---

QUERIES_ES = [
    '"campeón mundial" argentino',
    '"campeona mundial" argentina',
    '"campeón del mundo" argentino',
    '"campeona del mundo" argentina',
    '"campeones del mundo" argentinos',
    '"campeonas mundiales" argentinas',
    '"campeonas del mundo" argentinas',
    'argentino "se consagró campeón" mundial',
    'argentina "se consagró campeona" mundial',
    '"subcampeón mundial" argentino',
    '"subcampeona mundial" argentina',
    'argentino "tercer puesto" mundial',
    'argentino "medalla de oro" mundial',
    'argentino "medalla de plata" mundial',
    'argentino "medalla de bronce" mundial',
    'argentina "medalla" "olimpiada internacional"',
    'argentino campeón "mundial de"',
    'argentino ganó "el mundial de"',
    '"campeón sudamericano" argentino',
    '"campeona sudamericana" argentina',
    'argentina "se consagró campeón sudamericano"',
    'argentino "campeón panamericano"',
    'argentina "medalla de oro" panamericano',
    'argentina campeona "Copa América"',
    # plata/bronce continental: sin estas, los subcampeonatos dependían de que
    # el texto completo de la nota matcheara de casualidad alguna query de oro
    '"subcampeón sudamericano" OR "subcampeona sudamericana" argentina',
    'argentina "medalla de plata" sudamericano OR panamericano',
    'argentina "medalla de bronce" sudamericano OR panamericano',
]
QUERIES_EN = [
    'argentine "world champion"',
    'argentinian wins "world championship"',
    'argentina "world title" wins',
    'argentine "bronze" OR "silver" "world championship"',
]
QUERIES = QUERIES_ES + QUERIES_EN

# ------------------------------------------------------------- normalizing --

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def norm(s: str) -> str:
    s = strip_accents(s.lower())
    s = re.sub(r"[^a-z0-9ñ\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

STOPWORDS = set("""
de del la las el los un una unos unas y o en a al con por para que se su sus es fue son
tras ante como mas muy este esta estos estas ese esa hoy ayer the of in at on and or to for
a las los una del ante entre sobre desde hasta año años vez tambien también
""".split())

def strip_source(title: str) -> str:
    """Google News agrega ' - Fuente' al final del título; sacarlo para no contaminar el dedup."""
    return title.rsplit(" - ", 1)[0] if " - " in title else title

def sig_tokens(s: str):
    # palabras de 4+ letras, y números de 2+ cifras (distinguen categorías: +40 vs +50, sub 17, 2026…)
    return {
        t for t in norm(strip_source(s)).split()
        if (len(t) >= 4 or (t.isdigit() and len(t) >= 2)) and t not in STOPWORDS
    }

# --------------------------------------------------------------- patterns ---
# Todo se evalúa sobre texto normalizado (minúsculas, sin acentos).

# Gentilicios provinciales y de ciudades grandes, masculino Y femenino (con plurales):
# la prensa local titula "la catamarqueña que se coronó…" sin decir nunca "argentina".
# Escritos ya normalizados (sin tildes; la ñ se normaliza a n).
PROV = (
    r"bonaerenses?|porten[oa]s?|catamarquen[oa]s?|chaquen[oa]s?|chubutenses?|"
    r"cordobes(?:a|as|es)?|correntin[oa]s?|entrerrian[oa]s?|formosen[oa]s?|jujen[oa]s?|"
    r"pampean[oa]s?|riojan[oa]s?|mendocin[oa]s?|misioner[oa]s?|neuquin[oa]s?|"
    r"rionegrin[oa]s?|salten[oa]s?|sanjuanin[oa]s?|sanluisen[oa]s?|puntan[oa]s?|"
    r"santacrucen[oa]s?|santafe[cs]in[oa]s?|santiaguen[oa]s?|fueguin[oa]s?|"
    r"tucuman[oa]s?|marplatenses?|rosarin[oa]s?|platenses?|bahienses?"
)
ARG = rf"(argentin\w+|albiceleste|los pumas|las leonas|los gladiadores|las panteras|los murcielagos|{PROV})"
# Adjetivos de nivel (mundial y continental). El alcance se decide después con detect_scope.
CONT_ADJ = r"(sudamerican\w+|panamerican\w+|latinoamerican\w+|iberoamerican\w+|continental\w*)"
WORLD = (
    r"(mundial\w*|del mundo|world|olimpiada internacional|olimpiada iberoamericana|"
    r"international olympiad|planetari\w+|sudamerican\w+|panamerican\w+|latinoamerican\w+|"
    r"iberoamerican\w+|continental\w*|copa america|de america)"
)
# Adjetivo pegado a "campeón/subcampeón": campeón mundial, campeona sudamericana…
ADJ = (
    r"(mundial|del mundo|sudamerican[oa]|panamerican[oa]|latinoamerican[oa]|"
    r"iberoamerican[oa]|continental|de america)"
)
CONT_RE = re.compile(CONT_ADJ + r"|copa america|de america")
MUNDIAL_RE = re.compile(r"mundial\w*|del mundo|world|olimpiada internacional|international olympiad|planetari\w+")

# Verbos de logro (conjugaciones frecuentes en titulares)
AV = r"(se consagr\w+|se coron\w+|se proclam\w+|conquist\w+|gan(?:o|aron)|logr(?:o|aron)|obtuv(?:o|ieron)|consigui(?:o|eron)|se qued(?:o|aron) con|se llev(?:o|aron)|arrebat\w+|recuper(?:o|aron))"
# "campeon" sin que sea sub/vice campeón
CHAMP = r"(?<!sub)(?<!vice)(?<!sub )(?<!vice )campeon"

# (regex, medalla) — patrones de PODIO logrado (no futuro). El ORDEN importa:
# plata/bronce primero para que "subcampeón" no matchee como "campeón".
PODIUM_PATTERNS = [
    # --- plata ---
    (rf"(sub ?campeon\w*|vice ?campeon\w*).{{0,45}}{WORLD}", "plata"),
    (rf"{WORLD}.{{0,35}}(sub ?campeon\w*|vice ?campeon\w*)", "plata"),
    (rf"runner.?up.{{0,40}}world", "plata"),
    (rf"(segundo puesto|segundo lugar|segunda posicion).{{0,50}}{WORLD}", "plata"),
    (rf"medalla\w* de plata.{{0,60}}{WORLD}", "plata"),
    (rf"{AV} la plata.{{0,60}}{WORLD}", "plata"),
    (rf"\bla plata ({ADJ}|en el {WORLD}|del {WORLD})", "plata"),
    # --- bronce ---
    (rf"(tercer puesto|tercer lugar|tercera posicion).{{0,50}}{WORLD}", "bronce"),
    (rf"{WORLD}.{{0,40}}(tercer puesto|tercer lugar)", "bronce"),
    (rf"medalla\w* de bronce.{{0,60}}{WORLD}", "bronce"),
    (rf"{AV} el bronce.{{0,60}}{WORLD}", "bronce"),
    (rf"\bel bronce ({ADJ}|en el {WORLD}|del {WORLD})", "bronce"),
    (rf"bronce para .{{0,40}}{WORLD}", "bronce"),
    # --- oro ---
    (rf"{CHAMP}\w* {ADJ}", "oro"),
    (rf"{CHAMP}\w* de (la |el |los |las )?(copa )?(america|sudamerica|panamerica)\b", "oro"),
    (rf"(sub ?campeon\w*|vice ?campeon\w*) de (la |el )?(copa )?america\b", "plata"),
    (rf"{CHAMP}\w* .{{0,30}}\b{ADJ}\b", "oro"),
    (rf"{AV}(?:(?!final|semifinal).){{0,45}}(el )?(titulo )?{WORLD}", "oro"),
    (rf"{AV} el (titulo|campeonato|mundial)", "oro"),
    (rf"(?<!final del ){WORLD}(?:(?!final).){{0,45}}(se consagr\w+|se coron\w+|conquist\w+|gan(?:o|aron)|{CHAMP}\w*)", "oro"),
    (rf"medalla\w* de oro.{{0,60}}{WORLD}", "oro"),
    (rf"{AV} el oro.{{0,60}}{WORLD}", "oro"),
    (rf"\bel oro ({ADJ}|en el {WORLD}|del {WORLD})", "oro"),
    # Consagración por superlativo, sin verbo de logro: "El mejor alfajor del mundo
    # es argentino" / "elegida la mejor del mundo" (el Mundial del Alfajor nos enseñó)
    (rf"\b(el |la )mejor .{{0,30}}del mundo (es|de)\b", "oro"),
    (rf"(elegid\w+|premiad\w+|coronad\w+|consagrad\w+) como (el |la )?mejor .{{0,30}}del mundo", "oro"),
    (rf"world champion", "oro"),
    (rf"(wins?|won|clinch\w*|crowned|captur\w*|claim\w*|tak\w*|took).{{0,40}}world (title|championship|cup|crown)", "oro"),
    (rf"world (title|championship|cup).{{0,30}}(win|won|victory|champion)", "oro"),
    # --- genéricos ---
    (rf"{WORLD}.{{0,50}}medalla\w* de (oro|plata|bronce)", "medalla"),
    (rf"medalla\w* (de (oro|plata|bronce) )?en la olimpiada", "medalla"),
    (rf"(gold|silver|bronze) (medal )?at .{{0,30}}world", "medalla"),
    (rf"(subio al|se subio al|se subieron al) podio.{{0,45}}{WORLD}", "podio"),
    (rf"{WORLD}.{{0,35}}podio para", "podio"),
]
PODIUM_RE = [(re.compile(p), m) for p, m in PODIUM_PATTERNS]
ARG_RE = re.compile(rf"\b{ARG}\b")

# Países/gentilicios NO argentinos (para atribuir el logro al sujeto correcto)
FOREIGN = (
    r"(espan\w+|spain|spanish|la roja|la furia|francia|frances\w*|france|french|"
    r"inglaterra|ingles\w*|england|english|brit\w+|alem\w+|german\w+|brasil\w+|brazil\w+|"
    r"ital\w+|urugua\w+|chile\b|chilen\w+|mexic\w+|colombi\w+|portug\w+|"
    r"holand\w+|neerland\w+|paises bajos|dutch|croa\w+|japon\w+|japan\w*|"
    r"china\b|chin[oa]s?\b|marroqu\w+|marruecos|estados unidos|estadounid\w+|eeuu|"
    r"belg\w+|suiz\w+|suec\w+|norueg\w+|danes\w+|dinamarca|polac\w+|polonia|"
    r"austral\w+|canad\w+|kenia\w*|etiop\w+|jamaiq\w+|jamaica|cuban\w+|cuba\b|"
    r"venezol\w+|venezuela|chec\w+|rus\w+|ucrani\w+|serbi\w+|griego\w*|grecia|"
    r"turc\w+|turquia|irani\w+|iran\b|indi[oa]s?\b|india\b|corean\w+|corea\b|"
    r"sudafric\w+|south africa|springboks|nueva zelanda|neozeland\w+|all blacks|"
    r"fiji\w*|tonga\w*|samoa\w*|gales\b|escoc\w+|irland\w+)"
)
FOREIGN_RE = re.compile(rf"\b{FOREIGN}\b")

# Verbos de derrota aplicados a Argentina ("España venció a Argentina...")
DEFEAT_RE = re.compile(
    r"\b(vencio|vence|derrot\w+|super(?:o|a)|elimin(?:o|a)|tumb(?:o|a)|destron(?:o|a)|"
    r"aplast(?:o|a)|arras(?:o|a)|domin(?:o|a)|derrib(?:o|a)|acab(?:o|a) con|le gano|gano|"
    r"se impuso a|remont(?:o|a)|arrebat\w+|romp\w+|beat|beats|defeated|edged?|downed|topped)"
    r".{0,34}argentin"
)
# Argentina como rival vencido: "triunfo/victoria/final/gritó ANTE Argentina", "España 1-0 Argentina"
ANTE_RE = re.compile(r"\bante (la |una )?(seleccion |el seleccionado )?argentin")
SCORE_RE = re.compile(rf"\b{FOREIGN}\b[^a-z]{{0,4}}\d+ \d+[^a-z]{{0,4}}argentin")
# Argentina como perdedor / no ganador
ARG_LOST_RE = re.compile(
    r"\bargentin\w+\b.{0,60}\b(cayo|perdio|no pudo|no le alcanzo|no logro|fue superad\w+|"
    r"fue derrotad\w+|se vacio|dejo todo|dejo el alma|dio pelea|quedo eliminad\w+|se despidio)\b"
    r"|\b(cayo|perdio|no pudo) (la |una )?(seleccion |el seleccionado )?argentin"
)
# Rescates: el logro argentino real dentro de una noticia de derrota (subcampeonato/bronce)
SILVER_RESCUE_RE = re.compile(
    r"argentin\w+[^.]{0,30}\bes (un enorme |una enorme )?subcampeon"
    r"|argentin\w+[^.]{0,25}\bsubcampeon"
    r"|subcampeon\w*[:,]? a? ?(la |el )?(seleccion |seleccionado )?argentin"
    r"|subcampeonato\w*.{0,30}argentin|argentin\w+.{0,30}subcampeonato"
)
BRONZE_RESCUE_RE = re.compile(
    r"(bronce|tercer puesto|tercer lugar) (mundial\w* )?para (la |el |los |las )?argentin"
    r"|argentin\w+.{0,25}se qued\w+ con el (bronce|tercer puesto)"
)
# Años pasados junto a frases de campeonato = nota histórica/comparativa.
# "Viejo" = anterior al año en curso, calculado en runtime: así "2026" pasa a ser
# viejo solo el 1/1/2027, sin tocar código cada enero.
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
CURRENT_YEAR = datetime.now(timezone.utc).year

def has_old_year(nt: str) -> bool:
    return any(int(y) < CURRENT_YEAR for y in YEAR_RE.findall(nt))

# Rechazo duro: previa / futuro / historia / ruido / declaraciones / mercado de pases
HARD_EXCLUDE = [
    r"\b(buscara|buscaran|ira por|iran por|va por|van por|va en busca|suena con|suenan con|aspira|quiere ser|puede\w*|pueden|podria\w*|podrian|podra|podran|intentara|jugara|jugaran|enfrentara|enfrentaran|enfrentarse|se mide|se miden|se enfrenta|chocara|viajara\w*|para ser campeon)\b",
    r"\bsi (la seleccion|argentina|sale|gana|es|se consagra)\b",
    r"\b(donde ver|como ver|a que hora|hora y tv|en vivo|en directo|minuto a minuto|formaciones|posibles formaciones|fixture|calendario|sorteo|entradas|amistoso\w*)\b",
    r"\b(previa|palpita|antesala|expectativa por|se prepara|se alista|rumbo al|de cara al|clasifico|clasificaron|clasifica)\b",
    r"\b(desafi\w+|hay que saber|todo lo que|partido (vs|ante|contra)|reciben? a|visitan? a)\b",
    r"\ba l[oa]s campeon\w+ del mundo\b",
    r"\b(prelista|pre lista|lista de (convocados|buena fe)|convocad\w+|convocatoria\w*|citad\w+|nomina|prenomina|plantel|refuerzo\w*|se suma\w*|se incorpora\w*|para el mundial|de cara al mundial)\b",
    r"\b(se corre|se juega|se disputa\w*|se celebrara|se realizara|sera sede|defin\w+|disputa\w*|entregar\w+)\b",
    r"\b(a \d+ anos|anos despues|anos mas tarde|aniversario|efemerides|se cumplen|recuerd\w+|recordo|homenaje\w*|murio|fallecio|fallecimiento|adios a|luto|la historia|historico rival|palmares|listado|lista de|record\w*|vigente|defensor\w* del titulo)\b",
    r"\b(apuest\w+|apost\w+|cuotas|pronostic\w*|prediccion\w*|predijo|tarot\w*|vidente|supercomputadora|simulador|videojuego|fifa \d+|quiniela|influencer\w*|donacion\w*|salia campeon)\b",
    r"\b(mercado|habria\w*|sacudiria|cerraria|firmaria|llegaria|pagaria|estaria|iria|wwe|lucha libre)\b",
    r"\b(ranking|encuesta|segun la ia|inteligencia artificial elige|los mejores de la historia)\b",
    r"\b(semifinal\w*|cuartos de final|octavos de final|fase de grupos|debut\w*)\b",
    r"\b(horoscopo|receta|estreno|serie|pelicula|documental|trailer)\b",
    r"\b(visito|visita a|de visita|fue recibido|recibio (a|en)|agasaj\w+|caravana|desfil\w+|festej\w+|celebr\w+|multitud|recibimiento|regres\w+|llegada|arribo|ezeiza|hinchas|aficionados)\b",
    r"\b(dijo|aseguro|asegura|afirmo|opino|hablo|revelo|conto|confeso|critico|cuestiono|liquido|elogio|felicit\w+|lament\w+|carta|mensaje|palabras|reaccion\w*)\b",
    r"\b(la prensa|los medios|las tapas|portada\w*|reflejaron|titularon|se rinde|asi titularon)\b",
    r"\b(resumen|highlights|los goles|gol y resumen|repaso|en fotos|informe|analisis|cronica|memes?|broma|viral\w*|se viraliza|(hizo|hace) creer|engan\w+)\b",
    r"\b(cuant\w+|millonari\w+|dinero|premio economico|recaud\w+|negocio\w*|ventas|audiencia|rating|millones en tv|sponsor\w*|adidas|nike|gin|whisky|cerveza|vodka|bebida\w*)\b",
    r"\b(sueno|maldicion|ilusion\w*|esperanza\w*|dolor|promesa|se erige|por que\b)\b",
    r"\b(milei|trump|infantino|papa|presidente)\b",
    r"\b(oferta|traspaso|fichaje|fichar\w*|se iria|se va al?|prestamo|recalar|mudarse|cambiara de equipo|busca\b|pretende|contratar\w*|amenaza\w*|amenazado)\b",
    r"\b(un campeon|una campeona|el campeon del mundo que|ex campeon|excampeon)\b",
    r"\bcampeon\w* del mundo (con (la seleccion|el seleccionado|argentina|alemania|espana|francia|italia|brasil|uruguay|inglaterra)|en \d{4})\b",
    r"^asi\b",
    # (los continentales ya NO se excluyen: entran como scope=continental)
    r"\b(torneo (local|nacional|federal|regional)|liga argentina|campeonato argentino|copa argentina|primera division)\b",
]
HARD_RE = [re.compile(p) for p in HARD_EXCLUDE]

# Rechazo blando: si matchea, el evento queda con confianza media (página sí, email no)
SOFT_EXCLUDE = [
    r"\b(serian|seria|casi|cerca de|a un paso)\b",
    r"\b(leyenda|retiro|se retira)\b",
    r"\b(juvenil|sub ?\d+|cadete|mundialista)\b",  # títulos juveniles / gentilicio ambiguo
]
SOFT_RE = [re.compile(p) for p in SOFT_EXCLUDE]

# ----------------------------------------------------------------- fetch ----

def fetch_rss(query: str, days: int):
    q = f"{query} when:{days}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "es-419", "gl": "AR", "ceid": "AR:es-419"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def parse_items(xml_bytes: bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        def txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = txt("title")
        link = txt("link")
        pub = txt("pubDate")
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        desc = txt("description")
        desc = re.sub(r"<[^>]+>", " ", desc)
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        items.append({"title": title, "link": link, "source": source, "desc": desc, "dt": dt})
    return items

# ---------------------------------------------------------------- classify --

def detect_scope(title: str) -> str:
    """mundial | continental — según qué nivel nombra el titular (mundial gana si aparecen ambos)."""
    nt = norm(strip_source(title))
    if MUNDIAL_RE.search(nt):
        return "mundial"
    if CONT_RE.search(nt):
        return "continental"
    return "mundial"


def classify(title: str, desc: str):
    """Devuelve (verdict, medal, reasons). verdict: accept | soft | reject"""
    # Titulares interrogativos nunca son confirmaciones ("¿Argentina campeón?")
    if "?" in title or "¿" in title:
        return "reject", None, ["interrogative-title"]
    # Clasificar SIN el nombre del medio: "Reconquista Radios" no es una conquista
    # y "El Futbolero Argentina" no convierte una noticia en argentina.
    nt = norm(strip_source(title))
    nd = norm(desc)[:400]
    reasons = []

    medal, span = None, None
    for rx, m in PODIUM_RE:
        mt = rx.search(nt)
        if mt:
            medal, span = m, mt.span()
            reasons.append(f"podium:title:{rx.pattern[:40]}")
            break
    title_hit = medal is not None
    if not medal:
        for rx, m in PODIUM_RE:
            if rx.search(nd):
                medal = m
                reasons.append(f"podium:desc:{rx.pattern[:40]}")
                break
    if not medal:
        return "reject", None, ["no-podium-pattern"]

    if not (ARG_RE.search(nt) or ARG_RE.search(nd)):
        return "reject", None, ["no-arg-marker"]

    # Nota histórica/comparativa: año pasado en el título junto a frase de podio
    if title_hit and has_old_year(nt):
        return "reject", None, ["old-year"]

    for rx in HARD_RE:
        if rx.search(nt):
            return "reject", None, [f"hard:{rx.pattern[:50]}"]

    # ---- Atribución del logro: ¿el campeón es Argentina u otro país? ----
    if title_hit:
        ps, pe = span
        # el marcador argentino debe estar razonablemente cerca de la frase de podio
        arg_spans = [m.span() for m in ARG_RE.finditer(nt)]
        near = any(s <= pe + 45 and e >= ps - 70 for s, e in arg_spans)
        if not near:
            return "reject", None, ["arg-far-from-podium"]

        # sujeto extranjero más pegado a la frase de campeonato que el argentino
        f_before = [m.end() for m in FOREIGN_RE.finditer(nt) if m.end() <= ps + 12]
        a_before = [e for s, e in arg_spans if e <= ps + 12]
        veto_a = bool(f_before) and (not a_before or max(f_before) > max(a_before))
        veto_b = bool(DEFEAT_RE.search(nt) or ANTE_RE.search(nt) or SCORE_RE.search(nt))
        veto_c = bool(ARG_LOST_RE.search(nt))
        if veto_a or veto_b or veto_c:
            if SILVER_RESCUE_RE.search(nt):
                medal, reasons = "plata", reasons + ["rescued:subcampeonato-argentino"]
            elif BRONZE_RESCUE_RE.search(nt):
                medal, reasons = "bronce", reasons + ["rescued:bronce-argentino"]
            else:
                which = "foreign-subject" if veto_a else ("defeated-argentina" if veto_b else "argentina-lost")
                return "reject", None, [which]

    soft = [rx.pattern[:40] for rx in SOFT_RE if rx.search(nt)]
    if soft:
        return "soft", medal, [f"soft:{s}" for s in soft]
    if not title_hit:
        return "soft", medal, reasons + ["podium-only-in-desc"]
    return "accept", medal, reasons

# ------------------------------------------------------------------ dedup ---

# Tokens presentes en casi cualquier candidato: no aportan para distinguir eventos
GENERIC_TOKENS = {
    "argentina", "argentino", "argentinos", "argentinas", "seleccion", "seleccionado",
    "mundial", "mundo", "campeon", "campeona", "campeones", "campeonas", "campeonato",
    "subcampeon", "subcampeona", "subcampeones", "subcampeonato", "consagro", "corono",
    "titulo", "final", "copa", "medalla", "world", "champion", "historico", "historica",
    # vocabulario continental: también es genérico, no distingue eventos
    "sudamericano", "sudamericana", "sudamericanos", "sudamericanas", "sudamericano2026",
    "panamericano", "panamericana", "panamericanos", "panamericanas", "america",
    "americano", "americana", "continental", "bicampeon", "bicampeona", "bicampeones",
    "tricampeon", "tricampeona", "consagra", "consagraron", "proclamo", "venciendo",
    # palabras muleto de celebración: aparecen en cualquier titular y arman puentes
    # falsos entre eventos distintos ("hizo historia" enterró al fútbol phygital
    # contra una nota vieja de hockey)
    "hizo", "hicieron", "historia", "triunfo", "triunfazo", "epico", "epica",
    "orgullo", "gano", "ganaron", "vencio", "vencieron", "derroto", "derrotaron",
    "gloria", "hazana", "primera", "primer", "primeros", "otra", "otro",
    "nuevo", "nueva", "mejor", "mejores",
}

# La disciplina es la huella más fuerte: dos podios de deportes distintos nunca son el mismo evento.
DISCIPLINAS = {
    "voley": {"voley", "voleibol", "volei", "volleyball"},
    "basquet": {"basquet", "basquetbol", "baloncesto", "basketball"},
    "hockey": {"hockey"},
    "futsal": {"futsal"},
    "futbol": {"futbol"},
    "rugby": {"rugby", "pumas"},
    "handball": {"handball", "balonmano", "gladiadores"},
    "judo": {"judo", "judoca"},
    "ajedrez": {"ajedrez"},
    "tenis": {"tenis"},
    "natacion": {"natacion"},
    "atletismo": {"atletismo"},
    "quimica": {"quimica"},
    "matematica": {"matematica"},
    "patin": {"patin", "patinaje"},
    "pelota": {"paleta", "pelota"},
    "justdance": {"dance"},
}

def disciplina(tokens: set):
    for nombre, palabras in DISCIPLINAS.items():
        if tokens & palabras:
            return nombre
    return None

def core_tokens(s: str):
    return {t for t in sig_tokens(s) if t not in GENERIC_TOKENS}

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def same_event(tokens_a: set, medal_a: str, tokens_b: set, medal_b: str) -> bool:
    """Dos titulares hablan del mismo evento si comparten medalla y suficiente léxico distintivo."""
    if medal_a != medal_b and "podio" not in (medal_a, medal_b) and "medalla" not in (medal_a, medal_b):
        return False
    # números de categoría distintos = eventos distintos (+45 IMC vs +50, sub 17 vs sub 20)
    nums_a = {t for t in tokens_a if t.isdigit()}
    nums_b = {t for t in tokens_b if t.isdigit()}
    if nums_a and nums_b and not (nums_a & nums_b):
        return False
    # disciplinas distintas = eventos distintos (vóley ≠ básquet), aunque compartan palabras
    da, db = disciplina(tokens_a), disciplina(tokens_b)
    if da and db and da != db:
        return False
    if jaccard(tokens_a, tokens_b) >= 0.3:
        return True
    # títulos cortos: exigir al menos 2 palabras distintivas en común (antes bastaba 1)
    return len(tokens_a & tokens_b) >= 2 and (len(tokens_a) <= 6 or len(tokens_b) <= 6)

def event_id(title: str) -> str:
    return hashlib.sha1(" ".join(sorted(sig_tokens(title))).encode()).hexdigest()[:12]

# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()
    days = args.days or (30 if args.dry_run else 3)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    raw = []
    for q in QUERIES:
        try:
            items = parse_items(fetch_rss(q, days))
            raw.extend(items)
            print(f"[q] {q!r}: {len(items)} items", file=sys.stderr)
        except Exception as e:
            print(f"[q] {q!r}: ERROR {e}", file=sys.stderr)
        time.sleep(1.2)

    # de-dup exacto por título normalizado
    by_title = {}
    for it in raw:
        key = norm(it["title"])
        if key and key not in by_title:
            by_title[key] = it
    print(f"[i] {len(raw)} items, {len(by_title)} únicos", file=sys.stderr)

    candidates = []
    for it in by_title.values():
        # sin fecha parseable → afuera (antes pasaba de largo el filtro de recencia)
        if it["dt"] is None or it["dt"] < cutoff:
            continue
        if norm(it.get("source", "")) in BLOCKED_SOURCES:
            continue
        verdict, medal, reasons = classify(it["title"], it["desc"])
        if verdict == "reject" and reasons == ["no-podium-pattern"]:
            continue  # ni vale la pena listarlo
        candidates.append({
            "title": it["title"], "source": it["source"], "url": it["link"],
            "date": (it["dt"] or now).date().isoformat(),
            "verdict": verdict, "medal": medal, "reasons": reasons,
        })

    if args.dry_run:
        candidates.sort(key=lambda c: (c["verdict"] != "accept", c["date"]), reverse=False)
        with open(CANDIDATES_PATH, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)
        acc = sum(1 for c in candidates if c["verdict"] == "accept")
        soft = sum(1 for c in candidates if c["verdict"] == "soft")
        rej = sum(1 for c in candidates if c["verdict"] == "reject")
        print(f"[dry-run] accept={acc} soft={soft} reject(listado)={rej} → candidates.json")
        return

    # ---- modo normal: estado + salida ----
    seen = {}
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, encoding="utf-8") as f:
            seen = json.load(f)
    podios = []
    if os.path.exists(PODIOS_PATH):
        with open(PODIOS_PATH, encoding="utf-8") as f:
            podios = json.load(f)

    recent_seen = {
        k: v for k, v in seen.items()
        if v.get("date", "1970-01-01") >= (now - timedelta(days=21)).date().isoformat()
    }
    # Solo anclan el dedup las notas CON JUICIO DE LA IA: eventos publicados o
    # mergeados (llm_merged). Ni las rechazadas (una previa rechazada enterraría
    # el resultado real: "Los Pumas desafían…" → "Los Pumas vencieron…"), ni las
    # dup_of (nunca pasaron por la IA: una nota de fase de grupos de Las Leonas,
    # encadenada como dup, compartía "leonas"+"hockey" con la futura final y la
    # habría enterrado — la misma mecánica que enterró al phygital). El costo es
    # que algún candidato más llega a la IA, que viaja en la misma llamada única.
    recent_events = [
        (k, set(v.get("core", v.get("tokens", []))), v.get("medal", "podio"))
        for k, v in recent_seen.items()
        if "llm_rejected" not in v and "dup_of" not in v
    ]

    accepted = sorted([c for c in candidates if c["verdict"] == "accept"], key=lambda c: (c["date"], len(c["title"])))
    new_events = []
    for c in accepted:
        core = core_tokens(c["title"])
        eid = event_id(c["title"])
        if eid in seen:
            continue
        medal = c["medal"] or "podio"
        # ¿ya cubierto en corridas anteriores?
        dup_of = next((k for k, ts, m in recent_events if same_event(core, medal, ts, m)), None)
        if dup_of:
            seen[eid] = {"date": c["date"], "core": sorted(core), "medal": medal, "dup_of": dup_of, "title": c["title"]}
            continue
        # dedup entre los nuevos de esta corrida (gana el titular más corto/limpio)
        twin = next((ev for ev in new_events if same_event(core, medal, set(ev["_core"]), ev["medal"])), None)
        if twin:
            seen[eid] = {"date": c["date"], "core": sorted(core), "medal": medal, "dup_of": twin["id"], "title": c["title"]}
            twin["_core"] = sorted(set(twin["_core"]) | core)  # enriquecer el cluster
            continue
        ev = {
            "id": eid, "date": c["date"], "title": strip_source(c["title"]), "source": c["source"],
            "url": c["url"], "medal": medal, "scope": detect_scope(c["title"]), "_core": sorted(core),
        }
        new_events.append(ev)
        seen[eid] = {"date": c["date"], "core": sorted(core), "medal": medal, "title": c["title"]}

    for ev in new_events:
        recent_events.append((ev["id"], set(ev.pop("_core")), ev["medal"]))
    if new_events:
        podios = new_events + podios
        with open(PODIOS_PATH, "w", encoding="utf-8") as f:
            json.dump(podios, f, ensure_ascii=False, indent=2)
        with open(NEW_EVENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(new_events, f, ensure_ascii=False, indent=2)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)

    print(f"[done] nuevos eventos: {len(new_events)}")
    for ev in new_events:
        print(f"  🏅 {ev['medal']}: {ev['title']}")

if __name__ == "__main__":
    main()
