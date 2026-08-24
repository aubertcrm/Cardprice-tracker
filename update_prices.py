"""
Met a jour la cote de chaque carte listee dans cards.json.

Sources interrogees :
- eBay (Browse API officielle, cle gratuite) : fiable, utilise en priorite
- Mercari (scraping best-effort) : peut casser si le site change, entoure de try/except
- SNKR DUNK (scraping best-effort) : idem

Sortie : prices.json avec le prix courant + un historique glissant sur 90 jours,
que le dashboard va lire directement (raw.githubusercontent.com).

Variables d'environnement attendues (a definir en secrets GitHub Actions) :
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
        print(f"Erreur token eBay: {e}")
        return None


def price_from_ebay(token, query):
    if not token:
        return None
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "limit": 15, "sort": "price"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("itemSummaries", [])
        prices = [
            float(i["price"]["value"])
            for i in items
            if "price" in i and i["price"].get("currency") in ("EUR", "USD")
        ]
        if not prices:
            return None
        return round(statistics.median(prices), 2)
    except Exception as e:
        print(f"Erreur eBay pour '{query}': {e}")
        return None


def price_from_mercari(query):
    try:
        resp = requests.get(
            "https://jp.mercari.com/search",
            params={"keyword": query, "status": "sold_out"},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return None
    except Exception as e:
        print(f"Erreur Mercari pour '{query}': {e}")
        return None


def price_from_snkrdunk(query):
    try:
        resp = requests.get(
            "https://snkrdunk.com/search",
            params={"q": query},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return None
    except Exception as e:
        print(f"Erreur SNKR DUNK pour '{query}': {e}")
        return None


def compute_price(token, card):
    sources = []
    for term in card.get("search_terms", [card["name"]]):
        for fn in (price_from_ebay, price_from_mercari, price_from_snkrdunk):
            val = fn(token, term) if fn is price_from_ebay else fn(term)
            if val:
                sources.append(val)
    if not sources:
        return None
    return round(sum(sources) / len(sources), 2)


def main():
    with open(CARDS_FILE, encoding="utf-8") as f:
        cards = json.load(f)

    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            existing = {p["id"]: p for p in json.load(f).get("cards", [])}
    except FileNotFoundError:
        existing = {}

    token = get_ebay_token()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    updated = []
    for card in cards:
        price = compute_price(token, card)
        prev = existing.get(card["id"])
        history = prev["history"][-(HISTORY_DAYS - 1):] if prev else []

        if price is None and prev:
            price = prev["history"][-1]["price"] if prev["history"] else None

        if price is not None:
            history.append({"date": today, "price": price})

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
