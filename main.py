from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    Page,
)

MOODLE_BASE = "https://moodle.elct.lnu.edu.ua"
LOGIN_URL = f"{MOODLE_BASE}/login/index.php?loginredirect=1"
DASHBOARD_URL = f"{MOODLE_BASE}/my/"

DOWNLOAD_ROOT = Path("moodle_downloads")


# ============================================================
#  Допоміжні функції для імен файлів / папок
# ============================================================

def safe_name(name: str, max_len: int = 80) -> str:
    """
    Нормалізує назву для використання як ім'я папки:
    - прибирає заборонені символи
    - обрізає дуже довгі рядки
    """
    name = name.strip().replace("\u00a0", " ")
    name = re.sub(r"[\\/]", "-", name)
    name = re.sub(r'[:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def extract_filename_from_url(file_url: str) -> str:
    """
    Дістає останню частину шляху з URL як ім'я файлу.
    """
    parsed = urlparse(file_url)
    name = Path(unquote(parsed.path)).name
    return name or "file"


def make_unique_path(path: Path) -> Path:
    """
    Якщо файл уже існує – додає суфікс _2, _3, ...
    """
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 2
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


# ============================================================
#  Playwright: логін
# ============================================================

def parse_folder_tree(soup: BeautifulSoup) -> List[str]:
    """
    Рекурсивно проходить дерево Moodle mod_folder (foldertree / ygtvitem)
    і дістає всі посилання pluginfile.php.
    """
    files: List[str] = []

    def recurse(node):
        # 1. Шукаємо <a href="...pluginfile.php...">
        for a in node.select("a[href*='pluginfile.php']"):
            href = a.get("href")
            if not href:
                continue
            if href.startswith("/"):
                href = MOODLE_BASE + href
            if href not in files:
                files.append(href)

        # 2. Рекурсія по піддеревам — ygtvchildren
        # важливо: без '>' на початку, інакше bs4 падає
        for child in node.select(".ygtvchildren > .ygtvitem"):
            recurse(child)


    # корені дерев у всіх .foldertree на сторінці
    for tree in soup.select(".foldertree"):
        # всередині зазвичай <div class="filemanager"><div class="ygtvitem" id="ygtv0">...
        roots = tree.find_all("div", class_="ygtvitem", recursive=False)
        if not roots:
            # fallback – якщо структура інша
            roots = tree.select(".ygtvitem")
        for r in roots:
            recurse(r)

    return files


def wait_for_manual_login(page: Page) -> None:
    """
    1. Відкриває сторінку логіну.
    2. Чекає, поки ти вручну залогінишся і дійдеш до /my/.
    """
    page.goto(LOGIN_URL)
    print(f"[+] Відкрив: {LOGIN_URL}")
    print("[+] Натисни 'OpenID Connect', залогінься через Microsoft.")
    print("[+] Коли в браузері відкриється 'Особистий кабінет' (/my/), повернись в консоль і натисни Enter.")
    input("    >>> ")

    if not page.url.startswith(DASHBOARD_URL):
        print(f"[!] Увага: зараз сторінка = {page.url}, а не {DASHBOARD_URL}")
        print("[!] Але все одно продовжую. Якщо це не той екран – зупини скрипт Ctrl+C.")


# ============================================================
#  Парсинг дашборду
# ============================================================

def parse_dashboard_courses(html: str) -> List[Dict[str, str]]:
    """
    З HTML дашборду витягує блок 'Мої курси'
    і повертає список словників {title, url}.
    """
    soup = BeautifulSoup(html, "html.parser")

    headers = soup.find_all(["h2", "h3"])
    target_section = None

    for h in headers:
        if "мої курси" in h.get_text(strip=True).lower():
            target_section = h.find_parent("section")
            break

    if not target_section:
        print("[!] Не знайшов блок з заголовком 'Мої курси'.")
        return []

    course_links = target_section.select("ul.unlist li a")

    courses: List[Dict[str, str]] = []
    for a in course_links:
        title = a.get_text(strip=True)
        url = a.get("href")
        if not url:
            continue
        if url.startswith("/"):
            url = MOODLE_BASE + url
        courses.append({"title": title, "url": url})

    return courses


# ============================================================
#  Парсинг assign (дедлайни, оцінки, файли)
# ============================================================

def _normalize_number(text: str) -> Optional[float]:
    """
    Перетворює '7,00' -> 7.0, '10.5' -> 10.5
    Повертає None, якщо не вдалося розпарсити.
    """
    t = text.strip().replace("\u00a0", " ")
    t = t.replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_assign_page(page: Page, url: str) -> Dict[str, Any]:
    """
    Заходить на сторінку assign і повертає метадані:
    - start_at / due_at / cutoff_at (як сирий текст, якщо є)
    - attempt / submission_status / grading_status / time_remaining / last_modified
    - files: список URL (pluginfile.php)
    - grade_text: '7,00 / 10,00'
    - grade_raw: 7.0
    - grade_max: 10.0
    """
    print(f"        [assign] відкриваю сторінку завдання: {url}")
    try:
        page.goto(url, timeout=30000)
        time.sleep(1)
    except Exception as e:
        print(f"        [!] Не вдалось відкрити assign {url}: {e}")
        return {}

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    meta: Dict[str, Any] = {
        "start_at": None,
        "due_at": None,
        "cutoff_at": None,
        "attempt": None,
        "submission_status": None,
        "grading_status": None,
        "time_remaining": None,
        "last_modified": None,
        "files": [],
        "grade_text": None,
        "grade_raw": None,
        "grade_max": None,
    }

    # -------------------------------
    # ДАТИ (activity header + .dates + таблиці)
    # -------------------------------

    activity_dates_block = soup.select_one(".activity-dates")
    if activity_dates_block:
        for div in activity_dates_block.find_all("div", recursive=False):
            strong_tag = div.find("strong")
            if not strong_tag:
                continue
            label_raw = strong_tag.get_text(" ", strip=True)
            label = label_raw.lower()
            full_text = div.get_text(" ", strip=True)
            date_text = full_text.replace(label_raw, "", 1).strip(" :")

            if not date_text:
                continue

            if "початок приймання" in label and meta["start_at"] is None:
                meta["start_at"] = date_text
            elif "термін спливає" in label and meta["due_at"] is None:
                meta["due_at"] = date_text

    dates_block = soup.select_one(".dates")
    date_rows: List[Any] = []
    if dates_block:
        date_rows.extend(dates_block.find_all("div"))

    status_table_rows = soup.select(".submissionstatustable tr")
    date_rows.extend(status_table_rows)

    for row in date_rows:
        text_raw = row.get_text(" ", strip=True)
        text = text_raw.lower()

        if ("доступно з" in text or "доступне з" in text or "available from" in text) and meta["start_at"] is None:
            meta["start_at"] = text_raw
        if ("термін здачі" in text or "due date" in text) and meta["due_at"] is None:
            meta["due_at"] = text_raw
        if ("остання можливість здачі" in text or "cut-off" in text) and meta["cutoff_at"] is None:
            meta["cutoff_at"] = text_raw

    # -------------------------------
    # SUBMISSION STATUS TABLE
    # -------------------------------
    for row in status_table_rows:
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        label = th.get_text(" ", strip=True).lower()
        value = td.get_text(" ", strip=True)

        if "спроба номер" in label:
            meta["attempt"] = value
        elif "статус роботи" in label:
            meta["submission_status"] = value
        elif "статус оцінення" in label:
            meta["grading_status"] = value
        elif "залишилося часу" in label:
            meta["time_remaining"] = value
        elif "востаннє змінено" in label:
            meta["last_modified"] = value

    # -------------------------------
    # ПРИКРІПЛЕНІ ФАЙЛИ (URL)
    # -------------------------------
    file_links = soup.select("a[href*='pluginfile.php']")
    files: List[str] = []
    for a in file_links:
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = MOODLE_BASE + href
        if href not in files:
            files.append(href)

    # !!!! ВАЖЛИВО: тут ми більше НЕ робимо fallback на будь-які інші a[href],
    # щоб не тягнути коментарні URL-и типу mod/assign/view.php?...comment_area=...
    meta["files"] = files

    # -------------------------------
    # ОЦІНКА (блок Відгук → Оцінка)
    # -------------------------------
    feedback_rows = soup.select(".feedbacktable tr")
    for row in feedback_rows:
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        label = th.get_text(" ", strip=True).lower()
        if "оцінка" in label:
            grade_text = td.get_text(" ", strip=True)
            meta["grade_text"] = grade_text

            parts = grade_text.split("/")
            if len(parts) == 2:
                left = _normalize_number(parts[0])
                right = _normalize_number(parts[1])
                meta["grade_raw"] = left
                meta["grade_max"] = right
            break

    return meta


# ============================================================
#  Завантаження файлів через requests
# ============================================================

def fix_encoding(filename: str) -> str:
    """
    Фіксит стандартний випадок: UTF-8 байти були прочитані як latin1,
    через що з'явилися 'Ð', 'Ñ', 'â', '€' і т.п.
    """
    if not filename:
        return filename

    # якщо видно класичні артефакти — пробуємо перетворити
    if any(ch in filename for ch in ["Ð", "Ñ", "â", "€"]):
        try:
            # повертаємося у байти так, як Python прочитав заголовок
            raw = filename.encode("latin1", errors="strict")
            # а тепер декодуємо як справжній UTF-8
            fixed = raw.decode("utf-8")
            return fixed
        except UnicodeError:
            # якщо раптом не вийшло — лишаємо як є
            return filename

    return filename

def filename_from_content_disposition(cd: str | None) -> Optional[str]:
    """
    Парсить заголовок Content-Disposition і дістає ім'я файлу, якщо воно є.
    Повертає None, якщо нічого не знайшло.
    """
    if not cd:
        return None

    cd = cd.strip()

    # Варіант filename*=UTF-8''....
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, flags=re.IGNORECASE)
    if m:
        fname = unquote(m.group(1))
    else:
        # Звичайний filename="..."
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, flags=re.IGNORECASE)
        if not m:
            m = re.search(r'filename\s*=\s*([^;]+)', cd, flags=re.IGNORECASE)
        if not m:
            return None
        fname = m.group(1).strip().strip('"')

    # Забираємо можливі шляхи типу path/to/file.pdf
    fname = fname.split("/")[-1].split("\\")[-1]
    fname = fix_encoding(fname)
    return fname or None

