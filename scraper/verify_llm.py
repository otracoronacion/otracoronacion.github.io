#!/usr/bin/env python3
"""
Verificador con IA: revisa EN CONJUNTO los candidatos que pasaron el prefiltro de
regex, antes de publicar/enviar. Una sola llamada con todos los titulares del día:
así puede (a) descartar los que no son coronaciones y (b) agrupar las notas que
hablan del mismo logro, usando unos titulares para desambiguar otros.

- Sin ANTHROPIC_API_KEY: no hace nada (el pipeline sigue como siempre).
- Rechazado o agrupado: se saca de new_events.json y de data/podios.json,
  y queda anotado en data/seen.json (llm_rejected / llm_merged).
- Si la API falla definitivamente: NO rechaza nada; crea el flag `llm_failed`
  → el email sale como BORRADOR y el tweet se saltea (modo conservador).

Uso:
  python3 scraper/verify_llm.py          # modo pipeline
  python3 scraper/verify_llm.py --test   # golden set, no toca archivos
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_EVENTS = os.path.join(ROOT, "new_events.json")
PODIOS = os.path.join(ROOT, "data", "podios.json")
SEEN = os.path.join(ROOT, "data", "seen.json")
FLAG_FAILED = os.path.join(ROOT, "llm_failed")
API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"

SYSTEM = """Sos el verificador de "Otra Coronación de Gloria", un servicio que informa podios YA OBTENIDOS por argentinos en competencias de nivel MUNDIAL o CONTINENTAL.

Recibís una lista numerada de titulares de noticias del día. Devolvé ÚNICAMENTE un array JSON, un objeto por titular, sin texto alrededor:
[{"i": 1, "ok": true|false, "medalla": "oro"|"plata"|"bronce"|null, "alcance": "mundial"|"continental"|null, "grupo": <entero>, "motivo": "<frase corta>"}]

## ok = true
Solo si el titular informa un HECHO CONSUMADO: una persona, equipo o selección ARGENTINA obtuvo el 1°, 2° o 3° puesto en un campeonato de nivel:
- MUNDIAL: mundiales, copas del mundo, olimpiadas internacionales.
- CONTINENTAL: sudamericanos, panamericanos, Copa América, campeonatos de América/iberoamericanos, DONDE COMPITEN SELECCIONES NACIONALES o deportistas representando al país.

Los campeonatos de categoría cuentan: juveniles (sub 17/20), masters (+40, +50), por género, por peso, por especialidad, y disciplinas no deportivas (matemática, química, asado, tango, peluquería).

OJO con "Copa Sudamericana": en vóley, básquet, handball y la mayoría de los deportes es el torneo de SELECCIONES (válido). Solo en FÚTBOL es un torneo de clubes (no válido, igual que Libertadores o Recopa).

## ok = false
- previa o partido futuro; convocatorias, prelistas, nóminas, mercado de pases
- el campeón es de otro país, o "campeones del mundo" nombra al rival
- Argentina perdió SIN quedar en el podio (cayó en fase de grupos, semifinal, o en un partido suelto/test match); nota histórica, aniversario, obituario, ranking, apuestas
- entretenimiento con guión (WWE), publicidad, memes, sucesos policiales
- liga o torneo nacional/local, torneo de CLUBES de fútbol, circuito profesional semanal (ATP, Challenger, ITF)
- declaraciones, reacciones o celebraciones posteriores a un logro

## grupo
Número entero que agrupa los titulares que hablan del MISMO logro (misma disciplina, misma competencia, misma categoría), aunque uno hable de la selección y otro del deportista local. Titulares vagos ("Argentina campeona sudamericana") van al grupo del logro que mejor encaje según el resto de la lista. Distintos logros = distinto número. A los ok=false ponéles grupo 0.

OJO: perder la FINAL de un mundial o continental SÍ es un podio → ok=true, medalla "plata" (subcampeonato). Perder el partido por el 3er puesto no da medalla; ganarlo sí ("bronce").

