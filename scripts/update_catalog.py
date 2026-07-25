#!/usr/bin/env python3
"""
Скачивает выгрузку остатков/цен у поставщика (opt.lu.ru), оставляет только
товары брендов Voltum, Werkel и трековых систем (MyFar) и перезаписывает
JSON-файлы каталога, которые читает сайт (data/voltum-products.json,
data/werkel-products.json, data/track-products.json).

Запускается вручную:
    CATALOG_EXPORT_URL="https://opt.lu.ru/export/..." python3 scripts/update_catalog.py

Или автоматически раз в неделю через GitHub Actions (см. .github/workflows/update-catalog.yml),
где ссылка хранится в секрете репозитория CATALOG_EXPORT_URL — потому что в ней
зашит приватный токен доступа, и её не стоит хранить в открытом виде в коде.
"""

import io
import json
import os
import re
import sys
import datetime

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Настройки

EXPORT_URL = os.environ.get("CATALOG_EXPORT_URL", "").strip()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

# Возможные варианты названия колонки с производителем/брендом — выгрузка
# может отличаться, поэтому проверяем несколько вариантов.
MANUFACTURER_COLUMNS = ["Фабрика", "Производитель", "Бренд", "Manufacturer", "Brand"]

BRANDS = {
    "voltum": "Voltum",
    "werkel": "Werkel",
}

# Бренды трековых систем, которые сейчас включены на сайте. Остальные
# (Maytoni, Artelamp, StLuce, Novotech, Arlight) пока сознательно не берём —
# добавятся сюда, когда будут готовы их данные/правила определения типа системы.
TRACK_BRANDS = ["MyFar"]

# Значение колонки "Группа" для трековых и шинных систем в выгрузке.
TRACK_GROUP_VALUE = "Трековые и шинные системы"

# Определение типа системы (однофазная/магнитная) по названию серии (колонка
# "Серия") — специфично для каждого бренда, т.к. одинаковые слова у разных
# брендов означают разное. Сопоставление даётся как список ключевых слов,
# которые ищутся в значении "Серия" (регистронезависимо, по вхождению).
BRAND_SYSTEM_KEYWORDS = {
    "MyFar": {
        "magnetic": ["magline", "flowpoint", "flow", "ray", "sphere", "neon"],
        "single": ["single", "lines", "beam", "edging", "tube", "ball"],
    },
}

# Дополнительные характеристики трек-товаров, которые нужно сохранить, если
# они заполнены в выгрузке (колонка -> короткий ключ в JSON). Если у конкретной
# позиции значения нет — соответствующий ключ в неё просто не добавляется.
TRACK_SPEC_COLUMNS = {
    "Напряжение": "volt",
    "Световой поток, lm": "lm",
    "Цветовая температура, K": "k",
    "Длина, см": "len",
}
TRACK_COLOR_COLUMN = "Цвет арматуры"

# Voltum: код цвета — последние 2 цифры артикула (используется как запасной
# вариант, если цвет не удалось вытащить из названия).
VOLTUM_SUFFIX_COLOR = {
    "01": "белый глянцевый", "02": "белый матовый", "03": "кашемир", "04": "шёлк",
    "05": "сталь", "06": "титан", "07": "графит", "08": "черный матовый",
    "10": "платина", "12": "антрацит", "13": "хлопок", "14": "капучино",
    "15": "деним", "16": "серый",
}


def log(*args):
    print(*args, file=sys.stderr)


