#!/usr/bin/env python3
"""Продвинутый мониторинг процессов с гибкими настройками"""

import subprocess
import sys
try:
    import psutil
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "requests"])
    import psutil
    import requests

import time
import json
from datetime import datetime, timedelta
from typing import Set, Dict, List, Optional
from threading import Thread, Lock
from collections import defaultdict

# Конфигурация
TELEGRAM_TOKEN = "KEY"
CHECK_INTERVAL = 5
BASE_DIR = "/root/Desktop/process-monitor"
IGNORED_FILE = f"{BASE_DIR}/ignored_processes.json"
USERS_FILE = f"{BASE_DIR}/active_users.json"
SETTINGS_FILE = f"{BASE_DIR}/user_settings.json"
WHITELIST_FILE = f"{BASE_DIR}/whitelist.json"
STATS_FILE = f"{BASE_DIR}/stats.json"

# Глобальные переменные
known_processes: Set[int] = set()
ignored_processes: Set[str] = set()
whitelist_processes: Set[str] = set()
active_users: Set[str] = set()
user_settings: Dict[str, Dict] = {}
process_stats: Dict[str, List] = defaultdict(list)
pending_notifications: Dict[str, List] = defaultdict(list)
last_update_id = 0
data_lock = Lock()

# Системные процессы
DEFAULT_SYSTEM_PROCESSES = {
    "systemd", "kthreadd", "rcu_gp", "rcu_par_gp", "kworker", "kcompactd", "ksoftirqd", "migration", "watchdog", "cpuhp", "kdevtmpfs", "netns", "khungtaskd", "oom_reaper", "writeback", "kblockd", "kintegrityd", "md", "devfreq_wq", "watch
dogd", "kswapd", "sshd"
}

# Настройки по умолчанию
DEFAULT_USER_SETTINGS = {
    "mode": "blacklist",
    "group_notifications": True,
    "group_interval": 30,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "08:00",
    "ignore_system": True,
    "min_cpu_percent": 0,
    "min_memory_mb": 0,
    "track_stats": True,
    "last_message_id": None,
    "update_single_message": False
}

def load_json_file(filepath: str, default) -> any:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default

def save_json_file(filepath: str, data: any) -> None:
    with data_lock:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def load_ignored_processes() -> None:
    global ignored_processes
    data = load_json_file(IGNORED_FILE, list(DEFAULT_SYSTEM_PROCESSES))
    ignored_processes = set(data)

def save_ignored_processes() -> None:
    save_json_file(IGNORED_FILE, list(ignored_processes))

def load_whitelist() -> None:
    global whitelist_processes
    data = load_json_file(WHITELIST_FILE, [])
    whitelist_processes = set(data)

def save_whitelist() -> None:
    save_json_file(WHITELIST_FILE, list(whitelist_processes))

def load_active_users() -> None:
    global active_users
    data = load_json_file(USERS_FILE, [])
    active_users = set(data)

def save_active_users() -> None:
    save_json_file(USERS_FILE, list(active_users))

def load_user_settings() -> None:
    global user_settings
    user_settings = load_json_file(SETTINGS_FILE, {})
    for user_id in active_users:
        if user_id not in user_settings:
            user_settings[user_id] = DEFAULT_USER_SETTINGS.copy()

def save_user_settings() -> None:
    save_json_file(SETTINGS_FILE, user_settings)

def get_user_settings(chat_id: str) -> Dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_USER_SETTINGS.copy()
        save_user_settings()
    return user_settings[chat_id]

def load_stats() -> None:
    global process_stats
    data = load_json_file(STATS_FILE, {})
    process_stats = defaultdict(list, data)

def save_stats() -> None:
    save_json_file(STATS_FILE, dict(process_stats))

def add_process_stat(process_name: str, info: Dict) -> None:
    process_stats[process_name].append({
        "timestamp": datetime.now().isoformat(),
        "pid": info["pid"],
        "user": info["username"],
        "cpu": info["cpu_percent"],
        "memory": info["memory_mb"]
    })
    if len(process_stats[process_name]) > 1000:
        process_stats[process_name] = process_stats[process_name][-1000:]

