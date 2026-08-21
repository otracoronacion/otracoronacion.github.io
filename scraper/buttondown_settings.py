#!/usr/bin/env python3
"""
Sincroniza la identidad pública del newsletter en Buttondown: lo que ve la
gente al entrar a buttondown.com/coronadosdegloria (la página que abre el
formulario de la landing, y a la que caería el tráfico de una pauta).

Nació porque esa página tenía el nombre y el formulario y nada más: nunca se
configuró la descripción, porque hasta ahora la API se usaba solo para enviar.

La API de Buttondown NO es alcanzable desde el sandbox de Claude, así que esto
corre dentro de GitHub Actions, como todo lo que toca Buttondown.

Uso:
  python3 scraper/buttondown_settings.py             # muestra antes, aplica, muestra después
  python3 scraper/buttondown_settings.py --dry-run   # solo muestra lo que hay hoy
  python3 scraper/buttondown_settings.py --dump      # vuelca TODOS los campos (api_key redactada)

NOTA — plan de Buttondown (verificado 21/08/2026):
  Los cuatro campos custom_subscription_* devuelven HTTP 403
  "Customizing transactional emails requires a Standard plan".
  La copy queda escrita igual: el día que se pase a Standard, un solo
  dispatch del workflow la aplica sin tocar una línea.
  Lo que SÍ entra en el plan gratis: description, from_name, tint_color
  y subscription_confirmation_redirect_url.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.buttondown.com/v1/newsletters"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buttondown_settings.json")

# Lo que queremos que diga la página. La primera oración va sola al frente
# porque es la que sobrevive al recorte de ~160 caracteres en buscadores.
AJUSTES = {
    # --- identidad pública (la página buttondown.com/coronadosdegloria) ---
    "description": (
        "Cada vez que un argentino o argentina sube a un podio mundial o continental, te llega un mail. "
        "Del hockey al alfajor, del judo a la robótica: un rastreador automático lee las noticias cada "
        "mañana y escribe solo los días que hay coronación. Coronados de gloria vivamos, y acá las "
        "contamos todas."
    ),
    "from_name": "Otra Coronación de Gloria",
    "tint_color": "#3E7CB1",  # el celeste profundo de la landing

    # --- doble opt-in: el mail que pide confirmar ---
    # OJO: {{ confirmation_url }} es OBLIGATORIO (Django templating). Sin eso
    # nadie puede confirmar y el alta queda rota. Hay un test que lo verifica.
    "custom_subscription_confirmation_email_subject": "Un click y te despertás coronado 🏆",
    "custom_subscription_confirmation_email_text": (
        "¡Buenas! Estás a un click de sumarte a **Otra Coronación de Gloria**.\n\n"
        "👉 [Confirmar mi suscripción]({{ confirmation_url }})\n\n"
        "Después de confirmar vas a recibir un mail **solo los días que un argentino o argentina "
        "se sube a un podio mundial o continental**. Ni uno más: si no hay coronación, silencio.\n\n"
        "Del hockey al alfajor, del judo a la robótica. Todo lo que se gana representando al país.\n\n"
        "Si no te suscribiste vos, ignorá este mail y listo."
    ),

    # --- recordatorio para el que no confirmó ---
    "custom_subscription_confirmation_reminder_email_subject": "Te quedó pendiente la coronación 🏅",
    "custom_subscription_confirmation_reminder_email_text": (
        "Te habías anotado en **Otra Coronación de Gloria** pero quedó sin confirmar.\n\n"
        "👉 [Confirmar ahora]({{ confirmation_url }})\n\n"
        "Es un mail solo los días que Argentina se sube a un podio del mundo o de América. "
        "Los demás días, silencio.\n\n"
        "Si ya no te interesa, ignorá este mail: no insistimos más."
    ),

    # --- bienvenida, después de confirmar (requiere plan Standard en Buttondown) ---
    "custom_subscription_confirmed_email_subject": "Listo: ya estás coronado 🇦🇷",
    "custom_subscription_confirmed_email_text": (
        "Ya estás adentro.\n\n"
        "Desde ahora, cada vez que un argentino o argentina se suba a un podio **mundial o "
        "continental**, te llega el mail a la mañana. Así funciona:\n\n"
        "- Un rastreador lee la prensa argentina todos los días, tempranito.\n"
        "- Si hubo coronación, te escribimos. Si no hubo, no te escribimos.\n"
        "- Cuenta todo lo que se gana representando al país: hockey, judo, vóley, robótica, alfajores.\n\n"
        "Mientras tanto, mirá las últimas: https://otracoronacion.github.io/\n\n"
        "¿Preferís enterarte por WhatsApp? El canal es este: "
        "https://whatsapp.com/channel/0029Vb85r2RDZ4Lb3Qsnkq0P\n\n"
        "Y si se nos escapa alguna, escribinos a otracoronacion@gmail.com. Nos pasó y nos va a "
        "volver a pasar: el país gana demasiado seguido."
    ),

    # --- a dónde cae después de confirmar: página propia, no la genérica ---
    "subscription_confirmation_redirect_url": "https://otracoronacion.github.io/gracias.html",
}


def verificar_copy():
    """El error caro sería publicar un mail de confirmación sin el link: nadie
    podría confirmar y el alta quedaría rota en silencio."""
    problemas = []
    for campo in ("custom_subscription_confirmation_email_text",
                  "custom_subscription_confirmation_reminder_email_text"):
        if "{{ confirmation_url }}" not in AJUSTES.get(campo, ""):
            problemas.append(f"{campo} no tiene {{{{ confirmation_url }}}}")
    if problemas:
        for p in problemas:
            print(f"ABORTADO: {p}", file=sys.stderr)
        sys.exit(1)


def pedir(metodo, url, key, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=metodo,
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def mostrar(titulo, n):
    print(f"\n--- {titulo} ---")
    for campo in ["name", "username"] + list(AJUSTES):
        valor = n.get(campo) or ""
        print(f"  {campo:56.56} : {(valor[:88] + chr(8230) if len(valor) > 88 else valor) if valor else '(vacío)'}")


SECRETOS = ("api_key", "secret", "token", "password")


def redactar(n):
    """El repo es público: nada de claves en la branch de resultados."""
    return {k: ("(REDACTADO)" if any(s in k.lower() for s in SECRETOS) else v)
            for k, v in n.items()}


def volcar(n):
    limpio = redactar(n)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(limpio, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"{len(limpio)} campos en el objeto newsletter:\n")
    for k in sorted(limpio):
        v = limpio[k]
        if isinstance(v, str):
            estado = f"{len(v)} chars: {v[:70]!r}" if v.strip() else "(VACÍO)"
        elif v in (None, [], {}):
            estado = "(VACÍO)"
        else:
            estado = repr(v)[:80]
        print(f"  {k:34} {estado}")


def main():
    verificar_copy()
    key = os.environ.get("BUTTONDOWN_API_KEY")
    if not key:
        print("FALTA BUTTONDOWN_API_KEY", file=sys.stderr)
        sys.exit(1)

    try:
        listado = pedir("GET", API, key)
    except urllib.error.HTTPError as e:
        detalle = f"GET falló {e.code}: {e.read().decode()[:300]}"
        print(detalle, file=sys.stderr)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"error": detalle}, f, ensure_ascii=False, indent=2)
        sys.exit(1)

    resultados = listado.get("results") or []
    if not resultados:
        print("La cuenta no tiene newsletters.", file=sys.stderr)
        sys.exit(1)
    antes = resultados[0]
    nid = antes["id"]
    mostrar("ANTES", antes)

    if "--dump" in sys.argv:
        print()
        volcar(antes)
        return

    # Guardamos los valores previos: si algo no gusta, se revierte con esto.
    previos = {c: antes.get(c) for c in AJUSTES}

    if "--dry-run" in sys.argv:
        print("\n[dry-run] No se modificó nada. Se aplicaría:")
        for c, v in AJUSTES.items():
            print(f"  {c:12} : {v}")
        return

    cambios = {c: v for c, v in AJUSTES.items() if antes.get(c) != v}
    if not cambios:
        print("\nYa estaba todo como queremos; no hay nada que cambiar.")
        return
    print(f"\nAplicando {len(cambios)} campo(s), uno por uno.")

    # Uno por uno a propósito: si el plan de Buttondown no habilita algún campo,
    # la API rechaza el PATCH entero. Así un campo caído no se lleva al resto, y
    # queda registrado cuál falló y por qué (los logs de Actions no se pueden
    # leer desde el sandbox, así que el diagnóstico viaja por la branch).
    aplicados, fallidos = {}, {}
    for campo, valor in cambios.items():
        try:
            pedir("PATCH", f"{API}/{nid}", key, {campo: valor})
            aplicados[campo] = valor
            print(f"  ✓ {campo}")
        except urllib.error.HTTPError as e:
            detalle = e.read().decode()[:300]
            fallidos[campo] = f"HTTP {e.code}: {detalle}"
            print(f"  ✗ {campo} → HTTP {e.code}: {detalle[:160]}", file=sys.stderr)
        except Exception as e:
            fallidos[campo] = str(e)[:300]
            print(f"  ✗ {campo} → {e}", file=sys.stderr)

    despues = (pedir("GET", API, key).get("results") or [{}])[0]
    mostrar("DESPUÉS", despues)

    no_pegaron = [c for c, v in aplicados.items() if despues.get(c) != v]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"previos": previos, "aplicados": sorted(aplicados),
                   "fallidos": fallidos, "no_confirmados": no_pegaron,
                   "ahora": {c: despues.get(c) for c in AJUSTES}}, f, ensure_ascii=False, indent=2)

    print(f"\n{len(aplicados)} aplicados, {len(fallidos)} rechazados por la API, {len(no_pegaron)} sin confirmar.")
    sys.exit(1 if (fallidos or no_pegaron) else 0)


if __name__ == "__main__":
    main()
