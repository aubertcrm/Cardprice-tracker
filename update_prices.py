"""
Met a jour la cote de chaque carte listee dans cards.json en moyennant
plusieurs sources.

Sources interrogees :
- PriceCharting (catalogue par ID de produit - le plus fiable, source payante).
  Identifiable par pricecharting_id (exact) ou pricecharting_url (URL de la
  page produit, resolue automatiquement en comparant nom, set et numero).
- eBay, ventes reussies (scraping de la page "Sold Items")
- eBay, annonces actives (Browse API officielle, cle gratuite)
- Mercari (scraping best-effort)
- SNKR DUNK (scraping best-effort)
- Cardrush (scraping best-effort)
- Yuyu-tei (scraping best-effort)

Les sources eBay/Mercari/SNKR DUNK/Cardrush/Yuyu-tei ne servent que de repli
pour les cartes qui n'ont ni pricecharting_id ni pricecharting_url.

Tous les prix sont convertis en EUR. Un ajustement global (GLOBAL_PRICE_ADJUSTMENT)
est applique a toutes les cartes, sauf si une carte definit son propre
"price_adjustment" dans cards.json.

Variables d'environnement attendues (secrets GitHub Actions), optionnelles :
- EBAY_CLIENT_ID
- EBAY_CLIENT_SECRET
- PRICECHARTING_TOKEN
"""

import json
import os
import re
import statistics
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup

CARDS_FILE = "cards.json"
PRICES_FILE = "prices.json"
HISTORY_DAYS = 90
FALLBACK_USD_TO_EUR = 0.92
FALLBACK_JPY_TO_EUR = 0.0060

# Ajustement global applique a toutes les cartes (1.30 = +30%).
GLOBAL_PRICE_ADJUSTMENT = 1.30

EBAY_CLIENT_ID = (os.environ.get("EBAY_CLIENT_ID") or "").strip()
EBAY_CLIENT_SECRET = (os.environ.get("EBAY_CLIENT_SECRET") or "").strip()
PRICECHARTING_TOKEN = (os.environ.get("PRICECHARTING_TOKEN") or "").strip()

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


# ---------------------------------------------------------------------------
# Taux de change
# ---------------------------------------------------------------------------

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


def trimmed_median(values):
    """Retire le quart le plus bas et le quart le plus haut avant de calculer
    la mediane, pour limiter l'impact des mauvaises correspondances."""
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    if n >= 4:
        cut = n // 4
        values = values[cut: n - cut]
    return round(statistics.median(values), 2)


# ---------------------------------------------------------------------------
# PriceCharting
# ---------------------------------------------------------------------------

PRICECHARTING_GRADE_FIELDS = {
    "psa 10": "manual-only-price",
    "psa 9": "graded-price",
    "psa 8.5": "new-price",
    "psa 8": "new-price",
    "psa 7.5": "cib-price",
    "psa 7": "cib-price",
    "psa 6": "condition-16-price",
    "psa 5": "condition-15-price",
    "psa 4": "condition-14-price",
    "psa 3": "condition-13-price",
    "psa 2": "condition-10-price",
    "psa 1": "condition-9-price",
    "bgs 10": "bgs-10-price",
    "bgs 10 black": "condition-20-price",
    "bgs 9.5": "box-only-price",
    "cgc 10": "condition-17-price",
    "cgc 10 pristine": "condition-19-price",
    "sgc 10": "condition-18-price",
    "tag 10": "condition-21-price",
    "ace 10": "condition-22-price",
}