def download_file(
    session: requests.Session,
    file_url: str,
    dest_dir: Path,
    default_name: str,
) -> Optional[Path]:
    """
    Завантажує файл через HTTP (requests) і повертає реальний локальний шлях.
    - dest_dir: папка, куди класти файл
    - default_name: ім'я за замовчуванням (з URL), якщо в Content-Disposition нічого немає
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        print(f"            ↳ качаю файл: {file_url}")
        with session.get(file_url, stream=True, timeout=120) as r:
            r.raise_for_status()

            # Пробуємо дістати ім'я з Content-Disposition
            cd = r.headers.get("Content-Disposition")
            cd_name = filename_from_content_disposition(cd)

            filename = cd_name or default_name or "file"
            filename = fix_encoding(filename)
            filename = safe_name(filename)
            dest_path = make_unique_path(dest_dir / filename)

            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        return dest_path

    except Exception as e:
        print(f"            [!] Помилка при завантаженні {file_url}: {e}")
        return None

def download_files_for_activity(
    session: requests.Session,
    file_urls: List[str],
    course_title: str,
    section_index: int,
    section_name: str,
    activity_index: int,
    activity_name: str,
) -> List[str]:
    """
    Завантажує всі file_urls у папку:
    moodle_downloads/Курс/NN_Секція/NN_Активність/файл
    Повертає список локальних шляхів (str).
    """
    downloaded_paths: List[str] = []
    if not file_urls:
        return downloaded_paths

    course_dir = DOWNLOAD_ROOT / safe_name(course_title)
    section_dir_name = f"{section_index:02d}_{safe_name(section_name or f'Section {section_index}')}"
    activity_dir_name = f"{activity_index:02d}_{safe_name(activity_name or f'Activity {activity_index}')}"
    dest_dir = course_dir / section_dir_name / activity_dir_name

    for file_url in file_urls:
        default_name = extract_filename_from_url(file_url)
        saved_path = download_file(session, file_url, dest_dir, default_name)
        if saved_path is not None:
            downloaded_paths.append(str(saved_path))

    return downloaded_paths



# ============================================================
#  Інші активності (resource / folder): збирання лінків
# ============================================================

def collect_activity_files(page: Page, activity_url: str) -> List[str]:
    """
    Заходить на сторінку активності (resource/folder/щось ще) і збирає:
    - всі pluginfile.php (href/src у a/iframe/img/source/video/audio)
    - для mod_folder рекурсивно проходить foldertree
    """
    if not activity_url or activity_url == "#" or not activity_url.startswith("http"):
        return []

    # Якщо вже дають прямий pluginfile – просто повертаємо його.
    if "pluginfile.php" in activity_url:
        return [activity_url]

    print(f"        [activity] відкриваю сторінку активності: {activity_url}")
    try:
        page.goto(activity_url, timeout=30000)
        time.sleep(1)
    except Exception as e:
        print(f"        [!] Помилка при відкритті активності {activity_url}: {e}")
        return []

    soup = BeautifulSoup(page.content(), "html.parser")

    # 1) FOLDER TREE (mod_folder)
    if soup.select_one(".foldertree"):
        try:
            urls = parse_folder_tree(soup)
            print(f"        [foldertree] знайдено файлів: {len(urls)}")
            if urls:
                return urls
        except Exception as e:
            # не валимо весь курс через один кривий foldertree
            print(f"        [foldertree] помилка парсингу: {e}")

    # 2) Пошук pluginfile.php у href/src різних тегів
    urls: List[str] = []

    def add_if_pluginfile(raw: Optional[str]) -> None:
        if not raw:
            return
        u = raw.strip()
        if not u or u.startswith("#") or u.lower().startswith("javascript:"):
            return
        if u.startswith("/"):
            u = MOODLE_BASE + u
        if "pluginfile.php" in u and u not in urls:
            urls.append(u)

    # <a href="...pluginfile.php...">
    for a in soup.find_all("a"):
        add_if_pluginfile(a.get("href"))

    # <iframe src="...pluginfile.php...">, <img>, <source>, <video>, <audio>
    for tag in soup.find_all(["iframe", "img", "source", "video", "audio"]):
        add_if_pluginfile(tag.get("src"))

    if urls:
        # тепер для ресурсів типу "Тематика лекцій" ти отримаєш прямі *.pdf
        return urls

    # 3) fallback – перший адекватний лінк (типу resource),
    # якщо раптом жодного pluginfile.php не знайшли
    candidates = soup.select(
        ".resourceworkaround a, "
        ".activityinstance a, "
        ".region-main a"
    )
    for a in candidates:
        href = a.get("href")
        if not href:
            continue
        href = href.strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if href.startswith("/"):
            href = MOODLE_BASE + href
        urls.append(href)
        break

    if not urls:
        # зовсім fallback – повертаємо саму сторінку активності
        urls.append(activity_url)

    return urls


# ============================================================
#  Парсинг сторінки курсу
# ============================================================

def parse_course_page(
    page: Page,
    session: requests.Session,
    html: str,
    url: str,
) -> Dict[str, Any]:
    """
    Парсить одну сторінку курсу:
    - назву курсу
    - список секцій
    - в кожній секції – активності
    - для assign: meta + завантаження файлів
    - для resource/folder: збір файлів + завантаження
    """
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("h1")
    course_title = title_tag.get_text(strip=True) if title_tag else url

    course_data: Dict[str, Any] = {
        "title": course_title,
        "url": url,
        "sections": [],
        "webinars": [],
    }

    sections = soup.select("li.section.main")
    if not sections:
        sections = soup.select("li.section")

    for sec_index, sec in enumerate(sections, start=1):
        name_el = sec.select_one(".sectionname, h3.sectionname, h3")
        section_name = name_el.get_text(strip=True) if name_el else ""
        section_data_name = (sec.get("data-sectionname") or "").strip()
        is_webinars_section = section_name.lower() == "вебінари" or section_data_name.lower() == "вебінари"

        section_obj: Dict[str, Any] = {
            "name": section_name,
            "activities": [],
        }

        activities = sec.select("li.activity, li[class*=modtype_]")
        for act_index, act in enumerate(activities, start=1):
            link = act.select_one("a")
            if not link:
                continue

            act_url = link.get("href") or ""
            if act_url.startswith("/"):
                act_url = MOODLE_BASE + act_url

            # 1) пробуємо стандартну назву Moodle
            name_el = act.select_one(".instancename")

            # 2) якщо немає .instancename – пробуємо data-activityname на activity-item
            if name_el:
                act_name = name_el.get_text(strip=True)
            else:
                activity_item = act.select_one(".activity-item")
                act_name = ""
                if activity_item and activity_item.has_attr("data-activityname"):
                    act_name = activity_item["data-activityname"].strip()

                # 3) fallback – текст першого лінка
                if not act_name:
                    act_name = link.get_text(strip=True)


            act_type = ""
            for cls in act.get("class", []):
                if cls.startswith("modtype_"):
                    act_type = cls.replace("modtype_", "")
                    break

            activity_obj: Dict[str, Any] = {
                "name": act_name,
                "type": act_type,
                "url": act_url,
            }

            # Якщо це секція "Вебінари" і активність є вебінаром (BigBlueButton або Google Meet)
            platform = None
            if act_type == "bigbluebuttonbn":
                platform = "bigbluebutton"
            elif act_type == "googlemeet":
                platform = "google_meet"

            if is_webinars_section and platform:
                course_data["webinars"].append(
                    {
                        "name": act_name,
                        "platform": platform,
                        "moodle_url": act_url,
                        "section_name": section_name,
                        "section_index": sec_index,
                    }
                )

            urls_to_download: List[str] = []

            if act_type == "assign":
                print(f"    [assign] парсю деталі → {act_url}")
                meta = parse_assign_page(page, act_url)
                activity_obj["meta"] = meta
                urls_to_download = meta.get("files") or []


            elif act_type in ("resource", "folder"):

                file_urls: List[str] = []

                if act_type == "folder":

                    # 1) пробуємо витягнути файли прямо з HTML курсу

                    snippet_soup = BeautifulSoup(str(act), "html.parser")

                    inline_urls = parse_folder_tree(snippet_soup)

                    if inline_urls:

                        print(f"    [folder-inline] знайдено файлів: {len(inline_urls)}")

                        file_urls = inline_urls

                    else:

                        # 2) якщо в курсі немає дерева — йдемо на окрему сторінку

                        file_urls = collect_activity_files(page, act_url)

                else:

                    # resource як і раніше

                    file_urls = collect_activity_files(page, act_url)

                activity_obj["files"] = file_urls

                urls_to_download = file_urls


            if urls_to_download:
                downloaded_paths = download_files_for_activity(
                    session=session,
                    file_urls=urls_to_download,
                    course_title=course_title,
                    section_index=sec_index,
                    section_name=section_name,
                    activity_index=act_index,
                    activity_name=act_name,
                )
                activity_obj["downloaded_files"] = downloaded_paths

            section_obj["activities"].append(activity_obj)

        if section_obj["name"] or section_obj["activities"]:
            course_data["sections"].append(section_obj)

    return course_data


# ============================================================
#  Основний цикл
# ============================================================

def scrape_everything() -> Dict[str, Any]:
    """
    Головна функція:
    - відкриває браузер
    - чекає твій логін
    - тягне дашборд
    - створює requests.Session з куками Moodle
    - обходить усі курси, парсить та качає файли
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # 1. Логін
        wait_for_manual_login(page)

        # 2. HTML дашборду
        print("[+] Знімаю HTML з дашборду...")
        page.goto(DASHBOARD_URL)
        time.sleep(2)
        dashboard_html = page.content()

        # 3. Курси
        courses_list = parse_dashboard_courses(dashboard_html)
        print(f"[+] Знайдено курсів: {len(courses_list)}")
        for c in courses_list:
            print(f"    - {c['title']} → {c['url']}")

        # 4. Створюємо requests.Session з куками з браузера
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/123.0.0.0 Safari/537.36"
        })

        cookies = context.cookies()
        for c in cookies:
            session.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )

        result: Dict[str, Any] = {
            "dashboard_url": DASHBOARD_URL,
            "courses": [],
        }

        # 5. Обхід курсів
        for idx, course in enumerate(courses_list, start=1):
            print(f"[+] [{idx}/{len(courses_list)}] Парсю курс: {course['title']}")
            try:
                page.goto(course["url"], timeout=30000)
                time.sleep(2)
                html = page.content()
                course_data = parse_course_page(page, session, html, course["url"])
                result["courses"].append(course_data)
            except Exception as e:
                print(f"[!] Помилка при парсі курсу {course['title']} ({course['url']}): {e}")

        context.close()
        browser.close()

    return result


def main() -> None:
    data = scrape_everything()
    out_path = Path("moodle_dump.json")
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] Готово! Дані збережені у {out_path.resolve()}")
    print(f"[+] Файли завантажені у {DOWNLOAD_ROOT.resolve()}")


if __name__ == "__main__":
    main()
collect_activity_files