def is_quiet_hours(chat_id: str) -> bool:
    settings = get_user_settings(chat_id)
    if not settings["quiet_hours_enabled"]:
        return False
    now = datetime.now().time()
    start = datetime.strptime(settings["quiet_hours_start"], "%H:%M").time()
    end = datetime.strptime(settings["quiet_hours_end"], "%H:%M").time()
    if start < end:
        return start <= now <= end
    else:
        return now >= start or now <= end

def send_telegram_message(message: str, reply_markup: dict = None, chat_id: str = None, edit_message_id: int = None) -> Optional[int]:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    chat_ids = [chat_id] if chat_id else list(active_users)
    for cid in chat_ids:
        if not edit_message_id and is_quiet_hours(cid):
            print(f"Тихие часы для {cid}, пропуск")
            continue
        payload = {
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            if edit_message_id:
                url = f"{base_url}/editMessageText"
                payload["message_id"] = edit_message_id
            else:
                url = f"{base_url}/sendMessage"
            print(f"Отправка в {cid}: {message[:50]}...")
            r = requests.post(url, json=payload, timeout=10)
            print(f"Telegram ответ: статус {r.status_code}, тело {r.text}")
            if r.status_code == 200:
                result = r.json()
                if not result.get("ok"):
                    print(f"API ошибка: {result.get('description')}")
                if result.get("ok") and "result" in result:
                    return result["result"].get("message_id")
        except Exception as e:
            print(f"Ошибка отправки: {e}")
    return None

def get_process_info(proc: psutil.Process) -> Optional[Dict]:
    try:
        return {
            "pid": proc.pid,
            "name": proc.name(),
            "exe": proc.exe() or "N/A",
            "cmdline": " ".join(proc.cmdline()) if proc.cmdline() else "N/A",
            "username": proc.username(),
            "create_time": datetime.fromtimestamp(proc.create_time()).strftime("%Y-%m-%d %H:%M:%S"),
            "status": proc.status(),
            "cpu_percent": proc.cpu_percent(interval=0.1),
            "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 2)
        }
    except:
        return None

def should_notify_process(info: Dict, chat_id: str) -> bool:
    settings = get_user_settings(chat_id)
    if info["cpu_percent"] < settings["min_cpu_percent"]:
        return False
    if info["memory_mb"] < settings["min_memory_mb"]:
        return False
    mode = settings["mode"]
    if mode == "whitelist":
        return info["name"] in whitelist_processes
    elif mode == "blacklist":
        if info["name"] in ignored_processes:
            return False
        if settings["ignore_system"] and info["name"] in DEFAULT_SYSTEM_PROCESSES:
            return False
        return True
    elif mode == "smart":
        if info["name"] in whitelist_processes:
            return True
        if info["name"] in ignored_processes:
            return False
        if settings["ignore_system"] and info["name"] in DEFAULT_SYSTEM_PROCESSES:
            return False
        return True
    return True

def format_process_notification(info: Dict) -> str:
    return f"""🔔 <b>Новый процесс</b>
📋 <b>Название:</b> {info['name']}
🆔 <b>PID:</b> {info['pid']}
👤 <b>Пользователь:</b> {info['username']}
📅 <b>Время:</b> {info['create_time']}
📊 <b>Статус:</b> {info['status']}
💾 <b>Память:</b> {info['memory_mb']} MB
⚙️ <b>CPU:</b> {info['cpu_percent']:.1f}%
📂 <b>Файл:</b> <code>{info['exe']}</code>
🖥 <b>Команда:</b> <code>{info['cmdline'][:500]}</code>"""

def format_grouped_notification(processes: List[Dict]) -> str:
    count = len(processes)
    message = f"🔔 <b>Обнаружено новых процессов: {count}</b>\n\n"
    for info in processes[:10]:
        message += f"• <b>{info['name']}</b> (PID: {info['pid']}, CPU: {info['cpu_percent']:.1f}%, RAM: {info['memory_mb']}MB)\n"
        message += f" 👤 {info['username']} | 📅 {info['create_time']}\n\n"
    if count > 10:
        message += f"\n<i>... и ещё {count - 10} процессов</i>"
    return message

def create_process_keyboard(process_name: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "❌ Игнорировать", "callback_data": f"ignore_{process_name}"},
            {"text": "⭐ В белый список", "callback_data": f"whitelist_{process_name}"}
        ]]
    }