def _slugify(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _extract_numbers(text):
    return set(re.findall(r"\d{2,}", text))


def resolve_pricecharting_id_from_url(url):
    """A partir de l'URL d'une page produit PriceCharting (celle qu'on visite
    dans le navigateur), retrouve automatiquement l'ID du produit via une
    recherche, en comparant les slugs pour prendre la bonne correspondance.
    """
    try:
        path = urlparse(url).path.strip("/").split("/")
        if len(path) < 2:
            return None
        console_slug, product_slug = path[-2], path[-1]
        console_slug, product_slug = unquote(console_slug), unquote(product_slug)
        query = product_slug.replace("-", " ")

        resp = requests.get(
            "https://www.pricecharting.com/api/products",
            params={"t": PRICECHARTING_TOKEN, "q": query},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") != "success":
            return None

        products = data.get("products", [])
        target_slug = _slugify(product_slug)
        target_console_slug = _slugify(console_slug)
        target_numbers = _extract_numbers(product_slug)

        # 1er passage : exige que le nom ET le set correspondent (le plus fiable,
        # evite de confondre deux variantes qui partagent le meme nom de carte
        # mais un set different, ex: version "Japanese" vs version normale).
        for p in products:
            if _slugify(p.get("product-name", "")) == target_slug and _slugify(p.get("console-name", "")) == target_console_slug:
                print(f"Debug PriceCharting: URL resolue vers '{p.get('product-name')}' / '{p.get('console-name')}' (id={p['id']})")
                return p["id"]

        # 2e passage : a defaut, nom de carte identique seul (moins fiable)
        for p in products:
            if _slugify(p.get("product-name", "")) == target_slug:
                print(f"Debug PriceCharting: correspondance nom seul (set different) '{p.get('product-name')}' / '{p.get('console-name')}' (id={p['id']})")
                return p["id"]

        # 3e passage (secours prudent) : le numero de carte doit se retrouver
        # dans le nom du candidat, sinon on risque de recuperer une carte
        # totalement differente (ex: #98 au lieu de #204 sur un meme visuel).
        # Sans numero correspondant, on abandonne plutot que de deviner au hasard.
        if target_numbers:
            for p in products:
                candidate_numbers = _extract_numbers(p.get("product-name", ""))
                if target_numbers & candidate_numbers:
                    print(f"Debug PriceCharting: correspondance partielle par numero '{p.get('product-name')}' / '{p.get('console-name')}' (id={p['id']})")
                    return p["id"]

        print(f"Debug PriceCharting: aucune correspondance fiable pour '{product_slug}', carte ignoree (pas de prix devine au hasard)")
        return None
    except Exception as e:
        print(f"Erreur resolution URL PriceCharting: {e}")
        return None


def price_from_pricecharting(card, rates):
    if not PRICECHARTING_TOKEN:
        return []
    product_id = card.get("pricecharting_id")
    if not product_id and card.get("pricecharting_url"):
        product_id = resolve_pricecharting_id_from_url(card["pricecharting_url"])
    if not product_id:
        return []
    try:
        resp = requests.get(
            "https://www.pricecharting.com/api/product",
            params={"t": PRICECHARTING_TOKEN, "id": product_id},
            timeout=15,
        )
        print(f"Debug PriceCharting: status={resp.status_code}")
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("status") != "success":
            print(f"Debug PriceCharting: {data.get('error-message')}")
            return []

        grade = (card.get("grade") or "").strip().lower()
        field = PRICECHARTING_GRADE_FIELDS.get(grade, "loose-price")
        cents = data.get(field)
        if cents is None:
            print(f"Debug PriceCharting: pas de valeur pour le champ '{field}'")
            return []
        usd_value = cents / 100
        converted = to_eur(usd_value, "USD", rates)
        return [converted] if converted else []
    except Exception as e:
        print(f"Erreur PriceCharting pour l'id {product_id}: {e}")
        return []


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------

def build_query(card):
    if card.get("custom_query"):
        base = card["custom_query"]
    else:
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
        base = " ".join(parts)

    exclusions = "-lot -custom -proxy -repro -reprint -fake -playmat -sleeve -binder -case -display -deck -box -bulk -set -bundle"
    return base + " " + exclusions


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
        result = trimmed_median(prices)
        return [result] if result else []
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
            params={"q": query, "limit": 30},
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
        result = trimmed_median(prices)
        return [result] if result else []
    except Exception as e:
        print(f"Erreur eBay (annonces) pour '{query}': {e}")
        return []


# ---------------------------------------------------------------------------
# Mercari
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SNKR DUNK
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Cardrush
# ---------------------------------------------------------------------------

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
        print(f"Debug Cardrush: {len(prices_jpy)} prix trouves dans le HTML")
        if not prices_jpy:
            return []
        converted = to_eur(statistics.median(prices_jpy), "JPY", rates)
        return [converted] if converted else []
    except Exception as e:
        print(f"Erreur Cardrush pour '{query}': {e}")
        return []


# ---------------------------------------------------------------------------
# Yuyu-tei
# ---------------------------------------------------------------------------

def price_from_yuyutei(query, rates):
    try:
        resp = requests.get(
            "https://yuyu-tei.jp/sell/poc/s/search",
            params={"search_word": query},
            headers=HEADERS_BROWSER,
            timeout=15,
        )
        print(f"Debug Yuyu-tei: status={resp.status_code}, taille reponse={len(resp.text)}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        prices_jpy = []
        for tag in soup.select(".price, .card_price"):
            m = re.search(r"([\d,]+)\s*円", tag.get_text())
            if m:
                prices_jpy.append(int(m.group(1).replace(",", "")))
        print(f"Debug Yuyu-tei: {len(prices_jpy)} prix trouves dans le HTML")
        if not prices_jpy:
            return []
        converted = to_eur(statistics.median(prices_jpy), "JPY", rates)
        return [converted] if converted else []
    except Exception as e:
        print(f"Erreur Yuyu-tei pour '{query}': {e}")
        return []


# ---------------------------------------------------------------------------
# Agregation
# ---------------------------------------------------------------------------

def compute_price(token, rates, card):
    pc = price_from_pricecharting(card, rates)
    if pc:
        sources = {"pricecharting": pc[0]}
        avg = pc[0]
        adjustment = card.get("price_adjustment", GLOBAL_PRICE_ADJUSTMENT)
        if adjustment and adjustment != 1:
            avg = round(avg * adjustment, 2)
            sources["_adjustment_applied"] = adjustment
        return avg, sources

    sources = {}

    sold = price_from_ebay_sold(card, rates)
    if sold:
        sources["ebay_sold"] = sold[0]
    active = price_from_ebay_active(token, card, rates)
    if active:
        sources.setdefault("ebay_active", active[0])

    query = build_query(card)
    mercari = price_from_mercari(query, rates)
    if mercari:
        sources.setdefault("mercari", mercari[0])
    snkrdunk = price_from_snkrdunk(query, rates)
    if snkrdunk:
        sources.setdefault("snkrdunk", snkrdunk[0])
    cardrush = price_from_cardrush(query, rates)
    if cardrush:
        sources.setdefault("cardrush", cardrush[0])
    yuyutei = price_from_yuyutei(query, rates)
    if yuyutei:
        sources.setdefault("yuyutei", yuyutei[0])

    if not sources:
        return None, {}

    avg = round(sum(sources.values()) / len(sources), 2)

    adjustment = card.get("price_adjustment", GLOBAL_PRICE_ADJUSTMENT)
    if adjustment and adjustment != 1:
        avg = round(avg * adjustment, 2)
        sources["_adjustment_applied"] = adjustment

    return avg, sources


def main():
    with open(CARDS_FILE, encoding="utf-8") as f:
        cards = json.load(f)

    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            existing = {p["id"]: p for p in json.load(f).get("cards", [])}
    except FileNotFoundError:
        existing = {}

    token = get_ebay_token()
    rates = get_rates()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    updated = []
    for card in cards:
        price, sources = compute_price(token, rates, card)
        prev = existing.get(card["id"])
        history = prev["history"][-(HISTORY_DAYS - 1):] if prev else []

        if price is None and prev:
            price = prev["history"][-1]["price"] if prev["history"] else None

        if price is not None:
            history.append({"date": today, "price": price})

        print(f"{card['name']}: {price} EUR | sources: {sources}")

        updated.append(
            {
                "id": card["id"],
                "game": card["game"],
                "name": card["name"],
                "set": card["set"],
                "grade": card["grade"],
                "qty": card.get("qty", 1),
                "price": price,
                "sources": sources,
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
