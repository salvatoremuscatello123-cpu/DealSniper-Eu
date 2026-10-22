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


def http(method, url, headers=None, data=None, timeout=30, retries=3):
    """Richiesta con ritentativi: un timeout o una connessione caduta non fermano il programma."""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise


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


def search(token, marketplace, product, max_total, cfg, buying="FIXED_PRICE|AUCTION", sort="newlyListed", limit=None):
    filters = [
        f"deliveryCountry:{cfg['delivery_country']}",
        "buyingOptions:{" + buying + "}",
        "priceCurrency:EUR",
    ]
    if max_total:
        filters.append(f"price:[..{int(max_total)}]")
    if product.get("condition_new", True):
        filters.append("conditions:{NEW}")
    params = {
        "q": product["query"],
        "filter": ",".join(filters),
        "limit": str(limit or cfg.get("results_per_search", 50)),
    }
    if sort:
        params["sort"] = sort
    if product.get("category_id"):
        params["category_ids"] = str(product["category_id"])
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={cfg['delivery_country']},zip={cfg['delivery_zip']}",
    }
    try:
        return http("GET", url, headers).get("itemSummaries", [])
    except urllib.error.HTTPError as e:
        print(f"[!] {marketplace} '{product['name']}': HTTP {e.code} {e.read()[:300]!r}", file=sys.stderr)
        return []
    except Exception as e:  # rete instabile: salta questa ricerca, continua con le altre
        print(f"[!] {marketplace} '{product['name']}': {e}", file=sys.stderr)
        return []


SINGLE_HINTS = re.compile(r"\b\d{1,3}/\d{1,4}\b|(?<!\d)/\d{1,4}\b|\bauto(graph)?\b|\bpsa\b|\bbgs\b|\bcgc\b|\bsgc\b|\brookie\b|\brc\b|#|\brefractor\b|\bparallel\b|\bpatch\b|\brelic\b")
PACK_ONLY = re.compile(r"\b(1|one|single|einzel\w*|singol\w*)\s*(x\s*)?(pack|packung|bustina|booster)\b|\bhobby pack\b|\bpack only\b")


def parse(item, product, cfg):
    """Annuncio valido per questo prodotto -> (title, country, auction, price, ship, ship_known, total), altrimenti None."""
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

    return title, country, auction, price[0], ship, ship_known, price[0] + ship


def evaluate(item, product, cfg, value):
    """Ritorna un dict con l'occasione, oppure None se va scartata."""
    parsed = parse(item, product, cfg)
    if not parsed:
        return None
    title, country, auction, price, ship, ship_known, total = parsed
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
        "price": price,
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


MARKET = os.path.join(HERE, "state", "market.json")


def load_market():
    try:
        with open(MARKET) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def compute_market(token, product, cfg):
    """Valore di mercato EU = media dei N Compralo Subito più economici (prezzo + spedizione, venditori UE).
    Riferimento 'competitivo': il prezzo a cui dovresti vendere per essere tra i più bassi."""
    totals = []
    floor = product["value_eur"] * cfg.get("min_price_ratio", 0.35) if not product.get("value_guess") else 0
    for mkt in cfg["marketplaces"]:
        for item in search(token, mkt, product, None, cfg, buying="FIXED_PRICE", sort="price", limit=100):
            parsed = parse(item, product, cfg)
            if parsed and parsed[6] >= floor:  # scarta pacchetti/carte finiti tra i risultati
                totals.append(parsed[6])
    totals.sort()
    n = len(totals)
    if n < cfg.get("market_min_listings", 5):
        return None, n
    lowest = totals[:cfg.get("market_lowest_n", 10)]
    return round(sum(lowest) / len(lowest), 2), n


def fmt(d):
    lines = [f"🎯 <b>{esc(d['product'])}</b>",
             esc(d["title"]),
             f"💶 {d['price']:.2f} € + {d['ship']:.2f} € sped.{'' if d['ship_known'] else ' (stima)'} = <b>{d['total']:.2f} €</b>",
             f"📉 Valore EU {d['value']:.0f} € ({d.get('value_src', 'manuale')}) → <b>-{d['discount']*100:.0f}%</b> · venditore {d['country']}"]
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
    try:
        _telegram_send(token, chat_id, text)
    except Exception as e:
        print(f"[!] Telegram: {e}", file=sys.stderr)


