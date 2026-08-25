"""
Met a jour la cote de chaque carte listee dans cards.json en moyennant
plusieurs sources.

Sources interrogees :
- eBay, ventes reussies (scraping de la page "Sold Items")
- eBay, annonces actives (Browse API officielle, cle gratuite)
- Mercari (scraping best-effort)
- SNKR DUNK (scraping best-effort)
- Cardrush (scraping best-effort)
- Yuyu-tei (scraping best-effort)

La requete de recherche est construite automatiquement a partir des champs
de chaque carte (nom, set, annee, numero, langue, grade) et exclut les faux
positifs frequents (lots, reproductions, accessoires, mauvaise reedition...).
"""

import json
import os
import re
import statistics
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CARDS_FILE = "cards.json"
PRICES_FILE = "prices.json"
HISTORY_DAYS = 90
FALLBACK_USD_TO_EUR = 0.92
FALLBACK_JPY_TO_EUR = 0.0060

EBAY_CLIENT_ID = (os.environ.get("EBAY_CLIENT_ID") or "").strip()
EBAY_CLIENT_SECRET = (os.environ.get("EBAY_CLIENT_SECRET") or "").strip()

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def get_rates():
    rates = {"USD": FALLBACK_USD_TO_EUR, "JPY": FALLBACK_JPY_TO_EUR}
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "EUR", "to": "USD,JPY"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()["rates"]
        rates["USD"] = round(1 / data["USD"], 6)
        rates["JPY"] = round(1 / data["JPY"], 6)
        print(f"Taux du jour: 1 USD = {rates['USD']} EUR | 1 JPY = {rates['JPY']} EUR")
    except Exception as e:
        print(f"Taux de change par defaut utilises ({e})")
    return rates


def to_eur(value, currency, rates):
    if currency == "EUR":
        return value
    rate = rates.get(currency)
    if not rate:
        return None
    return round(value * rate, 2)


def build_query(card):
    """Construit une requete eBay precise a partir des champs de la carte,
    pour eviter les faux positifs (reproductions, lots, accessoires,
    reeditions d'une autre annee...).
    """
    parts = [f'"{card["name"]}"']
    if card.get("set"):
        parts.append(f'"{card["set"]}"')
    if card.get("year"):
        parts.append(str(card["year"]))
    if card.get("card_number"):
        parts.append(f'"{card["card_number"]}"')
    if card.get("language") and card["language"].lower() not in ("en", "english", "anglais"):
        parts.append(card["language"])
    grade = card.get("grade")
    if grade and grade not in ("-", "—"):
        parts.append(grade)

    exclusions = "-lot -custom -proxy -repro -reprint -fake -playmat -sleeve -binder -case -display -deck -box -bulk"
    return " ".join(parts) + " " + exclusions


def price_from_ebay_sold(card, rates):
    query = build_query(card)
    try:
        resp = requests.get(
            "https://www.ebay.com/sch/i.html",
            params={"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "_ipg": "60"},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        print(f"Debug eBay sold: status={resp.status_code}, taille reponse={len(resp.text)}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        prices = []
        matched_tags = soup.select(".s-item__price")
        print(f"Debug eBay sold: {len(matched_tags)} balises .s-item__price trouvees")
        for tag in matched_tags:
            m = re.search(r"([\d,]+\.\d{2})", tag.get_text())
            if m:
                value = float(m.group(1).replace(",", ""))
                converted = to_eur(value, "USD", rates)
                if converted:
                    prices.append(converted)
        if not prices:
            return []
        return [round(statistics.median(prices), 2)]
    except Exception as e:
        print(f"Erreur eBay (ventes) pour '{query}': {e}")
        return []


def get_ebay_token():
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return None
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
            body = f" | Reponse eBay: {e.response.text[:200]}"
        print(f"Erreur token eBay: {e}{body}")
        return None


def price_from_ebay_active(token, card, rates):
    if not token:
        return []
    query = build_query(card)
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "limit": 15, "sort": "price"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("itemSummaries", [])
        prices = []
        for i in items:
            price_obj = i.get("price")
            if not price_obj:
                continue
            converted = to_eur(float(price_obj["value"]), price_obj.get("currency"), rates)
            if converted:
                prices.append(converted)
        if not prices:
            return []
        return [round(statistics.median(prices), 2)]
    except Exception as e:
        print(f"Erreur eBay (annonces) pour '{query}': {e}")
        return []


def price_from_mercari(query, rates):
    try:
        resp = requests.get(
            "https://jp.mercari.com/search",
            params={"keyword": query, "status": "on_sale"},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        print(f"Debug Mercari: status={resp.status_code}, taille reponse={len(resp.text)}")
        if resp.status_code != 200:
            return []
        prices_jpy = [int(v.replace(",", "")) for v in re.findall(r'"price":"?(\d[\d,]*)"?', resp.text)]
        prices_jpy = [p for p in prices_jpy if 100 <= p <= 5_000_000]
        print(f"Debug Mercari: {len(prices_jpy)} prix trouves dans le HTML")
        if not prices_jpy:
            return []
        converted = to_eur(statistics.median(prices_jpy), "JPY", rates)
        return [converted] if converted else []
    except Exception as e:
        print(f"Erreur Mercari pour '{query}': {e}")
        return []


def price_from_snkrdunk(query, rates):
    try:
        resp = requests.get(
            "https://snkrdunk.com/search",
            params={"keyword": query},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        print(f"Debug SNKR DUNK: status={resp.status_code}, taille reponse={len(resp.text)}")
        if resp.status_code != 200:
            return []
        prices_jpy = [int(v.replace(",", "")) for v in re.findall(r'"price":"?(\d[\d,]*)"?', resp.text)]
        prices_jpy = [p for p in prices_jpy if 100 <= p <= 5_000_000]
        print(f"Debug SNKR DUNK: {len(prices_jpy)} prix trouves dans le HTML")
        if not prices_jpy:
            return []
        converted = to_eur(statistics.median(prices_jpy), "JPY", rates)
        return [converted] if converted else []
    except Exception as e:
        print(f"Erreur SNKR DUNK pour '{query}': {e}")
        return []


def price_from_cardrush(query, rates):
    try:
        resp = requests.get(
            "https://www.cardrush-pokemon.jp/product-list",
            params={"keyword": query},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        print(f"Debug Cardrush: status={resp.status_code}, taille reponse={len(resp.text)}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        prices_jpy = []
        for tag in soup.select(".price, .item_price, .productPrice"):
            m = re.search(r"([\d,]+)\s*円", tag.get_text())
            if m:
                prices_jpy.append(int(m.group(1).replace(",", "")))
