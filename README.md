# Deal Sniper EU

Cerca ogni 10 minuti su **eBay Italia e Germania** gli annunci (Compralo Subito e aste in chiusura entro 24 ore) dei prodotti in `watchlist.json`, solo da **venditori UE**, e manda un messaggio **Telegram** quando prezzo + spedizione è almeno il 20% sotto il valore europeo.

## Codici segreti (Settings → Secrets and variables → Actions → New repository secret)

| Nome | Dove lo trovi |
|---|---|
| `EBAY_CLIENT_ID` | developer.ebay.com → Application Keysets → Production → App ID (Client ID) |
| `EBAY_CLIENT_SECRET` | stesso posto → Cert ID (Client Secret) |
| `TELEGRAM_BOT_TOKEN` | il token che ti ha dato @BotFather |
| `TELEGRAM_CHAT_ID` | facoltativo: se manca, viene preso dall'ultimo messaggio che hai scritto al bot |

## Avvio
Actions → **Deal Sniper EU** → **Run workflow**. Al primo giro ricevi un messaggio di conferma e le 5 migliori occasioni già online; poi solo quelle nuove.

## Modificare la lista
In `watchlist.json` ogni prodotto ha:
- `query`: cosa cercare su eBay
- `value_eur`: valore europeo consegnato in Italia
- `must_include` / `exclude`: parole che il titolo deve contenere / non deve contenere
- `threshold` (facoltativo): sconto minimo per quel prodotto (default 0.20 = 20%)
- `enabled: false` per metterlo in pausa

## Limiti
eBay concede di base ~5.000 ricerche al giorno. Ogni prodotto = 1 ricerca per sito eBay a ogni giro: 14 prodotti × 2 siti × 144 giri/giorno ≈ 4.000. Se aggiungi prodotti o siti, allunga l'intervallo nel file `.github/workflows/sniper.yml`.
