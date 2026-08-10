#!/usr/bin/env python3
"""
Verificador con IA: reclasifica los candidatos que pasaron el prefiltro de regex,
ANTES de publicar/enviar. Lee el significado del titular, no patrones.

- Sin ANTHROPIC_API_KEY: no hace nada (el pipeline sigue como siempre).
- Evento rechazado por la IA: se saca de new_events.json y de data/podios.json,
  y queda anotado en data/seen.json (no vuelve a entrar).
- Si la API falla definitivamente: NO rechaza nada; crea el flag `llm_failed`
  → el email sale como BORRADOR y el tweet se saltea (modo conservador).

Uso:
  python3 scraper/verify_llm.py          # modo pipeline
  python3 scraper/verify_llm.py --test   # corre el golden set, no toca archivos
"""
import json
import os
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

SYSTEM = """Sos el verificador de "Otra Coronación de Gloria", un servicio que SOLO informa podios YA OBTENIDOS por argentinos en competencias de nivel MUNDIAL o CONTINENTAL.

Recibís el titular de una noticia. Respondé ÚNICAMENTE un JSON válido, sin texto extra:
{"es_coronacion": true|false, "medalla": "oro"|"plata"|"bronce"|null, "alcance": "mundial"|"continental"|null, "motivo": "<una frase corta>"}

es_coronacion = true SOLO si el titular informa un HECHO CONSUMADO: una persona, un equipo o una SELECCIÓN ARGENTINA obtuvo el 1°, 2° o 3° puesto (oro/plata/bronce, campeón/subcampeón/tercero) en un campeonato de nivel MUNDIAL o CONTINENTAL.

Cuentan como nivel válido:
- MUNDIAL: mundiales, copas del mundo, olimpiadas internacionales, campeonatos planetarios.
- CONTINENTAL: sudamericanos, panamericanos, Copa América, campeonatos de América / iberoamericanos — siempre que compitan SELECCIONES NACIONALES o deportistas representando al país.

Los campeonatos de categoría SÍ cuentan: juveniles (sub 17, sub 20), masters/veteranos (+40, +50), por género, por peso, por especialidad, y disciplinas no deportivas (matemática, química, asado, tango, peluquería).

NO cuentan como nivel válido, aunque digan "sudamericana" o "continental": torneos de CLUBES (Copa Libertadores, Copa Sudamericana de fútbol de clubes, Recopa), ligas y torneos nacionales o locales, y torneos semanales de circuito profesional (ATP, Challenger, ITF).

Además de la medalla, devolvé el nivel en "alcance": "mundial" o "continental".

es_coronacion = false si ocurre cualquiera de estas:
- previa o partido futuro ("desafía", "enfrenta", "buscará", "va por", fixture, dónde ver)
- convocatorias, prelistas, nóminas, refuerzos, mercado de pases
- el campeón es de OTRO país, o "campeones del mundo" nombra al RIVAL
- nota histórica, aniversario, obituario, ranking, encuesta, apuestas, predicciones
- entretenimiento con guión (WWE), publicidad, sorteos, memes
- torneo de nivel insuficiente: liga o torneo nacional/local, torneo de CLUBES (Libertadores, Copa Sudamericana de clubes), torneo semanal de circuito (ATP, Challenger, ITF)
- declaraciones, reacciones o celebraciones sobre un logro ya conocido

Ante la duda, false: el costo de un falso positivo es alto (se envía un email y un tweet)."""

