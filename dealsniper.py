#!/usr/bin/env python3
"""Deal Sniper EU — cerca su eBay Europa annunci sotto il valore di mercato e avvisa su Telegram.

Usa solo la libreria standard di Python (nessuna dipendenza).

Variabili d'ambiente (in GitHub: Settings > Secrets and variables > Actions):
  EBAY_CLIENT_ID       App ID (Client ID) del keyset Production
  EBAY_CLIENT_SECRET   Cert ID (Client Secret) del keyset Production
  TELEGRAM_BOT_TOKEN   token del bot dato da @BotFather
  TELEGRAM_CHAT_ID     (facoltativo) id della chat; se manca viene ricavato dall'ultimo messaggio inviato al bot

Opzioni:
  --dry-run   non invia messaggi, stampa a schermo
  --only-hot  controlla solo i prodotti con "hot": true
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")
STATE = os.path.join(HERE, "state", "seen.json")

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
      "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ebay_token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    res = http("POST", TOKEN_URL, {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, body)
    return res["access_token"]


def money(obj):
    if not obj:
        return None
    try:
        return float(obj["value"]), obj.get("currency", "EUR")
    except (KeyError, TypeError, ValueError):
        return None


def search(token, marketplace, product, max_total, cfg):
    filters = [
        f"deliveryCountry:{cfg['delivery_country']}",
        "buyingOptions:{FIXED_PRICE|AUCTION}",
        f"price:[..{int(max_total)}]",
        "priceCurrency:EUR",
    ]
    if product.get("condition_new", True):
        filters.append("conditions:{NEW}")
    params = {
        "q": product["query"],
        "filter": ",".join(filters),
        "sort": "newlyListed",
        "limit": str(cfg.get("results_per_search", 50)),
    }
    if product.get("category_id"):
        params["category_ids"] = str(product["category_id"])
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={cfg['delivery_country']},zip={cfg['delivery_zip']}",
        "Accept-Language": "it-IT",
    }
    try:
        return http("GET", url, headers).get("itemSummaries", [])
    except urllib.error.HTTPError as e:
        print(f"[!] {marketplace} '{product['name']}': HTTP {e.code} {e.read()[:300]!r}", file=sys.stderr)
        return []


SINGLE_HINTS = re.compile(r"\b\d{1,3}/\d{1,4}\b|(?<!\d)/\d{1,4}\b|\bauto(graph)?\b|\bpsa\b|\bbgs\b|\bcgc\b|\bsgc\b|\brookie\b|\brc\b|#|\brefractor\b|\bparallel\b|\bpatch\b|\brelic\b")
PACK_ONLY = re.compile(r"\b(1|one|single|einzel\w*|singol\w*)\s*(x\s*)?(pack|packung|bustina|booster)\b|\bhobby pack\b|\bpack only\b")


def evaluate(item, product, cfg):
    """Ritorna un dict con l'occasione, oppure None se va scartata."""
    title = item.get("title", "")
    low = title.lower()
    excl = [w.lower() for w in cfg.get("global_exclude", []) + product.get("exclude", [])]
    if any(w in low for w in excl):
        return None
    if SINGLE_HINTS.search(low) or PACK_ONLY.search(low):
        return None  # carta singola o pacchetto singolo, non il box
    must = [w.lower() for w in product.get("must_include", [])]
    if must and not all(w in low for w in must):
        return None

    country = (item.get("itemLocation") or {}).get("country", "")
    if country not in EU:
        return None

    auction = "AUCTION" in item.get("buyingOptions", []) and "FIXED_PRICE" not in item.get("buyingOptions", [])
    price = money(item.get("currentBidPrice")) if auction else None
    price = price or money(item.get("price"))
    if not price or price[1] != "EUR":
        return None

    ship = None
    for opt in item.get("shippingOptions", []) or []:
        m = money(opt.get("shippingCost"))
        if m and m[1] == "EUR":
            ship = m[0] if ship is None else min(ship, m[0])
    ship_known = ship is not None
    if not ship_known:
        ship = cfg.get("default_shipping_eur", 10)

    total = price[0] + ship
    value = product["value_eur"]
    threshold = product.get("threshold", cfg["threshold"])
    if total > value * (1 - threshold):
        return None
    if total < value * cfg.get("min_price_ratio", 0.35):
        return None  # troppo economico per essere il box: pacchetto, carta o annuncio sospetto

    end = item.get("itemEndDate")
    if auction and end:
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left > cfg.get("auction_max_hours_left", 24):
                return None
        except ValueError:
            pass

    return {
        "id": item.get("itemId"),
        "title": title,
        "url": item.get("itemWebUrl"),
        "price": price[0],
        "ship": ship,
        "ship_known": ship_known,
        "total": total,
        "value": value,
        "discount": 1 - total / value,
        "country": country,
        "auction": auction,
        "end": end,
        "max_bid": value * (1 - threshold) - ship if auction else None,
        "product": product["name"],
    }


