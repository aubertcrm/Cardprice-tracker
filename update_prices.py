"""
Met a jour la cote de chaque carte listee dans cards.json.

Sources interrogees :
- Pokemon TCG API (api.pokemontcg.io) : agrege TCGplayer (USD) et Cardmarket (EUR).
  Gratuite, sans cle, mais uniquement pour les cartes Pokemon.
- eBay (Browse API officielle, cle gratuite) : annonces actives, toutes cartes.
- Mercari (scraping best-effort) : pas d'API publique, fragile.
- SNKR DUNK (scraping best-effort) : idem.

Tous les prix sont convertis en EUR avant d'etre moyennes, pour eviter de
melanger des dollars et des euros dans un meme calcul.

Sortie : prices.json avec le prix courant (moyenne des sources disponibles)
+ un historique glissant sur 90 jours.

Variables d'environnement attendues (secrets GitHub Actions) :
- EBAY_CLIENT_ID
- EBAY_CLIENT_SECRET
"""

import json
import os
import statistics
from datetime import datetime, timezone

import requests

CARDS_FILE = "cards.json"
PRICES_FILE = "prices.json"
HISTORY_DAYS = 90

EBAY_CLIENT_ID = (os.environ.get("EBAY_CLIENT_ID") or "").strip()
EBAY_CLIENT_SECRET = (os.environ.get("EBAY_CLIENT_SECRET") or "").strip()

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (compatible; CardTrackerBot/1.0; +https://github.com/)"
}

FALLBACK_USD_TO_EUR = 0.92


# ---------------------------------------------------------------------------
# Taux de change
# ---------------------------------------------------------------------------

def get_usd_to_eur_rate():
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10,
        )
        resp.raise_for_status()
        rate = resp.json()["rates"]["EUR"]
        print(f"Taux USD->EUR du jour: {rate}")
        return rate
    except Exception as e:
        print(f"Impossible de recuperer le taux de change, valeur par defaut utilisee: {e}")
        return FALLBACK_USD_TO_EUR


# ---------------------------------------------------------------------------
# Pokemon TCG API (Pokemon uniquement)
# ---------------------------------------------------------------------------

def price_from_pokemontcgio(card, usd_to_eur):
    if card.get("game") != "pokemon":
        return []
    try:
        query = f'name:"{card["name"]}"'
        if card.get("set"):
            query += f' set.name:"{card["set"]}"'
        resp = requests.get(
            "https://api.pokemontcg.io/v2/cards",
            params={"q": query, "pageSize": 1},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])
        if not results:
            return []
        result = results[0]
        prices = []

        tcgplayer = (result.get("tcgplayer") or {}).get("prices") or {}
        for variant in tcgplayer.values():
            market = variant.get("market")
            if market:
                prices.append(round(market * usd_to_eur, 2))
                break

        cardmarket = (result.get("cardmarket") or {}).get("prices") or {}
        avg_sell = cardmarket.get("averageSellPrice") or cardmarket.get("trendPrice")
        if avg_sell:
            prices.append(round(avg_sell, 2))

        return prices
    except Exception as e:
        print(f"Erreur Pokemon TCG API pour '{card['name']}': {e}")
        return []


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------

def get_ebay_token():
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        print("Cles eBay absentes, source eBay ignoree.")
        return None
    print(f"Debug: longueur EBAY_CLIENT_ID={len(EBAY_CLIENT_ID)}, longueur EBAY_CLIENT_SECRET={len(EBAY_CLIENT_SECRET)}")
    try:
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = f" | Reponse eBay: {e.response.text[:300]}"
        print(f"Erreur token eBay: {e}{body}")
        return None


def price_from_ebay(token, query, usd_to_eur):
    if not token:
        return []
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "limit": 15, "sort": "price"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("itemSummaries", [])
        prices_eur = []
        for i in items:
            price_obj = i.get("price")
            if not price_obj:
                continue
            value = float(price_obj["value"])
            currency = price_obj.get("currency")
            if currency == "EUR":
                prices_eur.append(value)
            elif currency == "USD":
                prices_eur.append(round(value * usd_to_eur, 2))
        if not prices_eur:
            return []
        return [round(statistics.median(prices_eur), 2)]
    except Exception as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = f" | Reponse eBay: {e.response.text[:300]}"
        print(f"Erreur eBay pour '{query}': {e}{body}")
        return []


# ---------------------------------------------------------------------------
# Mercari / SNKR DUNK (best-effort, pas d'API publique)
# ---------------------------------------------------------------------------

def price_from_mercari(query):
    try:
        resp = requests.get(
            "https://jp.mercari.com/search",
            params={"keyword": query, "status": "sold_out"},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return []
    except Exception as e:
        print(f"Erreur Mercari pour '{query}': {e}")
        return []


def price_from_snkrdunk(query):
    try:
        resp = requests.get(
            "https://snkrdunk.com/search",
            params={"q": query},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return []
    except Exception as e:
        print(f"Erreur SNKR DUNK pour '{query}': {e}")
        return []


# ---------------------------------------------------------------------------
# Agregation
# ---------------------------------------------------------------------------

def compute_price(token, usd_to_eur, card):
    sources = []

    sources += price_from_pokemontcgio(card, usd_to_eur)

    for term in card.get("search_terms", [card["name"]]):
        sources += price_from_ebay(token, term, usd_to_eur)
        sources += price_from_mercari(term)
        sources += price_from_snkrdunk(term)

    if not sources:
        return None, 0

    return round(sum(sources) / len(sources), 2), len(sources)


def main():
    with open(CARDS_FILE, encoding="utf-8") as f:
        cards = json.load(f)

    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            existing = {p["id"]: p for p in json.load(f).get("cards", [])}
    except FileNotFoundError:
        existing = {}

    token = get_ebay_token()
    usd_to_eur = get_usd_to_eur_rate()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    updated = []
    for card in cards:
        price, nb_sources = compute_price(token, usd_to_eur, card)
        prev = existing.get(card["id"])
        history = prev["history"][-(HISTORY_DAYS - 1):] if prev else []

        if price is None and prev:
            price = prev["history"][-1]["price"] if prev["history"] else None

        if price is not None:
            history.append({"date": today, "price": price})

        print(f"{card['name']}: {price} EUR ({nb_sources} source(s))")

        updated.append(
            {
                "id": card["id"],
                "game": card["game"],
                "name": card["name"],
                "set": card["set"],
                "grade": card["grade"],
                "qty": card.get("qty", 1),
                "price": price,
                "history": history,
            }
        )

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cards": updated,
    }
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"prices.json ecrit avec {len(updated)} cartes.")


if __name__ == "__main__":
    main()