Ante la duda sobre ok, false: el costo de un falso positivo es alto (se envía un email y un tweet)."""

GOLDEN = [
    ("Messi y Prestianni en prelista de campeón Argentina para el Mundial", "La Propuesta Digital", False),
    ("Los Pumas desafían a los campeones del mundo en Vélez: todo lo que hay que saber del partido vs Sudáfrica", "DeporTV", False),
    ("España venció a Argentina y se consagró campeón del Mundial 2026", "La 100", False),
    ("La WWE vuelve a la Argentina con todas sus figuras y los campeones mundiales", "DeporTV", False),
    ("Una influencer apostó 15 millones a que Argentina salía campeón del mundo y ahora le pide donaciones", "Radio Mitre", False),
    ("Racing se consagró campeón de la Copa Sudamericana de fútbol ante el brasileño Cruzeiro", "TyC Sports", False),
    ("Boca ganó la Copa Libertadores y es el rey de América", "Olé", False),
    ("Argentina campeón mundial de futsal tras vencer a Brasil en la final", "Olé", True),
    ("Argentina se consagró bicampeona mundial de hockey de mayores de 50 años", "bardeo.news", True),
    ("Argentina no pudo con España y es subcampeón mundial", "Mendoza Post", True),
    ("Bronce para Argentina en la Olimpíada Internacional de Química gracias a un estudiante rosarino", "El Diario de Carlos Paz", True),
    ("Argentina se consagró campeón sudamericano de voley tras vencer a Brasil", "Diario Crónica", True),
    ("Los Pumas 7s ganaron la medalla de oro en los Juegos Panamericanos", "Olé", True),
    ("Argentina se consagró campeón de la Copa Sudamericana de vóley masculino", "La Nación", True),
]


def _post(key, payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def ask_batch(key: str, items):
    """items: [(title, source, date)] → [{ok, medalla, alcance, grupo, motivo}] alineado por índice."""
    listado = "\n".join(
        f'{n+1}. "{t}" (fuente: {s}{", " + d if d else ""})' for n, (t, s, d) in enumerate(items)
    )
    payload = {
        "model": MODEL, "max_tokens": 4000, "system": SYSTEM,
        "messages": [{"role": "user", "content": f"Titulares de hoy:\n{listado}"}],
    }
    last = None
    for attempt in range(1, 4):
        try:
            resp = _post(key, payload)
            text = "".join(b.get("text", "") for b in resp.get("content", []))
            m = re.search(r"\[.*\]", text, re.S)
            arr = json.loads(m.group(0) if m else text)
            out = [None] * len(items)
            for o in arr:
                i = int(o.get("i", 0)) - 1
                if 0 <= i < len(items):
                    out[i] = {
                        "ok": bool(o.get("ok")),
                        "medalla": o.get("medalla"),
                        "alcance": o.get("alcance"),
                        "grupo": o.get("grupo") if isinstance(o.get("grupo"), int) else 0,
                        "motivo": str(o.get("motivo", ""))[:180],
                    }
            faltan = [n for n, v in enumerate(out) if v is None]
            if faltan:
                raise ValueError(f"faltaron veredictos para {faltan}")
            return out
        except urllib.error.HTTPError as e:
            last = f"{e.code}: {e.read().decode()[:200]}"
            print(f"[intento {attempt}/3] API {last}", file=sys.stderr)
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(20 * attempt)
                continue
            break
        except Exception as e:
            last = str(e)[:200]
            print(f"[intento {attempt}/3] {last}", file=sys.stderr)
            if attempt < 3:
                time.sleep(15 * attempt)
    print(f"Verificador IA sin veredicto: {last}", file=sys.stderr)
    return None


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")

    if "--test" in sys.argv:
        if not key:
            print("FALTA ANTHROPIC_API_KEY", file=sys.stderr)
            sys.exit(1)
        res = ask_batch(key, [(t, s, "") for t, s, _ in GOLDEN])
        if not res:
            print("golden set: la API no respondió", file=sys.stderr)
            sys.exit(1)
        bad = 0
        for (t, s, want), v in zip(GOLDEN, res):
            ok = v["ok"] == want
            bad += (not ok)
            print(f"{'✓' if ok else '✗'} [{want}→{v['ok']}] {t[:66]} :: {v['motivo'][:60]}")
        with open(os.path.join(ROOT, "llm_test.json"), "w", encoding="utf-8") as f:
            json.dump([{"title": t[:80], "want": w, **v} for (t, _, w), v in zip(GOLDEN, res)], f,
                      ensure_ascii=False, indent=2)
        print(f"golden set: {len(GOLDEN)-bad}/{len(GOLDEN)}")
        sys.exit(1 if bad else 0)

    if not os.path.exists(NEW_EVENTS):
        print("Sin eventos nuevos; nada que verificar.")
        return
    if not key:
        print("Sin ANTHROPIC_API_KEY: verificador desactivado, sigue el pipeline clásico.")
        return

    with open(NEW_EVENTS, encoding="utf-8") as f:
        events = json.load(f)
    res = ask_batch(key, [(e["title"], e.get("source", ""), e.get("date", "")) for e in events])

    if res is None:  # API caída → modo conservador, no rechazamos nada
        open(FLAG_FAILED, "w").close()
        print("⚠️ Verificador sin respuesta: modo conservador (email como borrador, sin tweet).")
        return

    keep, descartes, grupos = [], [], {}
    for ev, v in zip(events, res):
        if not v["ok"]:
            print(f"✗ IA rechaza: {ev['title'][:66]} :: {v['motivo'][:70]}")
            descartes.append((ev, "llm_rejected", v["motivo"]))
            continue
        if v["medalla"] in ("oro", "plata", "bronce"):
            ev["medal"] = v["medalla"]
        if v["alcance"] in ("mundial", "continental"):
            ev["scope"] = v["alcance"]
        g = v["grupo"]
        if g and g in grupos:
            print(f"↳ IA agrupa con «{grupos[g]['title'][:45]}»: {ev['title'][:55]}")
            descartes.append((ev, "llm_merged", f"mismo logro que {grupos[g]['id']}"))
            continue
        if g:
            grupos[g] = ev
        print(f"✓ IA confirma [{ev['medal']}/{ev.get('scope','mundial')}]: {ev['title'][:62]}")
        keep.append(ev)

    if descartes:
        ids = {ev["id"] for ev, _, _ in descartes}
        podios = [e for e in json.load(open(PODIOS, encoding="utf-8")) if e["id"] not in ids]
        with open(PODIOS, "w", encoding="utf-8") as f:
            json.dump(podios, f, ensure_ascii=False, indent=2)
        seen = json.load(open(SEEN, encoding="utf-8"))
        for ev, campo, motivo in descartes:
            if ev["id"] in seen:
                seen[ev["id"]][campo] = motivo[:150]
        with open(SEEN, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=2)

    if keep:
        with open(NEW_EVENTS, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=2)
    else:
        os.remove(NEW_EVENTS)
        print("La IA no confirmó ninguna: día silencioso.")
    print(f"Verificador: {len(keep)} confirmados, "
          f"{sum(1 for _, c, _ in descartes if c == 'llm_rejected')} rechazados, "
          f"{sum(1 for _, c, _ in descartes if c == 'llm_merged')} agrupados.")


if __name__ == "__main__":
    main()
