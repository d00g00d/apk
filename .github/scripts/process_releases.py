import os
import re
import json
import shutil
import zipfile
import subprocess
import requests
from pathlib import Path
from androguard.core.apk import APK

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
DEFAULT_DPI = os.environ.get("DEFAULT_DPI", "240")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}
API_URL = f"https://api.github.com/repos/{REPO}"

PATCHED_PATTERN = re.compile(r"^([a-zA-Z0-9_-]+)_v(\d+(?:\.\d+)+)_pine(\.[a-zA-Z0-9]+)$")
PLAIN_PATTERN = re.compile(r"^([a-zA-Z0-9_-]+)_v(\d+(?:\.\d+)+)(\.[a-zA-Z0-9]+)$")

PAYLOAD_DIR = Path("patcher_payload")

def run_cmd(cmd: list):
    print(f"Выполнение: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

def clean_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", name.replace(" ", "_"))
    return cleaned or "App"

def extract_metadata(file_path: Path):
    ext = file_path.suffix.lower()
    if ext == ".apk":
        try:
            apk = APK(str(file_path))
            app_name = apk.get_app_name() or file_path.stem
            version = apk.get_androidversion_name() or "1.0.0"
            return clean_name(app_name), version, ext
        except Exception as e:
            print(f"Ошибка чтения метаданных APK {file_path}: {e}")

    stem_clean = re.sub(r"_nopatch$", "", file_path.stem, flags=re.IGNORECASE)
    match = re.search(r"^(.*?)[-_ ]*v?(\d+(?:\.\d+)+)", stem_clean, re.IGNORECASE)
    if match:
        raw_name, version = match.groups()
        return clean_name(raw_name), version, ext

    return clean_name(stem_clean), "1.0.0", ext

def patch_apk(input_apk: Path, output_apk: Path, target_dpi: str):
    work_dir = Path("apk_work")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir()

    apk_obj = APK(str(input_apk))
    package_name = apk_obj.get_package()
    orig_app_name = apk_obj.get_attribute_value("application", "name") or ""
    print(f"Патчинг пакета: {package_name} (Application: '{orig_app_name}', целевой DPI: {target_dpi})")

    with zipfile.ZipFile(input_apk, "r") as zf:
        zf.extractall(work_dir)

    meta_inf = work_dir / "META-INF"
    if meta_inf.exists():
        for sig_file in meta_inf.iterdir():
            if sig_file.suffix.upper() in {".SF", ".RSA", ".DSA", ".EC", ".MF"}:
                sig_file.unlink()

    dex_files = list(work_dir.glob("classes*.dex"))
    next_dex_num = len(dex_files) + 1
    target_dex_name = "classes.dex" if next_dex_num == 1 else f"classes{next_dex_num}.dex"
    shutil.copy(PAYLOAD_DIR / "classes_patch.dex", work_dir / target_dex_name)
    print(f"Добавлен DEX: {target_dex_name}")

    src_lib = PAYLOAD_DIR / "lib"
    dst_lib = work_dir / "lib"
    if src_lib.exists():
        for arch_dir in src_lib.iterdir():
            if arch_dir.is_dir():
                target_arch = dst_lib / arch_dir.name
                if target_arch.exists() or not dst_lib.exists():
                    target_arch.mkdir(parents=True, exist_ok=True)
                    for so_file in arch_dir.glob("*.so"):
                        shutil.copy(so_file, target_arch / so_file.name)
                        print(f"Скопирована библиотека: {arch_dir.name}/{so_file.name}")

    raw_manifest = work_dir / "AndroidManifest.xml"
    patched_manifest = work_dir / "AndroidManifest_patched.xml"

    manifest_cmd = [
        "java", "-jar", "/usr/local/bin/ManifestEditor.jar",
        str(raw_manifest),
        "-o", str(patched_manifest),
        "-f",
        "-e", "1",
        "-md", f"pine.target_dpi:{target_dpi}",
        "-an", "com.store.patcher.PineApplication"
    ]
    if orig_app_name:
        manifest_cmd.extend(["-md", f"pine.orig_app:{orig_app_name}"])

    run_cmd(manifest_cmd)
    shutil.move(patched_manifest, raw_manifest)

    unsigned_apk = Path("unsigned_patched.apk")
    aligned_apk = Path("aligned_patched.apk")
    no_compress_ext = {".arsc", ".so"}

    with zipfile.ZipFile(unsigned_apk, "w") as zf:
        for file_path in work_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(work_dir).as_posix()
                compress_type = zipfile.ZIP_STORED if file_path.suffix.lower() in no_compress_ext else zipfile.ZIP_DEFLATED
                zf.write(file_path, arcname, compress_type=compress_type)

    run_cmd(["zipalign", "-f", "-p", "4", str(unsigned_apk), str(aligned_apk)])
    run_cmd([
        "apksigner", "sign",
        "--ks", "release.jks",
        "--ks-pass", "env:KEYSTORE_PASSWORD",
        "--ks-key-alias", os.environ["KEY_ALIAS"],
        "--key-pass", "env:KEY_PASSWORD",
        "--out", str(output_apk),
        str(aligned_apk)
    ])

    shutil.rmtree(work_dir, ignore_errors=True)
    unsigned_apk.unlink(missing_ok=True)
    aligned_apk.unlink(missing_ok=True)

def process_all_releases():
    resp = requests.get(f"{API_URL}/releases", headers=HEADERS)
    resp.raise_for_status()
    releases = resp.json()

    store_items = []
    temp_dir = Path("temp_assets")
    temp_dir.mkdir(exist_ok=True)

    for release in releases:
        if release.get("draft"):
            continue

        upload_url = release["upload_url"].split("{")[0]
        assets = release.get("assets", [])

        body_text = release.get("body") or ""
        dpi_match = re.search(r"dpi\s*:\s*(\d+)", body_text, re.IGNORECASE)
        release_dpi = dpi_match.group(1) if dpi_match else DEFAULT_DPI

        for asset in assets:
            asset_id = asset["id"]
            asset_name = asset["name"]
            download_url = asset["browser_download_url"]

            patched_match = PATCHED_PATTERN.match(asset_name)
            plain_match = PLAIN_PATTERN.match(asset_name)

            if patched_match:
                app_name, version, ext = patched_match.groups()
                final_name = asset_name
                final_url = download_url
                file_size = asset["size"]
                is_patched = True
                print(f"Пропуск (уже пропатчен): {asset_name}")
            elif plain_match:
                app_name, version, ext = plain_match.groups()
                final_name = asset_name
                final_url = download_url
                file_size = asset["size"]
                is_patched = False
                print(f"Пропуск (уже стандартизирован): {asset_name}")
            else:
                print(f"Обработка нового файла: {asset_name}")
                skip_patch = "_nopatch" in asset_name.lower()
                raw_path = temp_dir / asset_name

                with requests.get(download_url, headers=HEADERS, stream=True) as r:
                    r.raise_for_status()
                    with open(raw_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)

                app_name, version, ext = extract_metadata(raw_path)

                if ext == ".apk" and not skip_patch:
                    final_name = f"{app_name}_v{version}_pine{ext}"
                    processed_path = temp_dir / final_name
                    patch_apk(raw_path, processed_path, release_dpi)
                    is_patched = True
                else:
                    final_name = f"{app_name}_v{version}{ext}"
                    processed_path = temp_dir / final_name
                    shutil.copy(raw_path, processed_path)
                    is_patched = False

                for existing in assets:
                    if existing["name"] == final_name and existing["id"] != asset_id:
                        print(f"Удаление старого дубликата: {final_name}")
                        requests.delete(f"{API_URL}/releases/assets/{existing['id']}", headers=HEADERS)

                print(f"Загрузка в релиз: {final_name}")
                with open(processed_path, "rb") as f:
                    upload_headers = {**HEADERS, "Content-Type": "application/octet-stream"}
                    up_res = requests.post(
                        f"{upload_url}?name={final_name}",
                        headers=upload_headers,
                        data=f
                    )
                    up_res.raise_for_status()
                    up_data = up_res.json()

                print(f"Удаление исходного файла: {asset_name}")
                requests.delete(f"{API_URL}/releases/assets/{asset_id}", headers=HEADERS)

                final_url = up_data["browser_download_url"]
                file_size = up_data["size"]
                raw_path.unlink(missing_ok=True)
                processed_path.unlink(missing_ok=True)

            store_items.append({
                "name": app_name,
                "version": version,
                "patched_pine": is_patched,
                "target_dpi": int(release_dpi) if is_patched else None,
                "file_name": final_name,
                "size_bytes": file_size,
                "download_url": final_url,
                "release_tag": release["tag_name"],
                "updated_at": asset["updated_at"]
            })

    store_items.sort(key=lambda x: (x["name"].lower(), x["version"]), reverse=True)

    with open("store.json", "w", encoding="utf-8") as f:
        json.dump({
            "repository": REPO,
            "total_apps": len(store_items),
            "apps": store_items
        }, f, ensure_ascii=False, indent=2)
    print(f"Каталог store.json успешно обновлен. Всего приложений: {len(store_items)}")

if __name__ == "__main__":
    process_all_releases()