def download_export(url: str) -> pd.DataFrame:
    log(f"Скачиваю выгрузку: {url[:60]}...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return pd.read_excel(io.BytesIO(resp.content))


def find_column(df: pd.DataFrame, candidates) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise SystemExit(
        f"Не нашёл колонку производителя. Ожидал одну из {candidates}, "
        f"а в файле такие колонки: {df.columns.tolist()}"
    )


def find_column_soft(df: pd.DataFrame, name: str):
    """Как find_column, но не падает — возвращает None, если не нашёл (для
    необязательных колонок вроде доп. характеристик трек-товаров). Сначала
    ищет точное совпадение, потом — без учёта регистра/лишних пробелов."""
    if name in df.columns:
        return name
    norm = re.sub(r"\s+", " ", name).strip().lower()
    for c in df.columns:
        if re.sub(r"\s+", " ", str(c)).strip().lower() == norm:
            return c
    return None


def extract_color_from_name(name: str):
    """Ищет цвет в скобках в конце названия, например '... (кашемир) VLS010301'."""
    if not isinstance(name, str):
        return None
    matches = re.findall(r"\(([^)]+)\)", name)
    return matches[-1].strip() if matches else None


def voltum_color(article: str, name: str):
    color = extract_color_from_name(name)
    if color:
        return color
    m = re.search(r"(\d{2})$", str(article))
    if m and m.group(1) in VOLTUM_SUFFIX_COLOR:
        return VOLTUM_SUFFIX_COLOR[m.group(1)]
    return None


def fix_switch_category(df: pd.DataFrame, name_col: str, type_col: str) -> pd.DataFrame:
    """У поставщика 'Перекрестный/Проходной переключатель' иногда попадает
    в категорию 'Выключатели' — переносим в 'Переключатели'."""
    mask = (
        df[name_col].str.contains("Перекрестный переключатель|Проходной переключатель", case=False, na=False, regex=True)
        & (df[type_col] != "Переключатели")
    )
    df.loc[mask, type_col] = "Переключатели"
    return df


def guess_track_system(brand: str, series_text: str):
    if not isinstance(series_text, str):
        return None
    keywords = BRAND_SYSTEM_KEYWORDS.get(brand, {})
    low = series_text.lower()
    for sys_id, words in keywords.items():
        if any(w in low for w in words):
            return sys_id
    return None


def build_track_records(df: pd.DataFrame, name_col, type_col, art_col, price_col, stock_col, photo_col, series_col, manu_col):
    """Трек-товары собираются отдельно от build_records, т.к. у них своя логика
    цвета (колонка 'Цвет арматуры', а не 'Серия') и набор доп. характеристик."""
    color_col = find_column_soft(df, TRACK_COLOR_COLUMN)
    spec_cols = {key: find_column_soft(df, col_name) for col_name, key in TRACK_SPEC_COLUMNS.items()}
    missing_specs = [name for name, col in spec_cols.items() if col is None]
    if missing_specs:
        log(f"ℹ️  Не нашёл в выгрузке колонки для характеристик: {missing_specs} — они просто не попадут в товары.")

    records = []
    for _, r in df.iterrows():
        article = str(r[art_col]).strip()
        name = r[name_col]
        vid = r[type_col]
        brand = str(r[manu_col]).strip()

        img = r[photo_col]
        img = img if isinstance(img, str) and img.strip() else None

        try:
            price = int(r[price_col])
        except (ValueError, TypeError):
            continue
        try:
            stock = int(r[stock_col])
        except (ValueError, TypeError):
            stock = 0

        color = None
        if color_col and pd.notna(r.get(color_col)):
            color = str(r[color_col]).strip()

        series_val = r[series_col] if series_col and pd.notna(r.get(series_col)) else ""

        record = {
            "a": article,
            "b": "track",
            "br": brand,
            "t": vid,
            "n": name,
            "p": price,
            "st": stock,
            "img": img,
            "c": color,
            "sys": guess_track_system(brand, series_val),
        }

        # доп. характеристики — добавляем ключ, только если значение реально есть
        for key, col in spec_cols.items():
            if col and pd.notna(r.get(col)):
                val = r[col]
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
                record[key] = val

        records.append(record)
    return records


def build_records(df: pd.DataFrame, brand_key: str, name_col, type_col, art_col, price_col, stock_col, photo_col, series_col=None, manu_col=None):
    records = []
    for _, r in df.iterrows():
        article = str(r[art_col]).strip()
        name = r[name_col]
        vid = r[type_col]

        if brand_key == "voltum":
            color = voltum_color(article, name)
        else:
            color = r[series_col] if series_col and pd.notna(r.get(series_col)) else None

        img = r[photo_col]
        img = img if isinstance(img, str) and img.strip() else None

        try:
            price = int(r[price_col])
        except (ValueError, TypeError):
            continue  # пропускаем строки без цены — некорректные данные
        try:
            stock = int(r[stock_col])
        except (ValueError, TypeError):
            stock = 0

        record = {
            "a": article,
            "b": brand_key,
            "t": vid,
            "n": name,
            "p": price,
            "st": stock,
            "img": img,
            "c": color,
        }
        records.append(record)
    return records


def main():
    if not EXPORT_URL:
        raise SystemExit("Переменная окружения CATALOG_EXPORT_URL не задана.")

    df = download_export(EXPORT_URL)
    log(f"Всего строк в выгрузке: {len(df)}")

    manu_col = find_column(df, MANUFACTURER_COLUMNS)
    art_col = find_column(df, ["Артикул"])
    name_col = find_column(df, ["Наименование"])
    type_col = find_column(df, ["Вид"])
    price_col = find_column(df, ["МРЦ", "Цена"])
    stock_col = find_column(df, ["Остаток поставщика", "Остаток"])
    photo_col = find_column(df, ["Основное фото", "Фото"])
    series_col = "Серия" if "Серия" in df.columns else None

    # общее переименование категорий
    df[type_col] = df[type_col].replace({"Диммеры": "Светорегуляторы"})
    df = fix_switch_category(df, name_col, type_col)

    os.makedirs(DATA_DIR, exist_ok=True)
    summary = {}

    for brand_key, brand_name in BRANDS.items():
        brand_df = df[df[manu_col].astype(str).str.strip().str.lower() == brand_name.lower()].copy()
        if brand_df.empty:
            log(f"⚠️  Не нашёл ни одной позиции бренда {brand_name} — проверьте колонку '{manu_col}'.")
        records = build_records(
            brand_df, brand_key, name_col, type_col, art_col, price_col, stock_col, photo_col, series_col
        )
        out_path = os.path.join(DATA_DIR, f"{brand_key}-products.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
        summary[brand_key] = len(records)
        log(f"{brand_name}: {len(records)} товаров -> {out_path}")

    # Трековые системы: Фабрика из TRACK_BRANDS И Группа = "Трековые и шинные системы".
    # Если группа так не называется в этой выгрузке — берём всё для этих брендов
    # (лучше показать, чем ничего) и пишем предупреждение в лог.
    track_brand_mask = df[manu_col].astype(str).str.strip().str.lower().isin([b.lower() for b in TRACK_BRANDS])
    group_col = find_column_soft(df, "Группа")
    if group_col:
        track_group_mask = df[group_col].astype(str).str.strip() == TRACK_GROUP_VALUE
        track_df = df[track_brand_mask & track_group_mask].copy()
    else:
        log(f"⚠️  Не нашёл колонку 'Группа' — беру все строки брендов {TRACK_BRANDS} без фильтра по группе.")
        track_df = df[track_brand_mask].copy()

    track_records = build_track_records(track_df, name_col, type_col, art_col, price_col, stock_col, photo_col, series_col, manu_col)
    with open(os.path.join(DATA_DIR, "track-products.json"), "w", encoding="utf-8") as f:
        json.dump(track_records, f, ensure_ascii=False)
    summary["track"] = len(track_records)
    log(f"Трековые системы: {len(track_records)} товаров -> data/track-products.json")
    if len(track_records) == 0:
        log(f"ℹ️  Не нашёл строк с Фабрика in {TRACK_BRANDS} и Группа='{TRACK_GROUP_VALUE}'. "
            f"Проверьте точные значения этих колонок в выгрузке.")

    with open(os.path.join(DATA_DIR, "updated-at.json"), "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.date.today().strftime("%d.%m.%Y")}, f, ensure_ascii=False)

    log(f"Готово: {summary}")


if __name__ == "__main__":
    main()
