#!/usr/bin/env python3
"""
Envía el email diario vía Buttondown SOLO si scrape.py dejó new_events.json.
Env:
  BUTTONDOWN_API_KEY  (requerido)
  DRY_RUN=1           crea el email como borrador (no envía)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_EVENTS_PATH = os.path.join(ROOT, "new_events.json")
API = "https://api.buttondown.com/v1/emails"

MEDAL_EMOJI = {"oro": "🥇", "plata": "🥈", "bronce": "🥉", "medalla": "🏅", "podio": "🏅"}

SITIO = "https://otracoronacion.github.io/"
CANAL = "https://whatsapp.com/channel/0029Vb85r2RDZ4Lb3Qsnkq0P"


def mensaje_whatsapp(events):
    """El texto que sale cuando alguien reenvía la coronación.

    Dos links y en este orden: el permalink de la coronación primero (WhatsApp
    le arma la vista previa al PRIMER link del mensaje) y el canal después,
    como invitación aparte. El permalink va a nuestra página, no al diario: la
    nota queda a un toque igual, pero el que llega ve el resto de las
    coronaciones y el botón del canal. Y la URL de la nota viene de Google News
    y mide hasta 650 caracteres — arruinaría el mensaje.
    """
    def linea(ev):
        return f"{MEDAL_EMOJI.get(ev['medal'], '🏅')} {ev['title']}\n{SITIO}#e-{ev['id']}"

    if len(events) == 1:
        cuerpo = f"{MEDAL_EMOJI.get(events[0]['medal'], '🏅')} ¡Otra coronación de gloria!\n\n" \
                 f"{events[0]['title']}\n{SITIO}#e-{events[0]['id']}"
    else:
        # Tope de 3: más links seguidos y el mensaje se lee como spam.
        cuerpo = f"🇦🇷 Hoy Argentina se coronó {len(events)} veces:\n\n" + \
                 "\n\n".join(linea(ev) for ev in events[:3])
        if len(events) > 3:
            cuerpo += f"\n\n…y {len(events) - 3} más en {SITIO}"
    return f"{cuerpo}\n\n📲 Enterate de todas, apenas pasan:\n{CANAL}"


def main():
    if not os.path.exists(NEW_EVENTS_PATH):
        print("No hay eventos nuevos. Hoy no se envía nada. 🧉")
        return
    key = os.environ.get("BUTTONDOWN_API_KEY")
    if not key:
        print("FALTA BUTTONDOWN_API_KEY", file=sys.stderr)
        sys.exit(1)

    with open(NEW_EVENTS_PATH, encoding="utf-8") as f:
        events = json.load(f)
    if not events:
        print("new_events.json vacío; no se envía.")
        return

    if len(events) == 1:
        ev = events[0]
        subject = f"{MEDAL_EMOJI.get(ev['medal'], '🏅')} Otra coronación de gloria"
    else:
        subject = f"🇦🇷 {len(events)} coronaciones de gloria hoy"

    # "del mundo" / "de América" / mixto, según el alcance de los logros del día
    scopes = {ev.get("scope", "mundial") for ev in events}
    donde = ("del mundo" if scopes == {"mundial"}
             else "de América" if scopes == {"continental"}
             else "del mundo y de América")
    lines = [f"**¡Buen día! Hoy te despertás coronado: Argentina al podio {donde}.**", ""]
    for ev in events:
        emoji = MEDAL_EMOJI.get(ev["medal"], "🏅")
        src = f" — _{ev['source']}_" if ev.get("source") else ""
        lines.append(f"{emoji} **[{ev['title']}]({ev['url']})**{src}")
        lines.append("")
    wa_link = "https://wa.me/?text=" + urllib.parse.quote(mensaje_whatsapp(events))

    lines += [
        f"📲 **[Pasala al grupo — compartir por WhatsApp]({wa_link})**",
        "",
        "---",
        "",
        "Ver todas las coronaciones: https://otracoronacion.github.io/",
        "Seguinos también en WhatsApp: https://whatsapp.com/channel/0029Vb85r2RDZ4Lb3Qsnkq0P",
        "",
        "_Recibís este correo porque te suscribiste a **Otra Coronación de Gloria**, el rastreador de argentinos en podios mundiales y continentales._",
    ]
    body = "\n".join(lines)

    payload = {"subject": subject, "body": body}
    payload["status"] = "draft" if os.environ.get("DRY_RUN") == "1" else "about_to_send"

    def post(p):
        req = urllib.request.Request(
            API,
            data=json.dumps(p).encode(),
            headers={
                "Authorization": f"Token {key}",
                "Content-Type": "application/json",
                # Requerido por Buttondown para envíos reales por API (anti-disparo accidental)
                "X-Buttondown-Live-Dangerously": "true",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    # Reintentos con espera para errores transitorios (5xx / 429)
    last_code, last_body = None, ""
    for attempt in range(1, 4):
        try:
            resp = post(payload)
            print(f"Email {'BORRADOR creado' if payload['status']=='draft' else 'ENVIADO'}: {resp.get('id')} — {subject}")
            return
        except urllib.error.HTTPError as e:
            last_code, last_body = e.code, e.read().decode()[:800]
            print(f"[intento {attempt}/3] Buttondown error {last_code}: {last_body[:300]}", file=sys.stderr)
            if last_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(25 * attempt)
                continue
            break
        except Exception as e:
            last_code, last_body = "conn", str(e)[:300]
            print(f"[intento {attempt}/3] error de conexión: {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(25 * attempt)

    # Falló definitivo: preservar el error y dejar el contenido como BORRADOR para no perder la coronación
    from datetime import datetime, timezone
    err = {
        "when": datetime.now(timezone.utc).isoformat(),
        "http": last_code,
        "body": last_body,
        "subject": subject,
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "last_email_error.json"), "w", encoding="utf-8") as f:
        json.dump(err, f, ensure_ascii=False, indent=2)
    try:
        resp = post({**payload, "status": "draft"})
        print(f"Fallback: BORRADOR creado ({resp.get('id')}) — mandalo a mano desde Buttondown.", file=sys.stderr)
    except Exception as e:
        print(f"Fallback a borrador también falló: {e}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
