#!/usr/bin/env python3
"""
setup.py — Windows setup wizard for voice-logger.
Run once: python setup.py
"""
import subprocess, sys, json, os, shutil
from pathlib import Path

RESET  = "\033[0m"; BOLD = "\033[1m"
CYAN   = "\033[96m"; GREEN = "\033[92m"
YELLOW = "\033[93m"; RED   = "\033[91m"

def h(text):    print(f"\n{BOLD}{CYAN}{text}{RESET}")
def ok(text):   print(f"  {GREEN}+{RESET} {text}")
def warn(text): print(f"  {YELLOW}!{RESET}  {text}")
def err(text):  print(f"  {RED}x{RESET} {text}")
def ask(prompt, default=""): return input(f"  {BOLD}>{RESET} {prompt} [{default}]: ").strip() or default

print(f"\n{BOLD}{'─'*55}{RESET}")
print(f"  Voice Logger - Windows Setup")
print(f"{'─'*55}")

# 1. Python version
h("1. Proverkа Python")
if sys.version_info < (3, 10):
    err(f"Нужен Python 3.10+. У вас: {sys.version}")
    sys.exit(1)
ok(f"Python {sys.version.split()[0]}")

# 2. ffmpeg
h("2. ffmpeg")
if shutil.which("ffmpeg"):
    ok("ffmpeg найден в PATH")
else:
    warn("ffmpeg не найден. Whisper не сможет работать без него.")
    print("""
  Установите одним из способов:

  A) Winget (Windows 10/11):
     winget install Gyan.FFmpeg

  B) Chocolatey:
     choco install ffmpeg

  C) Вручную: https://ffmpeg.org/download.html
     -> распакуйте, добавьте bin\\ в PATH

  После установки перезапустите этот скрипт.
""")
    if ask("Продолжить без ffmpeg? (y/n)", "n").lower() != "y":
        sys.exit(0)

# 3. GPU
h("3. GPU / CUDA")
print("""
  Модель Whisper large требует ~10 GB VRAM (GPU) или ~16 GB RAM (CPU).
  На CPU расшифровка 5 мин аудио займет ~3-8 мин.
  На GPU с CUDA — ~20-40 сек.
""")
has_gpu = ask("Есть NVIDIA GPU с CUDA? (y/n)", "n").lower() == "y"
torch_pkg = "torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121" if has_gpu else "torch"

# 4. Install deps
h("4. Установка зависимостей")
print("  Устанавливаем пакеты (может занять несколько минут)...\n")

pkgs = [
    ("PyTorch",    torch_pkg),
    ("Whisper",    "openai-whisper"),
    ("Audio",      "sounddevice soundfile numpy"),
    ("pyannote",   "pyannote.audio"),
    ("Google",     "google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"),
    ("Dashboard",  "flask flask-cors"),
    ("Outlook",    "pywin32"),
    ("Toast notif","win10toast"),
]

for label, pkg in pkgs:
    try:
        cmd = [sys.executable, "-m", "pip", "install", "-q"] + pkg.split()
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok(f"{label}")
    except subprocess.CalledProcessError:
        err(f"{label} — ошибка. Запустите вручную: pip install {pkg}")

