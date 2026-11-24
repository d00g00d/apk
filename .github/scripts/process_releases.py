import os
import re
import json
import requests
from pathlib import Path
from androguard.core.apk import APK  # Для чтения метаданных из APK

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}
API_URL = f"https://api.github.com/repos/{REPO}"

# Шаблон уже отформатированного имени: AppName_v1.0.0.ext
STANDARD_PATTERN = re.compile(r"^([a-zA-Z0-9_-]+)_v(\d+(?:\.\d+)+)(\.[a-zA-Z0-9]+)$")

def clean_name(name: str) -> str:
    """Оставляет только безопасные символы для имени файла."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", name.replace(" ", "_"))

def extract_metadata(file_path: Path):
    """Извлекает (Название, Версия, Расширение) из файла."""
    ext = file_path.suffix.lower()
    
    # 1. Если это APK — достаем точное имя и версию из манифеста
    if ext == ".apk":
        try:
            apk = APK(str(file_path))
            app_name = apk.get_app_name() or file_path.stem
            version = apk.get_androidversion_name() or "1.0.0"
            return clean_name(app_name), version, ext
        except Exception as e:
            print(f"Ошибка парсинга APK {file_path}: {e}")

    # 2. Для остальных файлов (или при ошибке APK) парсим имя файла
    # Ищем паттерн вида: My App 1.2.3.exe или my-app-v2.0.zip
    match = re.search(r"^(.*?)[-_ ]*v?(\d+(?:\.\d+)+)", file_path.stem, re.IGNORECASE)
    if match:
        raw_name, version = match.groups()
        return clean_name(raw_name), version, ext

    # Fallback, если версия не найдена в имени
    return clean_name(file_path.stem), "1.0.0", ext

def process_all_releases():
    releases = requests.get(f"{API_URL}/releases", headers=HEADERS).json()
    store_items = []
    temp_dir = Path("temp_assets")
    temp_dir.mkdir(exist_ok=True)

    for release in releases:
        if release.get("draft"):
            continue
            
        release_id = release["id"]
        upload_url = release["upload_url"].split("{")[0]
        assets = release.get("assets", [])

        for asset in assets:
            asset_id = asset["id"]
            asset_name = asset["name"]
            download_url = asset["browser_download_url"]

            # Проверяем, соответствует ли файл единому формату
            match = STANDARD_PATTERN.match(asset_name)
            
            if match:
                app_name, version, ext = match.groups()
                final_name = asset_name
                final_url = download_url
                file_size = asset["size"]
            else:
                # Скачиваем нестандартный файл
                print(f"Обработка нового файла: {asset_name}")
                local_path = temp_dir / asset_name
                with requests.get(download_url, headers=HEADERS, stream=True) as r:
                    r.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)

                app_name, version, ext = extract_metadata(local_path)
                final_name = f"{app_name}_v{version}{ext}"

                # Если файл с таким именем уже есть в релизе — удаляем старый дубль
                for existing in assets:
                    if existing["name"] == final_name and existing["id"] != asset_id:
                        requests.delete(f"{API_URL}/releases/assets/{existing['id']}", headers=HEADERS)

                # Загружаем переименованный файл в релиз
                with open(local_path, "rb") as f:
                    upload_headers = {
                        **HEADERS,
                        "Content-Type": "application/octet-stream"
                    }
                    up_res = requests.post(
                        f"{upload_url}?name={final_name}",
                        headers=upload_headers,
                        data=f
                    ).json()

                # Удаляем исходный кривой файл из релиза
                requests.delete(f"{API_URL}/releases/assets/{asset_id}", headers=HEADERS)

                final_url = up_res["browser_download_url"]
                file_size = up_res["size"]
                local_path.unlink()

            # Добавляем запись в каталог магазина
            store_items.append({
                "name": app_name,
                "version": version,
                "file_name": final_name,
                "extension": ext.lstrip("."),
                "size_bytes": file_size,
                "download_url": final_url,
                "release_tag": release["tag_name"],
                "updated_at": asset["updated_at"]
            })

    # Сортируем по названию и версии
    store_items.sort(key=lambda x: (x["name"].lower(), x["version"]), reverse=True)

    # Сохраняем JSON-магазин
    store_data = {
        "repository": REPO,
        "total_apps": len(store_items),
        "apps": store_items
    }
    with open("store.json", "w", encoding="utf-8") as f:
        json.dump(store_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    process_all_releases()
