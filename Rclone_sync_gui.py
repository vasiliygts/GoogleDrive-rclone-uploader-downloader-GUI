#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RcloneSync GUI - візуальна оболонка над rclone для Google Drive (та будь-яких інших remote).

Можливості:
  * дерево локальних папок/файлів + дерево віддаленого сховища;
  * черга завдань: кожен елемент має власне призначення;
  * режими: copy / sync (дзеркало) / move;
  * контроль перезапису: перезаписувати / тільки новіші / не чіпати існуючі;
  * прогрес у реальному часі (байти, швидкість, ETA, поточні файли);
  * зворотній напрямок - завантаження з хмари на диск;
  * dry-run для безпечної перевірки перед реальним запуском.

Потрібен встановлений rclone з налаштованим remote (rclone config).

RcloneSync GUI - a visual front end for rclone and Google Drive (or any other
remote).

Features:
  * a tree of local folders/files plus a tree of the remote storage;
  * a job queue where every item has its own destination;
  * modes: copy / sync (mirror) / move;
  * overwrite control: overwrite / only newer / leave existing alone;
  * live progress (bytes, speed, ETA, files in flight);
  * the reverse direction - downloading from the cloud to disk;
  * dry-run for a safe check before doing it for real.

Requires an installed rclone with a configured remote (rclone config).
"""

import json
import os
import queue
import re
import shlex
import shutil
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

APP_NAME = "RcloneSync GUI"
APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "RcloneSyncGUI"
CFG_PATH = APP_DIR / "settings.json"

NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# --------------------------------------------------------------------------- #
#  Мова інтерфейсу / UI language
#
#  Ключ словника — сам український рядок: так його видно в коді на місці
#  вживання, а якщо перекладу немає, лишиться українська, а не порожнеча.
#  Key is the Ukrainian source string itself: it stays readable at the call
#  site, and a missing translation falls back to Ukrainian instead of blank.
# --------------------------------------------------------------------------- #

LANG = "uk"

LANGS = {"Українська": "uk", "English": "en"}

EN = {
    # --- режими й перезапис / modes and overwrite ---
    "Копіювати (нічого не видаляє)": "Copy (never deletes)",
    "Синхронізувати (ДЗЕРКАЛО — видаляє зайве)": "Sync (MIRROR — deletes extras)",
    "Перемістити (видаляє джерело)": "Move (deletes the source)",
    "Перезаписувати змінені файли": "Overwrite changed files",
    "Тільки якщо джерело новіше (--update)": "Only if the source is newer (--update)",
    "Не чіпати існуючі (--ignore-existing)": "Leave existing files alone (--ignore-existing)",

    # --- типи передачі / transfer profiles ---
    "Великі файли (100 МБ – 1 ГБ)": "Large files (100 MB – 1 GB)",
    "Дрібні файли (до кількох МБ)": "Small files (a few MB at most)",
    "Суміш · один прохід — простіше": "Mixed · one pass — simpler",
    "Суміш · два проходи — швидше": "Mixed · two passes — faster",
    "Окремо · тільки дрібні (≤64 МБ)": "Separately · small only (≤64 MB)",
    "Окремо · тільки великі (≥64 МБ)": "Separately · large only (≥64 MB)",
    "Мало потоків, великі частини. На файл припадає менше запитів, "
    "тож менший шанс, що заливка обірветься і почнеться з нуля. "
    "Пам'ять ~256 МБ (128M × 2 потоки).":
        "Few threads, large chunks. Fewer requests per file, so less chance "
        "an upload breaks and restarts from zero. Memory ~256 MB "
        "(128M × 2 threads).",
    "Тут вузьке місце не канал, а звернення до API — рятує паралелізм. "
    "Розмір частини не важить: файли й так менші за поріг 8 МіБ. "
    "--fast-list пришвидшує сканування дерева.":
        "Here the bottleneck is API calls, not bandwidth — parallelism is what "
        "helps. Chunk size is irrelevant: the files are below the 8 MiB "
        "threshold anyway. --fast-list speeds up scanning the tree.",
    "Усереднені налаштування, черга проходить один раз. Підходить, "
    "коли файли приблизно однакові за розміром, коли передача невелика, "
    "або коли в теці ДУЖЕ багато файлів: два проходи сканували б "
    "таке дерево двічі, і на скануванні втратилось би більше, "
    "ніж виграється на самій передачі.":
        "Middle-of-the-road settings, the queue runs once. Good when files are "
        "of similar size, when the transfer is small, or when a folder holds a "
        "HUGE number of files: two passes would scan that tree twice, and "
        "scanning would cost more than the transfer itself gains.",
    "Старт проганяє чергу ДВІЧІ: спершу файли до 64 МБ дванадцятьма "
    "потоками, потім більші — двома, великими частинами. Кожна "
    "половина отримує свої оптимальні налаштування. Підходить, коли "
    "в теці і відео на сотні МБ, і купа дрібниці — тобто саме там, де "
    "усереднений набір програє обом. Ціна: дерево сканується двічі.":
        "Start runs the queue TWICE: first files up to 64 MB on twelve threads, "
        "then the larger ones on two, in big chunks. Each half gets settings "
        "that suit it. Good when a folder holds both hundred-megabyte videos "
        "and a pile of small files — exactly where one averaged set loses to "
        "both. The price: the tree is scanned twice.",
    "Половина від «двох проходів», запущена окремо: передасть лише "
    "файли до 64 МБ, великі не чіпатиме. Знадобиться, коли потрібен "
    "контроль між проходами або поки що лише дрібнота. Далі, "
    "не міняючи черги, обрати «тільки великі».":
        "One half of «two passes», run on its own: transfers only files up to "
        "64 MB and leaves the large ones alone. Useful when you want control "
        "between the passes, or only the small files for now. Afterwards pick "
        "«large only» without rebuilding the queue.",
    "Друга половина, окремо: тільки файли від 64 МБ, двома потоками. "
    "Черга та сама, переформовувати не треба — фільтр сам відбере "
    "потрібне. Порядок неважливий, можна почати й з великих.":
        "The other half, on its own: only files from 64 MB up, on two threads. "
        "The queue stays as it is — the filter picks what belongs here. Order "
        "does not matter, you may start with the large ones.",

    # --- довідка з прапорців / flag reference ---
    "Розмір частини при заливці файлів, більших за 8 МіБ. Більше — менше "
    "запитів на файл і швидше, але й більше пам'яті: N × кількість потоків.":
        "Chunk size when uploading files larger than 8 MiB. Bigger means fewer "
        "requests per file and more speed, but also more memory: N × threads.",
    "Не більше N запитів до API за секунду. Головний засіб проти помилки "
    "403 rate limit: тримає нижче межі замість того, щоб натикатись на неї.":
        "At most N API requests per second. The main defence against the 403 "
        "rate-limit error: stays below the ceiling instead of hitting it.",
    "Дозволений короткий сплеск запитів. Без нього обмежено одним, "
    "що надто жорстко — ставте приблизно як --tpslimit.":
        "Allowed short burst of requests. Without it the burst is one, which is "
        "too strict — set it roughly equal to --tpslimit.",
    "Читає дерево рекурсивно за один прохід: більше пам'яті, але значно "
    "менше запитів. Вигідно на теках з великою кількістю підтек.":
        "Reads the tree recursively in one go: more memory, far fewer requests. "
        "Pays off on folders with many subfolders.",
    "Брати лише файли не більші / не менші за N. Файл рівно N підпадає "
    "під обидва фільтри.":
        "Take only files no larger / no smaller than N. A file of exactly N "
        "matches both filters.",
    "Пропустити те, що збігається. Шаблон із пробілами — у лапках: "
    "--exclude \"Мої фото/**\"":
        "Skip whatever matches. Quote a pattern containing spaces: "
        "--exclude \"My photos/**\"",
    "Переносити й порожні теки: режим copy їх типово пропускає.":
        "Transfer empty folders too: copy mode skips them by default.",
    "Обмежити швидкість, наприклад --bwlimit 2M, щоб не забивати канал.":
        "Cap the speed, e.g. --bwlimit 2M, so the link stays usable.",
    "Видаляти назовсім, повз кошик. Обережно: відкату не буде.":
        "Delete for good, bypassing the bin. Careful: there is no undo.",
    "Не чіпати ярлики Google Drive — вони мають нульовий розмір і легко "
    "потрапляють під фільтри за розміром.":
        "Leave Google Drive shortcuts alone — they have zero size and are "
        "easily caught by size filters.",

    # --- проходи й журнал / passes and log ---
    "прохід 1 · дрібні": "pass 1 · small",
    "прохід 2 · великі": "pass 2 · large",
    "Коротко": "Brief",
    "Звичайно": "Normal",
    "Докладно": "Detailed",
    "тільки завдання й підсумок по кожному": "jobs and one summary each",
    "плюс рядок на кожен переданий файл": "plus a line per transferred file",
    "плюс пропущені й виключені файли": "plus skipped and excluded files",

    # --- повідомлення rclone / rclone messages ---
    "скопійовано": "copied",
    "замінено": "replaced",
    "скопійовано на боці сервера": "copied server-side",
    "переміщено на боці сервера": "moved server-side",
    "переміщено": "moved",
    "видалено": "deleted",
    "без змін": "unchanged",
    "dry-run, не копіюється": "dry-run, not copied",
    "dry-run, не видаляється": "dry-run, not deleted",
    "виключено": "excluded",
    "оновлено час": "timestamp updated",
    "час теки оновлено": "folder timestamp updated",
    "передавати нічого": "nothing to transfer",
    "виключено ({what})": "excluded ({what})",
    "невідома помилка": "unknown error",

    # --- одиниці / units ---
    "Б": "B", "КБ": "KB", "МБ": "MB", "ГБ": "GB", "ТБ": "TB", "ПБ": "PB",
    "{h}г {m:02d}хв": "{h}h {m:02d}m",
    "{m}хв {s:02d}с": "{m}m {s:02d}s",
    "{s}с": "{s}s",

    # --- верхня панель / top bar ---
    "rclone:": "rclone:",
    "   Remote:": "   Remote:",
    "⟳ Оновити remotes": "⟳ Refresh remotes",
    "⚙ rclone config": "⚙ rclone config",
    "   Мова:": "   Language:",

    # --- дерева / trees ---
    "Локальний диск": "Local disk",
    "Google Drive / remote": "Google Drive / remote",
    "Додати теку…": "Add folder…",
    "✕ Прибрати з дерева": "✕ Remove from tree",
    "Σ Розмір теки": "Σ Folder size",
    "🗑 Видалити": "🗑 Delete",
    "файли": "files",
    "＋ Нова тека": "＋ New folder",
    "Виділено: —": "Selected: —",
    "Виділено: {n} ({what})   ·   {size}": "Selected: {n} ({what})   ·   {size}",
    "тек {n}": "{n} folders",
    "файлів {n}": "{n} files",
    "   ·   без розміру: {n} — рахує «Σ Розмір теки»":
        "   ·   size unknown: {n} — «Σ Folder size» counts them",
    "[немає доступу: {err}]": "[no access: {err}]",
    "[…забагато елементів, показано 3000]": "[…too many entries, showing 3000]",
    "⚠ у назві теки є «\\» — rclone на Windows не може її відкрити. "
    "Таку теку треба перейменувати в Google Drive.":
        "⚠ the folder name contains «\\» — rclone on Windows cannot open it. "
        "Rename such a folder in Google Drive.",
    "⏳ завантаження…": "⏳ loading…",
    "[помилка] {err}": "[error] {err}",
    "Назва нової теки в «{where}»:": "Name of the new folder in «{where}»:",
    "Створено: {target}": "Created: {target}",
    "Додати теку в дерево": "Add a folder to the tree",
    "Жодної доданої теки немає. Диски з дерева прибрати не можна.":
        "No folders have been added. Drives cannot be removed from the tree.",
    "Треба виділити зліва саму додану теку — верхній рядок, той, "
    "що з'явився після «Додати теку…».\n\n"
    "Ця кнопка прибирає теку лише з дерева; на диску вона "
    "лишається. Щоб видалити теку з диска, є «🗑 Видалити».":
        "Select the added folder itself on the left — the top-level row that "
        "appeared after «Add folder…».\n\n"
        "This button removes the folder from the tree only; on disk it stays. "
        "To delete a folder from disk use «🗑 Delete».",
    "Прибрано з дерева (на диску лишилась): {path}":
        "Removed from the tree (kept on disk): {path}",

    # --- напрямок і черга / direction and queue ---
    "⬆  Вивантажити виділене  →  у виділену теку хмари":
        "⬆  Upload selected  →  into the selected cloud folder",
    "⬇  Завантажити виділене  →  у виділену локальну теку":
        "⬇  Download selected  →  into the selected local folder",
    "Черга завдань": "Job queue",
    "Джерело": "Source",
    "Призначення": "Destination",
    "Видалити з черги": "Remove from queue",
    "Очистити чергу": "Clear queue",
    "Порахувати обсяг черги": "Measure queue size",
    "Додано в чергу під час роботи: {name} — піде одразу після поточних":
        "Added to the queue while running: {name} — goes right after the current ones",
    "Черга порожня — рахувати нічого.\n\n"
    "Ця кнопка рахує обсяг ЧЕРГИ. Щоб дізнатися розмір окремої теки, "
    "її треба виділити в дереві й натиснути «Σ Розмір теки» над цим деревом.":
        "The queue is empty — nothing to measure.\n\n"
        "This button measures the QUEUE. To learn the size of a single folder, "
        "select it in a tree and press «Σ Folder size» above that tree.",
    "Рахую обсяг черги…": "Measuring the queue…",
    "Разом у черзі: {n} файлів, {size}": "Queue total: {n} files, {size}",
    "size {src}: {err}": "size {src}: {err}",

    # --- опції / options ---
    "Режим:": "Mode:",
    "Перезапис:": "Overwrite:",
    "Dry-run (лише перевірка)": "Dry-run (check only)",
    "Потоків:": "Threads:",
    "Тип передачі:": "Transfer type:",
    "?  Довідка з прапорців": "?  Flag reference",
    "Тип передачі підбирає прапорці й кількість потоків":
        "The transfer type picks the flags and the thread count",
    "Дод. прапорці:": "Extra flags:",
    "Прапорці rclone — коротка довідка": "rclone flags — a short reference",
    "Закрити": "Close",
    "Тип передачі «{name}» · Старт прожене чергу двічі":
        "Transfer type «{name}» · Start will run the queue twice",
    "Тип передачі «{name}» · потоків {n} · {flags}":
        "Transfer type «{name}» · {n} threads · {flags}",

    # --- прогрес / progress ---
    "Прогрес": "Progress",
    "Очікування…": "Waiting…",
    "Файл   —": "File   —",
    "Обсяг   —": "Size   —",
    "Файли   —": "Files   —",
    "Файл   {pct:5.1f}%   {done} / {size}   ·   {name}{more}":
        "File   {pct:5.1f}%   {done} / {size}   ·   {name}{more}",
    "Обсяг   {pct:5.1f}%   {done} / {total}{tail}":
        "Size   {pct:5.1f}%   {done} / {total}{tail}",
    "Файли   {pct:5.1f}%   {done} / {total}{tail}":
        "Files   {pct:5.1f}%   {done} / {total}{tail}",
    "   ·   завдання {pct:.0f}%": "   ·   job {pct:.0f}%",
    "{now}/с (сер. {avg}/с)   ·   ETA {eta}   ·   перевірено {checks}   ·   помилок {errors}":
        "{now}/s (avg {avg}/s)   ·   ETA {eta}   ·   checked {checks}   ·   errors {errors}",
    "Черга: завдання {pos} з {total}   ·   справді передано {n} файлів, {size}":
        "Queue: job {pos} of {total}   ·   actually transferred {n} files, {size}",
    "   ·   обсяг черги ще рахується…": "   ·   still measuring the queue…",

    # --- кнопки запуску й журналу / run and log controls ---
    "▶  Старт": "▶  Start",
    "▶  Старт (2 проходи)": "▶  Start (2 passes)",
    "■  Стоп": "■  Stop",
    "Міряти обсяг черги": "Measure the queue",
    "Очистити журнал": "Clear log",
    "Копіювати журнал": "Copy log",
    "Стислий журнал": "Compact log",
    "Файл журналу…": "Log file…",
    "Не писати у файл": "Stop writing to file",
    "Службові мітки": "Service details",
    "Журнал:": "Log:",
    "Журнал": "Log",
    "Копіювати виділене": "Copy selection",
    "Виділити все": "Select all",
    "Копіювати весь журнал": "Copy whole log",
    "— {hint}": "— {hint}",
    "   … +{n} рядків": "   … +{n} more lines",
    "Рівень журналу: {level} — {hint}. Діє з наступного запуску.":
        "Log level: {level} — {hint}. Takes effect on the next run.",
    "Запис у файл вимкнено ({path})": "Writing to file is off ({path})",
    "Куди писати повний журнал": "Where to write the full log",
    "Усі": "All",
    "Повний журнал пишеться у {path}": "The full log is written to {path}",
    "[!] Не вдалось писати журнал у файл: {err}":
        "[!] Could not write the log to file: {err}",

    # --- rclone: пошук, конфіг / rclone: lookup, config ---
    "Вкажіть rclone": "Locate rclone",
    "Не вдалося відкрити консоль:\n{err}": "Could not open a console:\n{err}",
    "rclone не знайдено. Його треба встановити "
    "(winget install Rclone.Rclone), покласти rclone.exe поруч зі "
    "скриптом або вказати шлях кнопкою «…».":
        "rclone was not found. Install it (winget install Rclone.Rclone), put "
        "rclone.exe next to the script, or point to it with the «…» button.",
    "Помилка запуску rclone: {err}": "Failed to run rclone: {err}",
    "rclone читає конфіг: {path}   (файл {state})":
        "rclone reads its config from: {path}   (file {state})",
    "на місці": "present",
    "ВІДСУТНІЙ": "MISSING",
    "Немає жодного налаштованого remote. Створити можна кнопкою "
    "«⚙ rclone config»: n → ім'я (напр. gdrive) → drive → далі за майстром.":
        "No remote is configured. Create one with the «⚙ rclone config» button: "
        "n → name (e.g. gdrive) → drive → then follow the wizard.",
    "lsjson: {err}": "lsjson: {err}",
    "size: {err}": "size: {err}",

    # --- видалення / deletion ---
    "Видалити {n} об'єктів {where}?\n\n{preview}\n\n"
    "Теки видаляються разом із усім вмістом.":
        "Delete {n} items {where}?\n\n{preview}\n\n"
        "Folders are deleted together with everything inside.",
    "\n  … і ще {n}": "\n  … and {n} more",
    "у Кошик Windows": "to the Windows Recycle Bin",
    "у Кошик Google Drive": "to the Google Drive trash",
    "Кошик підтримується лише на Windows": "The recycle bin is Windows-only",
    "Корінь диска видалити не можна — пропущено {n}.":
        "A drive root cannot be deleted — {n} skipped.",
    "Треба виділити зліва те, що видалити.":
        "Select on the left what should be deleted.",
    "Треба виділити справа те, що видалити.":
        "Select on the right what should be deleted.",
    "Не вдалося видалити:\n{err}": "Could not delete:\n{err}",
    "[✓] У Кошик Windows: {n} об'єктів":
        "[✓] To the Windows Recycle Bin: {n} items",
    "[!] Видалення завершилось з кодом {rc}":
        "[!] Deletion finished with code {rc}",
    " (перервано)": " (aborted)",
    "Видаляю {n} об'єктів у Кошик Google Drive…":
        "Deleting {n} items to the Google Drive trash…",
    "[✓] Видалено: {target}": "[✓] Deleted: {target}",
    "[!] {target}: {err}": "[!] {target}: {err}",
    "Готово: видалено {ok}, з помилками {fail}. "
    "Видалене лежить у Кошику Drive 30 днів.":
        "Done: {ok} deleted, {fail} failed. Deleted items stay in the Drive "
        "trash for 30 days.",

    # --- розмір тек / folder sizes ---
    "{base}   (недосяжна через «\\» в назві)":
        "{base}   (unreachable: «\\» in the name)",
    "{base}   (рахую…)": "{base}   (measuring…)",
    "{base}   (не порахувалось)": "{base}   (could not measure)",
    "{base}   [{size} · {n} файлів]": "{base}   [{size} · {n} files]",
    "Треба виділити справа одну або кілька тек.":
        "Select one or more folders on the right.",
    "Треба виділити зліва одну або кілька тек.":
        "Select one or more folders on the left.",

    # --- постановка в чергу / queueing ---
    "Треба виділити зліва папки або файли для вивантаження.":
        "Select folders or files to upload on the left.",
    "Треба виділити справа теку призначення в хмарі.":
        "Select the destination folder in the cloud on the right.",
    "Треба виділити справа папки або файли для завантаження.":
        "Select folders or files to download on the right.",
    "Треба виділити зліва локальну теку призначення.":
        "Select the local destination folder on the left.",

    # --- запуск / running ---
    "Черга порожня.": "The queue is empty.",
    "Режим SYNC робить призначення точною копією джерела:\n"
    "усе зайве в теці призначення БУДЕ ВИДАЛЕНО.\n\nПродовжити?":
        "SYNC mode makes the destination an exact copy of the source:\n"
        "anything extra in the destination WILL BE DELETED.\n\nContinue?",
    "Режим MOVE видалить файли з джерела після передачі.\nПродовжити?":
        "MOVE mode deletes the source files after the transfer.\nContinue?",
    "СТАРТ · режим={mode} · перезапис={ow} · dry-run={dry}":
        "START · mode={mode} · overwrite={ow} · dry-run={dry}",
    "ТАК": "YES",
    "ні": "no",
    " · проходів: {n}": " · passes: {n}",
    "Журнал: {level} — {hint}": "Log: {level} — {hint}",
    "Міряю обсяг черги — шкали покажуть частку від усього":
        "Measuring the queue — the bars will show a share of the whole",
    "Обсяг черги не міряється — шкали показують лише поточне завдання":
        "The queue is not measured — the bars show the current job only",
    "Скасування…": "Cancelling…",
    "Скасовано користувачем.": "Cancelled by the user.",
    "Завершено з помилками: {n}": "Finished with errors: {n}",
    "Усе успішно за {time}.": "All done in {time}.",
    "Це був DRY-RUN — реальних змін не зроблено. "
    "Щоб виконати насправді, треба зняти галочку й натиснути «Старт» ще раз.":
        "That was a DRY RUN — nothing was actually changed. To do it for real, "
        "clear the checkbox and press «Start» again.",
    "Триває передача. Перервати і вийти?":
        "A transfer is running. Abort it and quit?",

    # --- виконавець / worker ---
    "[!] Обсяг «{name}» не порахувався: {err}":
        "[!] Could not measure «{name}»: {err}",
    "+ Додано в роботу: {n} нових завдань": "+ Picked up {n} new jobs",
    "══  {label}  ·  потоків {n}": "══  {label}  ·  {n} threads",
    "   · прапорці: {flags}": "   · flags: {flags}",
    "[–] Пропущено «{name}»: файл {why}": "[–] Skipped «{name}»: the file is {why}",
    "більший за {limit}": "larger than {limit}",
    "менший за {limit}": "smaller than {limit}",
    "[ПОМИЛКА ЗАПУСКУ] {err}": "[LAUNCH ERROR] {err}",
    "    ↳ Якщо призначення — корінь теки «Комп'ютери», "
    "Google не дозволяє створювати там нові теки. "
    "Призначенням треба обрати наявну теку рівнем нижче.":
        "    ↳ If the destination is the root of «Computers», Google does not "
        "allow creating folders there. Pick an existing folder one level down.",
    "×  Перервано: {name}": "×  Aborted: {name}",
    "!  {name} — завершилось з кодом {rc}": "!  {name} — exited with code {rc}",
    "за {time}": "in {time}",
    "вже є в призначенні": "already in the destination",
    "передано {n}": "{n} transferred",
    "вже було {n}": "{n} already there",
    "{n} збігаються за назвою та розміром": "{n} match by name and size",
    "нема файлів {bound}": "no files {bound}",
    "порожня тека": "empty folder",
}


def tr(s: str, /, **kw) -> str:
    """Переклад рядка плюс підстановка значень.

    Перший параметр позиційний: інакше поле на кшталт {s} чи {n} зіткнулося б
    із його іменем. / First parameter is positional-only, otherwise a field
    named {s} or {n} would clash with it.

    Translate a string and substitute values. Unknown strings pass through
    unchanged, so a missing translation shows Ukrainian rather than nothing.
    """
    out = EN.get(s, s) if LANG == "en" else s
    return out.format(**kw) if kw else out


MODES = {
    "copy": "Копіювати (нічого не видаляє)",
    "sync": "Синхронізувати (ДЗЕРКАЛО — видаляє зайве)",
    "move": "Перемістити (видаляє джерело)",
}

OVERWRITE = {
    "always": "Перезаписувати змінені файли",
    "newer": "Тільки якщо джерело новіше (--update)",
    "never": "Не чіпати існуючі (--ignore-existing)",
}

# Готові профілі: кожен задає і прапорці, і кількість потоків.
#
# Поріг --drive-upload-cutoff (8 МіБ) ділить файли надвоє: дрібніші йдуть
# одним запитом, і розмір частини для них не важить; більші ріжуться на
# частини по --drive-chunk-size, і кожна частина — окремий запит.
# Звідси й різниця: великим потрібні великі частини й мало потоків,
# дрібним — навпаки, бо там вузьке місце не канал, а звернення до API.
#
# --tpslimit усюди, поки використовується спільний client_id rclone:
# він тримає нас нижче межі замість того, щоб натикатись на 403.
#
# Ready-made profiles: each sets both the flags and the thread count.
#
# The --drive-upload-cutoff threshold (8 MiB) splits files in two: smaller
# ones go in a single request and chunk size does not matter for them; larger
# ones are cut into --drive-chunk-size pieces, each piece its own request.
# Hence the difference: large files want big chunks and few threads, small
# files the opposite, because there the bottleneck is API calls, not bandwidth.
#
# --tpslimit everywhere while rclone's shared client_id is in use: it keeps us
# below the ceiling instead of running into a 403.
PROFILES = {
    "Великі файли (100 МБ – 1 ГБ)": {
        "flags": "--drive-chunk-size 128M --tpslimit 10 --tpslimit-burst 10",
        "transfers": 2,
        "note": "Мало потоків, великі частини. На файл припадає менше запитів, "
                "тож менший шанс, що заливка обірветься і почнеться з нуля. "
                "Пам'ять ~256 МБ (128M × 2 потоки).",
    },
    "Дрібні файли (до кількох МБ)": {
        "flags": "--drive-chunk-size 16M --fast-list --tpslimit 10 --tpslimit-burst 10",
        "transfers": 12,
        "note": "Тут вузьке місце не канал, а звернення до API — рятує паралелізм. "
                "Розмір частини не важить: файли й так менші за поріг 8 МіБ. "
                "--fast-list пришвидшує сканування дерева.",
    },
    "Суміш · один прохід — простіше": {
        "flags": "--drive-chunk-size 32M --tpslimit 10 --tpslimit-burst 10",
        "transfers": 4,
        "note": "Усереднені налаштування, черга проходить один раз. Підходить, "
                "коли файли приблизно однакові за розміром, коли передача невелика, "
                "або коли в теці ДУЖЕ багато файлів: два проходи сканували б "
                "таке дерево двічі, і на скануванні втратилось би більше, "
                "ніж виграється на самій передачі.",
    },
    # Один пункт, який сам робить обидва проходи: Старт проганяє чергу двічі.
    # One entry that does both passes itself: Start runs the queue twice.
    "Суміш · два проходи — швидше": {
        "flags": "",
        "transfers": 0,
        "two_pass": True,
        "note": "Старт проганяє чергу ДВІЧІ: спершу файли до 64 МБ дванадцятьма "
                "потоками, потім більші — двома, великими частинами. Кожна "
                "половина отримує свої оптимальні налаштування. Підходить, коли "
                "в теці і відео на сотні МБ, і купа дрібниці — тобто саме там, де "
                "усереднений набір програє обом. Ціна: дерево сканується двічі.",
    },
    # Ті самі проходи окремо — якщо потрібен контроль між ними
    # або треба прогнати лише одну частину файлів.
    # Два проходи по тій самій парі «джерело → призначення»: спершу дрібні
    # з високим паралелізмом, потім великі з великими частинами. На змішаних
    # теках це помітно швидше за будь-який усереднений набір.
    # Файл рівно 64 МБ підпадає під обидва фільтри, але другий прохід
    # побачить його вже завантаженим і пропустить.
    #
    # The same passes separately - when control between them is needed, or
    # only one part of the files should go. Two passes over the same
    # source -> destination pair: first the small files with high parallelism,
    # then the large ones in big chunks. On mixed folders that is noticeably
    # faster than any averaged set. A file of exactly 64 MB matches both
    # filters, but the second pass finds it already uploaded and skips it.
    "Окремо · тільки дрібні (≤64 МБ)": {
        "flags": "--max-size 64M --drive-chunk-size 16M --fast-list "
                 "--tpslimit 10 --tpslimit-burst 10",
        "transfers": 12,
        "note": "Половина від «двох проходів», запущена окремо: передасть лише "
                "файли до 64 МБ, великі не чіпатиме. Знадобиться, коли потрібен "
                "контроль між проходами або поки що лише дрібнота. Далі, "
                "не міняючи черги, обрати «тільки великі».",
    },
    "Окремо · тільки великі (≥64 МБ)": {
        "flags": "--min-size 64M --drive-chunk-size 128M "
                 "--tpslimit 10 --tpslimit-burst 10",
        "transfers": 2,
        "note": "Друга половина, окремо: тільки файли від 64 МБ, двома потоками. "
                "Черга та сама, переформовувати не треба — фільтр сам відбере "
                "потрібне. Порядок неважливий, можна почати й з великих.",
    },
}

# Коротка довідка, щоб не лазити в документацію rclone
# A short reference, so the rclone docs are not needed for every switch
FLAG_HELP = [
    ("--drive-chunk-size N",
     "Розмір частини при заливці файлів, більших за 8 МіБ. Більше — менше "
     "запитів на файл і швидше, але й більше пам'яті: N × кількість потоків."),
    ("--tpslimit N",
     "Не більше N запитів до API за секунду. Головний засіб проти помилки "
     "403 rate limit: тримає нижче межі замість того, щоб натикатись на неї."),
    ("--tpslimit-burst N",
     "Дозволений короткий сплеск запитів. Без нього обмежено одним, "
     "що надто жорстко — ставте приблизно як --tpslimit."),
    ("--fast-list",
     "Читає дерево рекурсивно за один прохід: більше пам'яті, але значно "
     "менше запитів. Вигідно на теках з великою кількістю підтек."),
    ("--max-size N / --min-size N",
     "Брати лише файли не більші / не менші за N. Файл рівно N підпадає "
     "під обидва фільтри."),
    ("--exclude ШАБЛОН",
     "Пропустити те, що збігається. Шаблон із пробілами — у лапках: "
     "--exclude \"Мої фото/**\""),
    ("--create-empty-src-dirs",
     "Переносити й порожні теки: режим copy їх типово пропускає."),
    ("--bwlimit N",
     "Обмежити швидкість, наприклад --bwlimit 2M, щоб не забивати канал."),
    ("--drive-use-trash=false",
     "Видаляти назовсім, повз кошик. Обережно: відкату не буде."),
    ("--drive-skip-shortcuts",
     "Не чіпати ярлики Google Drive — вони мають нульовий розмір і легко "
     "потрапляють під фільтри за розміром."),
]

# чим заповнене поле прапорців до першого вибору типу передачі
# what the flags field holds before a transfer type is picked
DEFAULT_FLAGS = PROFILES["Суміш · один прохід — простіше"]["flags"]

# з чого складається профіль «обидва проходи»
# what the «both passes» profile is made of
TWO_PASS = [
    ("прохід 1 · дрібні",  "Окремо · тільки дрібні (≤64 МБ)"),
    ("прохід 2 · великі",  "Окремо · тільки великі (≥64 МБ)"),
]


# --------------------------------------------------------------------------- #
#  Утиліти / Utilities
# --------------------------------------------------------------------------- #

def find_rclone() -> str:
    # портативний rclone поруч зі скриптом має пріоритет над системним
    # a portable rclone next to the script wins over a system-wide one
    here = Path(__file__).resolve().parent / ("rclone.exe" if os.name == "nt" else "rclone")
    if here.exists():
        return str(here)
    p = shutil.which("rclone")
    if p:
        return p
    candidates = [
        r"C:\Program Files\rclone\rclone.exe",
        r"C:\rclone\rclone.exe",
        str(Path.home() / "rclone" / "rclone.exe"),
        str(Path.home() / "scoop" / "shims" / "rclone.exe"),
        "/usr/bin/rclone",
        "/usr/local/bin/rclone",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def rclone_error(stderr: str, limit: int = 300) -> str:
    """Витягує з виводу rclone саме помилку.

    Перший рядок stderr — це зазвичай NOTICE про спільний client_id,
    і показувати його як причину збою тільки збиває з пантелику.

    Pick the actual error out of rclone's output. The first stderr line is
    usually a NOTICE about the shared client_id, and showing that as the cause
    of a failure only misleads.
    """
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    lines = [l for l in lines if "client_id" not in l]
    pick = next((l for l in lines if "ERROR" in l), "") \
        or next((l for l in lines if "Failed" in l), "") \
        or (lines[-1] if lines else tr("невідома помилка"))
    for tag in ("ERROR :", "NOTICE:", "INFO  :"):
        if tag in pick:
            pick = pick.split(tag, 1)[1].strip()
            break
    return pick[:limit]


# Як показувати типові повідомлення rclone: (фрагмент, позначка, текст).
# Перший збіг виграє; невідомі повідомлення проходять як є.
# How to show rclone's usual messages: (fragment, mark, text). First match
# wins; unknown messages pass through as they are.
RCLONE_PHRASES = [
    ("Copied (new)",                      "+", "скопійовано"),
    ("Copied (replaced existing)",        "+", "замінено"),
    ("Copied (server-side copy)",         "+", "скопійовано на боці сервера"),
    ("Moved (server-side)",               "+", "переміщено на боці сервера"),
    ("Moved (into",                       "+", "переміщено"),
    ("Deleted",                           "×", "видалено"),
    ("Unchanged skipping",                "=", "без змін"),
    ("Skipped copy as --dry-run is set",  "·", "dry-run, не копіюється"),
    ("Skipped delete as --dry-run is set", "·", "dry-run, не видаляється"),
    ("Excluded (",                        "–", "виключено"),
    ("Updated modification time",         "·", "оновлено час"),
    ("Set directory modification time",   "·", "час теки оновлено"),
    ("There was nothing to transfer",     "·", "передавати нічого"),
]

# Рівень DEBUG вмикається тільки в докладному режимі, і навіть там
# беремо лише те, що говорить про долю конкретного файлу.
# DEBUG is enabled only in the detailed mode, and even there only what speaks
# about the fate of a particular file is kept.
DEBUG_KEEP = ("Unchanged skipping", "Excluded (")

# Скільки показувати в журналі. Повний текст пишеться у файл незалежно.
# How much to show in the log. The full text goes to the file regardless.
LOG_WIDGET_MAX = 5000      # рядків у вікні / lines kept in the window

LOG_LEVELS = {
    "Коротко": "short",      # лише завдання й підсумки / jobs and summaries
    "Звичайно": "normal",    # + передані файли / + transferred files
    "Докладно": "detail",    # + пропущені й службові / + skipped and service
}

LEVEL_HINT = {
    "short":  "тільки завдання й підсумок по кожному",
    "normal": "плюс рядок на кожен переданий файл",
    "detail": "плюс пропущені й виключені файли",
}

# Службове, що в звичайному режимі лише засмічує
# Service noise that only clutters the normal mode
INFO_HIDE = ("Set directory modification time", "Starting transaction limiter",
             "Starting bandwidth limiter",
             # те саме, але з причиною, скаже підсумковий рядок завдання
             # the job summary says the same, but with a reason
             "There was nothing to transfer")


def short_path(p: str, limit: int = 56) -> str:
    """Скорочує довгий шлях до початку й двох останніх частин.

    Повний шлях лишається видимим у черзі й у докладному режимі.

    Shorten a long path to its head plus the last two parts. The full path
    stays visible in the queue and in the detailed mode.
    """
    if len(p) <= limit:
        return p
    sep = "\\" if "\\" in p else "/"
    parts = [x for x in p.split(sep) if x]
    if len(parts) < 4:          # ховати нічого / nothing to hide
        return p
    return f"{parts[0]}{sep}…{sep}" + sep.join(parts[-2:])


def tidy_rclone_msg(name: str, msg: str):
    """(позначка, текст) для рядка журналу. / (mark, text) for a log line."""
    for frag, mark, text in RCLONE_PHRASES:
        if frag in msg:
            if frag == "Excluded (" and ")" in msg:
                text = tr("виключено ({what})",
                          what=msg.split("(", 1)[1].rsplit(")", 1)[0])
            else:
                text = tr(text)
            return mark, f"{name} — {text}" if name else text
    if name and not msg.startswith(name):
        msg = f"{name}: {msg}"
    return "·", msg


def parse_size(text: str):
    """«64M», «250k», «1G» -> байти. None, якщо не розпізнано.

    «64M», «250k», «1G» -> bytes. None when unrecognised.
    """
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)i?b?\s*$", (text or "").lower())
    if not m:
        return None
    mult = {"": 1, "k": 1024, "m": 1024 ** 2,
            "g": 1024 ** 3, "t": 1024 ** 4, "p": 1024 ** 5}[m.group(2)]
    return int(float(m.group(1)) * mult)


def size_bound(flags: str) -> str:
    """«>64 МБ» чи «<64 МБ» — межа, за якою прохід відбирає файли.

    «>64 MB» or «<64 MB» — the bound a pass selects files by.
    """
    for opt, sign in (("--min-size", ">"), ("--max-size", "<")):
        m = re.search(rf"{opt}\s+(\S+)", flags or "")
        if m:
            n = parse_size(m.group(1))
            if n:
                return sign + human(n).replace(".0", "")
    return ""


def size_flags(flags: str) -> list:
    """Лише обмеження за розміром — щоб порахувати обсяг саме цього проходу.

    Size limits only — so that a pass can be measured on its own.
    """
    toks = split_flags(flags)
    out = []
    for i, t in enumerate(toks[:-1]):
        if t in ("--max-size", "--min-size"):
            out += [t, toks[i + 1]]
    return out


def skip_by_size(job, flags: str) -> str:
    """Чи пропустити завдання через --max-size / --min-size.

    rclone застосовує ці фільтри лише при обході теки. Коли джерелом
    указано конкретний файл, він бере його беззастережно — тож у режимі
    двох проходів великий файл поїхав би з налаштуваннями для дрібних.
    Повертає причину пропуску або порожній рядок.

    Whether to skip a job because of --max-size / --min-size. rclone applies
    those filters only while walking a folder. When the source is one specific
    file it takes it unconditionally — so in two-pass mode a large file would
    travel with the settings meant for small ones. Returns the reason for
    skipping, or an empty string.
    """
    if job.is_dir or job.size is None:
        return ""
    toks = split_flags(flags)
    for i, t in enumerate(toks[:-1]):
        if t not in ("--max-size", "--min-size"):
            continue
        limit = parse_size(toks[i + 1])
        if limit is None:
            continue
        if t == "--max-size" and job.size > limit:
            return tr("більший за {limit}", limit=toks[i + 1])
        if t == "--min-size" and job.size < limit:
            return tr("менший за {limit}", limit=toks[i + 1])
    return ""


def same_path(p: str) -> str:
    """Ключ для порівняння шляхів: діалог вибору теки віддає «/», дерево «\\».

    A key for comparing paths: the folder dialog returns «/», the tree «\\».
    """
    return os.path.normcase(os.path.normpath(p)) if p else ""


def sane_geometry(geo: str, sw: int, sh: int) -> str:
    """Відкидає збережену позицію, якщо вікно опинилося б поза екраном.

    Класичний випадок — від'єднали другий монітор: вікно є в панелі задач,
    але на екрані його не видно й дістати важко. Розмір лишаємо, позицію
    віддаємо системі.

    Drop a saved position that would put the window off screen. The classic
    case is unplugging a second monitor: the window is in the taskbar but
    nowhere to be seen and hard to retrieve. The size is kept, the position is
    left to the system.
    """
    m = re.match(r"^(\d+)x(\d+)([+-])(-?\d+)([+-])(-?\d+)$", (geo or "").strip())
    if not m:
        # порожнє або зіпсоване — хай спрацює типовий розмір
        # empty or malformed — let the default size apply
        return ""
    w, h = int(m.group(1)), int(m.group(2))
    w, h = min(w, sw), min(h, sh)
    sx, vx = m.group(3), int(m.group(4))
    sy, vy = m.group(5), int(m.group(6))
    # «-» у Tk означає відступ від правого/нижнього краю
    # in Tk «-» means an offset from the right/bottom edge
    left = vx if sx == "+" else sw - w - vx
    top = vy if sy == "+" else sh - h - vy
    visible = 120           # пікселів має лишитись видимими / px must stay visible
    if left < visible - w or top < visible - h or left > sw - visible or top > sh - visible:
        return f"{w}x{h}"
    return f"{w}x{h}{sx}{vx}{sy}{vy}"


def send_to_recycle_bin(paths):
    """Переносить локальні шляхи в Кошик Windows.

    У stdlib такого немає: os.remove і shutil.rmtree видаляють назовсім.
    Тому йдемо через SHFileOperationW із прапорцем FOF_ALLOWUNDO —
    саме так це робить провідник.
    Повертає (код, чи_перервано); нуль означає успіх.

    Move local paths to the Windows Recycle Bin. The stdlib has nothing for
    it: os.remove and shutil.rmtree delete for good. So we go through
    SHFileOperationW with the FOF_ALLOWUNDO flag — exactly what Explorer does.
    Returns (code, aborted); zero means success.
    """
    if os.name != "nt":
        raise RuntimeError(tr("Кошик підтримується лише на Windows"))

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040          # без нього — повз кошик / else bypasses the bin
    FOF_NOERRORUI = 0x0400

    # список шляхів має бути з подвійним нулем у кінці
    # the path list must be double-null terminated
    buf = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buf
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc, bool(op.fAnyOperationsAborted)


def split_flags(s: str) -> list:
    """Розбиває рядок прапорців із повагою до лапок.

    posix=False, щоб не з'їдало зворотні слеші у Windows-шляхах
    (--exclude "D:\\Temp\\**"); зайві лапки знімаємо вручну.

    Split a flag string while respecting quotes. posix=False so backslashes in
    Windows paths survive (--exclude "D:\\Temp\\**"); leftover quotes are
    stripped by hand.
    """
    if not s or not s.strip():
        return []
    try:
        parts = shlex.split(s, posix=False)
    except ValueError:
        return s.split()
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        out.append(p)
    return out


def human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {tr(unit)}".replace(",", " ")
        n /= 1024.0
    return f"{n:.1f} {tr('ПБ')}"


def human_time(sec) -> str:
    if sec is None:
        return "—"
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "—"
    if sec < 0:
        return "—"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return tr("{h}г {m:02d}хв", h=h, m=m)
    if m:
        return tr("{m}хв {s:02d}с", m=m, s=s)
    return tr("{s}с", s=s)


def load_cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_capture(args, timeout=90):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NOWIN,
        timeout=timeout,
    )


@dataclass
class Job:
    """Одне завдання в черзі. / One job in the queue."""
    src: str          # локальний шлях або "remote:path" / local path or "remote:path"
    dst_dir: str      # тека призначення / destination folder
    is_dir: bool
    direction: str    # "up" | "down"
    name: str
    # лише для файлів; потрібен, щоб відсіяти їх за розміром
    # files only; needed to filter them out by size
    size: int = None

    def dst_full(self, mode: str) -> str:
        """rclone copy DIR DST кладе ВМІСТ теки в DST, тому дописуємо ім'я теки.

        rclone copy DIR DST puts the CONTENTS of the folder into DST, so the
        folder name is appended.
        """
        if not self.is_dir:
            return self.dst_dir
        sep = "\\" if (self.direction == "down" and os.name == "nt") else "/"
        base = self.dst_dir.rstrip("/\\")
        return f"{base}{sep}{self.name}"


# --------------------------------------------------------------------------- #
#  Обмірювання черги: знаменник для шкал прогресу
#  Measuring the queue: the denominator for the progress bars
# --------------------------------------------------------------------------- #

class PlanWorker(threading.Thread):
    """Рахує, скільки всього файлів і байтів доведеться передати.

    Сам rclone знає лише те, що встиг обійти, тому його знаменник росте
    на ходу. Тут обсяг кожного завдання міряється наперед окремою
    командою «rclone size», а список завдань перечитується в циклі —
    чергу можна поповнювати під час роботи, і шкали це врахують.

    Counts how many files and bytes there are to transfer in total. rclone
    itself knows only what it has walked so far, so its denominator grows as
    it goes. Here each job is measured up front with a separate «rclone size»
    command, and the job list is re-read in a loop — the queue may be topped
    up while running and the bars will account for it.
    """

    def __init__(self, rclone, jobs, passes, out_q):
        super().__init__(daemon=True)
        self.rclone = rclone
        self.jobs = jobs
        self.passes = passes
        self.q = out_q
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        seen = set()
        while not self._stop.is_set():
            batch = [j for j in list(self.jobs) if id(j) not in seen]
            if not batch:
                self._stop.wait(0.7)
                continue
            seen.update(id(j) for j in batch)
            for job in batch:
                for label, flags, _tr in self.passes:
                    if self._stop.is_set():
                        return
                    n, b, err = self._measure(job, flags)
                    if err:
                        self.q.put(("log", tr("[!] Обсяг «{name}» не порахувався: {err}",
                                              name=job.name, err=err)))
                    self.q.put(("plan", (id(job), label, n, b)))

    def _measure(self, job, flags):
        if not job.is_dir:
            if skip_by_size(job, flags):
                return 0, 0, ""
            return 1, int(job.size or 0), ""
        cmd = [self.rclone, "size", job.src, "--json", "--fast-list"] \
            + size_flags(flags)
        try:
            r = run_capture(cmd, timeout=1800)
            if r.returncode != 0:
                return 0, 0, rclone_error(r.stderr)
            d = json.loads(r.stdout or "{}")
            return int(d.get("count") or 0), int(d.get("bytes") or 0), ""
        except Exception as e:
            return 0, 0, str(e)


# --------------------------------------------------------------------------- #
#  Виконавець rclone у фоновому потоці
#  The rclone runner, on a background thread
# --------------------------------------------------------------------------- #

class RcloneWorker(threading.Thread):
    def __init__(self, rclone, jobs, mode, overwrite, dry_run, passes, out_q,
                 level="normal", show_cmd=False):
        """passes — список (підпис, прапорці, потоки).

        Звичайний запуск подає один прохід, кнопка «Обидва проходи» — два:
        ту саму чергу проганяємо двічі з різними фільтрами за розміром.

        passes is a list of (label, flags, threads). An ordinary run supplies
        one pass, the «both passes» type two: the same queue is run twice with
        different size filters.
        """
        super().__init__(daemon=True)
        self.rclone = rclone
        self.jobs = jobs
        self.mode = mode
        self.overwrite = overwrite
        self.dry_run = dry_run
        self.passes = passes
        self.level = level
        self.detailed = (level == "detail")
        self.show_cmd = show_cmd
        self._last_stats = None
        self._bound = ""
        self._copied = False
        # файл -> коли почався, щоб порахувати тривалість
        # file -> when it started, so the duration can be measured
        self._begun = {}
        self.q = out_q
        self.proc = None
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def emit(self, kind, payload):
        self.q.put((kind, payload))

    def skip_by_size(self, job: Job, flags: str) -> str:
        return skip_by_size(job, flags)

    def build_cmd(self, job: Job, flags: str, transfers: int):
        mode = self.mode
        # sync для одиничного файлу небезпечний/некоректний -> зводимо до copy
        # sync on a single file is unsafe/incorrect -> downgrade it to copy
        if mode == "sync" and not job.is_dir:
            mode = "copy"

        cmd = [self.rclone, mode, job.src, job.dst_full(self.mode)]
        cmd += [
            "--use-json-log",
            "--stats", "1s",
            "--stats-log-level", "NOTICE",
            "--transfers", str(transfers),
            "--checkers", str(max(4, transfers * 2)),
            "--retries", "3",
            "--low-level-retries", "10",
            "-vv" if self.detailed else "-v",
        ]
        if self.overwrite == "newer":
            cmd.append("--update")
        elif self.overwrite == "never":
            cmd.append("--ignore-existing")
        if self.dry_run:
            cmd.append("--dry-run")
        cmd += split_flags(flags)
        return cmd

    def run(self):
        """self.jobs — жива черга, спільна з вікном.

        Те, що додали вже під час роботи, підхоплюється наступним колом:
        нову порцію проганяємо через усі проходи окремо, інакше додане
        посеред другого проходу пропустило б перший.

        self.jobs is the live queue, shared with the window. Whatever is added
        while running is picked up on the next round: a new batch goes through
        all the passes of its own, otherwise something added in the middle of
        the second pass would miss the first one.
        """
        errors = 0
        started = time.time()
        idx = 0
        handled = set()
        round_no = 0

        while not self._stop.is_set():
            batch = [j for j in list(self.jobs) if id(j) not in handled]
            if not batch:
                break
            handled.update(id(j) for j in batch)
            round_no += 1
            if round_no > 1:
                self.emit("log", tr("+ Додано в роботу: {n} нових завдань", n=len(batch)))

            for label, flags, transfers in self.passes:
                if self._stop.is_set():
                    break
                if label:
                    self.emit("log", tr("══  {label}  ·  потоків {n}",
                                        label=tr(label), n=transfers))
                    if self.show_cmd and flags:
                        self.emit("log", tr("   · прапорці: {flags}", flags=flags))

                for job in batch:
                    if self._stop.is_set():
                        break
                    idx += 1
                    total = max(len(self.jobs) * len(self.passes), idx)
                    why = self.skip_by_size(job, flags)
                    if why:
                        self.emit("log", tr("[–] Пропущено «{name}»: файл {why}",
                                            name=job.name, why=why))
                        self.emit("jobdone", (job, "skip"))
                        continue
                    cmd = self.build_cmd(job, flags, transfers)
                    self.emit("job", {"index": idx, "total": total, "job": job,
                                      "dst": job.dst_full(self.mode), "pass": label})
                    arrow = "⬆" if job.direction == "up" else "⬇"
                    self.emit("log", f"▶ [{idx}/{total}] {arrow} "
                                     f"{short_path(job.src)}  →  "
                                     f"{short_path(job.dst_full(self.mode))}")
                    if self.show_cmd:
                        self.emit("log", "   · " + " ".join(
                            f'"{c}"' if " " in c else c for c in cmd))
                    ok = self._run_one(job, cmd, bound=size_bound(flags))
                    if not ok:
                        errors += 1
                    self.emit("jobdone", (job, "ok" if ok else "fail"))

        self.emit("done", {"errors": errors,
                           "cancelled": self._stop.is_set(),
                           "elapsed": time.time() - started})

    def _run_one(self, job: Job, cmd, bound="") -> bool:
        """Один запуск rclone: читає вивід до кінця. True — без помилки.

        One rclone run: reads its output to the end. True means no error.
        """
        self._last_stats = None
        self._bound = bound
        self._copied = False
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=NOWIN,
            )
        except Exception as e:
            self.emit("log", tr("[ПОМИЛКА ЗАПУСКУ] {err}", err=e))
            return False

        for line in self.proc.stderr:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    self.emit("log", line)
                    continue
                stats = obj.get("stats")
                if stats:
                    self._last_stats = stats
                    # запам'ятовуємо, коли файл з'явився в роботі: rclone
                    # тривалості не повідомляє, її доводиться міряти самим
                    # remember when a file entered the transfer: rclone does
                    # not report durations, so they have to be measured here
                    for t in (stats.get("transferring") or []):
                        nm = t.get("name")
                        if nm and nm not in self._begun:
                            self._begun[nm] = time.time()
                    self.emit("stats", stats)
                else:
                    msg = obj.get("msg", "").strip()
                    lvl = obj.get("level", "")
                    # назву файлу чи теки rclone кладе окремим полем
                    # rclone puts the file or folder name in a field of its own
                    name = str(obj.get("object") or "").strip()
                    if not msg:
                        continue

                    if lvl in ("error", "warning", "notice"):
                        text = msg if msg.startswith(name) or not name \
                            else f"{name}: {msg}"
                        self.emit("log", f"[{lvl}] {text}")
                        # Google віддає голий 400 на спробу створити теку
                        # в корені розділу «Комп'ютери» — підкажемо, що робити
                        # Google answers a bare 400 when a folder is created
                        # in the «Computers» root — say what to do about it
                        if "badRequest" in msg and "make directory" in msg:
                            self.emit("log", tr(
                                "    ↳ Якщо призначення — корінь теки «Комп'ютери», "
                                "Google не дозволяє створювати там нові теки. "
                                "Призначенням треба обрати наявну теку рівнем нижче."))
                        continue

                    if lvl == "debug":
                        if not self.detailed or not any(k in msg for k in DEBUG_KEEP):
                            continue
                    elif not self.detailed and any(s in msg for s in INFO_HIDE):
                        continue

                    mark, text = tidy_rclone_msg(name, msg)
                    if mark == "+":
                        self._copied = True
                        text += self._extra(name, obj.get("size"))

                    # «Коротко» — без рядків по окремих файлах
                    # «Brief» — no per-file lines
                    if self.level == "short" and name:
                        continue

                    self.emit("log", f"   {mark} {text}")
            else:
                self.emit("log", line)

        rc = self.proc.wait()
        if self._stop.is_set():
            self.emit("log", tr("×  Перервано: {name}", name=job.name))
            return True        # скасування — не помилка / cancelling is not an error
        if rc != 0:
            self.emit("log", tr("!  {name} — завершилось з кодом {rc}",
                                name=job.name, rc=rc))
            return False
        self.emit("log", f"✓  {job.name} — {self._summary(job)}")
        return True

    def _extra(self, name: str, size) -> str:
        """Розмір і, якщо вдалось виміряти, тривалість передачі файлу.

        The size and, when it could be measured, how long the file took.
        """
        bits = []
        if isinstance(size, (int, float)) and size > 0:
            bits.append(human(size))
        started = self._begun.pop(name, None)
        if started:
            took = time.time() - started
            if took >= 1:
                bits.append(tr("за {time}", time=human_time(took)))
        return (", " + ", ".join(bits)) if bits else ""

    def _summary(self, job: Job) -> str:
        """Короткий підсумок завдання з останньої статистики rclone.

        A short job summary built from rclone's last statistics.
        """
        st = self._last_stats or {}
        n = st.get("transfers") or 0
        checks = st.get("checks") or 0
        if not job.is_dir:
            # для завдання-файлу rclone не рахує ні передач, ні перевірок,
            # тож судити можна лише за тим, чи був рядок про копіювання
            # for a single-file job rclone counts neither transfers nor
            # checks, so the only evidence is whether a «copied» line appeared
            return tr("скопійовано") if self._copied else tr("вже є в призначенні")
        if n:
            parts = [tr("передано {n}", n=n), human(st.get("bytes") or 0)]
            if checks:
                parts.append(tr("вже було {n}", n=checks))
        elif checks:
            parts = [tr("{n} збігаються за назвою та розміром", n=checks)]
        elif self._bound:
            # обмеження за розміром відсіює файли ще на обході теки,
            # тому rclone їх навіть не рахує як перевірені
            # a size limit filters files out while the folder is walked, so
            # rclone does not even count them as checked
            parts = [tr("нема файлів {bound}", bound=self._bound)]
        else:
            parts = [tr("порожня тека")]
        el = st.get("elapsedTime") or 0
        if el >= 1:
            parts.append(tr("за {time}", time=human_time(el)))
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
#  Головне вікно / The main window
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        global LANG
        super().__init__()
        self.title(APP_NAME)
        self.cfg = load_cfg()
        LANG = self.cfg.get("lang", "uk") if self.cfg.get("lang") in ("uk", "en") else "uk"
        # історія прапорців більше не ведеться / flag history is no longer kept
        self.cfg.pop("flags_history", None)

        # розмір вікна: збережений з минулого разу, інакше підганяємо під екран
        # (на 1536x864 стандартні 780 не влазили — кнопки «Старт/Стоп» виїжджали)
        # window size: the saved one, otherwise fitted to the screen (on
        # 1536x864 the usual 780 did not fit — Start/Stop went off the edge)
        sh = self.winfo_screenheight()
        saved = sane_geometry(self.cfg.get("geometry", ""),
                              self.winfo_screenwidth(), sh)
        self.geometry(saved or f"1180x{min(820, max(520, sh - 90))}")
        self.minsize(900, 480)
        saved = (self.cfg.get("rclone") or "").strip()
        # збережений шлях міг протухнути (перенесли теку) — тоді шукаємо заново
        # the saved path may be stale (the folder moved) — then look again
        self.rclone = saved if (saved and Path(saved).exists()) else find_rclone()
        self.remotes = []
        self.q = queue.Queue()
        self.worker = None
        self.jobs = []
        self.job_rows = {}       # id(Job) -> рядок черги / queue row
        self.job_state = {}      # id(Job) -> стан по всіх проходах / state over all passes
        self.log_full = []       # повний журнал / the log without any shortening
        self.var_compact = tk.BooleanVar(value=bool(self.cfg.get("compact_log", False)))
        self.log_level = self.cfg.get("log_level", "normal")
        self.var_cmd = tk.BooleanVar(value=bool(self.cfg.get("show_cmd", False)))
        # порожньо — у файл не пишемо / empty means no file logging
        self.log_path = self.cfg.get("log_file", "")
        # (поточне, усього) — для відсотка в заголовку / for the title percentage
        self._job_pos = (0, 0)
        # підсумки по всій черзі: оброблене, заміряне наперед і поточний запуск
        # queue-wide totals: processed, measured up front, and the current run
        self._acc_bytes = self._acc_files = 0
        self._sent_bytes = self._sent_files = 0
        self.plan_bytes = self.plan_files = 0
        self.plan_per = {}       # (id(job), pass) -> (файлів, байтів) / (files, bytes)
        self._cur_key = None
        self._last_st = {}
        self.planner = None
        self.var_plan = tk.BooleanVar(value=bool(self.cfg.get("plan", True)))
        # профіль «обидва проходи» — Старт жене чергу двічі
        # the «both passes» profile — Start runs the queue twice
        self._two_pass = False

        # мапа iid -> метадані вузлів дерев / iid -> tree node metadata
        self.lmeta = {}
        self.rmeta = {}

        self._build_ui()
        self._check_rclone()
        self._pump_id = self.after(120, self._pump)
        # позиції роздільників — коли вікно вже промальоване
        # splitter positions, once the window has actually been drawn
        self._sash_id = self.after(250, self._restore_sash)
        self.bind("<F5>", self._refresh_focused)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ---------------- #

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=22)
        self._bind_clipboard_by_keycode()

        # --- закріплена нижня смуга: прогрес і Старт/Стоп поза прокруткою ---
        # --- pinned bottom strip: progress and Start/Stop stay out of the scroll ---
        bottom_fixed = ttk.Frame(self)
        bottom_fixed.pack(fill="x", side="bottom")

        # --- прокручувана область для всієї решти форми ---
        # --- the scrollable area for everything else ---
        scroll_wrap = ttk.Frame(self)
        scroll_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(scroll_wrap, highlightthickness=0)
        vsb = ttk.Scrollbar(scroll_wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(self.canvas)
        self._inner_id = self.canvas.create_window((0, 0), window=inner, anchor="nw")
        # висота вмісту змінилась -> перерахувати діапазон прокрутки
        # the content height changed -> recompute the scroll range
        inner.bind("<Configure>",
                   lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # ширину вмісту тримаємо рівною ширині полотна, щоб не було горизонтальної прокрутки
        # keep the content as wide as the canvas, so there is no horizontal scroll
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel)

        # --- верхня панель / top bar ---
        top = ttk.Frame(inner, padding=(8, 6))
        top.pack(fill="x")

        ttk.Label(top, text="rclone:").pack(side="left")
        self.var_rclone = tk.StringVar(value=self.rclone)
        ttk.Entry(top, textvariable=self.var_rclone, width=46).pack(side="left", padx=4)
        ttk.Button(top, text="…", width=3, command=self._pick_rclone).pack(side="left")

        ttk.Label(top, text="   Remote:").pack(side="left")
        self.var_remote = tk.StringVar()
        self.cb_remote = ttk.Combobox(top, textvariable=self.var_remote, width=22, state="readonly")
        self.cb_remote.pack(side="left", padx=4)
        self.cb_remote.bind("<<ComboboxSelected>>", lambda e: self._load_remote_root())

        ttk.Button(top, text="⟳ Оновити remotes", command=self._check_rclone).pack(side="left", padx=4)
        ttk.Button(top, text="⚙ rclone config", command=self._open_config).pack(side="left")

        ttk.Label(top, text="   Мова:").pack(side="left")
        self.cb_lang = ttk.Combobox(top, width=12, state="readonly",
                                    values=list(LANGS.keys()))
        self.cb_lang.current(list(LANGS.values()).index(LANG))
        self.cb_lang.pack(side="left", padx=4)
        self.cb_lang.bind("<<ComboboxSelected>>", self._set_lang)

        # --- дерева: власна висота, яка тягнеться смужкою під ними ---
        # --- the trees: their own height, dragged by the strip beneath ---
        self.tree_area = ttk.Frame(inner, height=int(self.cfg.get("tree_h", 340)))
        self.tree_area.pack(fill="x", padx=8, pady=(4, 0))
        # тримати задану висоту, а не підганятись під вміст
        # hold the given height instead of fitting to the content
        self.tree_area.pack_propagate(False)

        # --- два дерева / the two trees ---
        panes = ttk.PanedWindow(self.tree_area, orient="horizontal")
        self.hpanes = panes

        # локальне / local
        lf = ttk.LabelFrame(panes, text="Локальний диск", padding=4)
        panes.add(lf, weight=1)

        lbar = ttk.Frame(lf)
        lbar.pack(fill="x", pady=(0, 4))
        ttk.Button(lbar, text="⟳", width=3, command=self._refresh_local_node).pack(side="left")
        ttk.Button(lbar, text="Додати теку…", command=self._add_local_root).pack(side="left", padx=4)
        ttk.Button(lbar, text="✕ Прибрати з дерева",
                   command=self._drop_local_root).pack(side="left")
        ttk.Button(lbar, text="Σ Розмір теки", command=self._calc_local_size).pack(side="left")
        ttk.Button(lbar, text="🗑 Видалити", command=self._delete_local).pack(side="left", padx=4)
        self.var_showfiles_l = tk.BooleanVar(value=True)
        ttk.Checkbutton(lbar, text="файли", variable=self.var_showfiles_l,
                        command=self._load_local_roots).pack(side="left", padx=6)

        # обгортка на grid: так вертикальна і горизонтальна смуги не б'ються між собою
        # a grid wrapper: that way the vertical and horizontal bars do not fight
        lwrap = ttk.Frame(lf)
        lwrap.pack(fill="both", expand=True)
        self.ltree = ttk.Treeview(lwrap, selectmode="extended", show="tree")
        lsb = ttk.Scrollbar(lwrap, orient="vertical", command=self.ltree.yview)
        lhb = ttk.Scrollbar(lwrap, orient="horizontal", command=self.ltree.xview)
        self.ltree.configure(yscrollcommand=lsb.set, xscrollcommand=lhb.set)
        self.ltree.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        lhb.grid(row=1, column=0, sticky="ew")
        lwrap.rowconfigure(0, weight=1)
        lwrap.columnconfigure(0, weight=1)
        self.ltree.column("#0", width=420, minwidth=200, stretch=True)
        self.ltree.bind("<<TreeviewOpen>>", self._expand_local)
        self.ltree.bind("<<TreeviewSelect>>", lambda e: self._update_selection("l"))

        self.lbl_lsel = ttk.Label(lf, text="Виділено: —", foreground="#555", anchor="w")
        self.lbl_lsel.pack(fill="x", pady=(3, 0))

        # віддалене / remote
        rf = ttk.LabelFrame(panes, text="Google Drive / remote", padding=4)
        panes.add(rf, weight=1)

        rbar = ttk.Frame(rf)
        rbar.pack(fill="x", pady=(0, 4))
        ttk.Button(rbar, text="⟳", width=3, command=self._refresh_remote_node).pack(side="left")
        ttk.Button(rbar, text="＋ Нова тека", command=self._mkdir_remote).pack(side="left", padx=4)
        ttk.Button(rbar, text="Σ Розмір теки", command=self._calc_remote_size).pack(side="left")
        ttk.Button(rbar, text="🗑 Видалити", command=self._delete_remote).pack(side="left", padx=4)
        self.var_showfiles_r = tk.BooleanVar(value=True)
        ttk.Checkbutton(rbar, text="файли", variable=self.var_showfiles_r,
                        command=self._load_remote_root).pack(side="left", padx=6)

        rwrap = ttk.Frame(rf)
        rwrap.pack(fill="both", expand=True)
        self.rtree = ttk.Treeview(rwrap, selectmode="extended", show="tree")
        rsb = ttk.Scrollbar(rwrap, orient="vertical", command=self.rtree.yview)
        rhb = ttk.Scrollbar(rwrap, orient="horizontal", command=self.rtree.xview)
        self.rtree.configure(yscrollcommand=rsb.set, xscrollcommand=rhb.set)
        self.rtree.grid(row=0, column=0, sticky="nsew")
        rsb.grid(row=0, column=1, sticky="ns")
        rhb.grid(row=1, column=0, sticky="ew")
        rwrap.rowconfigure(0, weight=1)
        rwrap.columnconfigure(0, weight=1)
        self.rtree.column("#0", width=420, minwidth=200, stretch=True)
        self.rtree.bind("<<TreeviewOpen>>", self._expand_remote)
        self.rtree.bind("<<TreeviewSelect>>", lambda e: self._update_selection("r"))

        self.lbl_rsel = ttk.Label(rf, text="Виділено: —", foreground="#555", anchor="w")
        self.lbl_rsel.pack(fill="x", pady=(3, 0))

        # --- смужка зміни висоти дерев (тягнути мишею вгору/вниз) ---
        # --- the strip that resizes the trees (drag it up or down) ---
        grip = tk.Frame(inner, height=7, bg="#b0b0b0", cursor="sb_v_double_arrow")
        grip.pack(fill="x", padx=8, pady=3)
        grip.bind("<ButtonPress-1>", self._grip_press)
        grip.bind("<B1-Motion>", self._grip_drag)

        # --- кнопки напрямку / direction buttons ---
        mid = ttk.Frame(inner, padding=(8, 2))
        ttk.Button(mid, text="⬆  Вивантажити виділене  →  у виділену теку хмари",
                   command=self._queue_upload).pack(side="left", padx=2)
        ttk.Button(mid, text="⬇  Завантажити виділене  →  у виділену локальну теку",
                   command=self._queue_download).pack(side="left", padx=2)

        # --- черга / the queue ---
        qf = ttk.LabelFrame(inner, text="Черга завдань", padding=4)

        cols = ("dir", "src", "dst")
        self.qtree = ttk.Treeview(qf, columns=cols, show="headings", height=4, selectmode="extended")
        self.qtree.heading("dir", text="")
        self.qtree.heading("src", text="Джерело")
        self.qtree.heading("dst", text="Призначення")
        self.qtree.column("dir", width=58, anchor="center", stretch=False)
        self.qtree.column("src", width=460)
        self.qtree.column("dst", width=460)
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qsb.set)
        qsb.pack(side="right", fill="y")
        self.qtree.pack(fill="both", expand=True)

        qb = ttk.Frame(inner, padding=(8, 0))
        ttk.Button(qb, text="Видалити з черги", command=self._remove_job).pack(side="left")
        ttk.Button(qb, text="Очистити чергу", command=self._clear_jobs).pack(side="left", padx=4)
        ttk.Button(qb, text="Порахувати обсяг черги", command=self._calc_size).pack(side="left")

        # --- опції / options ---
        of = ttk.Frame(inner, padding=(8, 6))

        # комбобокси тримають ключі окремо від підписів: підпис перекладний,
        # вибір визначається позицією, тож зміна мови нічого не збиває
        # the combo boxes keep keys apart from labels: the label is
        # translatable and the choice is an index, so switching language
        # cannot break the selection
        ttk.Label(of, text="Режим:").grid(row=0, column=0, sticky="w")
        self.var_mode = tk.StringVar(value=self.cfg.get("mode", "copy"))
        self.MODE_KEYS = list(MODES)
        cb_mode = ttk.Combobox(of, width=42, state="readonly")
        cb_mode.grid(row=0, column=1, sticky="w", padx=4)
        cb_mode.bind("<<ComboboxSelected>>",
                     lambda e: self.var_mode.set(self.MODE_KEYS[cb_mode.current()]))
        self.cb_mode = cb_mode

        ttk.Label(of, text="Перезапис:").grid(row=0, column=2, sticky="w", padx=(14, 0))
        self.var_ow = tk.StringVar(value=self.cfg.get("overwrite", "always"))
        self.OW_KEYS = list(OVERWRITE)
        cb_ow = ttk.Combobox(of, width=38, state="readonly")
        cb_ow.grid(row=0, column=3, sticky="w", padx=4)
        cb_ow.bind("<<ComboboxSelected>>",
                   lambda e: self.var_ow.set(self.OW_KEYS[cb_ow.current()]))
        self.cb_ow = cb_ow

        self.var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Dry-run (лише перевірка)", variable=self.var_dry)\
            .grid(row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(of, text="Потоків:").grid(row=1, column=2, sticky="w", padx=(14, 0), pady=(6, 0))
        self.var_tr = tk.IntVar(value=self.cfg.get("transfers", 4))
        self.sp_tr = ttk.Spinbox(of, from_=1, to=16, width=5, textvariable=self.var_tr)
        self.sp_tr.grid(row=1, column=3, sticky="w", padx=4, pady=(6, 0))

        # Профіль стоїть НАД прапорцями: він їх і заповнює, тож читається
        # згори вниз — вибір типу передачі, пояснення, отримані прапорці.
        # The profile sits ABOVE the flags because it fills them in, so it
        # reads top to bottom: pick a type, read why, see the flags you got.
        ttk.Label(of, text="Тип передачі:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.PROF_KEYS = list(PROFILES)
        self.cb_profile = ttk.Combobox(of, width=58, state="readonly")
        self.cb_profile.grid(row=2, column=1, columnspan=2, sticky="we", padx=4, pady=(8, 0))
        self.cb_profile.bind("<<ComboboxSelected>>", self._apply_profile)
        ttk.Button(of, text="?  Довідка з прапорців", command=self._show_flag_help)\
            .grid(row=2, column=3, sticky="w", padx=4, pady=(8, 0))

        self.lbl_profile = ttk.Label(of, text="Тип передачі підбирає прапорці "
                                               "й кількість потоків",
                                     foreground="#555", wraplength=900, justify="left")
        self.lbl_profile.grid(row=3, column=0, columnspan=4, sticky="we", pady=(4, 0))

        ttk.Label(of, text="Дод. прапорці:").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.var_flags = tk.StringVar(value=self.cfg.get("flags", DEFAULT_FLAGS))
        self.ent_flags = ttk.Entry(of, textvariable=self.var_flags, width=60)
        self.ent_flags.grid(row=4, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0))

        of.columnconfigure(3, weight=1)

        # --- прогрес (у закріпленій смузі) / progress (in the pinned strip) ---
        pf = ttk.LabelFrame(bottom_fixed, text="Прогрес", padding=6)

        self.lbl_job = ttk.Label(pf, text="Очікування…", anchor="w")
        self.lbl_job.pack(fill="x")

        # дві шкали: самого обсягу мало, коли в теці тисячі дрібних файлів,
        # самої кількості — коли серед них трапляється один великий
        # two bars: volume alone is not enough when a folder holds thousands
        # of small files, count alone is not enough when one large file is
        # among them
        bars = ttk.Frame(pf)
        bars.pack(fill="x", pady=3)
        for c in (0, 1):
            bars.columnconfigure(c, weight=1, uniform="half")
        # підписи width=1: хай колонки ділять ширину порівну, а не
        # розтягуються під найдовший текст
        # labels with width=1: let the columns split the width evenly instead
        # of stretching to the longest text
        self.pb_one = ttk.Progressbar(bars, maximum=1000)
        self.lbl_pb1 = ttk.Label(bars, anchor="w", width=1)
        self.pb = ttk.Progressbar(bars, maximum=1000)
        self.pb_files = ttk.Progressbar(bars, maximum=1000)
        self.lbl_pb = ttk.Label(bars, anchor="w", width=1)
        self.lbl_pbf = ttk.Label(bars, anchor="w", width=1)

        self.pb_one.grid(row=0, column=0, columnspan=2, sticky="we")
        self.lbl_pb1.grid(row=1, column=0, columnspan=2, sticky="we", pady=(1, 5))
        self.pb.grid(row=2, column=0, sticky="we", padx=(0, 6))
        self.pb_files.grid(row=2, column=1, sticky="we", padx=(6, 0))
        self.lbl_pb.grid(row=3, column=0, sticky="we", padx=(0, 6), pady=(1, 0))
        self.lbl_pbf.grid(row=3, column=1, sticky="we", padx=(6, 0), pady=(1, 0))

        self.lbl_stats = ttk.Label(pf, text="", anchor="w", foreground="#444")
        self.lbl_stats.pack(fill="x")
        self.lbl_queue = ttk.Label(pf, text="", anchor="w", foreground="#444")
        self.lbl_queue.pack(fill="x")
        self.lbl_cur = ttk.Label(pf, text="", anchor="w", foreground="#666")
        self.lbl_cur.pack(fill="x")

        # --- лог / the log ---
        lg = ttk.LabelFrame(inner, text="Журнал", padding=4)

        gwrap = ttk.Frame(lg)
        gwrap.pack(fill="both", expand=True)
        self.log = tk.Text(gwrap, height=5, wrap="none", font=("Consolas", 9))
        gsb = ttk.Scrollbar(gwrap, orient="vertical", command=self.log.yview)
        ghb = ttk.Scrollbar(gwrap, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=gsb.set, xscrollcommand=ghb.set, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        gsb.grid(row=0, column=1, sticky="ns")
        # довгі рядки JSON тепер можна докрутити / long JSON lines can be scrolled
        ghb.grid(row=1, column=0, sticky="ew")
        gwrap.rowconfigure(0, weight=1)
        gwrap.columnconfigure(0, weight=1)

        # Text зі state="disabled" не бере фокус, тому Ctrl+C до нього не доходить —
        # ставимо фокус по кліку і вішаємо копіювання вручну
        # a Text with state="disabled" takes no focus, so Ctrl+C never reaches
        # it — focus it on click and bind copying by hand
        self.log.bind("<Button-1>", lambda e: self.log.focus_set())
        for seq in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
            self.log.bind(seq, self._copy_log_sel)
        for seq in ("<Control-a>", "<Control-A>"):
            self.log.bind(seq, self._select_log_all)

        self.log_menu = tk.Menu(self, tearoff=0)
        self.log_menu.add_command(label="Копіювати виділене", command=self._copy_log_sel)
        self.log_menu.add_command(label="Виділити все", command=self._select_log_all)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="Копіювати весь журнал", command=self._copy_log_all)
        self.log.bind("<Button-3>", self._log_menu_popup)

        # --- нижня панель (у закріпленій смузі) / bottom bar (pinned strip) ---
        bf = ttk.Frame(bottom_fixed, padding=(8, 6))
        self.btn_start = ttk.Button(bf, text="▶  Старт", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bf, text="■  Стоп", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Checkbutton(bf, text="Міряти обсяг черги", variable=self.var_plan,
                        command=self._save_plan).pack(side="left", padx=6)
        ttk.Button(bf, text="Очистити журнал", command=self._clear_log).pack(side="right")
        ttk.Button(bf, text="Копіювати журнал", command=self._copy_log_all).pack(side="right", padx=6)
        ttk.Checkbutton(bf, text="Стислий журнал", variable=self.var_compact,
                        command=self._rerender_log).pack(side="right", padx=6)
        self.btn_logfile = ttk.Button(bf, text="Файл журналу…", command=self._pick_log_file)
        self.btn_logfile.pack(side="right", padx=6)
        ttk.Checkbutton(bf, text="Службові мітки", variable=self.var_cmd,
                        command=self._save_show_cmd).pack(side="right", padx=6)

        self.LVL_KEYS = list(LOG_LEVELS)
        self.cb_level = ttk.Combobox(bf, width=11, state="readonly")
        self.cb_level.pack(side="right", padx=4)
        self.cb_level.bind("<<ComboboxSelected>>", self._set_log_level)
        ttk.Label(bf, text="Журнал:").pack(side="right")
        # розшифровку обраного рівня ставимо ліворуч: там лишається вільне
        # місце, а праворуч її витіснили б кнопки при вужчому вікні
        # the level hint goes on the left, where space is free: on the right
        # the buttons would squeeze it out in a narrower window
        self.lbl_level = ttk.Label(bf, foreground="#555")
        self.lbl_level.pack(side="left", padx=10)
        # --- РОЗКЛАДКА / LAYOUT ---
        # Закріплена смуга внизу: її не видно в прокрутці, тому «Старт»
        # доступний завжди, хоч як розтягнуті дерева.
        # The pinned bottom strip is outside the scroll, so «Start» is always
        # reachable however far the trees are stretched.
        pf.pack(fill="x", padx=8, pady=4)
        bf.pack(fill="x")

        # Прокручувана частина, звичайним порядком згори вниз.
        # top і self.tree_area зі смужкою вже запаковані вище.
        # The scrollable part, in the usual top-to-bottom order. top and
        # self.tree_area with its strip are already packed above.
        panes.pack(fill="both", expand=True)     # усередині / inside self.tree_area
        mid.pack(fill="x")
        qf.pack(fill="x", padx=8, pady=4)
        qb.pack(fill="x")
        of.pack(fill="x")
        lg.pack(fill="x", padx=8, pady=(4, 8))

        self._snap_texts()
        self._retranslate()

    # ---------------- мова інтерфейсу / UI language ---------------- #

    def _snap_texts(self):
        """Запам'ятати український підпис кожного віджета.

        Підписи в коді лишаються українські, а переклад накладається зверху —
        тому знімок завжди містить оригінал, хоч якою мовою зараз вікно.
        Widget labels stay Ukrainian in the code and translation is applied on
        top, so the snapshot always holds the original.
        """
        # ці підписи пише сам код під час роботи — їх перекладає не знімок,
        # а власні оновлювачі, інакше сюди потрапив би застарілий текст
        # these labels are written by the code while it runs — their own
        # updaters translate them, otherwise stale text would land here
        live = {self.lbl_job, self.lbl_stats, self.lbl_queue, self.lbl_cur,
                self.lbl_pb, self.lbl_pbf, self.lbl_pb1, self.lbl_level,
                self.lbl_lsel, self.lbl_rsel, self.lbl_profile,
                self.btn_start, self.btn_logfile}
        self._orig = []

        def walk(w):
            if w not in live:
                try:
                    v = w.cget("text")
                    if isinstance(v, str) and v:
                        self._orig.append((w, v))
                except Exception:
                    pass
            for c in w.winfo_children():
                walk(c)

        walk(self)

    def _set_lang(self, _evt=None):
        global LANG
        LANG = list(LANGS.values())[self.cb_lang.current()]
        self.cfg["lang"] = LANG
        save_cfg(self.cfg)
        self._retranslate()

    def _retranslate(self):
        """Перемалювати всі підписи поточною мовою.

        Redraw every label in the current language.
        """
        for w, src in self._orig:
            try:
                w.configure(text=tr(src))
            except Exception:
                pass

        self.qtree.heading("src", text=tr("Джерело"))
        self.qtree.heading("dst", text=tr("Призначення"))
        for i, src in enumerate(("Копіювати виділене", "Виділити все")):
            self.log_menu.entryconfigure(i, label=tr(src))
        self.log_menu.entryconfigure(3, label=tr("Копіювати весь журнал"))

        self.cb_mode["values"] = [tr(MODES[k]) for k in self.MODE_KEYS]
        self.cb_mode.current(self.MODE_KEYS.index(self.var_mode.get()))
        self.cb_ow["values"] = [tr(OVERWRITE[k]) for k in self.OW_KEYS]
        self.cb_ow.current(self.OW_KEYS.index(self.var_ow.get()))

        picked = self.cb_profile.current()
        self.cb_profile["values"] = [tr(k) for k in self.PROF_KEYS]
        if picked >= 0:
            self.cb_profile.current(picked)
            self.lbl_profile.configure(
                text="ⓘ  " + tr(PROFILES[self.PROF_KEYS[picked]].get("note", "")))
        else:
            self.lbl_profile.configure(
                text=tr("Тип передачі підбирає прапорці й кількість потоків"))

        self.cb_level["values"] = [tr(k) for k in self.LVL_KEYS]
        self.cb_level.current(
            next(i for i, k in enumerate(self.LVL_KEYS)
                 if LOG_LEVELS[k] == self.log_level))

        self.btn_start.configure(
            text=tr("▶  Старт (2 проходи)") if self._two_pass else tr("▶  Старт"))
        self._sync_logfile_button()
        self._sync_level_hint()
        self._update_selection("l")
        self._update_selection("r")
        if not (self.worker and self.worker.is_alive()):
            self.lbl_job.configure(text=tr("Очікування…"))

    # ---------------- rclone ---------------- #

    def _bind_clipboard_by_keycode(self):
        """Ctrl+C/V/X/A, які працюють і на кириличній розкладці.

        Tk зіставляє прив'язку за keysym, а при кирилиці Ctrl+V дає
        «Cyrillic_em» замість «v» — і штатна прив'язка мовчить.
        Тому дивимось на keycode, який від розкладки не залежить.
        Вішаємо на клас: якщо розкладка латинська, спрацює точніший
        штатний «<Control-v>», а сюди виконання не дійде.

        Ctrl+C/V/X/A that also work on a Cyrillic layout. Tk matches bindings
        by keysym, and with Cyrillic Ctrl+V yields «Cyrillic_em» instead of
        «v», so the stock binding stays silent. Hence we look at the keycode,
        which does not depend on the layout. It is bound on the class: with a
        Latin layout the more specific stock «<Control-v>» fires first and
        execution never reaches here.
        """
        virt = {65: "<<SelectAll>>", 67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>"}

        def handler(e):
            if not (e.state & 0x0004):       # без Control / no Control, not ours
                return None
            name = virt.get(e.keycode)
            if not name:
                return None
            e.widget.event_generate(name)
            return "break"

        for cls in ("TEntry", "Entry", "Text", "TCombobox", "TSpinbox"):
            self.bind_class(cls, "<Control-KeyPress>", handler, add="+")

    def _apply_profile(self, _evt=None):
        """Профіль виставляє і прапорці, і потоки — одним вибором.

        One choice sets both the flags and the thread count.
        """
        i = self.cb_profile.current()
        if i < 0:
            return
        name = self.PROF_KEYS[i]
        prof = PROFILES[name]
        self._two_pass = bool(prof.get("two_pass"))
        self.lbl_profile.configure(text="ⓘ  " + tr(prof.get("note", "")))

        if self._two_pass:
            # прапорці й потоки задають самі проходи — щоб не збивати з пантелику,
            # вимикаємо поля й кажемо на кнопці, що запусків буде два
            # the passes set the flags and threads themselves — to avoid
            # confusion the fields are disabled and the button says there
            # will be two runs
            self.ent_flags.configure(state="disabled")
            self.sp_tr.configure(state="disabled")
            self.btn_start.configure(text=tr("▶  Старт (2 проходи)"))
            self._log(tr("Тип передачі «{name}» · Старт прожене чергу двічі",
                         name=tr(name)))
            return

        self.ent_flags.configure(state="normal")
        self.sp_tr.configure(state="normal")
        self.btn_start.configure(text=tr("▶  Старт"))
        self.var_flags.set(prof["flags"])
        self.var_tr.set(prof["transfers"])
        self._log(tr("Тип передачі «{name}» · потоків {n} · {flags}",
                     name=tr(name), n=prof["transfers"], flags=prof["flags"]))

    def _show_flag_help(self):
        win = tk.Toplevel(self)
        win.title(tr("Прапорці rclone — коротка довідка"))
        win.geometry("820x520")
        win.transient(self)

        ttk.Button(win, text=tr("Закрити"), command=win.destroy).pack(side="bottom", pady=6)
        sb = ttk.Scrollbar(win, orient="vertical")
        sb.pack(side="right", fill="y")
        txt = tk.Text(win, wrap="word", padx=12, pady=10,
                      font=("Segoe UI", 9), yscrollcommand=sb.set)
        txt.pack(fill="both", expand=True)
        sb.configure(command=txt.yview)

        txt.tag_configure("flag", font=("Consolas", 10, "bold"), foreground="#1a4a8a")
        txt.tag_configure("body", lmargin1=18, lmargin2=18, spacing3=8)
        for flag, desc in FLAG_HELP:
            txt.insert("end", flag + "\n", "flag")
            txt.insert("end", tr(desc) + "\n\n", "body")
        txt.configure(state="disabled")

    def _pick_rclone(self):
        p = filedialog.askopenfilename(
            title=tr("Вкажіть rclone"),
            filetypes=[("rclone", "rclone.exe rclone"), (tr("Усі"), "*.*")])
        if p:
            self.var_rclone.set(p)
            self._check_rclone()

    def _open_config(self):
        exe = self.var_rclone.get().strip()
        if not exe:
            return
        try:
            if os.name == "nt":
                subprocess.Popen(f'start "" cmd /k "{exe}" config', shell=True)
            else:
                subprocess.Popen(["x-terminal-emulator", "-e", exe, "config"])
        except Exception as e:
            messagebox.showerror(APP_NAME, tr("Не вдалося відкрити консоль:\n{err}", err=e))

    def _check_rclone(self):
        exe = self.var_rclone.get().strip()
        if not exe or not (Path(exe).exists() or shutil.which(exe)):
            self._log(tr("rclone не знайдено. Його треба встановити "
                         "(winget install Rclone.Rclone), покласти rclone.exe поруч зі "
                         "скриптом або вказати шлях кнопкою «…»."))
            # локальне дерево від rclone не залежить
            # the local tree does not depend on rclone
            self._load_local_roots()
            return
        self.rclone = exe
        try:
            r = run_capture([exe, "version"], timeout=20)
            self._log(r.stdout.splitlines()[0] if r.stdout else "rclone OK")
            r = run_capture([exe, "listremotes"], timeout=20)
            self.remotes = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        except Exception as e:
            self._log(tr("Помилка запуску rclone: {err}", err=e))
            return

        self.cb_remote["values"] = self.remotes
        if self.remotes:
            saved = self.cfg.get("remote")
            self.var_remote.set(saved if saved in self.remotes else self.remotes[0])
            self._load_remote_root()
        else:
            # Найчастіше remote «зникають» не тому, що їх немає, а тому що
            # rclone дивиться в інший конфіг. Показуємо, який саме.
            # Remotes usually «disappear» not because they are gone but
            # because rclone is reading a different config. Show which one.
            try:
                rf = run_capture([exe, "config", "file"], timeout=20)
                lines = ((rf.stdout or "") + (rf.stderr or "")).strip().splitlines()
                path = lines[-1].strip() if lines else ""
                if path:
                    state = tr("на місці") if Path(path).exists() else tr("ВІДСУТНІЙ")
                    self._log(tr("rclone читає конфіг: {path}   (файл {state})",
                                 path=path, state=state))
            except Exception:
                pass
            self._log(tr("Немає жодного налаштованого remote. Створити можна кнопкою "
                         "«⚙ rclone config»: n → ім'я (напр. gdrive) → drive → "
                         "далі за майстром."))
        self._load_local_roots()

    # ---------------- локальне дерево / local tree ---------------- #

    def _load_local_roots(self):
        self.ltree.delete(*self.ltree.get_children())
        self.lmeta.clear()
        roots = []
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if Path(d).exists():
                    roots.append(d)
        else:
            roots = ["/", str(Path.home())]
        roots += [r for r in self.cfg.get("extra_roots", []) if Path(r).exists()]

        for r in roots:
            iid = self.ltree.insert("", "end", text=r, open=False)
            self.lmeta[iid] = {"path": r, "dir": True, "loaded": False}
            self.ltree.insert(iid, "end", text="…")

    def _add_local_root(self):
        p = filedialog.askdirectory(title=tr("Додати теку в дерево"))
        if not p:
            return
        extra = self.cfg.setdefault("extra_roots", [])
        if p not in extra:
            extra.append(p)
            save_cfg(self.cfg)
        self._load_local_roots()

    def _drop_local_root(self):
        """Прибрати додану теку з дерева. Сама тека на диску лишається.

        Remove an added folder from the tree. The folder itself stays on disk.
        """
        extra = self.cfg.get("extra_roots", [])
        if not extra:
            messagebox.showinfo(APP_NAME, tr("Жодної доданої теки немає. "
                                             "Диски з дерева прибрати не можна."))
            return
        keys = {same_path(p): p for p in extra}
        gone = []
        for iid in self.ltree.selection():
            if self.ltree.parent(iid):    # не верхній рівень / not a top-level row
                continue
            p = keys.get(same_path(self.lmeta.get(iid, {}).get("path", "")))
            if p:
                gone.append(p)
        if not gone:
            messagebox.showinfo(APP_NAME, tr(
                "Треба виділити зліва саму додану теку — верхній рядок, той, "
                "що з'явився після «Додати теку…».\n\n"
                "Ця кнопка прибирає теку лише з дерева; на диску вона "
                "лишається. Щоб видалити теку з диска, є «🗑 Видалити»."))
            return
        self.cfg["extra_roots"] = [p for p in extra if p not in gone]
        save_cfg(self.cfg)
        self._load_local_roots()
        for p in gone:
            self._log(tr("Прибрано з дерева (на диску лишилась): {path}", path=p))

    def _expand_local(self, _evt):
        self._expand_local_node(self.ltree.focus())

    def _expand_local_node(self, iid):
        meta = self.lmeta.get(iid)
        if not meta or meta["loaded"]:
            return
        meta["loaded"] = True
        self.ltree.delete(*self.ltree.get_children(iid))
        base = Path(meta["path"])
        try:
            entries = sorted(base.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as e:
            self.ltree.insert(iid, "end",
                              text=tr("[немає доступу: {err}]", err=e.__class__.__name__))
            return

        shown = 0
        for p in entries:
            try:
                isdir = p.is_dir()
            except OSError:
                continue
            if not isdir and not self.var_showfiles_l.get():
                continue
            size = None
            if not isdir:
                try:
                    size = p.stat().st_size
                except OSError:
                    pass
            label = ("📁 " if isdir else "📄 ") + p.name
            if size is not None:
                label += f"   ({human(size)})"
            child = self.ltree.insert(iid, "end", text=label)
            self.lmeta[child] = {"path": str(p), "dir": isdir,
                                 "loaded": not isdir, "size": size}
            if isdir:
                self.ltree.insert(child, "end", text="…")
            shown += 1
            if shown >= 3000:
                self.ltree.insert(iid, "end", text=tr("[…забагато елементів, показано 3000]"))
                break

    # ---------------- віддалене дерево / remote tree ---------------- #

    def _remote_prefix(self) -> str:
        r = self.var_remote.get().strip()
        return r if r.endswith(":") else r + ":"

    def _load_remote_root(self):
        self.rtree.delete(*self.rtree.get_children())
        self.rmeta.clear()
        if not self.var_remote.get():
            return
        self.cfg["remote"] = self.var_remote.get()
        save_cfg(self.cfg)
        root = self._remote_prefix()
        iid = self.rtree.insert("", "end", text=root, open=False)
        self.rmeta[iid] = {"path": "", "dir": True, "loaded": False}
        self.rtree.insert(iid, "end", text="…")

    def _lsjson(self, rpath: str, prefix: str, show_files: bool):
        # prefix/show_files приходять з головного потоку: tkinter не потокобезпечний
        # prefix/show_files come from the main thread: tkinter is not thread-safe
        cmd = [self.rclone, "lsjson", prefix + rpath, "--no-modtime"]
        if not show_files:
            cmd.append("--dirs-only")
        r = run_capture(cmd, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(rclone_error(r.stderr))
        return json.loads(r.stdout or "[]")

    def _expand_remote(self, _evt):
        self._expand_remote_node(self.rtree.focus())

    def _expand_remote_node(self, iid):
        meta = self.rmeta.get(iid)
        if not meta or meta["loaded"]:
            return
        # Google Drive дозволяє «\» в імені, але rclone на Windows читає його
        # як роздільник шляху, тож така тека недосяжна інакше як за ID.
        # Не ганяємо марний запит — одразу пояснюємо.
        # Google Drive allows «\» in a name, but rclone on Windows reads it as
        # a path separator, so such a folder is unreachable except by ID.
        # Do not waste a request — explain right away.
        if "\\" in meta["path"]:
            meta["loaded"] = True
            self.rtree.delete(*self.rtree.get_children(iid))
            self.rtree.insert(iid, "end", text=tr(
                "⚠ у назві теки є «\\» — rclone на Windows не може її відкрити. "
                "Таку теку треба перейменувати в Google Drive."))
            return
        meta["loaded"] = True
        self.rtree.delete(*self.rtree.get_children(iid))
        self.rtree.insert(iid, "end", text=tr("⏳ завантаження…"))

        # читаємо tkinter-змінні тут, у головному потоці, і передаємо значеннями
        # read the tkinter variables here, on the main thread, and pass values
        prefix = self._remote_prefix()
        show_files = self.var_showfiles_r.get()

        def work():
            try:
                items = self._lsjson(meta["path"], prefix, show_files)
                self.q.put(("rls", (iid, meta["path"], items, None)))
            except Exception as e:
                self.q.put(("rls", (iid, meta["path"], None, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _fill_remote(self, iid, base, items, err):
        if not self.rtree.exists(iid):
            return
        self.rtree.delete(*self.rtree.get_children(iid))
        if err:
            self.rtree.insert(iid, "end", text=tr("[помилка] {err}", err=err[:120]))
            self._log(tr("lsjson: {err}", err=err))
            # даємо шанс на повтор: згорни й розгорни гілку ще раз
            # leave room for a retry: collapse and expand the branch again
            m = self.rmeta.get(iid)
            if m:
                m["loaded"] = False
            self.rtree.insert(iid, "end", text="…")
            return
        items.sort(key=lambda d: (not d.get("IsDir"), d.get("Name", "").lower()))
        for it in items:
            isdir = bool(it.get("IsDir"))
            name = it.get("Name", "")
            size = None if isdir else it.get("Size")
            label = ("📁 " if isdir else "📄 ") + name
            if size is not None:
                label += f"   ({human(size)})"
            child = self.rtree.insert(iid, "end", text=label)
            full = f"{base}/{name}" if base else name
            self.rmeta[child] = {"path": full, "dir": isdir, "loaded": not isdir,
                                 "name": name, "size": size}
            if isdir:
                self.rtree.insert(child, "end", text="…")

    def _mkdir_remote(self):
        sel = self.rtree.selection()
        base = ""
        if sel:
            m = self.rmeta.get(sel[0])
            if m:
                base = m["path"] if m["dir"] else m["path"].rsplit("/", 1)[0]
        name = simpledialog.askstring(
            APP_NAME, tr("Назва нової теки в «{where}»:",
                         where=f"{self._remote_prefix()}{base}"))
        if not name:
            return
        target = self._remote_prefix() + (f"{base}/{name}" if base else name)
        r = run_capture([self.rclone, "mkdir", target], timeout=60)
        if r.returncode == 0:
            self._log(tr("Створено: {target}", target=target))
            self._load_remote_root()
        else:
            messagebox.showerror(APP_NAME, r.stderr[:500])

    # ---------------- оновлення дерев / refreshing the trees ---------------- #

    def _refresh_focused(self, _evt=None):
        """F5 — оновити те дерево, у якому зараз курсор.

        F5 refreshes whichever tree currently holds the cursor.
        """
        if self.focus_get() is self.rtree:
            self._refresh_remote_node()
        else:
            self._refresh_local_node()

    def _refresh_node(self, which, iid):
        """Перечитати вміст вузла, лишивши решту дерева розгорнутою.

        Re-read a node's contents while the rest of the tree stays expanded.
        """
        tree = self.rtree if which == "r" else self.ltree
        meta = (self.rmeta if which == "r" else self.lmeta)[iid]
        meta["loaded"] = False
        tree.delete(*tree.get_children(iid))
        if tree.item(iid, "open"):
            if which == "r":
                self._expand_remote_node(iid)
            else:
                self._expand_local_node(iid)
        else:
            # підвантажиться при розгортанні / will load when expanded
            tree.insert(iid, "end", text="…")

    def _refresh_local_node(self):
        sel = [i for i in self.ltree.selection()
               if i in self.lmeta and self.lmeta[i].get("dir")]
        if not sel:
            # нічого не виділено — перебудова з нуля
            # nothing selected — rebuild from scratch
            self._load_local_roots()
            return
        for iid in sel:
            self._refresh_node("l", iid)

    def _refresh_remote_node(self):
        sel = [i for i in self.rtree.selection()
               if i in self.rmeta and self.rmeta[i].get("dir")]
        if not sel:
            self._load_remote_root()
            return
        for iid in sel:
            self._refresh_node("r", iid)

    def _grip_press(self, e):
        self._grip_y0 = e.y_root
        self._grip_h0 = self.tree_area.winfo_height()

    def _grip_drag(self, e):
        """Тягнення смужки під деревами змінює їхню висоту.

        Dragging the strip under the trees changes their height.
        """
        self.tree_area.configure(
            height=max(160, self._grip_h0 + (e.y_root - self._grip_y0)))

    def _on_wheel(self, e):
        """Колесо гортає форму; дерева і журнал прокручують себе самі.

        The wheel scrolls the form; the trees and the log scroll themselves.
        """
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, (ttk.Treeview, tk.Text)):
                return
            w = w.master
        self.canvas.yview_scroll(-2 if e.delta > 0 else 2, "units")

    def _restore_sash(self):
        """Роздільник між деревами — коли вікно вже промальоване.

        The splitter between the trees, once the window has been drawn.
        """
        p = self.cfg.get("hsash")
        if p:
            try:
                self.hpanes.sashpos(0, int(p))
            except Exception:
                pass

    # ---------------- підсумок по виділеному / selection summary ------------ #

    def _update_selection(self, which):
        """Скільки виділено і на скільки байтів.

        Розмір файлів відомий одразу зі списку, а тек — лише після
        «Σ Розмір теки»: Drive не віддає його при перегляді.

        How much is selected and how many bytes it is. File sizes come with
        the listing; folder sizes only after «Σ Folder size», because Drive
        does not report them while browsing.
        """
        if which == "r":
            tree, metas, lbl = self.rtree, self.rmeta, self.lbl_rsel
        else:
            tree, metas, lbl = self.ltree, self.lmeta, self.lbl_lsel

        sel = [i for i in tree.selection() if i in metas]
        if not sel:
            lbl.configure(text=tr("Виділено: —"))
            return

        total = files = dirs = unknown = 0
        for iid in sel:
            m = metas[iid]
            if m.get("dir"):
                dirs += 1
            else:
                files += 1
            size = m.get("size")
            if size is None:
                unknown += 1
            else:
                total += int(size)

        what = ", ".join(x for x in (tr("тек {n}", n=dirs) if dirs else "",
                                     tr("файлів {n}", n=files) if files else "") if x)
        text = tr("Виділено: {n} ({what})   ·   {size}",
                  n=len(sel), what=what, size=human(total))
        if unknown:
            text += tr("   ·   без розміру: {n} — рахує «Σ Розмір теки»", n=unknown)
        lbl.configure(text=text)

    # ---------------- видалення / deletion ---------------- #

    def _confirm_delete(self, names, where) -> bool:
        preview = "\n".join("  • " + n for n in names[:12])
        if len(names) > 12:
            preview += tr("\n  … і ще {n}", n=len(names) - 12)
        return messagebox.askyesno(
            APP_NAME,
            tr("Видалити {n} об'єктів {where}?\n\n{preview}\n\n"
               "Теки видаляються разом із усім вмістом.",
               n=len(names), where=tr(where), preview=preview),
            icon="warning")

    def _delete_local(self):
        sel = [i for i in self.ltree.selection() if i in self.lmeta]
        # корінь диска — не чіпаємо / a drive root is left alone
        safe, roots = [], 0
        for i in sel:
            p = Path(self.lmeta[i]["path"])
            if p.parent == p:
                roots += 1
            else:
                safe.append(i)
        if roots:
            self._log(tr("Корінь диска видалити не можна — пропущено {n}.", n=roots))
        if not safe:
            messagebox.showinfo(APP_NAME, tr("Треба виділити зліва те, що видалити."))
            return

        paths = [self.lmeta[i]["path"] for i in safe]
        if not self._confirm_delete([Path(p).name for p in paths], "у Кошик Windows"):
            return
        try:
            rc, aborted = send_to_recycle_bin(paths)
        except Exception as e:
            messagebox.showerror(APP_NAME, tr("Не вдалося видалити:\n{err}", err=e))
            return
        if rc == 0 and not aborted:
            self._log(tr("[✓] У Кошик Windows: {n} об'єктів", n=len(paths)))
        else:
            self._log(tr("[!] Видалення завершилось з кодом {rc}", rc=rc)
                      + (tr(" (перервано)") if aborted else ""))
        self._after_delete("l", list({self.ltree.parent(i) for i in safe}))

    def _delete_remote(self):
        sel = [i for i in self.rtree.selection()
               if i in self.rmeta and self.rmeta[i].get("path")]
        if not sel:
            messagebox.showinfo(APP_NAME, tr("Треба виділити справа те, що видалити."))
            return
        names = [self.rmeta[i].get("name") or self.rmeta[i]["path"] for i in sel]
        if not self._confirm_delete(names, "у Кошик Google Drive"):
            return

        prefix = self._remote_prefix()
        targets = [(self.rmeta[i]["dir"], prefix + self.rmeta[i]["path"]) for i in sel]
        parents = list({self.rtree.parent(i) for i in sel})
        self._log(tr("Видаляю {n} об'єктів у Кошик Google Drive…", n=len(targets)))
        threading.Thread(target=self._delete_remote_worker,
                         args=(targets, parents), daemon=True).start()

    def _delete_remote_worker(self, targets, parents):
        ok = fail = 0
        for isdir, target in targets:
            # purge прибирає теку з вмістом, deletefile — один файл;
            # обидві поважають --drive-use-trash, тож летить у Кошик
            # purge removes a folder with its contents, deletefile a single
            # file; both respect --drive-use-trash, so it goes to the trash
            cmd = [self.rclone, "purge" if isdir else "deletefile", target]
            try:
                r = run_capture(cmd, timeout=900)
                if r.returncode == 0:
                    ok += 1
                    self.q.put(("log", tr("[✓] Видалено: {target}", target=target)))
                else:
                    fail += 1
                    self.q.put(("log", tr("[!] {target}: {err}", target=target,
                                          err=rclone_error(r.stderr))))
            except Exception as e:
                fail += 1
                self.q.put(("log", tr("[!] {target}: {err}", target=target, err=e)))
        self.q.put(("log", tr("Готово: видалено {ok}, з помилками {fail}. "
                              "Видалене лежить у Кошику Drive 30 днів.",
                              ok=ok, fail=fail)))
        self.q.put(("rdel", ("r", parents)))

    def _after_delete(self, which, parents):
        tree = self.rtree if which == "r" else self.ltree
        metas = self.rmeta if which == "r" else self.lmeta
        for p in parents:
            if p and tree.exists(p) and p in metas:
                self._refresh_node(which, p)
            elif not p:        # видаляли з верхнього рівня / deleted at top level
                if which == "r":
                    self._load_remote_root()
                else:
                    self._load_local_roots()
        self._update_selection(which)

    # ------- розмір тек (на вимогу, для обох дерев) / folder sizes --------- #

    def _base_label(self, which: str, iid) -> str:
        """Підпис вузла без дописаного розміру ("r" — хмара, "l" — локальне).

        A node's label without the appended size ("r" cloud, "l" local).
        """
        if which == "r":
            meta = self.rmeta.get(iid) or {}
            name = meta.get("name")
            return f"📁 {name}" if name else self._remote_prefix()
        path = (self.lmeta.get(iid) or {}).get("path", "")
        name = Path(path).name
        # корінь диска — просто «C:\» / a drive root is just «C:\»
        return f"📁 {name}" if name else path

    def _calc_remote_size(self):
        """Drive не віддає розмір теки у списку — рахуємо рекурсивно, лише на вимогу.

        Drive does not report a folder size in listings — walk it
        recursively, and only on demand.
        """
        prefix = self._remote_prefix()
        sel = [i for i in self.rtree.selection()
               if i in self.rmeta and self.rmeta[i].get("dir")]
        self._start_size("r", sel, {i: prefix + self.rmeta[i]["path"] for i in sel},
                         tr("Треба виділити справа одну або кілька тек."))

    def _calc_local_size(self):
        sel = [i for i in self.ltree.selection()
               if i in self.lmeta and self.lmeta[i].get("dir")]
        self._start_size("l", sel, {i: self.lmeta[i]["path"] for i in sel},
                         tr("Треба виділити зліва одну або кілька тек."))

    def _start_size(self, which, sel, targets, empty_msg):
        if not sel:
            messagebox.showinfo(APP_NAME, empty_msg)
            return
        tree = self.rtree if which == "r" else self.ltree
        for iid in sel:
            base = self._base_label(which, iid)
            if which == "r" and "\\" in self.rmeta[iid]["path"]:
                tree.item(iid, text=tr("{base}   (недосяжна через «\\» в назві)",
                                       base=base))
                continue
            tree.item(iid, text=tr("{base}   (рахую…)", base=base))
            threading.Thread(target=self._size_worker,
                             args=(which, iid, targets[iid]), daemon=True).start()

    def _size_worker(self, which, iid, target):
        try:
            # --fast-list різко зменшує кількість запитів до Drive на великих теках
            # --fast-list cuts the number of Drive requests on large folders
            r = run_capture([self.rclone, "size", target, "--json", "--fast-list"], timeout=900)
            if r.returncode != 0:
                raise RuntimeError(rclone_error(r.stderr))
            d = json.loads(r.stdout or "{}")
            self.q.put(("rsize", (which, iid, d.get("bytes"), d.get("count"), None)))
        except Exception as e:
            self.q.put(("rsize", (which, iid, None, None, str(e))))

    def _apply_size(self, which, iid, nbytes, count, err):
        tree = self.rtree if which == "r" else self.ltree
        if not tree.exists(iid):
            return
        base = self._base_label(which, iid)
        meta = (self.rmeta if which == "r" else self.lmeta).get(iid)
        if err:
            tree.item(iid, text=tr("{base}   (не порахувалось)", base=base))
            self._log(tr("size: {err}", err=err))
        else:
            tree.item(iid, text=tr("{base}   [{size} · {n} файлів]",
                                   base=base, size=human(nbytes), n=count))
            if meta is not None:
                # тепер тека враховується в підсумку / now it counts in the total
                meta["size"] = nbytes
                meta["files"] = count
        self._update_selection(which)

    # ---------------- черга / the queue ---------------- #

    def _sel_dest_remote(self):
        sel = self.rtree.selection()
        if not sel:
            return None
        m = self.rmeta.get(sel[0])
        if not m:
            return None
        p = m["path"] if m["dir"] else m["path"].rsplit("/", 1)[0]
        return self._remote_prefix() + p

    def _sel_dest_local(self):
        sel = self.ltree.selection()
        if not sel:
            return None
        m = self.lmeta.get(sel[0])
        if not m:
            return None
        return m["path"] if m["dir"] else str(Path(m["path"]).parent)

    def _queue_upload(self):
        srcs = [self.lmeta[i] for i in self.ltree.selection() if i in self.lmeta]
        if not srcs:
            messagebox.showinfo(APP_NAME, tr("Треба виділити зліва папки або файли "
                                             "для вивантаження."))
            return
        dst = self._sel_dest_remote()
        if dst is None:
            messagebox.showinfo(APP_NAME, tr("Треба виділити справа теку призначення "
                                             "в хмарі."))
            return
        for m in srcs:
            name = Path(m["path"]).name or m["path"].replace("\\", "").replace(":", "")
            self._add_job(Job(m["path"], dst, m["dir"], "up", name, m.get("size")))

    def _queue_download(self):
        srcs = [self.rmeta[i] for i in self.rtree.selection() if i in self.rmeta]
        srcs = [m for m in srcs if m.get("path")]
        if not srcs:
            messagebox.showinfo(APP_NAME, tr("Треба виділити справа папки або файли "
                                             "для завантаження."))
            return
        dst = self._sel_dest_local()
        if dst is None:
            messagebox.showinfo(APP_NAME, tr("Треба виділити зліва локальну теку "
                                             "призначення."))
            return
        for m in srcs:
            self._add_job(Job(self._remote_prefix() + m["path"], dst, m["dir"], "down",
                              m.get("name") or m["path"].split("/")[-1], m.get("size")))

    def _add_job(self, job: Job):
        self.jobs.append(job)
        arrow = "⬆" if job.direction == "up" else "⬇"
        iid = self.qtree.insert("", "end",
                                values=(arrow, job.src, job.dst_full(self.var_mode.get())))
        self.job_rows[id(job)] = iid
        if self.worker and self.worker.is_alive():
            self._log(tr("Додано в чергу під час роботи: {name} — "
                         "піде одразу після поточних", name=job.name))

    # помилка значущіша за успіх, успіх — за пропуск
    # a failure outranks a success, a success outranks a skip
    _STATE_RANK = {"skip": 1, "ok": 2, "fail": 3}

    def _mark_job(self, job, state):
        """Позначка стану в першій колонці черги.

        У двопрохідному режимі завдання отримує подію двічі: в одному
        проході передається, в іншому відсівається фільтром за розміром.
        Тому лишаємо найзначущіший стан, а не останній — інакше «✓»
        затирається на «–», наче файл і не копіювався.

        The state mark in the queue's first column. In two-pass mode a job
        gets an event twice: in one pass it is transferred, in the other the
        size filter drops it. So the most significant state is kept rather
        than the last one — otherwise «✓» would be overwritten by «–», as if
        the file had never been copied.
        """
        iid = self.job_rows.get(id(job))
        if not iid or not self.qtree.exists(iid):
            return
        if state in self._STATE_RANK:
            prev = self.job_state.get(id(job))
            if prev is None or self._STATE_RANK[state] > self._STATE_RANK[prev]:
                self.job_state[id(job)] = state
            state = self.job_state[id(job)]
        elif not state:
            self.job_state.pop(id(job), None)
        mark = {"run": "▶", "ok": "✓", "fail": "!", "skip": "–"}.get(state, "")
        arrow = "⬆" if job.direction == "up" else "⬇"
        vals = list(self.qtree.item(iid, "values"))
        vals[0] = f"{mark}{arrow}" if mark else arrow
        self.qtree.item(iid, values=vals)

    def _remove_job(self):
        for iid in self.qtree.selection():
            idx = self.qtree.index(iid)
            self.qtree.delete(iid)
            if 0 <= idx < len(self.jobs):
                job = self.jobs.pop(idx)
                self.job_rows.pop(id(job), None)
                self.job_state.pop(id(job), None)

    def _clear_jobs(self):
        self.qtree.delete(*self.qtree.get_children())
        self.jobs.clear()
        self.job_rows.clear()
        self.job_state.clear()

    def _calc_size(self):
        if not self.jobs:
            messagebox.showinfo(APP_NAME, tr(
                "Черга порожня — рахувати нічого.\n\n"
                "Ця кнопка рахує обсяг ЧЕРГИ. Щоб дізнатися розмір окремої теки, "
                "її треба виділити в дереві й натиснути «Σ Розмір теки» над цим деревом."))
            return
        self._log(tr("Рахую обсяг черги…"))

        def work():
            total_b, total_f = 0, 0
            for j in self.jobs:
                try:
                    r = run_capture([self.rclone, "size", j.src, "--json"], timeout=300)
                    d = json.loads(r.stdout or "{}")
                    total_b += d.get("bytes", 0) or 0
                    total_f += d.get("count", 0) or 0
                except Exception as e:
                    self.q.put(("log", tr("size {src}: {err}", src=j.src, err=e)))
            self.q.put(("log", tr("Разом у черзі: {n} файлів, {size}",
                                  n=total_f, size=human(total_b))))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- запуск / running ---------------- #

    def _start(self, passes=None):
        if self.worker and self.worker.is_alive():
            return
        if not self.jobs:
            messagebox.showinfo(APP_NAME, tr("Черга порожня."))
            return
        mode = self.var_mode.get()
        if mode == "sync" and not self.var_dry.get():
            ok = messagebox.askyesno(
                APP_NAME,
                tr("Режим SYNC робить призначення точною копією джерела:\n"
                   "усе зайве в теці призначення БУДЕ ВИДАЛЕНО.\n\nПродовжити?"),
                icon="warning")
            if not ok:
                return
        if mode == "move" and not self.var_dry.get():
            if not messagebox.askyesno(
                    APP_NAME,
                    tr("Режим MOVE видалить файли з джерела після передачі.\n"
                       "Продовжити?"),
                    icon="warning"):
                return

        if passes is None:
            if self._two_pass:            # профіль «обидва проходи» / both passes
                passes = [(short, PROFILES[n]["flags"], PROFILES[n]["transfers"])
                          for short, n in TWO_PASS]
            else:                         # звичайний запуск / an ordinary single pass
                passes = [("", self.var_flags.get().strip(), int(self.var_tr.get()))]

        self.cfg.update({"rclone": self.rclone, "mode": mode, "overwrite": self.var_ow.get(),
                         "transfers": int(self.var_tr.get()), "flags": self.var_flags.get(),
                         "remote": self.var_remote.get()})
        save_cfg(self.cfg)

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pb["value"] = 0
        self.pb_files["value"] = 0
        self._acc_bytes = self._acc_files = 0
        self._sent_bytes = self._sent_files = 0
        self.plan_bytes = self.plan_files = 0
        self.plan_per.clear()
        self._cur_key = None
        self._last_st = {}
        self._log("=" * 70)
        self._log(tr("СТАРТ · режим={mode} · перезапис={ow} · dry-run={dry}",
                     mode=mode, ow=self.var_ow.get(),
                     dry=tr("ТАК") if self.var_dry.get() else tr("ні"))
                  + (tr(" · проходів: {n}", n=len(passes)) if len(passes) > 1 else ""))
        self._log(tr("Журнал: {level} — {hint}",
                     level=self.cb_level.get().lower(),
                     hint=tr(LEVEL_HINT[self.log_level])))

        # скидаємо позначки минулого запуску / clear last run's marks
        self.job_state.clear()
        for j in self.jobs:
            self._mark_job(j, "")
        # передаємо саме живий список: додане під час роботи підхопиться само
        # pass the live list: whatever is added while running is picked up
        self.worker = RcloneWorker(self.rclone, self.jobs, mode, self.var_ow.get(),
                                   self.var_dry.get(), passes, self.q,
                                   level=self.log_level,
                                   show_cmd=bool(self.var_cmd.get()))
        self.worker.start()
        if self.var_plan.get():
            self._log(tr("Міряю обсяг черги — шкали покажуть частку від усього"))
            self.planner = PlanWorker(self.rclone, self.jobs, passes, self.q)
            self.planner.start()
        else:
            self.planner = None
            self._log(tr("Обсяг черги не міряється — шкали показують лише "
                         "поточне завдання"))

    def _stop(self):
        if self.planner:
            self.planner.cancel()
        if self.worker:
            self._log(tr("Скасування…"))
            self.worker.cancel()

    # ------------- події з потоків / events from the threads --------------- #

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "rls":
                    self._fill_remote(*payload)
                elif kind == "rsize":
                    self._apply_size(*payload)
                elif kind == "rdel":
                    self._after_delete(*payload)
                elif kind == "jobdone":
                    self._mark_job(payload[0], payload[1])
                elif kind == "plan":
                    self._add_plan(*payload)
                elif kind == "job":
                    j = payload["job"]
                    self._roll_up()   # підбити попереднє / close off the previous job
                    self._mark_job(j, "run")
                    self._cur_key = (id(j), payload.get("pass") or "")
                    self._job_pos = (payload["index"], payload["total"])
                    tag = f"{tr(payload['pass'])} · " if payload.get("pass") else ""
                    self.lbl_job.configure(
                        text=f"[{payload['index']}/{payload['total']}] {tag}"
                             f"{'⬆' if j.direction == 'up' else '⬇'} {j.src}  →  {payload['dst']}")
                    self._redraw_progress()
                elif kind == "stats":
                    self._update_stats(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self._pump_id = self.after(120, self._pump)

    def _update_stats(self, st):
        self._last_st = st
        self._redraw_progress()
        cur = st.get("transferring") or []
        # поточна швидкість — сума по файлах, що саме йдуть; загальна «speed»
        # у rclone це середнє від початку, тож на паузах вона повзе вниз
        # the current speed is the sum over the files in flight; rclone's own
        # «speed» is an average since the start, so it sags during pauses
        now = sum((t.get("speed") or 0) for t in cur)
        self.lbl_stats.configure(text=tr(
            "{now}/с (сер. {avg}/с)   ·   ETA {eta}   ·   "
            "перевірено {checks}   ·   помилок {errors}",
            now=human(now), avg=human(st.get("speed")),
            eta=human_time(st.get("eta")),
            checks=st.get("checks", 0), errors=st.get("errors", 0)))
        self._show_queue_total()
        if cur:
            parts = []
            for t in cur[:3]:
                p = t.get("percentage")
                parts.append(f"{t.get('name', '')[:48]} {p}%" if p is not None else t.get("name", ""))
            more = f"  (+{len(cur) - 3})" if len(cur) > 3 else ""
            self.lbl_cur.configure(text="→ " + "   |   ".join(parts) + more)
        else:
            self.lbl_cur.configure(text="")

    def _roll_up(self):
        """Завдання, що завершилось, переносимо в оброблене.

        rclone рахує статистику заново на кожен запуск, а запусків стільки,
        скільки завдань помножити на проходи. Коли обсяг заміряно наперед,
        беремо саме його: інакше завдання, де все вже було на місці, так
        і лишилося б незарахованим, і шкала не дійшла б до кінця.

        Move a finished job into the processed totals. rclone counts its
        statistics afresh for every run, and there are as many runs as jobs
        times passes. When the size was measured up front that figure is
        used: otherwise a job where everything was already in place would
        never be credited and the bar would not reach the end.
        """
        st = self._last_st or {}
        self._sent_files += st.get("transfers", 0) or 0
        self._sent_bytes += st.get("bytes", 0) or 0
        planned = self.plan_per.get(self._cur_key) if self._cur_key else None
        if planned:
            self._acc_files += planned[0]
            self._acc_bytes += planned[1]
        else:
            self._acc_files += (st.get("transfers", 0) or 0) + (st.get("checks", 0) or 0)
            self._acc_bytes += st.get("bytes", 0) or 0
        self._last_st = {}
        self._cur_key = None

    def _redraw_one_file(self, cur):
        """Шкала поточного файлу.

        Потоків може бути кілька, а шкала одна, тож показуємо найбільший
        файл із тих, що зараз ідуть: саме він визначає, скільки чекати.
        Решту видно рядком нижче.

        The bar for the current file. There may be several threads but only
        one bar, so the largest file in flight is shown: that is what decides
        how long the wait is. The rest are on the line below.
        """
        if not cur:
            self.pb_one["value"] = 0
            self.lbl_pb1.configure(text=tr("Файл   —"))
            return
        t = max(cur, key=lambda x: x.get("size") or 0)
        size = t.get("size") or 0
        done = t.get("bytes") or 0
        p = t.get("percentage")
        frac = (p / 100.0) if p is not None else ((done / size) if size else 0.0)
        self.pb_one["value"] = min(1000, frac * 1000)
        name = str(t.get("name") or "")
        name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if len(name) > 34:
            name = name[:31] + "…"
        more = f"   (+{len(cur) - 1})" if len(cur) > 1 else ""
        self.lbl_pb1.configure(text=tr(
            "Файл   {pct:5.1f}%   {done} / {size}   ·   {name}{more}",
            pct=frac * 100, done=human(done), size=human(size),
            name=name, more=more))

    def _add_plan(self, key_id, label, files, nbytes):
        """Заміряне завдання: збільшуємо знаменник шкал.

        A measured job: grow the bars' denominator.
        """
        key = (key_id, label)
        if key in self.plan_per:
            return
        self.plan_per[key] = (files, nbytes)
        self.plan_files += files
        self.plan_bytes += nbytes
        self._redraw_progress()

    def _redraw_progress(self):
        """Три шкали: уся черга за обсягом, за кількістю файлів і поточний файл.

        Поки обсяг черги не заміряно, перші дві показують те, що встиг
        нарахувати сам rclone по поточному завданню, — інакше шкал не
        було б узагалі.

        Three bars: the whole queue by volume, by file count, and the current
        file. Until the queue has been measured the first two show whatever
        rclone has counted for the current job — otherwise there would be no
        bars at all.
        """
        st = self._last_st or {}
        self._redraw_one_file(st.get("transferring") or [])
        run_b = st.get("bytes", 0) or 0
        run_f = (st.get("transfers", 0) or 0) + (st.get("checks", 0) or 0)
        whole = self.plan_files or self.plan_bytes
        if whole:
            done_b, total_b = self._acc_bytes + run_b, self.plan_bytes
            done_f, total_f = self._acc_files + run_f, self.plan_files
            jb, jf = st.get("totalBytes") or 0, st.get("totalTransfers") or 0
            # по завданню лишаємо тільки відсоток: під половинну ширину
            # повні числа не влізають, а подробиці є в рядку нижче
            # only a percentage for the job: full figures do not fit into
            # half the width, and the details are on the line below
            tail_b = tr("   ·   завдання {pct:.0f}%", pct=run_b / jb * 100) if jb else ""
            tail_f = tr("   ·   завдання {pct:.0f}%", pct=run_f / jf * 100) if jf else ""
        else:
            done_b, total_b = run_b, st.get("totalBytes", 0) or 0
            done_f, total_f = run_f, st.get("totalTransfers", 0) or 0
            tail_b = tail_f = ""

        fb = (done_b / total_b) if total_b else 0.0
        ff = (done_f / total_f) if total_f else 0.0
        self.pb["value"] = min(1000, fb * 1000)
        self.pb_files["value"] = min(1000, ff * 1000)
        self.lbl_pb.configure(
            text=tr("Обсяг   {pct:5.1f}%   {done} / {total}{tail}",
                    pct=fb * 100, done=human(done_b), total=human(total_b),
                    tail=tail_b)
            if total_b else tr("Обсяг   —"))
        self.lbl_pbf.configure(
            text=tr("Файли   {pct:5.1f}%   {done} / {total}{tail}",
                    pct=ff * 100, done=done_f, total=total_f, tail=tail_f)
            if total_f else tr("Файли   —"))
        # відсоток дублюємо в заголовок: видно на панелі задач, коли вікно згорнуте
        # the percentage is mirrored in the title: visible in the taskbar
        # while the window is minimised
        if total_b:
            self.title(f"{APP_NAME} — {fb * 100:.0f}%  "
                       f"[{self._job_pos[0]}/{self._job_pos[1]}]")

    def _show_queue_total(self):
        st = self._last_st or {}
        files = self._sent_files + (st.get("transfers", 0) or 0)
        size = self._sent_bytes + (st.get("bytes", 0) or 0)
        pos, total = self._job_pos
        text = tr("Черга: завдання {pos} з {total}   ·   "
                  "справді передано {n} файлів, {size}",
                  pos=pos, total=total, n=files, size=human(size))
        if self.planner and self.planner.is_alive():
            text += tr("   ·   обсяг черги ще рахується…")
        self.lbl_queue.configure(text=text)

    def _finish(self, payload):
        if self.planner:
            self.planner.cancel()
        self._roll_up()
        self._redraw_progress()
        self._show_queue_total()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.title(APP_NAME)
        if payload["cancelled"]:
            msg = tr("Скасовано користувачем.")
        elif payload["errors"]:
            msg = tr("Завершено з помилками: {n}", n=payload["errors"])
        else:
            msg = tr("Усе успішно за {time}.", time=human_time(payload["elapsed"]))
            self.pb["value"] = 1000
        self.lbl_job.configure(text=msg)
        self._log(msg)
        if self.var_dry.get():
            self._log(tr("Це був DRY-RUN — реальних змін не зроблено. "
                         "Щоб виконати насправді, треба зняти галочку "
                         "й натиснути «Старт» ще раз."))

    # ---------------- дрібниці / small things ---------------- #

    def _log_shown(self, entry: str) -> str:
        """Що саме показати. Рішення приймається за формою запису, а не за
        його змістом: rclone завжди кладе опис у перший рядок, а машинні
        подробиці нижче. Однорядкові записи не чіпаються взагалі.

        What exactly to show. The decision rests on the entry's shape, not
        its meaning: rclone always puts the description on the first line and
        machine detail below. Single-line entries are left alone entirely.
        """
        if not self.var_compact.get():
            return entry
        lines = entry.splitlines()
        if len(lines) <= 1:
            return entry
        return lines[0] + tr("   … +{n} рядків", n=len(lines) - 1)

    def _log(self, text):
        entry = time.strftime("[%H:%M:%S] ") + str(text)
        self.log_full.append(entry)
        self._to_log_file(entry)
        self.log.configure(state="normal")
        self.log.insert("end", self._log_shown(entry) + "\n")
        # на великих передачах рядків десятки тисяч; у вікні тримаємо хвіст,
        # повний текст лишається в пам'яті та у файлі
        # big transfers produce tens of thousands of lines; the window keeps
        # the tail while the full text stays in memory and in the file
        if int(self.log.index("end-1c").split(".")[0]) > LOG_WIDGET_MAX + 1000:
            self.log.delete("1.0", f"{1000}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_log_level(self, _evt=None):
        i = self.cb_level.current()
        self.log_level = LOG_LEVELS[self.LVL_KEYS[i]] if i >= 0 else "normal"
        self.cfg["log_level"] = self.log_level
        save_cfg(self.cfg)
        self._sync_level_hint()
        self._log(tr("Рівень журналу: {level} — {hint}. Діє з наступного запуску.",
                     level=self.cb_level.get().lower(),
                     hint=tr(LEVEL_HINT[self.log_level])))

    def _sync_level_hint(self):
        self.lbl_level.configure(text=tr("— {hint}",
                                         hint=tr(LEVEL_HINT[self.log_level])))

    def _save_show_cmd(self):
        self.cfg["show_cmd"] = bool(self.var_cmd.get())
        save_cfg(self.cfg)

    def _save_plan(self):
        self.cfg["plan"] = bool(self.var_plan.get())
        save_cfg(self.cfg)

    def _pick_log_file(self):
        if self.log_path:        # повторний клік / a second click stops writing
            self._log(tr("Запис у файл вимкнено ({path})", path=self.log_path))
            self.log_path = ""
        else:
            p = filedialog.asksaveasfilename(
                title=tr("Куди писати повний журнал"), defaultextension=".log",
                initialfile=time.strftime("rclone_%Y-%m-%d_%H-%M.log"),
                filetypes=[(tr("Журнал"), "*.log"), (tr("Усі"), "*.*")])
            if not p:
                return
            self.log_path = p
            self._log(tr("Повний журнал пишеться у {path}", path=p))
        self.cfg["log_file"] = self.log_path
        save_cfg(self.cfg)
        self._sync_logfile_button()

    def _sync_logfile_button(self):
        self.btn_logfile.configure(
            text=tr("Не писати у файл") if self.log_path else tr("Файл журналу…"))

    def _to_log_file(self, entry: str):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            # щоб не сипати помилками щорядка / so it does not error per line
            self.log_path = ""
            self._sync_logfile_button()
            self._log(tr("[!] Не вдалось писати журнал у файл: {err}", err=e))

    def _rerender_log(self):
        self.cfg["compact_log"] = bool(self.var_compact.get())
        save_cfg(self.cfg)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for e in self.log_full[-LOG_WIDGET_MAX:]:
            self.log.insert("end", self._log_shown(e) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log_full.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _to_clipboard(self, text):
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        # щоб буфер пережив закриття вікна / so the clipboard outlives the window
        self.update_idletasks()

    def _copy_log_sel(self, _evt=None):
        """Ctrl+C: виділене, а якщо нічого не виділено — весь журнал.

        Ctrl+C: the selection, or the whole log when nothing is selected.
        """
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""
        self._to_clipboard(text or self.log.get("1.0", "end-1c"))
        return "break"

    def _copy_log_all(self):
        # у буфер завжди повний текст, навіть коли на екрані стислий
        # always the full text, even when the screen shows the compact form
        self._to_clipboard("\n".join(self.log_full))

    def _select_log_all(self, _evt=None):
        self.log.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _log_menu_popup(self, e):
        self.log.focus_set()
        try:
            self.log_menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.log_menu.grab_release()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, tr("Триває передача. Перервати і вийти?")):
                return
            self.worker.cancel()
        for aid in (getattr(self, "_pump_id", None), getattr(self, "_sash_id", None)):
            try:
                # щоб нічого не смикалось після знищення вікна
                # so nothing fires after the window is destroyed
                self.after_cancel(aid)
            except Exception:
                pass
        # запам'ятовуємо розмір вікна і положення роздільників на наступний запуск
        # remember the window size and splitter positions for the next run
        try:
            self.cfg["geometry"] = self.geometry()
            self.cfg["tree_h"] = self.tree_area.winfo_height()
            self.cfg["hsash"] = self.hpanes.sashpos(0)
            save_cfg(self.cfg)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()