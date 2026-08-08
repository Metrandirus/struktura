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


VOLTUM_CLAV_WORD = {"01": "одноклавишный", "02": "двухклавишный", "03": "трёхклавишный", "04": "четырёхклавишный"}

# Расшифровка кода артикула Voltum S70: VLS[категория][подтип][цвет].
# Ниже — то, что подтверждено официальным списком названий поставщика
# (включая "голые" механизмы с суффиксом M, у которых название говорит прямо,
# без цвета/клавиш — самый надёжный источник смысла кода).
#
# Важно: подтип (2-я пара цифр) означает РАЗНОЕ в зависимости от количества
# клавиш для кода "07" — у одноклавишных это "карточный" выключатель (16А),
# у двухклавишных — "для жалюзи" (10А). Поэтому такие коды прописаны отдельно
# по конкретному cc, а не общим правилом на все клавиши сразу — это как раз
# тот тип допущения, из-за которого мы уже один раз ошиблись с категориями.

# Подтипы, одинаковые по смыслу независимо от числа клавиш (cc):
VOLTUM_SWITCH_SUBTYPE = {
    "01": ("Выключатель", "10А"),
    "02": ("Выключатель с подсветкой", "10А"),
    "06": ("Выключатель с самовозвратом", "10А"),
}
# Подтипы, которые значат разное в зависимости от cc — ключ (cc, ss).
# Значение — (уточняющее слово, ампераж): в исходных названиях оно идёт ПОСЛЕ
# "VOLTUM S70", а не сразу после "Выключатель" (в отличие от "с подсветкой"/
# "с самовозвратом" в VOLTUM_SWITCH_SUBTYPE выше) — сверено дословно с
# "Выключатель встраиваемый VOLTUM S70 карточный 16А".
VOLTUM_SWITCH_SUBTYPE_BY_CC = {
    ("01", "07"): ("карточный", "16А"),
    ("02", "07"): ("для жалюзи", "10А"),
}
VOLTUM_PASSCROSS_SUBTYPE = {
    "03": ("Проходной переключатель", "10А"),
    "04": ("Проходной переключатель с подсветкой", "10А"),
    "05": ("Перекрёстный переключатель", "10А"),
}
# Розетки: категория (2-я пара цифр после VLS) переключает "линейку", подтип -
# конкретный вариант внутри неё.
VOLTUM_SOCKET_NAMES = {
    ("04", "01"): "Розетка с заземлением",
    ("04", "02"): "Розетка с заземлением и защитными шторками",
    ("04", "03"): "Розетка с заземлением, защитными шторками и крышкой (IP44)",
    ("04", "04"): "Розетка с заземлением, защитными шторками и USB (A+A)",
    # 04/05 (A+C) официально не подтверждён — только по аналогии с 04/04 (та же
    # цена A+A подтверждена, A+C предполагаем по обычной для USB-C наценке).
    ("04", "05"): "Розетка с заземлением, защитными шторками и USB (A+C)",
    ("06", "01"): "Розетка компьютерная RJ45 кат.6",
    ("06", "02"): "Розетка компьютерная двойная RJ45+RJ45 кат.6",
    ("06", "03"): "Розетка акустическая 4-местная",
    ("06", "04"): "Розетка телевизионная оконечная TV+SAT",
    ("06", "07"): "Розетка TV+RJ45 кат.6",
}

# "Голые" механизмы (артикул заканчивается на "M" без кода цвета, например
# VLS0101M) — не путать с линией "Металл" (M сразу после VLS, например
# VLSM120112). У них нет цвета/накладки, поэтому и название отдельное — берём
# формулировки прямо из официального списка поставщика, дословно.
VOLTUM_MECH_NAMES = {
    ("Выключатели", "01", "01"): "Механизм выключателя одноклавишного 10А",
    ("Выключатели", "02", "01"): "Механизм выключателя двухклавишного 10А",
    ("Выключатели", "03", "01"): "Механизм выключателя трёхклавишного 10А",
    ("Выключатели", "01", "06"): "Механизм выключателя с самовозвратом одноклавишного 10А",
    ("Выключатели", "02", "06"): "Механизм выключателя с самовозвратом двухклавишного 10А",
    ("Выключатели", "01", "07"): "Механизм выключателя карточного",
    ("Выключатели", "02", "07"): "Механизм выключателя жалюзи",
    ("Переключатели", "01", "05"): "Механизм перекрёстного переключателя одноклавишного 10А",
    ("Переключатели", "02", "05"): "Механизм перекрёстного переключателя двухклавишного 10А",
    ("Переключатели", "01", "03"): "Механизм проходного переключателя одноклавишного 10А",
    ("Переключатели", "02", "03"): "Механизм проходного переключателя двухклавишного 10А",
    ("Розетки", "06", "07"): "Механизм розетки TV+RJ45 кат. 6, S70",
    ("Розетки", "06", "03"): "Механизм розетки акустической 4-местной, S70",
    ("Розетки", "06", "01"): "Механизм розетки компьютерной RJ45 кат.6, S70",
    ("Розетки", "06", "02"): "Механизм розетки компьютерной двойной RJ45+RJ45 кат.6, S70",
    ("Розетки", "04", "01"): "Механизм розетки с заземлением 16А, S70",
    ("Розетки", "04", "04"): "Механизм розетки с заземлением и защитными шторками 16А, с USB A+A, S70",
    ("Розетки", "06", "04"): "Механизм розетки телевизионной оконечной TV+SAT, S70",
}