def _telegram_send(token, chat_id, text):
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

    # valori di mercato automatici, aggiornati ogni N ore
    market = load_market()
    refresh_h = cfg.get("market_refresh_hours", 6)
    changes = []
    method = f"lowest{cfg.get('market_lowest_n', 10)}"
    if market.get("_method") != method:
        market = {}  # metodo di calcolo cambiato: ricalcola subito tutto
    if cfg.get("auto_value", True) and time.time() - market.get("_ts", 0) > refresh_h * 3600:
        for p in [p for p in cfg["products"] if p.get("enabled", True)]:
            val, n = compute_market(token, p, cfg)
            calls += len(cfg["marketplaces"])
            old = (market.get(p["name"]) or {}).get("value")
            if val is not None:
                manual = p["value_eur"]
                lo, hi = (0.25, 4.0) if p.get("value_guess") else (0.4, 2.0)
                val = min(max(val, manual * lo), manual * hi)  # protezione contro dati anomali
                market[p["name"]] = {"value": val, "n": n, "ts": time.time()}
                if old and abs(val - old) / old >= 0.10:
                    changes.append(f"{'📈' if val > old else '📉'} {p['name']}: {old:.0f} → <b>{val:.0f} €</b> ({n} annunci)")
            print(f"Mercato {p['name']}: {val} da {n} annunci")
        had_market = "_ts" in market
        market["_ts"] = time.time()
        market["_method"] = method
        if not dry:
            os.makedirs(os.path.dirname(MARKET), exist_ok=True)
            with open(MARKET, "w") as f:
                json.dump(market, f)
        if tg_token and not dry and not had_market:
            lines = [f"• {p['name']}: <b>{market[p['name']]['value']:.0f} €</b> ({market[p['name']]['n']} annunci)"
                     if p["name"] in market else f"• {p['name']}: {p['value_eur']:.0f} € (manuale, pochi annunci)"
                     for p in cfg["products"] if p.get("enabled", True)]
            telegram_send(tg_token, chat_id, "📊 <b>Valori di mercato EU calcolati</b> (media dei 10 Compralo Subito UE più economici, spedizione inclusa)\n" + "\n".join(lines))
        elif changes and tg_token and not dry:
            telegram_send(tg_token, chat_id, "📊 <b>Valori di mercato aggiornati</b>\n" + "\n".join(changes))

    always = cfg.get("marketplaces_always", cfg["marketplaces"][:1])
    others = [m for m in cfg["marketplaces"] if m not in always]
    run_mkts = always + ([others[int(time.time() // 900) % len(others)]] if others else [])
    print("Siti di questo giro:", ", ".join(run_mkts))
    for p in products:
        info = market.get(p["name"]) if cfg.get("auto_value", True) else None
        if not info and p.get("value_guess"):
            continue  # valore solo stimato: niente avvisi finché il mercato non è calcolato
        value = info["value"] if info else p["value_eur"]
        src = f"mercato, {info['n']} annunci" if info else "manuale"
        max_total = value * (1 - p.get("threshold", cfg["threshold"]))
        for mkt in (run_mkts if p.get("hot") else always):
            calls += 1
            for item in search(token, mkt, p, max_total, cfg):
                d = evaluate(item, p, cfg, value)
                if d:
                    d["value_src"] = src
                if d and d["id"] not in seen:
                    seen[d["id"]] = time.time()
                    found.append(d)

    print(f"Ricerche: {calls} · prodotti: {len(products)} · nuove occasioni: {len(found)}")
    found.sort(key=lambda d: -d["discount"])
    if first_run and tg_token:
        telegram_send(tg_token, chat_id,
                      f"✅ Deal Sniper EU attivo. Monitoro {len(products)} prodotti su {len(cfg['marketplaces'])} siti eBay UE. "
                      f"Annunci già online sotto soglia: {len(found)}. Da ora ti scrivo solo per quelli nuovi.")
    to_send = found
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
