"""
enroll_me.py — Record and enroll the owner's voice profile.

Run once (or re-run to improve the profile):
    python enroll_me.py

Records 30 seconds of your voice, extracts a speaker embedding,
and saves it as the high-priority "owner" profile.
Re-running merges the new sample with the existing profile (running mean),
making identification more robust over time.
"""

import sys
import time
import queue
import threading
import numpy as np
import sounddevice as sd
import soundfile as sf
from pathlib import Path

RESET  = "\033[0m"; BOLD = "\033[1m"
CYAN   = "\033[96m"; GREEN = "\033[92m"
YELLOW = "\033[93m"; RED   = "\033[91m"

SAMPLE_RATE    = 16000
RECORD_SECONDS = 30
OUTPUT_WAV     = Path("speakers/enroll_me_temp.wav")


def record_voice(seconds: int) -> np.ndarray:
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, time_info, status):
        if status:
            print(f"  {YELLOW}Audio: {status}{RESET}")
        q.put(indata.copy())

    frames_needed = SAMPLE_RATE * seconds
    collected = []

    print(f"\n  {CYAN}●{RESET} Говорите сейчас… ({seconds} сек)\n")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        callback=cb, blocksize=int(SAMPLE_RATE * 0.5)):
        bar_width = 40
        start = time.time()
        while sum(len(b) for b in collected) < frames_needed:
            try:
                block = q.get(timeout=0.5)
                collected.append(block)
            except queue.Empty:
                continue

            elapsed = time.time() - start
            pct     = min(1.0, elapsed / seconds)
            filled  = int(bar_width * pct)
            bar     = "█" * filled + "░" * (bar_width - filled)
            remain  = max(0, seconds - elapsed)
            print(f"\r  [{bar}] {remain:.0f}s  ", end="", flush=True)

    print(f"\n\n  {GREEN}+ Запись завершена.{RESET}")
    return np.concatenate(collected, axis=0).flatten()


def main():
    print(f"\n{BOLD}{'─'*52}{RESET}")
    print(f"  Регистрация голосового профиля владельца")
    print(f"{'─'*52}")
    print(f"""
  Эта процедура создаёт ваш персональный голосовой профиль.
  Приложение будет автоматически распознавать вас в записях
  и помечать ваши реплики символом ★.

  Рекомендации для лучшего результата:
  • Говорите в обычном темпе, как на рабочей встрече
  • Произносите разные фразы — не повторяйте одно слово
  • Не нужно читать по бумаге — говорите свободно
  • Желательно тихое помещение (то же, где обычно проходят встречи)
  • Длительность: 30 секунд
""")
    name = input(f"  {BOLD}>{RESET} Ваше имя (для транскриптов): ").strip()
    if not name:
        name = "Вы"

    input(f"\n  Нажмите Enter чтобы начать запись…")

    Path("speakers").mkdir(exist_ok=True)

    audio = record_voice(RECORD_SECONDS)
    OUTPUT_WAV.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(OUTPUT_WAV), audio, SAMPLE_RATE)

    print(f"\n  Извлекаем голосовой вектор…")

    try:
        from speakers import enroll_speaker
        speaker_id = enroll_speaker(OUTPUT_WAV, name=name, is_owner=True)
    except ImportError as e:
        print(f"  {RED}Ошибка импорта: {e}{RESET}")
        print("  Запустите: pip install pyannote.audio")
        sys.exit(1)

    OUTPUT_WAV.unlink(missing_ok=True)

    if speaker_id:
        print(f"\n  {GREEN}+ Профиль сохранён!{RESET}")
        print(f"""
  Имя в транскриптах: {BOLD}{name} ★{RESET}
  ID профиля: {speaker_id}

  Запустите ещё раз чтобы дополнить профиль новыми образцами —
  чем больше образцов, тем точнее распознавание.
""")
    else:
        print(f"\n  {RED}Не удалось сохранить профиль.{RESET}")
        print("  Проверьте установку pyannote.audio и попробуйте снова.")
        sys.exit(1)


if __name__ == "__main__":
    main()