def create_settings_keyboard(chat_id: str) -> dict:
    settings = get_user_settings(chat_id)
    mode_emoji = {"blacklist": "🚫", "whitelist": "⭐", "smart": "🧠"}
    mode_text = mode_emoji.get(settings["mode"], "🚫")
    group_text = "✅" if settings["group_notifications"] else "❌"
    quiet_text = "✅" if settings["quiet_hours_enabled"] else "❌"
    system_text = "✅" if settings["ignore_system"] else "❌"
    stats_text = "✅" if settings["track_stats"] else "❌"
    return {
        "inline_keyboard": [
            [{"text": f"{mode_text} Режим: {settings['mode']}", "callback_data": "set_mode"}],
            [{"text": f"{group_text} Группировка уведомлений", "callback_data": "toggle_group"}],
            [{"text": f"{quiet_text} Тихие часы ({settings['quiet_hours_start']}-{settings['quiet_hours_end']})", "callback_data": "set_quiet"}],
            [{"text": f"{system_text} Игнорировать системные", "callback_data": "toggle_system"}],
            [{"text": f"⚙️ CPU порог: {settings['min_cpu_percent']}%", "callback_data": "set_cpu"}],
            [{"text": f"💾 RAM порог: {settings['min_memory_mb']}MB", "callback_data": "set_memory"}],
            [{"text": f"{stats_text} Статистика", "callback_data": "toggle_stats"}],
            [{"text": "🔙 Назад", "callback_data": "main_menu"}]
        ]
    }

def get_telegram_updates() -> List[dict]:
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        response = requests.get(url, params={"offset": last_update_id + 1, "timeout": 30})
        if response.status_code == 200:
            data = response.json()
            if data["ok"] and data["result"]:
                last_update_id = data["result"][-1]["update_id"]
                return data["result"]
    except Exception as e:
        print(f"getUpdates ошибка: {e}")
    return []

def handle_callback(cq: dict) -> None:
    cd = cq.get("data", "")
    chat_id = str(cq["message"]["chat"]["id"])
    mid = cq["message"]["message_id"]
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if cd.startswith("ignore_"):
        pn = cd.replace("ignore_", "")
        ignored_processes.add(pn)
        save_ignored_processes()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": f"✅ {pn} игнорируется"})
        requests.post(f"{base_url}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
        send_telegram_message(f"🔕 <b>{pn}</b> добавлен в игнорируемые", chat_id=chat_id)
    elif cd.startswith("unignore_"):
        pn = cd.replace("unignore_", "")
        if pn in ignored_processes:
            ignored_processes.remove(pn)
            save_ignored_processes()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": f"✅ {pn} включен"})
        send_telegram_message(f"🔔 <b>{pn}</b> удалён из игнорируемых", chat_id=chat_id)
    elif cd.startswith("whitelist_"):
        pn = cd.replace("whitelist_", "")
        whitelist_processes.add(pn)
        save_whitelist()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": f"⭐ {pn} в белом списке"})
        requests.post(f"{base_url}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
        send_telegram_message(f"⭐ <b>{pn}</b> добавлен в белый список", chat_id=chat_id)
    elif cd.startswith("unwhitelist_"):
        pn = cd.replace("unwhitelist_", "")
        if pn in whitelist_processes:
            whitelist_processes.remove(pn)
            save_whitelist()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": f"✅ {pn} удалён"})
        send_telegram_message(f"❌ <b>{pn}</b> удалён из белого списка", chat_id=chat_id)
    elif cd in ["toggle_group", "toggle_system", "toggle_stats"]:
        settings = get_user_settings(chat_id)
        if cd == "toggle_group":
            settings["group_notifications"] = not settings["group_notifications"]
        elif cd == "toggle_system":
            settings["ignore_system"] = not settings["ignore_system"]
        elif cd == "toggle_stats":
            settings["track_stats"] = not settings["track_stats"]
        save_user_settings()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": "✅ Изменено"})
        requests.post(f"{base_url}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": mid, "reply_markup": create_settings_keyboard(chat_id)})
    elif cd == "set_mode":
        settings = get_user_settings(chat_id)
        modes = ["blacklist", "whitelist", "smart"]
        current_idx = modes.index(settings["mode"])
        settings["mode"] = modes[(current_idx + 1) % 3]
        save_user_settings()
        mode_names = {"blacklist": "Черный список", "whitelist": "Белый список", "smart": "Умный"}
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": f"Режим: {mode_names[settings['mode']]}"})
        requests.post(f"{base_url}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": mid, "reply_markup": create_settings_keyboard(chat_id)})
    elif cd == "set_quiet":
        settings = get_user_settings(chat_id)
        settings["quiet_hours_enabled"] = not settings["quiet_hours_enabled"]
        save_user_settings()
        requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cq["id"], "text": "✅ Изменено"})
        requests.post(f"{base_url}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": mid, "reply_markup": create_settings_keyboard(chat_id)})
    elif cd == "set_cpu":
        # Здесь можно добавить логику запроса нового значения, но для полноты оставим заглушку
        send_telegram_message("Введите новый CPU порог: /setcpu <число>", chat_id=chat_id)
    elif cd == "set_memory":
        # Заглушка
        send_telegram_message("Введите новый RAM порог: /setram <число>", chat_id=chat_id)
    elif cd == "main_menu":
        # Возврат в главное меню
        send_telegram_message("Главное меню", reply_markup=create_settings_keyboard(chat_id), chat_id=chat_id, edit_message_id=mid)