def voltum_full_name(article: str, vid: str, fallback_name: str):
    """Пытается собрать описательное название вместо общего "Тип S70 VLSxxxx"
    по коду артикула. Возвращает fallback_name без изменений, если код не
    попадает ни в одну из подтверждённых схем — лучше оставить как есть,
    чем придумать неверное название."""
    m_mech = re.match(r"^VLS(\d{2})(\d{2})M$", str(article))
    if m_mech:
        cc, ss = m_mech.groups()
        return VOLTUM_MECH_NAMES.get((vid, cc, ss), fallback_name)

    m = re.match(r"^VLS(M?)(\d{2})(\d{2})\d{2}$", str(article))
    if not m:
        return fallback_name
    metal, cc, ss = m.groups()
    suffix = " Metal" if metal else ""

    if vid == "Рамки" and cc == "10":
        try:
            posts = int(ss)
        except ValueError:
            return fallback_name
        word = "пост" if posts == 1 else ("поста" if 2 <= posts <= 4 else "постов")
        return f"Рамка S70{suffix} на {posts} {word}"

    if vid == "Розетки":
        key = (cc, ss)
        if key in VOLTUM_SOCKET_NAMES:
            return f"{VOLTUM_SOCKET_NAMES[key]} S70{suffix}"
        return fallback_name

    if vid in ("Выключатели", "Переключатели"):
        by_cc = VOLTUM_SWITCH_SUBTYPE_BY_CC.get((cc, ss))
        if by_cc and vid == "Выключатели":
            qualifier, amps = by_cc
            return f"Выключатель встраиваемый VOLTUM S70{suffix} {qualifier} {amps}"

        entry = VOLTUM_SWITCH_SUBTYPE.get(ss) if vid == "Выключатели" else VOLTUM_PASSCROSS_SUBTYPE.get(ss)
        if entry:
            base, amps = entry
            clav = VOLTUM_CLAV_WORD.get(cc)
            if clav:
                return f"{base} встраиваемый VOLTUM S70{suffix} {clav} {amps}"
            return f"{base} встраиваемый VOLTUM S70{suffix} {amps}"

    return fallback_name


def fix_switch_category(df: pd.DataFrame, art_col: str, name_col: str, type_col: str) -> pd.DataFrame:
    """У поставщика 'Перекрестный/Проходной переключатель' иногда попадает
    в категорию 'Выключатели' — переносим в 'Переключатели'.

    Основной способ — по коду артикула: у Voltum S70 формат VLS[категория][подтип][цвет],
    где подтип 03/04 = проходной переключатель, 05 = перекрёстный. Он надёжен независимо
    от того, есть ли в выгрузке полное описание товара или только короткое "Выключатель S70 VLSxxxxxx"
    (в еженедельной автовыгрузке названия обычно короткие, так что раньше эта проверка
    молча не срабатывала). Плюс запасной вариант — по тексту названия, если он есть.
    """
    by_code = df[art_col].astype(str).str.match(r"^VLS\d{2}(03|04|05)\d{2}$")
    by_name = df[name_col].str.contains("Перекрестный переключатель|Проходной переключатель", case=False, na=False, regex=True)
    # ограничиваем текущей категорией "Выключатели" — иначе по чистому совпадению
    # цифр в артикуле (03/04/05 на этих позициях) под правило случайно попадают
    # совсем другие товары (розетки, рамки и т.д.), у которых те же цифры значат
    # совсем другое.
    mask = (by_code | by_name) & (df[type_col] == "Выключатели")
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
            name = voltum_full_name(article, vid, name)
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
    df = fix_switch_category(df, art_col, name_col, type_col)

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
