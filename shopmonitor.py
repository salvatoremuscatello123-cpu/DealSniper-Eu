#!/usr/bin/env python3
"""Monitor negozi (Shopify) — avvisa su Telegram per: nuovi prodotti, ritorni in stock, prezzi sotto soglia.

Legge il catalogo pubblico dei negozi Shopify (<negozio>/products.json). I negozi che non sono
su Shopify vengono segnalati al primo giro e poi ignorati.
Usa solo la libreria standard. Configurazione in shops.json.
Variabili: TELEGRAM_BOT_TOKEN (obbligatoria), TELEGRAM_CHAT_ID (facoltativa).
Opzione: --dry-run (stampa invece di inviare).
"""
import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "shops.json")
STATE = os.path.join(HERE, "state", "shops.json")
UA = {"User-Agent": "Mozilla/5.0 (personal stock monitor)", "Accept": "application/json"}


def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_catalog(base, max_pages):
    """Ritorna la lista prodotti, oppure None se il negozio non espone products.json."""
    products = []
    for page in range(1, max_pages + 1):
        try:
            data = get_json(f"{base.rstrip('/')}/products.json?limit=250&page={page}")
        except Exception as e:  # qualsiasi problema di rete o formato: salta il negozio
            if page == 1:
                print(f"[!] {base}: non leggibile ({e})", file=sys.stderr)
                return None
            break
        batch = data.get("products", []) if isinstance(data, dict) else []
        if page == 1 and not isinstance(data, dict):
            return None
        products.extend(batch)
        if len(batch) < 250:
            break
        time.sleep(1)
    return products


BOX_WORDS = ["box", "boxes", "blaster", "hanger", "mega", "tin", "display", "hobby", "jumbo", "fat pack", "value", "case"]
PACK_RE = re.compile(r"\bpacks?\b|\bpackung\b|\bbustin[ae]\b|\bbooster\b|\bsingle\b|\bsingolo\b|\beinzel")
SINGLE_HINTS = re.compile(r"\b\d{1,3}/\d{1,4}\b|(?<!\d)/\d{1,4}\b|\bauto(graph)?\b|\bpsa\b|\bbgs\b|\bcgc\b|\bsgc\b|\brookie\b|\brc\b|#|\brefractor\b|\bparallel\b|\bvariation\b|\binsert\b|\bpatch\b|\brelic\b|\bsingle card\b|\bcarta singola\b")


def match(title, rule, global_exclude):
    low = title.lower()
    if rule.get("box_only", True) and not any(re.search(rf"\b{re.escape(w)}\b", low) for w in BOX_WORDS):
        return False  # salta le carte singole
    if rule.get("box_only", True) and SINGLE_HINTS.search(low):
        return False  # numerate (12/99), autografi, gradate: sono carte singole
    if any(w.lower() in low for w in global_exclude + rule.get("exclude", [])):
        return False
    for key in ("any_of", "any_of_2"):
        group = rule.get(key, [])
        if group and not any(w.lower() in low for w in group):
            return False
    return all(w.lower() in low for w in rule.get("must_include", []))


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg(token, chat_id, text, dry):
    if dry or not token:
        print(text, "\n")
        return
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        print(f"[!] Telegram: HTTP {e.code} {e.read()[:300]!r}", file=sys.stderr)
    except Exception as e:
        print(f"[!] Telegram: {e}", file=sys.stderr)
    time.sleep(1)


def chat_id_from_updates(token):
    data = get_json(f"https://api.telegram.org/bot{token}/getUpdates")
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            return str(msg["chat"]["id"])
    raise SystemExit("ERRORE: non trovo la chat. Scrivi 'ciao' al bot su Telegram e rilancia (oppure aggiungi il secret TELEGRAM_CHAT_ID).")


