#!/usr/bin/env python3
"""
Postea las coronaciones nuevas en X (Twitter). Sin dependencias externas:
OAuth 1.0a firmado a mano con stdlib.

Uso:
  python3 scraper/post_x.py              # postea los eventos de new_events.json
  python3 scraper/post_x.py --self-test  # postea un tweet de prueba y lo borra

Env requeridas: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
"""
import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_EVENTS_PATH = os.path.join(ROOT, "new_events.json")
API = "https://api.x.com/2/tweets"
SITE = "https://otracoronacion.github.io/"

MEDAL_EMOJI = {"oro": "🥇", "plata": "🥈", "bronce": "🥉", "medalla": "🏅", "podio": "🏅"}


def _enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="-._~")


def oauth_request(method: str, url: str, json_body=None):
    keys = {k: os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        print(f"FALTAN secrets: {missing}", file=sys.stderr)
        sys.exit(1)

    oauth = {
        "oauth_consumer_key": keys["X_API_KEY"],
        "oauth_nonce": pysecrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": keys["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    # base string: solo params oauth (el body JSON no se firma en OAuth1)
    param_str = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(oauth.items()))
    base = f"{method}&{_enc(url)}&{_enc(param_str)}"
    signing_key = f"{_enc(keys['X_API_SECRET'])}&{_enc(keys['X_ACCESS_SECRET'])}"
    sig = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    auth_header = "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(oauth.items()))

    data = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r) if r.length != 0 else {}


def compose(ev) -> str:
    emoji = MEDAL_EMOJI.get(ev.get("medal"), "🏅")
    head = f"{emoji} ¡Otra coronación de gloria!\n\n"
    # Sin link en el cuerpo: X cobra ~13x más por tweets con URL.
    # El sitio va en la bio de la cuenta. (X_INCLUDE_LINK=1 para volver a incluirlo.)
    if os.environ.get("X_INCLUDE_LINK") == "1":
        tail = f"\n\nDespertate coronado 👉 {SITE}"
        budget = 280 - len(head) - (len(tail) - len(SITE) + 23)
    else:
        tail = "\n\nDespertate coronado 🧉 (suscribite: link en la bio)"
        budget = 280 - len(head) - len(tail)
    # X cuenta cada emoji como 2 caracteres; len() de Python los cuenta como 1.
    # Margen para el emoji del head, el del tail y alguno suelto en el título.
    budget -= 4
    title = ev["title"].strip()
    if len(title) > budget:
        title = title[: budget - 1].rstrip() + "…"
    return head + title + tail


def post_with_retry(text: str):
    last = None
    for attempt in range(1, 4):
        try:
            resp = oauth_request("POST", API, {"text": text})
            tid = resp.get("data", {}).get("id")
            print(f"Posteado: https://x.com/i/status/{tid}")
            return tid
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            last = f"{e.code}: {body}"
            print(f"[intento {attempt}/3] X error {last}", file=sys.stderr)
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(30 * attempt)
                continue
            break
        except Exception as e:
            last = str(e)
            print(f"[intento {attempt}/3] error: {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(30 * attempt)
    print(f"No se pudo postear: {last}", file=sys.stderr)
    return None


def main():
    if "--self-test" in sys.argv:
        tid = post_with_retry("🧪 Prueba de conexión — este tweet se borra solo. (Si lo estás viendo, parpadeá.)")
        if not tid:
            sys.exit(1)
        time.sleep(3)
        oauth_request("DELETE", f"{API}/{tid}")
        print("Self-test OK: tweet de prueba creado y borrado.")
        return

    if not os.path.exists(NEW_EVENTS_PATH):
        print("Sin eventos nuevos; nada para postear.")
        return
    with open(NEW_EVENTS_PATH, encoding="utf-8") as f:
        events = json.load(f)
    ok, fail = 0, 0
    for ev in events:
        if post_with_retry(compose(ev)):
            ok += 1
        else:
            fail += 1
        time.sleep(5)
    print(f"X: {ok} posteados, {fail} fallidos")
    if fail and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