GOLDEN = [
    ("Messi y Prestianni en prelista de campeón Argentina para el Mundial", "La Propuesta Digital", False),
    ("Los Pumas desafían a los campeones del mundo en Vélez: todo lo que hay que saber del partido vs Sudáfrica", "DeporTV", False),
    ("España venció a Argentina y se consagró campeón del Mundial 2026", "La 100", False),
    ("La WWE vuelve a la Argentina con todas sus figuras y los campeones mundiales", "DeporTV", False),
    ("Una influencer apostó 15 millones a que Argentina salía campeón del mundo y ahora le pide donaciones", "Radio Mitre", False),
    ("Argentina campeón mundial de futsal tras vencer a Brasil en la final", "Olé", True),
    ("Argentina se consagró bicampeona mundial de hockey de mayores de 50 años", "bardeo.news", True),
    ("Una correntina es campeona mundial con Argentina en hockey Máster +45 IMC", "El Libertador", True),
    ("Argentina no pudo con España y es subcampeón mundial", "Mendoza Post", True),
    ("Bronce para Argentina en la Olimpíada Internacional de Química gracias a un estudiante rosarino", "El Diario de Carlos Paz", True),
    ("Argentina se consagró campeón sudamericano de voley tras vencer a Brasil", "Diario Crónica", True),
    ("Los Pumas 7s ganaron la medalla de oro en los Juegos Panamericanos", "Olé", True),
    ("Racing se consagró campeón de la Copa Sudamericana ante el brasileño Cruzeiro", "TyC Sports", False),
    ("Boca ganó la Copa Libertadores y es el rey de América", "Olé", False),
]


def ask(key: str, title: str, source: str, date: str = ""):
    payload = {
        "model": MODEL,
        "max_tokens": 300,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": f'Titular: "{title}" | Fuente: {source} | Fecha: {date}'}],
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                resp = json.load(r)
            text = "".join(b.get("text", "") for b in resp.get("content", []))
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            v = json.loads(text)
            return {"ok": True, "es_coronacion": bool(v.get("es_coronacion")),
                    "medalla": v.get("medalla"), "alcance": v.get("alcance"),
                    "motivo": str(v.get("motivo", ""))[:200]}
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
                time.sleep(20 * attempt)
    return {"ok": False, "error": last}


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")

    if "--test" in sys.argv:
        if not key:
            print("FALTA ANTHROPIC_API_KEY", file=sys.stderr)
            sys.exit(1)
        results, bad = [], 0
        for title, source, want in GOLDEN:
            v = ask(key, title, source)
            got = v.get("es_coronacion") if v.get("ok") else None
            ok = v.get("ok") and got == want
            bad += (not ok)
            results.append({"title": title[:80], "want": want, "got": got,
                            "medalla": v.get("medalla"), "motivo": v.get("motivo", v.get("error", ""))})
            print(f"{'✓' if ok else '✗'} [{want}→{got}] {title[:70]} :: {v.get('motivo', v.get('error', ''))[:80]}")
        with open(os.path.join(ROOT, "llm_test.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
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
    keep, reject, failed = [], [], False
    for ev in events:
        v = ask(key, ev["title"], ev.get("source", ""), ev.get("date", ""))
        if not v.get("ok"):
            print(f"⚠️ API falló para: {ev['title'][:70]} — modo conservador", file=sys.stderr)
            failed = True
            keep.append(ev)  # no rechazamos sin veredicto
            continue
        if v["es_coronacion"]:
            if v.get("alcance") in ("mundial", "continental"):
                ev["scope"] = v["alcance"]
            print(f"✓ IA confirma [{v.get('medalla')} / {ev.get('scope','mundial')}]: {ev['title'][:70]}")
            keep.append(ev)
        else:
            print(f"✗ IA rechaza: {ev['title'][:70]} :: {v.get('motivo', '')[:90]}")
            reject.append((ev, v.get("motivo", "")))

    if reject:
        rejected_ids = {ev["id"] for ev, _ in reject}
        podios = [e for e in json.load(open(PODIOS, encoding="utf-8")) if e["id"] not in rejected_ids]
        with open(PODIOS, "w", encoding="utf-8") as f:
            json.dump(podios, f, ensure_ascii=False, indent=2)
        seen = json.load(open(SEEN, encoding="utf-8"))
        for ev, motivo in reject:
            if ev["id"] in seen:
                seen[ev["id"]]["llm_rejected"] = motivo[:150]
        with open(SEEN, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=2)

    if keep:
        with open(NEW_EVENTS, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=2)
    else:
        os.remove(NEW_EVENTS)
        print("La IA rechazó todo: día silencioso.")

    if failed:
        open(FLAG_FAILED, "w").close()
    print(f"Verificador: {len(keep)} confirmados, {len(reject)} rechazados{' (API con fallas: modo conservador)' if failed else ''}")


if __name__ == "__main__":
    main()