# pywin32 post-install
try:
    scripts = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
    if scripts.exists():
        subprocess.call([sys.executable, str(scripts), "-install"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception:
    pass

# 5. Config
h("5. Конфигурация")
cfg = json.loads(Path("config.json").read_text())

cfg["whisper_model"] = "large"
ok("Whisper model = large")

lang = ask("Язык записей (ru/en/ka/пусто=авто)", "ru")
cfg["language"] = lang if lang else None
ok(f"Язык = {cfg['language'] or 'авто'}")

chunk = int(ask("Длина чанка записи в секундах", "300"))
cfg["chunk_seconds"] = chunk
ok(f"Чанк = {chunk} сек ({chunk//60} мин)")

# 6. Google Drive
h("6. Google Drive -> NotebookLM")
print("""
  Шаги (один раз):
  1. https://console.cloud.google.com/
     -> Создать проект -> API & Services -> Google Drive API -> Enable

  2. Credentials -> + Create Credentials -> OAuth 2.0 Client ID
     -> Desktop app -> Download JSON
     -> сохранить как credentials.json в папку voice-logger\\

  3. В Google Drive создайте папку "VoiceLogs"
     Скопируйте ID из адресной строки:
     drive.google.com/drive/folders/ВОТ_ЭТОТ_ID

  4. В NotebookLM: + New notebook -> Add Source
     -> Google Drive -> выберите папку VoiceLogs
""")
gdrive_id = ask("Вставьте Google Drive Folder ID (или Enter чтобы пропустить)", "")
cfg["gdrive_folder_id"] = gdrive_id
if gdrive_id:
    ok(f"Drive folder ID: {gdrive_id[:30]}...")
else:
    warn("ID не задан. Добавьте позже в config.json или в дашборде.")

# 7. HuggingFace token (pyannote diarization)
h("7. HuggingFace token (диаризация спикеров)")
print("""
  pyannote.audio требует бесплатный HuggingFace токен.
  Шаги (один раз, 2 минуты):
  1. Зарегистрируйтесь на https://huggingface.co/
  2. Settings -> Access Tokens -> New token (read) -> скопируйте
  3. Примите условия использования моделей:
     https://huggingface.co/pyannote/speaker-diarization-3.1
     https://huggingface.co/pyannote/segmentation-3.0
     (нажмите "Agree and access repository" на каждой странице)
""")
hf_token = ask("Вставьте HuggingFace токен (hf_...)", "")
cfg["hf_token"] = hf_token
if hf_token.startswith("hf_"):
    ok("HuggingFace токен сохранён")
elif hf_token:
    warn("Токен выглядит необычно. Проверьте — должен начинаться с hf_")
else:
    warn("Токен не задан. Диаризация будет работать в режиме одного спикера.")
    warn("Добавьте позже: hf_token в config.json")

# 8. Loopback (Teams/Zoom)
h("8. Loopback для Teams/Zoom")
print("  Проверяем наличие устройства захвата системного звука...")
try:
    from audio_capture import find_loopback_device, list_audio_devices
    lb = find_loopback_device()
    if lb is not None:
        devices = list_audio_devices()
        name = next((d["name"] for d in devices if d["index"] == lb), str(lb))
        ok(f"Loopback обнаружен: [{lb}] {name}")
        ok("Teams / Zoom звонки будут записываться автоматически")
        cfg["loopback_device"] = lb
    else:
        warn("Loopback устройство не найдено.")
        print("""
  Для записи Teams/Zoom включите «Стерео микшер» (Stereo Mix):
  1. Правой кнопкой на значок звука в трее → Параметры звука
  2. Вкладка «Запись» → правой кнопкой в пустом месте
  3. «Показать отключённые устройства»
  4. Правой кнопкой на «Стерео микшер» → Включить
  5. Запустите setup.py снова

  Или используйте VB-Cable (бесплатно):
  https://vb-audio.com/Cable/
""")
except Exception as e:
    warn(f"Не удалось проверить loopback: {e}")

# 9. Outlook
h("9. Outlook Calendar")
try:
    import win32com.client
    win32com.client.Dispatch("Outlook.Application")
    ok("Outlook доступен — интеграция активна")
except Exception as e:
    warn(f"Outlook недоступен: {e}")
    warn("Убедитесь что Outlook Desktop запущен перед стартом recorder.py")

# 10. Write config & launchers
h("10. Сохранение")
Path("config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
ok("config.json записан")

start_bat = (
    "@echo off\n"
    "title Voice Logger\n"
    "echo Starting Voice Logger...\n"
    "start \"Recorder\" /min cmd /k python recorder.py\n"
    "timeout /t 3 /nobreak >nul\n"
    "start \"Dashboard\" /min cmd /k python dashboard_server.py\n"
    "timeout /t 2 /nobreak >nul\n"
    "start http://localhost:7331\n"
    "echo Done. Dashboard: http://localhost:7331\n"
)
Path("start.bat").write_text(start_bat)
ok("start.bat создан")

Path("stop.bat").write_text(
    "@echo off\n"
    "taskkill /f /fi \"WINDOWTITLE eq Recorder*\" >nul 2>&1\n"
    "taskkill /f /fi \"WINDOWTITLE eq Dashboard*\" >nul 2>&1\n"
    "echo Voice Logger остановлен.\n"
)
ok("stop.bat создан")

h("Готово!")
print(f"""
  СЛЕДУЮЩИЙ ШАГ — зарегистрируйте свой голос:
    python enroll_me.py
  (30 секунд речи — приложение запомнит вас навсегда)

  Запуск:   дважды кликните start.bat
  Стоп:     дважды кликните stop.bat
  Дашборд:  http://localhost:7331

  Первый запуск:
    - Whisper large (~3 GB) скачается автоматически
    - Браузер откроется для авторизации Google Drive
    - Outlook должен быть запущен для метаданных встреч
    - Новые незнакомые спикеры появятся в баннере дашборда
""")
