# Konfigurator

Многостраничный сайт-подборщик товаров:

- `/` — главная, ссылки на все конфигураторы
- `/voltum/` — конфигуратор электрофурнитуры Voltum S70 (розетки, выключатели, переключатели, светорегуляторы, терморегуляторы, рамки). Фото, артикулы, цвета, корзина, выгрузка КП в PDF.
- `/track/` — конфигуратор трековых систем (в разработке):
  - `/track/single-phase/`, `/track/magnetic/`, `/track/three-phase/` — по типу шинопровода
  - внутри каждого типа — отдельные страницы по брендам: `mayfar.html`, `maytoni.html`, `artelamp.html`, `stluce.html`, `novotech.html`

Статический сайт, без сборки и бэкенда — открывается напрямую или публикуется через GitHub Pages.

## Публикация через GitHub Pages

1. Settings → Pages → Source: `main` branch, папка `/ (root)`.
2. Сайт появится по адресу `https://<username>.github.io/konfigurator/`.

## Автообновление остатков и цен (раз в неделю)

Каталог Voltum и Werkel хранится не внутри HTML, а в отдельных файлах:
`voltum/data/voltum-products.json` и `voltum/data/werkel-products.json`.
Страница `voltum/index.html` загружает их при открытии через `fetch()`.

Раз в неделю (по понедельникам, можно поменять в cron) GitHub Actions
(`.github/workflows/update-catalog.yml`) сам:

1. скачивает полную выгрузку по ссылке поставщика (`scripts/update_catalog.py`);
2. оставляет только строки с `Фабрика = Voltum` и `Фабрика = Werkel`;
3. переписывает `voltum/data/*.json`;
4. коммитит и пушит изменения — GitHub Pages подхватывает их автоматически.

### Настройка (один раз)

1. **Settings → Secrets and variables → Actions → New repository secret**
   Имя: `CATALOG_EXPORT_URL`
   Значение: `https://opt.lu.ru/export/catalog/?uid=...&token=...&file=...`
   (ссылку с токеном не храним в коде — так безопаснее и правильнее)

2. **Settings → Actions → General → Workflow permissions** → выставить
   *Read and write permissions*, чтобы workflow мог закоммитить обновлённые файлы.

3. Проверить: **Actions → Обновление каталога → Run workflow** — запустить вручную
   и посмотреть в логах, сколько товаров Voltum/Werkel найдено. Если там 0 —
   значит в выгрузке колонка производителя называется не «Фабрика»; откройте
   `scripts/update_catalog.py` и добавьте нужное имя в `MANUFACTURER_COLUMNS`.

После первого успешного запуска обновление будет происходить само каждый
понедельник — ничего вручную делать не нужно.

## Как наполнить раздел трековых систем

Каждая страница бренда (`/track/<тип>/<бренд>.html`) сейчас — заглушка с блоком
`<div class="placeholder-box">`. Чтобы добавить каталог, замените этот блок на
галерею товаров по образцу `/voltum/index.html` (тот же подход: JSON с товарами,
фильтры, корзина, кнопка «Подготовить КП»).