def main():
    dry = "--dry-run" in sys.argv
    cfg = json.load(open(CONFIG))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or (chat_id_from_updates(token) if token and not dry else None)

    try:
        state = json.load(open(STATE))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = not state
    variants = state.setdefault("variants", {})   # id -> {"a": available, "p": price}
    shop_status = state.setdefault("shops", {})

    alerts, report, new_shops = [], [], []
    for shop in cfg["shops"]:
        name, base = shop["name"], shop["url"]
        new_shop = base not in shop_status  # negozio mai letto: prima memorizza in silenzio
        if shop_status.get(base) == "not_shopify" and not first_run:
            continue
        try:
            catalog = fetch_catalog(base, cfg.get("max_pages", 8))
        except Exception as e:
            print(f"[!] {name}: {e}", file=sys.stderr)
            catalog = None
        if catalog is None:
            shop_status[base] = "not_shopify"
            report.append(f"❌ {name}: non leggibile (non è Shopify o blocca l'accesso)")
            continue
        shop_status[base] = "ok"
        n_match = 0
        for prod in catalog:
            title = prod.get("title", "")
            rule = next((r for r in cfg["rules"] if match(title, r, cfg.get("global_exclude", []))), None)
            if not rule:
                continue
            n_match += 1
            url = f"{base.rstrip('/')}/products/{prod.get('handle', '')}"
            for v in prod.get("variants", []):
                vid = f"{base}#{v.get('id')}"
                try:
                    price = float(v.get("price"))
                except (TypeError, ValueError):
                    continue
                avail = bool(v.get("available"))
                old = variants.get(vid)
                variants[vid] = {"a": avail, "p": price, "t": time.time()}
                vtitle = title if v.get("title") in (None, "Default Title") else f"{title} – {v['title']}"
                max_price = rule.get("max_price")
                vname = (v.get("title") or "").lower()
                if rule.get("box_only", True) and PACK_RE.search(vname) and not re.search(r"\bbox\b|\bdisplay\b|\bcase\b", vname):
                    continue  # variante "singolo pacchetto" di un box: non confrontarla col prezzo del box
                if max_price is not None and price < max_price * cfg.get("min_price_ratio", 0.35):
                    continue  # prezzo troppo basso per essere il box: quasi certamente un pacchetto o una carta
                cheap = max_price is not None and price <= max_price
                reason = None
                if old is None:
                    if avail and (cheap or rule.get("alert_new", False)):
                        reason = "🆕 Nuovo prodotto"
                elif avail and not old["a"]:
                    if cheap or rule.get("alert_restock", True):
                        reason = "🔁 Tornato disponibile"
                elif avail and cheap and price < old["p"] - 0.01:
                    reason = f"📉 Prezzo sceso (era {old['p']:.2f} €)"
                if first_run or new_shop:
                    reason = None  # al primo giro memorizza tutto; avvisa solo per i prezzi già sotto soglia
                if reason or (first_run and avail and cheap):
                    alerts.append((rule["name"], reason or "✅ Disponibile sotto soglia", name, vtitle, price, max_price, url))
        report.append(f"✅ {name}: {len(catalog)} prodotti letti, {n_match} che ti interessano")
        if new_shop and not first_run:
            new_shops.append(f"{name} ({n_match} prodotti che ti interessano)")

    if first_run:
        tg(token, chat_id, "🛒 <b>Monitor negozi attivo</b>\n" + "\n".join(esc(r) for r in report) +
           f"\nDisponibili sotto soglia adesso: {len(alerts)}", dry)
    max_alerts = cfg.get("max_alerts_per_run", 8)
    if not first_run and new_shops:
        tg(token, chat_id, "➕ Negozi aggiunti al monitor: " + ", ".join(esc(n) for n in new_shops), dry)
    extra = 0
    if max_alerts:  # 0 = nessun limite
        extra = len(alerts) - max_alerts
        alerts = alerts[:max_alerts]
    for rname, reason, shop, title, price, maxp, url in alerts:
        soglia = f" · soglia {maxp:.0f} €" if maxp is not None else ""
        tg(token, chat_id, f"{reason} — <b>{esc(rname)}</b>\n🏪 {esc(shop)}\n{esc(title)}\n💶 <b>{price:.2f} €</b>{soglia}\n{url}", dry)

    if extra > 0:
        tg(token, chat_id, f"… e altri {extra} avvisi non inviati per non intasare la chat.", dry)
    print("\n".join(report), f"\nAvvisi inviati: {len(alerts)}")
    if not dry:
        cutoff = time.time() - 60 * 86400
        state["variants"] = {k: v for k, v in variants.items() if v.get("t", 0) > cutoff}
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    main()