def fmt(d):
    lines = [f"🎯 <b>{esc(d['product'])}</b>",
             esc(d["title"]),
             f"💶 {d['price']:.2f} € + {d['ship']:.2f} € sped.{'' if d['ship_known'] else ' (stima)'} = <b>{d['total']:.2f} €</b>",
             f"📉 Valore EU {d['value']:.0f} € → <b>-{d['discount']*100:.0f}%</b> · venditore {d['country']}"]
    if d["auction"]:
        end = d["end"] or "?"
        try:
            end = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M")
        except ValueError:
            pass
        lines.append(f"⏱ Asta, chiude {end} · offerta max <b>{d['max_bid']:.0f} €</b>")
    else:
        lines.append("🛒 Compralo Subito")
    lines.append(d["url"])
    return "\n".join(lines)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram_chat_id(token):
    res = http("GET", f"https://api.telegram.org/bot{token}/getUpdates")
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            return str(msg["chat"]["id"])
    raise SystemExit("Nessun messaggio trovato: scrivi 'ciao' al tuo bot su Telegram e riprova.")


def telegram_send(token, chat_id, text):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    http("POST", f"https://api.telegram.org/bot{token}/sendMessage",
         {"Content-Type": "application/x-www-form-urlencoded"}, body)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(seen):
    cutoff = time.time() - 14 * 86400  # dimentica dopo 14 giorni
    seen = {k: v for k, v in seen.items() if v > cutoff}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(seen, f)


def main():
    dry = "--dry-run" in sys.argv
    only_hot = "--only-hot" in sys.argv
    with open(WATCHLIST) as f:
        cfg = json.load(f)
    products = [p for p in cfg["products"] if p.get("enabled", True) and (p.get("hot") or not only_hot)]

    token = ebay_token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or (telegram_chat_id(tg_token) if tg_token and not dry else None)

    seen = load_state()
    first_run = not seen and not dry
    found, calls = [], 0
    for p in products:
        max_total = p["value_eur"] * (1 - p.get("threshold", cfg["threshold"]))
        for mkt in cfg["marketplaces"]:
            calls += 1
            for item in search(token, mkt, p, max_total, cfg):
                d = evaluate(item, p, cfg)
                if d and d["id"] not in seen:
                    seen[d["id"]] = time.time()
                    found.append(d)

    print(f"Ricerche: {calls} · prodotti: {len(products)} · nuove occasioni: {len(found)}")
    found.sort(key=lambda d: -d["discount"])
    if first_run and tg_token:
        telegram_send(tg_token, chat_id,
                      f"✅ Deal Sniper EU attivo. Monitoro {len(products)} prodotti su {', '.join(cfg['marketplaces'])}. "
                      f"Annunci già online sotto soglia: {len(found)}"
                      f"{' (ti mando i 5 migliori)' if len(found) > 5 else ''}. Da ora ti scrivo solo per quelli nuovi.")
    to_send = found[:5] if first_run else found
    for d in to_send:
        text = fmt(d)
        if dry or not tg_token:
            print(text, "\n")
        else:
            telegram_send(tg_token, chat_id, text)
            time.sleep(1)
    if not dry:
        save_state(seen)


if __name__ == "__main__":
    main()
