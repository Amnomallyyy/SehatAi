import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://dawaai.pk"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def section_text(section: Tag) -> str:
    heading = section.find(["h2", "h3"])
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    return clean_text(section.get_text(" ", strip=True).replace(heading_text, "", 1))


def extract_faqs(soup: BeautifulSoup) -> list[dict[str, str]]:
    faq_section = soup.select_one('.content_container[data-tab="faqs"]')
    if not faq_section:
        return []

    faqs = []
    for title in faq_section.select(".accordion .title"):
        question = clean_text(title.get_text(" ", strip=True))
        answer_node = title.find_next_sibling(class_="content")
        answer = clean_text(answer_node.get_text(" ", strip=True) if answer_node else "")
        if question:
            faqs.append({"question": question, "answer": answer})
    return faqs


def extract_products(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    listing = soup.select_one("section.medicine_listing")
    if not listing:
        return []

    products = []
    for item in listing.select(":scope > .container > div"):
        name_link = item.select_one("h4 a")
        if not name_link:
            continue

        manufacturer_node = item.select_one("h4 + p")
        manufacturer_text = clean_text(
            manufacturer_node.get_text(" ", strip=True) if manufacturer_node else ""
        )
        manufacturer = re.sub(r"^By\s+", "", manufacturer_text, flags=re.I)
        pack_node = item.find("span", string=re.compile(r"Pack size:", re.I))
        price = ""
        for node in reversed(item.select("p")):
            value = clean_text(node.get_text(" ", strip=True))
            if re.fullmatch(r"Rs\.\s*[\d,]+(?:\.\d+)?", value, flags=re.I):
                price = value
                break

        products.append({
            "brand_name": clean_text(name_link.get_text(" ", strip=True)),
            "manufacturer": manufacturer,
            "pack_size": (
                clean_text(pack_node.get_text(" ", strip=True))
                .replace("Pack size:", "", 1)
                .strip()
                if pack_node else ""
            ),
            "price_pkr": price,
            "product_url": urljoin(page_url, name_link.get("href", "")),
        })
    return products


def extract_generic_page(html: str, page_url: str = BASE_URL) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one(".generics-glossary-detail .title h1")
    description_node = soup.select_one(".generics-glossary-detail > section:first-child p")
    details: dict[str, Any] = {
        "source_url": page_url,
        "generic_salt": clean_text(title.get_text(" ", strip=True) if title else ""),
        "description": clean_text(
            description_node.get_text(" ", strip=True) if description_node else ""
        ),
        "contraindications": "",
        "side_effects": "",
        "expert_advice": [],
        "faqs": extract_faqs(soup),
        "products": extract_products(soup, page_url),
    }

    for section in soup.select(".generics-glossary-detail .content_container"):
        tab_name = section.get("data-tab", "")
        if tab_name == "contradications":
            details["contraindications"] = section_text(section)
        elif tab_name == "side-effect":
            details["side_effects"] = section_text(section)
        elif tab_name == "Expert_Advice":
            details["expert_advice"] = [
                clean_text(li.get_text(" ", strip=True))
                for li in section.select("li")
                if clean_text(li.get_text(" ", strip=True))
            ]

    return details


def load_html(source: str, session: requests.Session) -> tuple[str, str]:
    source_path = Path(source)
    if source_path.is_file():
        return source_path.read_text(encoding="utf-8"), source_path.resolve().as_uri()

    url = source if source.startswith(("http://", "https://")) else f"{BASE_URL}/generic/{source.strip().lower().replace(' ', '-') }"
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text, response.url


def write_outputs(data: dict[str, Any], json_path: str, csv_path: str) -> None:
    Path(json_path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    fields = ["generic_salt", "brand_name", "manufacturer", "pack_size", "price_pkr", "product_url"]
    with Path(csv_path).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for product in data["products"]:
            writer.writerow({
                "generic_salt": data["generic_salt"],
                **{field: product.get(field, "") for field in fields[1:]},
            })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract complete generic medicine information from Dawaai HTML."
    )
    parser.add_argument("source", help="Dawaai generic URL or a saved HTML file")
    parser.add_argument("--json", default="medicines.json", dest="json_path")
    parser.add_argument("--csv", default="medicines.csv", dest="csv_path")
    args = parser.parse_args()

    with requests.Session() as session:
        html, page_url = load_html(args.source, session)
    data = extract_generic_page(html, page_url)
    write_outputs(data, args.json_path, args.csv_path)
    print(f"Extracted {len(data['products'])} products for {data['generic_salt']!r}")
    print(f"JSON: {args.json_path}")
    print(f"CSV:  {args.csv_path}")


if __name__ == "__main__":
    main()