def handle_commands(msg: dict) -> None:
    txt = msg.get("text", "").strip()
    cid = str(msg["chat"]["id"])
    username = msg.get("from", {}).get("username", "unknown")
    print(f"Команда '{txt}' от @{username} (chat_id: {cid})")
    if txt == "/start":
        if cid not in active_users:
            active_users.add(cid)
            save_active_users()
            user_settings[cid] = DEFAULT_USER_SETTINGS.copy()
            save_user_settings()
        send_telegram_message(
            "✅ <b>Добро пожаловать в Process Monitor!</b>\n\n"
            "🔔 Уведомления включены\n"
            "⚙️ /settings — настройки\n\n"
            "📚 <b>Команды:</b>\n"
            "/settings /list /whitelist /stats /help", chat_id=cid
        )
    elif txt == "/stop":
        if cid in active_users:
            active_users.remove(cid)
            save_active_users()
        send_telegram_message("👋 Уведомления отключены. /start для включения", chat_id=cid)
    elif cid not in active_users:
        send_telegram_message("⚠️ /start для активации", chat_id=cid)
        return
    elif txt == "/help":
        help_text = """ 📚 <b>Справка</b>
Основные: /start /stop /settings /help
Списки: /list /whitelist
Статистика: /stats /history <процесс>
Тихие часы: /quiet 22:00-08:00 или /quiet off
Режимы: 🚫 черный список ⭐ белый список 🧠 умный"""
        send_telegram_message(help_text, chat_id=cid)
    elif txt == "/list":
        ignored = '\n'.join(ignored_processes)
        send_telegram_message(f"Игнорируемые: {ignored}", chat_id=cid)
    elif txt == "/whitelist":
        white = '\n'.join(whitelist_processes)
        send_telegram_message(f"Белый список: {white}", chat_id=cid)
    elif txt.startswith("/history"):
        _, proc = txt.split(maxsplit=1)
        stats = process_stats.get(proc, [])
        msg = f"Статистика {proc}: {len(stats)} записей"
        send_telegram_message(msg, chat_id=cid)
    elif txt.startswith("/quiet"):
        # Логика установки тихих часов
        parts = txt.split()
        if len(parts) > 1:
            if parts[1] == "off":
                get_user_settings(cid)["quiet_hours_enabled"] = False
            else:
                start, end = parts[1].split('-')
                settings = get_user_settings(cid)
                settings["quiet_hours_start"] = start
                settings["quiet_hours_end"] = end
                settings["quiet_hours_enabled"] = True
            save_user_settings()
            send_telegram_message("Тихие часы обновлены", chat_id=cid)
    elif txt.startswith("/setcpu"):
        _, val = txt.split()
        get_user_settings(cid)["min_cpu_percent"] = float(val)
        save_user_settings()
        send_telegram_message(f"CPU порог: {val}%", chat_id=cid)
    elif txt.startswith("/setram"):
        _, val = txt.split()
        get_user_settings(cid)["min_memory_mb"] = float(val)
        save_user_settings()
        send_telegram_message(f"RAM порог: {val}MB", chat_id=cid)
    else:
        send_telegram_message("❓ Неизвестная команда. /help", chat_id=cid)

def telegram_bot_listener() -> None:
    print("🤖 Bot listener запущен")
    while True:
        try:
            updates = get_telegram_updates()
            for upd in updates:
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd and "text" in upd["message"]:
                    handle_commands(upd["message"])
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(3)

def notification_sender() -> None:
    print("📤 Notification sender запущен")
    while True:
        try:
            time.sleep(5)
            with data_lock:
                for chat_id in list(pending_notifications):
                    processes = pending_notifications[chat_id]
                    if not processes:
                        continue
                    settings = get_user_settings(chat_id)
                    if settings["group_notifications"]:
                        first_time = datetime.strptime(processes[0]["create_time"], "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - first_time).seconds < settings["group_interval"]:
                            continue
                    if len(processes) == 1:
                        msg = format_process_notification(processes[0])
                        kb = create_process_keyboard(processes[0]["name"])
                        send_telegram_message(msg, reply_markup=kb, chat_id=chat_id)
                    else:
                        msg = format_grouped_notification(processes)
                        send_telegram_message(msg, chat_id=chat_id)
                    pending_notifications[chat_id].clear()
        except Exception as e:
            print(f"Notification error: {e}")
        time.sleep(5)

def initialize_known_processes() -> None:
    global known_processes
    known_processes = {p.pid for p in psutil.process_iter()}
    print(f"Инициализировано процессов: {len(known_processes)}")

def monitor_processes() -> None:
    global known_processes
    print(f"Мониторинг запущен (пользователей: {len(active_users)})")
    while True:
        try:
            current_processes = set()
            for proc in psutil.process_iter():
                current_processes.add(proc.pid)
                if proc.pid not in known_processes:
                    info = get_process_info(proc)
                    if not info:
                        continue
                    for chat_id in list(active_users):
                        if should_notify_process(info, chat_id):
                            settings = get_user_settings(chat_id)
                            if settings["track_stats"]:
                                add_process_stat(info["name"], info)
                            if settings["group_notifications"]:
                                pending_notifications[chat_id].append(info)
                            else:
                                msg = format_process_notification(info)
                                kb = create_process_keyboard(info["name"])
                                send_telegram_message(msg, reply_markup=kb, chat_id=chat_id)
            known_processes = current_processes.copy()
            if int(time.time()) % 60 == 0:
                save_stats()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"Monitor error: {e}")
            time.sleep(CHECK_INTERVAL)

def main():
    print("=" * 50)
    print("🚀 Process Monitor Pro")
    print("=" * 50)
    load_ignored_processes()
    load_whitelist()
    load_active_users()
    load_user_settings()
    load_stats()
    print(f"Пользователей: {len(active_users)}")
    print(f"Игнорируемых: {len(ignored_processes)}")
    print(f"Белый список: {len(whitelist_processes)}")
    initialize_known_processes()
    Thread(target=telegram_bot_listener, daemon=True).start()
    Thread(target=notification_sender, daemon=True).start()
    try:
        monitor_processes()
    finally:
        save_ignored_processes()
        save_whitelist()
        save_active_users()
        save_user_settings()
        save_stats()
        print("Данные сохранены")

if __name__ == "__main__":
    main()
