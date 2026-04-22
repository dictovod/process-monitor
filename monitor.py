#!/usr/bin/env python3
"""
Process Monitor Pro — профессиональный Telegram-бот мониторинга процессов
Версия 2.0
"""

import subprocess
import sys
import os

# Автоустановка зависимостей
def _install_deps():
    pkgs = ["psutil", "requests"]
    for pkg in pkgs:
        try:
            __import__(pkg)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"])

_install_deps()

import psutil
import requests
import time
import json
import socket
import logging
from datetime import datetime
from typing import Optional, Dict, List, Set, Any
from threading import Thread, Lock, Event
from collections import defaultdict

# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ — измени токен здесь
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = "TOKEN"
CHECK_INTERVAL  = 5          # секунд между проверками процессов
BASE_DIR        = "/root/Desktop/process-monitor"
LOG_FILE        = f"{BASE_DIR}/monitor.log"

# ─── пути к файлам данных ───
IGNORED_FILE  = f"{BASE_DIR}/ignored_processes.json"
USERS_FILE    = f"{BASE_DIR}/active_users.json"
SETTINGS_FILE = f"{BASE_DIR}/user_settings.json"
WHITELIST_FILE= f"{BASE_DIR}/whitelist.json"
STATS_FILE    = f"{BASE_DIR}/stats.json"

# ─── системные процессы (игнорируются по умолчанию) ───
DEFAULT_SYSTEM = {
    "systemd","kthreadd","rcu_gp","rcu_par_gp","kworker","kcompactd",
    "ksoftirqd","migration","watchdog","cpuhp","kdevtmpfs","netns",
    "khungtaskd","oom_reaper","writeback","kblockd","kintegrityd",
    "devfreq_wq","watchdogd","kswapd","sshd","bash","sh","python3",
    "python","grep","cat","ls","ps","top","htop","systemd-journald",
    "systemd-udevd","systemd-logind","dbus-daemon","NetworkManager",
    "rsyslogd","cron","agetty","login",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "blacklist",          # blacklist | whitelist | smart
    "group_notifications": True,
    "group_interval": 30,         # секунд для группировки
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end":   "08:00",
    "ignore_system": True,
    "min_cpu_percent": 0.0,
    "min_memory_mb":  0.0,
    "track_stats": True,
}

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
os.makedirs(BASE_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("monitor")

# ─────────────────────────────────────────────
#  ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ─────────────────────────────────────────────
_lock               = Lock()
known_pids:         Set[int]              = set()
ignored_procs:      Set[str]             = set()
whitelist_procs:    Set[str]             = set()
active_users:       Set[str]             = set()
user_settings:      Dict[str, Dict]      = {}
process_stats:      Dict[str, List]      = defaultdict(list)
pending:            Dict[str, List]      = defaultdict(list)   # chat_id → [info, ...]
last_update_id:     int                  = 0
stop_event:         Event                = Event()

# ─────────────────────────────────────────────
#  УТИЛИТЫ: JSON-хранилище
# ─────────────────────────────────────────────
def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path: str, data) -> None:
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)   # атомарная запись

def load_all() -> None:
    global ignored_procs, whitelist_procs, active_users, user_settings, process_stats
    ignored_procs   = set(_load(IGNORED_FILE,  list(DEFAULT_SYSTEM)))
    whitelist_procs = set(_load(WHITELIST_FILE, []))
    active_users    = set(str(u) for u in _load(USERS_FILE, []))
    user_settings   = _load(SETTINGS_FILE, {})
    process_stats   = defaultdict(list, _load(STATS_FILE, {}))
    # гарантируем настройки для каждого пользователя
    for uid in active_users:
        user_settings.setdefault(uid, DEFAULT_SETTINGS.copy())

def save_all() -> None:
    _save(IGNORED_FILE,  list(ignored_procs))
    _save(WHITELIST_FILE, list(whitelist_procs))
    _save(USERS_FILE,    list(active_users))
    _save(SETTINGS_FILE, user_settings)
    _save(STATS_FILE,    dict(process_stats))

def get_settings(chat_id: str) -> Dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_SETTINGS.copy()
        _save(SETTINGS_FILE, user_settings)
    return user_settings[chat_id]

# ─────────────────────────────────────────────
#  TELEGRAM API
# ─────────────────────────────────────────────
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SESSION  = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

def _tg(method: str, **kwargs) -> Optional[Dict]:
    """Универсальный вызов Telegram Bot API с логированием ошибок."""
    try:
        r = SESSION.post(f"{BASE_URL}/{method}", json=kwargs, timeout=15)
        data = r.json()
        if not data.get("ok"):
            log.warning("TG %s error: %s", method, data.get("description", "?"))
            return None
        return data.get("result")
    except Exception as e:
        log.error("TG %s exception: %s", method, e)
        return None

def send_message(chat_id: str, text: str,
                 markup: dict = None,
                 edit_id: int = None,
                 parse_mode: str = "HTML") -> Optional[int]:
    """Отправить или отредактировать сообщение. Возвращает message_id."""
    params = dict(chat_id=chat_id, text=text, parse_mode=parse_mode)
    if markup:
        params["reply_markup"] = markup
    if edit_id:
        params["message_id"] = edit_id
        res = _tg("editMessageText", **params)
    else:
        res = _tg("sendMessage", **params)
    if isinstance(res, dict):
        return res.get("message_id")
    return None

def answer_callback(callback_id: str, text: str = "") -> None:
    _tg("answerCallbackQuery", callback_query_id=callback_id, text=text)

def get_updates(offset: int, timeout: int = 30) -> List[Dict]:
    res = _tg("getUpdates", offset=offset, timeout=timeout,
               allowed_updates=["message", "callback_query"])
    return res if isinstance(res, list) else []

# ─────────────────────────────────────────────
#  МЕНЮ / КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def kb_main() -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 Статус системы",      "callback_data": "sys_status"}],
        [{"text": "⚙️ Настройки",           "callback_data": "menu_settings"}],
        [{"text": "📋 Управление списками",  "callback_data": "menu_lists"}],
        [{"text": "📈 Статистика процессов", "callback_data": "menu_stats"}],
        [{"text": "❓ Помощь",               "callback_data": "menu_help"}],
        [{"text": "🔕 Отключить уведомления","callback_data": "do_stop"}],
    ]}

def kb_settings(cid: str) -> dict:
    s = get_settings(cid)
    mode_label = {"blacklist": "🚫 Чёрный список", "whitelist": "⭐ Белый список", "smart": "🧠 Умный"}
    qh = f" ({s['quiet_hours_start']}–{s['quiet_hours_end']})" if s["quiet_hours_enabled"] else ""
    return {"inline_keyboard": [
        [{"text": f"Режим: {mode_label[s['mode']]}", "callback_data": "toggle_mode"}],
        [{"text": ("✅" if s["group_notifications"] else "❌") + " Группировка уведомлений", "callback_data": "toggle_group"}],
        [{"text": ("✅" if s["ignore_system"]        else "❌") + " Игнорировать системные",  "callback_data": "toggle_system"}],
        [{"text": ("✅" if s["track_stats"]          else "❌") + " Сбор статистики",         "callback_data": "toggle_stats"}],
        [{"text": f"🔇 Тихие часы{qh}",             "callback_data": "menu_quiet"}],
        [{"text": f"⚙️ CPU порог: {s['min_cpu_percent']}%",   "callback_data": "set_cpu"}],
        [{"text": f"💾 RAM порог: {s['min_memory_mb']} MB",   "callback_data": "set_ram"}],
        [{"text": "🔙 Главное меню",                 "callback_data": "menu_main"}],
    ]}

def kb_quiet(cid: str) -> dict:
    s = get_settings(cid)
    return {"inline_keyboard": [
        [{"text": ("✅" if s["quiet_hours_enabled"] else "❌") + " Тихие часы вкл/выкл", "callback_data": "toggle_quiet"}],
        [{"text": "⏰ Изменить время (команда /quiet HH:MM-HH:MM)", "callback_data": "hint_quiet"}],
        [{"text": "🔙 Настройки", "callback_data": "menu_settings"}],
    ]}

def kb_lists() -> dict:
    return {"inline_keyboard": [
        [{"text": "🚫 Игнорируемые процессы", "callback_data": "list_ignored_0"}],
        [{"text": "⭐ Белый список",           "callback_data": "list_whitelist_0"}],
        [{"text": "🔙 Главное меню",           "callback_data": "menu_main"}],
    ]}

def kb_list_page(list_type: str, page: int, total_pages: int,
                 items: List[str]) -> dict:
    PER = 8
    start = page * PER
    rows = []
    for item in items[start:start + PER]:
        cb = f"rm_{list_type}_{item}"
        rows.append([{"text": f"🗑 {item}", "callback_data": cb[:64]}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️ Назад", "callback_data": f"list_{list_type}_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "Вперёд ▶️", "callback_data": f"list_{list_type}_{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": f"🗑 Очистить всё", "callback_data": f"clear_{list_type}"}])
    rows.append([{"text": "🔙 Списки",        "callback_data": "menu_lists"}])
    return {"inline_keyboard": rows}

def kb_stats_menu() -> dict:
    top = sorted(process_stats.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    rows = [[{"text": f"📊 {n} ({len(v)} событий)", "callback_data": f"pstat_{n[:40]}"}]
            for n, v in top]
    rows += [
        [{"text": "📈 Общая сводка",     "callback_data": "stats_total"}],
        [{"text": "🗑 Очистить статистику","callback_data": "stats_clear"}],
        [{"text": "🔙 Главное меню",      "callback_data": "menu_main"}],
    ]
    return {"inline_keyboard": rows}

def kb_help() -> dict:
    return {"inline_keyboard": [
        [{"text": "📖 Команды",           "callback_data": "help_cmds"}],
        [{"text": "⚙️ Фильтры и режимы", "callback_data": "help_filters"}],
        [{"text": "📊 Статистика",        "callback_data": "help_stats"}],
        [{"text": "🔧 Списки",            "callback_data": "help_lists"}],
        [{"text": "🔙 Главное меню",      "callback_data": "menu_main"}],
    ]}

def kb_process(name: str) -> dict:
    safe = name[:40]
    return {"inline_keyboard": [
        [{"text": "🚫 Игнорировать",    "callback_data": f"add_ignored_{safe}"},
         {"text": "⭐ В белый список", "callback_data": f"add_whitelist_{safe}"}],
        [{"text": "📊 Статистика",      "callback_data": f"pstat_{safe}"}],
        [{"text": "🏠 Главное меню",    "callback_data": "menu_main"}],
    ]}

def kb_status() -> dict:
    return {"inline_keyboard": [[
        {"text": "🔄 Обновить", "callback_data": "sys_status"},
        {"text": "🏠 Меню",     "callback_data": "menu_main"},
    ]]}

# ─────────────────────────────────────────────
#  ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────
def fmt_process(info: Dict) -> str:
    return (
        f"🔔 <b>Новый процесс</b>\n"
        f"📋 <b>Название:</b> <code>{info['name']}</code>\n"
        f"🆔 <b>PID:</b> {info['pid']}\n"
        f"👤 <b>Пользователь:</b> {info['username']}\n"
        f"📅 <b>Время:</b> {info['create_time']}\n"
        f"⚙️ <b>CPU:</b> {info['cpu']:.1f}%\n"
        f"💾 <b>RAM:</b> {info['memory_mb']} MB\n"
        f"📂 <b>Файл:</b> <code>{info['exe'][:200]}</code>\n"
        f"🖥 <b>Команда:</b> <code>{info['cmdline'][:300]}</code>"
    )

def fmt_grouped(procs: List[Dict]) -> str:
    lines = [f"🔔 <b>Новых процессов: {len(procs)}</b>\n"]
    for p in procs[:15]:
        lines.append(
            f"• <b>{p['name']}</b> (PID {p['pid']}) "
            f"CPU {p['cpu']:.1f}% RAM {p['memory_mb']}MB "
            f"👤{p['username']}"
        )
    if len(procs) > 15:
        lines.append(f"\n<i>…и ещё {len(procs)-15}</i>")
    return "\n".join(lines)

def fmt_system_status() -> str:
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu  = psutil.cpu_percent(interval=0.5)
    disk = psutil.disk_usage("/")

    # Открытые порты
    ports = []
    try:
        seen = set()
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr:
                key = (conn.laddr.port, "TCP")
                if key not in seen:
                    seen.add(key)
                    pname = "?"
                    try:
                        if conn.pid:
                            pname = psutil.Process(conn.pid).name()
                    except Exception:
                        pass
                    ports.append(f"  TCP:{conn.laddr.port} → {pname}")
    except Exception:
        pass
    ports_str = "\n".join(sorted(ports[:25])) or "  нет"

    return (
        f"📊 <b>Статус системы</b>  <i>{datetime.now().strftime('%H:%M:%S')}</i>\n\n"
        f"🖥 <b>CPU:</b> {cpu:.1f}%\n\n"
        f"💾 <b>RAM:</b> {mem.used/1024**3:.1f} / {mem.total/1024**3:.1f} GB ({mem.percent}%)\n"
        f"🔄 <b>Swap:</b> {swap.used/1024**3:.1f} / {swap.total/1024**3:.1f} GB ({swap.percent}%)\n\n"
        f"💿 <b>Диск /:</b> {disk.used/1024**3:.1f} / {disk.total/1024**3:.1f} GB ({disk.percent}%)\n\n"
        f"🔌 <b>Открытые порты (TCP LISTEN):</b>\n{ports_str}"
    )

def fmt_stats_total() -> str:
    total = sum(len(v) for v in process_stats.values())
    unique = len(process_stats)
    top = sorted(process_stats.items(), key=lambda x: len(x[1]), reverse=True)[:10]
    lines = [f"📈 <b>Общая статистика</b>\n",
             f"Всего событий: <b>{total}</b>",
             f"Уникальных процессов: <b>{unique}</b>\n",
             "<b>Топ-10:</b>"]
    for i, (name, stats) in enumerate(top, 1):
        lines.append(f"{i}. <code>{name}</code> — {len(stats)}")
    return "\n".join(lines)

def fmt_proc_stats(name: str) -> str:
    stats = process_stats.get(name, [])
    if not stats:
        return f"📊 Нет статистики для <code>{name}</code>"
    last = stats[-1]
    lines = [
        f"📊 <b>Статистика: {name}</b>\n",
        f"Всего событий: <b>{len(stats)}</b>",
        f"Первый раз: {stats[0]['ts'][:16]}",
        f"Последний раз: {last['ts'][:16]}\n",
        f"Последнее: CPU {last['cpu']:.1f}%  RAM {last['mem']}MB  PID {last['pid']}",
    ]
    if len(stats) >= 2:
        cpus = [s["cpu"] for s in stats[-20:]]
        lines.append(f"Среднее CPU (посл.20): {sum(cpus)/len(cpus):.1f}%")
    return "\n".join(lines)

# ─────────────────────────────────────────────
#  ЛОГИКА ПРОЦЕССОВ
# ─────────────────────────────────────────────
def get_proc_info(proc: psutil.Process) -> Optional[Dict]:
    try:
        with proc.oneshot():
            return {
                "pid":        proc.pid,
                "name":       proc.name(),
                "exe":        proc.exe() or "N/A",
                "cmdline":    " ".join(proc.cmdline()) if proc.cmdline() else "N/A",
                "username":   proc.username(),
                "create_time":datetime.fromtimestamp(proc.create_time()).strftime("%Y-%m-%d %H:%M:%S"),
                "status":     proc.status(),
                "cpu":        proc.cpu_percent(interval=0.1),
                "memory_mb":  round(proc.memory_info().rss / 1024**2, 1),
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None

def should_notify(info: Dict, cid: str) -> bool:
    s = get_settings(cid)
    if info["cpu"] < s["min_cpu_percent"]:
        return False
    if info["memory_mb"] < s["min_memory_mb"]:
        return False
    mode = s["mode"]
    name = info["name"]
    in_wl = name in whitelist_procs
    in_bl = name in ignored_procs
    in_sys= name in DEFAULT_SYSTEM and s["ignore_system"]
    if mode == "whitelist":
        return in_wl
    elif mode == "blacklist":
        return not in_bl and not in_sys
    elif mode == "smart":
        if in_wl: return True
        return not in_bl and not in_sys
    return True

def is_quiet(cid: str) -> bool:
    s = get_settings(cid)
    if not s["quiet_hours_enabled"]:
        return False
    now   = datetime.now().time()
    start = datetime.strptime(s["quiet_hours_start"], "%H:%M").time()
    end   = datetime.strptime(s["quiet_hours_end"],   "%H:%M").time()
    return (now >= start or now <= end) if start > end else (start <= now <= end)

def record_stat(info: Dict) -> None:
    name = info["name"]
    process_stats[name].append({
        "ts":  info["create_time"],
        "pid": info["pid"],
        "cpu": info["cpu"],
        "mem": info["memory_mb"],
        "usr": info["username"],
    })
    if len(process_stats[name]) > 2000:
        process_stats[name] = process_stats[name][-2000:]

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД
# ─────────────────────────────────────────────
def cmd_start(cid: str, username: str) -> None:
    if cid not in active_users:
        active_users.add(cid)
        user_settings[cid] = DEFAULT_SETTINGS.copy()
        save_all()
    log.info("User %s (%s) started", username, cid)
    send_message(cid,
        "✅ <b>Process Monitor Pro</b> — активирован!\n\n"
        "🔔 Уведомления о новых процессах включены.\n"
        "Используй кнопки меню для управления.",
        markup=kb_main())

def cmd_stop(cid: str) -> None:
    active_users.discard(cid)
    save_all()
    send_message(cid, "👋 Уведомления отключены.\nНапиши /start чтобы включить снова.")

def cmd_status(cid: str) -> None:
    send_message(cid, fmt_system_status(), markup=kb_status())

def cmd_help(cid: str) -> None:
    send_message(cid, "❓ <b>Помощь</b>\n\nВыберите раздел:", markup=kb_help())

def cmd_settings(cid: str) -> None:
    send_message(cid, "⚙️ <b>Настройки мониторинга</b>", markup=kb_settings(cid))

def cmd_list(cid: str, list_type: str = "ignored") -> None:
    items = sorted(ignored_procs if list_type == "ignored" else whitelist_procs)
    PER   = 8
    total = max(1, (len(items) + PER - 1) // PER)
    title = "🚫 Игнорируемые" if list_type == "ignored" else "⭐ Белый список"
    send_message(cid,
        f"<b>{title}</b>  (стр. 1/{total}, всего {len(items)})",
        markup=kb_list_page(list_type, 0, total, items))

def cmd_quiet(cid: str, arg: str) -> None:
    if arg == "off":
        get_settings(cid)["quiet_hours_enabled"] = False
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, "🔊 Тихие часы отключены.", markup=kb_settings(cid))
        return
    try:
        start, end = arg.split("-")
        datetime.strptime(start, "%H:%M")
        datetime.strptime(end,   "%H:%M")
        s = get_settings(cid)
        s["quiet_hours_start"]   = start
        s["quiet_hours_end"]     = end
        s["quiet_hours_enabled"] = True
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, f"🔇 Тихие часы установлены: {start}–{end}", markup=kb_settings(cid))
    except Exception:
        send_message(cid, "❌ Формат: <code>/quiet 22:00-08:00</code> или <code>/quiet off</code>")

def cmd_setcpu(cid: str, val: str) -> None:
    try:
        get_settings(cid)["min_cpu_percent"] = float(val)
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, f"✅ CPU порог: {val}%", markup=kb_settings(cid))
    except Exception:
        send_message(cid, "❌ Пример: <code>/setcpu 5</code>")

def cmd_setram(cid: str, val: str) -> None:
    try:
        get_settings(cid)["min_memory_mb"] = float(val)
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, f"✅ RAM порог: {val} MB", markup=kb_settings(cid))
    except Exception:
        send_message(cid, "❌ Пример: <code>/setram 100</code>")

def cmd_history(cid: str, proc_name: str) -> None:
    stats = process_stats.get(proc_name, [])
    if not stats:
        send_message(cid, f"📊 Нет истории для <code>{proc_name}</code>")
        return
    last20 = stats[-20:]
    lines  = [f"📊 <b>История {proc_name}</b>  ({len(stats)} событий)\n"]
    for s in last20:
        lines.append(f"• {s['ts'][:16]}  CPU {s['cpu']:.1f}%  RAM {s['mem']}MB")
    send_message(cid, "\n".join(lines))

def handle_command(msg: dict) -> None:
    text = msg.get("text", "").strip()
    cid  = str(msg["chat"]["id"])
    uname= msg.get("from", {}).get("username", "unknown")

    parts = text.split(maxsplit=1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""

    log.info("CMD '%s' arg='%s' from @%s (%s)", cmd, arg, uname, cid)

    if cmd == "/start":
        cmd_start(cid, uname)
    elif cmd == "/stop":
        cmd_stop(cid)
    elif cmd in ("/status", "/stat"):
        cmd_status(cid)
    elif cmd == "/help":
        cmd_help(cid)
    elif cmd in ("/settings", "/config"):
        cmd_settings(cid)
    elif cmd == "/list":
        cmd_list(cid, "ignored")
    elif cmd == "/whitelist":
        cmd_list(cid, "whitelist")
    elif cmd == "/quiet":
        if arg:
            cmd_quiet(cid, arg)
        else:
            send_message(cid, "Пример: <code>/quiet 22:00-08:00</code> или <code>/quiet off</code>")
    elif cmd == "/setcpu":
        cmd_setcpu(cid, arg)
    elif cmd == "/setram":
        cmd_setram(cid, arg)
    elif cmd == "/history":
        if arg:
            cmd_history(cid, arg)
        else:
            send_message(cid, "Пример: <code>/history python3</code>")
    elif cid not in active_users:
        send_message(cid, "⚠️ Напиши /start для активации бота.")
    else:
        send_message(cid, "❓ Неизвестная команда.\n\nДоступные команды:\n"
            "/start /stop /status /help /settings /list /whitelist\n"
            "/quiet /setcpu /setram /history",
            markup=kb_main())

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ CALLBACK
# ─────────────────────────────────────────────
def handle_callback(cq: dict) -> None:
    cd      = cq.get("data", "")
    chat_id = str(cq["message"]["chat"]["id"])
    mid     = cq["message"]["message_id"]
    cb_id   = cq["id"]

    log.info("CB '%s' from %s", cd, chat_id)

    # ─── гарантируем ответ на callback ───
    # (будет вызван в конце или при ошибке)
    answer_text = ""

    try:
        _dispatch_callback(cd, chat_id, mid)
    except Exception as e:
        log.error("Callback dispatch error: %s", e)
        answer_text = "⚠️ Ошибка обработки"
    finally:
        answer_callback(cb_id, answer_text)

def _dispatch_callback(cd: str, cid: str, mid: int) -> None:
    """Маршрутизация callback без дублирования answerCallbackQuery."""

    # ─── навигация по меню ───
    if cd == "menu_main":
        send_message(cid, "🏠 <b>Главное меню</b>", markup=kb_main(), edit_id=mid)

    elif cd == "menu_settings":
        send_message(cid, "⚙️ <b>Настройки мониторинга</b>",
                     markup=kb_settings(cid), edit_id=mid)

    elif cd == "menu_quiet":
        s = get_settings(cid)
        qh = f"{s['quiet_hours_start']}–{s['quiet_hours_end']}" if s["quiet_hours_enabled"] else "выкл"
        send_message(cid, f"🔇 <b>Тихие часы</b>  ({qh})",
                     markup=kb_quiet(cid), edit_id=mid)

    elif cd == "menu_lists":
        send_message(cid, "📋 <b>Управление списками</b>",
                     markup=kb_lists(), edit_id=mid)

    elif cd == "menu_stats":
        send_message(cid, "📈 <b>Статистика процессов</b>",
                     markup=kb_stats_menu(), edit_id=mid)

    elif cd == "menu_help":
        send_message(cid, "❓ <b>Помощь и документация</b>\n\nВыберите раздел:",
                     markup=kb_help(), edit_id=mid)

    # ─── системный статус ───
    elif cd == "sys_status":
        send_message(cid, fmt_system_status(), markup=kb_status(), edit_id=mid)

    # ─── переключатели настроек ───
    elif cd == "toggle_mode":
        s = get_settings(cid)
        modes = ["blacklist", "whitelist", "smart"]
        s["mode"] = modes[(modes.index(s["mode"]) + 1) % 3]
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, f"✅ Режим изменён: <b>{s['mode']}</b>",
                     markup=kb_settings(cid), edit_id=mid)

    elif cd == "toggle_group":
        s = get_settings(cid)
        s["group_notifications"] = not s["group_notifications"]
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, "✅ Группировка уведомлений изменена",
                     markup=kb_settings(cid), edit_id=mid)

    elif cd == "toggle_system":
        s = get_settings(cid)
        s["ignore_system"] = not s["ignore_system"]
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, "✅ Фильтр системных процессов изменён",
                     markup=kb_settings(cid), edit_id=mid)

    elif cd == "toggle_stats":
        s = get_settings(cid)
        s["track_stats"] = not s["track_stats"]
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, "✅ Сбор статистики изменён",
                     markup=kb_settings(cid), edit_id=mid)

    elif cd == "toggle_quiet":
        s = get_settings(cid)
        s["quiet_hours_enabled"] = not s["quiet_hours_enabled"]
        _save(SETTINGS_FILE, user_settings)
        send_message(cid, "✅ Тихие часы изменены",
                     markup=kb_quiet(cid), edit_id=mid)

    elif cd in ("set_cpu", "hint_quiet", "set_ram"):
        hints = {
            "set_cpu":    "Введите CPU порог командой:\n<code>/setcpu 5</code>",
            "set_ram":    "Введите RAM порог командой:\n<code>/setram 100</code>",
            "hint_quiet": "Установите тихие часы командой:\n<code>/quiet 22:00-08:00</code>\nили отключите: <code>/quiet off</code>",
        }
        send_message(cid, hints[cd], edit_id=mid)

    # ─── отключение уведомлений ───
    elif cd == "do_stop":
        active_users.discard(cid)
        save_all()
        send_message(cid, "🔕 Уведомления отключены.\n/start чтобы включить.", edit_id=mid)

    # ─── просмотр списков с пагинацией ───
    elif cd.startswith("list_"):
        # формат: list_{type}_{page}
        parts = cd.split("_", 2)
        if len(parts) == 3:
            ltype, page = parts[1], int(parts[2])
            items = sorted(ignored_procs if ltype == "ignored" else whitelist_procs)
            PER   = 8
            total = max(1, (len(items) + PER - 1) // PER)
            title = "🚫 Игнорируемые" if ltype == "ignored" else "⭐ Белый список"
            send_message(cid,
                f"<b>{title}</b>  (стр. {page+1}/{total}, всего {len(items)})",
                markup=kb_list_page(ltype, page, total, items), edit_id=mid)

    # ─── удаление из списка ───
    elif cd.startswith("rm_"):
        # формат: rm_{type}_{name}
        parts = cd.split("_", 2)
        if len(parts) == 3:
            ltype, name = parts[1], parts[2]
            if ltype == "ignored":
                ignored_procs.discard(name)
                _save(IGNORED_FILE, list(ignored_procs))
                send_message(cid, f"🔔 <code>{name}</code> удалён из игнорируемых",
                             markup=kb_lists(), edit_id=mid)
            else:
                whitelist_procs.discard(name)
                _save(WHITELIST_FILE, list(whitelist_procs))
                send_message(cid, f"❌ <code>{name}</code> удалён из белого списка",
                             markup=kb_lists(), edit_id=mid)

    # ─── очистка списка ───
    elif cd.startswith("clear_"):
        ltype = cd.split("_", 1)[1]
        if ltype == "ignored":
            ignored_procs.clear()
            ignored_procs.update(DEFAULT_SYSTEM)
            _save(IGNORED_FILE, list(ignored_procs))
            send_message(cid, "✅ Игнорируемые очищены (восстановлены системные)",
                         markup=kb_lists(), edit_id=mid)
        elif ltype == "whitelist":
            whitelist_procs.clear()
            _save(WHITELIST_FILE, list(whitelist_procs))
            send_message(cid, "✅ Белый список очищен", markup=kb_lists(), edit_id=mid)

    # ─── добавить в игнорируемые ───
    elif cd.startswith("add_ignored_"):
        name = cd[len("add_ignored_"):]
        ignored_procs.add(name)
        _save(IGNORED_FILE, list(ignored_procs))
        send_message(cid, f"🚫 <code>{name}</code> добавлен в игнорируемые", edit_id=mid)

    # ─── добавить в белый список ───
    elif cd.startswith("add_whitelist_"):
        name = cd[len("add_whitelist_"):]
        whitelist_procs.add(name)
        _save(WHITELIST_FILE, list(whitelist_procs))
        send_message(cid, f"⭐ <code>{name}</code> добавлен в белый список", edit_id=mid)

    # ─── статистика процесса ───
    elif cd.startswith("pstat_"):
        name = cd[len("pstat_"):]
        send_message(cid, fmt_proc_stats(name), edit_id=mid)

    elif cd == "stats_total":
        send_message(cid, fmt_stats_total(), markup=kb_stats_menu(), edit_id=mid)

    elif cd == "stats_clear":
        process_stats.clear()
        _save(STATS_FILE, {})
        send_message(cid, "✅ Статистика очищена", markup=kb_stats_menu(), edit_id=mid)

    # ─── разделы помощи ───
    elif cd == "help_cmds":
        send_message(cid,
            "<b>📖 Основные команды</b>\n\n"
            "/start — активировать бот\n"
            "/stop — отключить уведомления\n"
            "/status — RAM, CPU, диск, порты\n"
            "/settings — настройки фильтров\n"
            "/list — игнорируемые процессы\n"
            "/whitelist — белый список\n"
            "/history &lt;имя&gt; — история процесса\n"
            "/quiet 22:00-08:00 — тихие часы\n"
            "/setcpu 5 — CPU порог (%)\n"
            "/setram 100 — RAM порог (MB)",
            markup=kb_help(), edit_id=mid)

    elif cd == "help_filters":
        send_message(cid,
            "<b>⚙️ Режимы работы</b>\n\n"
            "🚫 <b>Чёрный список</b> — уведомлять обо всём кроме игнорируемых\n"
            "⭐ <b>Белый список</b> — только явно разрешённые процессы\n"
            "🧠 <b>Умный</b> — белый список имеет приоритет, остальные фильтруются чёрным\n\n"
            "<b>Пороги CPU/RAM</b> — игнорировать процессы ниже порога\n"
            "<b>Тихие часы</b> — нет уведомлений в указанное время",
            markup=kb_help(), edit_id=mid)

    elif cd == "help_stats":
        send_message(cid,
            "<b>📊 Статистика и мониторинг</b>\n\n"
            "• /status — CPU, RAM, диск, открытые порты\n"
            "• /history &lt;имя&gt; — история запусков процесса\n"
            "• Меню Статистика — топ процессов\n\n"
            "История хранится до 2000 событий на процесс.\n"
            "Данные сохраняются в <code>stats.json</code>",
            markup=kb_help(), edit_id=mid)

    elif cd == "help_lists":
        send_message(cid,
            "<b>🔧 Управление списками</b>\n\n"
            "При получении уведомления о процессе нажми:\n"
            "• 🚫 Игнорировать — добавить в чёрный список\n"
            "• ⭐ В белый список — добавить в белый список\n\n"
            "Просмотр и удаление через /list и /whitelist.\n"
            "Удали нажав кнопку 🗑 рядом с именем процесса.",
            markup=kb_help(), edit_id=mid)

    else:
        log.warning("Unknown callback: %s", cd)
        send_message(cid, "⚠️ Неизвестное действие.", markup=kb_main(), edit_id=mid)

# ─────────────────────────────────────────────
#  ПОТОКИ
# ─────────────────────────────────────────────
def bot_listener() -> None:
    """Polling Telegram updates."""
    global last_update_id
    log.info("🤖 Bot listener started")
    while not stop_event.is_set():
        try:
            updates = get_updates(last_update_id + 1, timeout=25)
            for upd in updates:
                last_update_id = upd["update_id"]
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd and "text" in upd["message"]:
                    handle_command(upd["message"])
        except Exception as e:
            log.error("Bot listener error: %s", e)
            time.sleep(3)
        else:
            time.sleep(0.3)


def notification_flusher() -> None:
    """Отправка сгруппированных уведомлений."""
    log.info("📤 Notification flusher started")
    while not stop_event.is_set():
        time.sleep(5)
        try:
            with _lock:
                for cid in list(pending.keys()):
                    procs = pending[cid]
                    if not procs:
                        continue
                    if is_quiet(cid):
                        continue
                    s = get_settings(cid)
                    if s["group_notifications"]:
                        # ждём group_interval секунд с момента первого процесса
                        try:
                            first_time = datetime.strptime(procs[0]["create_time"], "%Y-%m-%d %H:%M:%S")
                            if (datetime.now() - first_time).seconds < s["group_interval"]:
                                continue
                        except Exception:
                            pass
                    if len(procs) == 1:
                        send_message(cid, fmt_process(procs[0]),
                                     markup=kb_process(procs[0]["name"]))
                    else:
                        send_message(cid, fmt_grouped(procs))
                    pending[cid].clear()
        except Exception as e:
            log.error("Flusher error: %s", e)


def process_monitor() -> None:
    """Основной цикл мониторинга новых процессов."""
    global known_pids
    log.info("🔍 Process monitor started, known pids: %d", len(known_pids))
    save_counter = 0
    while not stop_event.is_set():
        try:
            current = set()
            new_procs: List[psutil.Process] = []
            for proc in psutil.process_iter():
                current.add(proc.pid)
                if proc.pid not in known_pids:
                    new_procs.append(proc)
            known_pids = current

            for proc in new_procs:
                info = get_proc_info(proc)
                if not info:
                    continue
                for cid in list(active_users):
                    if not should_notify(info, cid):
                        continue
                    s = get_settings(cid)
                    if s["track_stats"]:
                        record_stat(info)
                    if s["group_notifications"]:
                        with _lock:
                            pending[cid].append(info)
                    else:
                        if not is_quiet(cid):
                            send_message(cid, fmt_process(info),
                                         markup=kb_process(info["name"]))

            save_counter += 1
            if save_counter >= 60:   # сохраняем раз в ~5 минут
                save_counter = 0
                _save(STATS_FILE, dict(process_stats))

        except Exception as e:
            log.error("Monitor error: %s", e)

        time.sleep(CHECK_INTERVAL)

# ─────────────────────────────────────────────
#  ТОЧКА ВХОДА
# ─────────────────────────────────────────────
def main() -> None:
    log.info("=" * 55)
    log.info("🚀  Process Monitor Pro  v2.0")
    log.info("=" * 55)

    load_all()
    log.info("Пользователей: %d  Игнорируемых: %d  Белый список: %d",
             len(active_users), len(ignored_procs), len(whitelist_procs))

    # инициализация известных процессов
    global known_pids
    known_pids = {p.pid for p in psutil.process_iter()}
    log.info("Процессов при старте: %d", len(known_pids))

    threads = [
        Thread(target=bot_listener,       name="BotListener",   daemon=True),
        Thread(target=notification_flusher,name="Flusher",       daemon=True),
        Thread(target=process_monitor,    name="ProcessMonitor", daemon=False),
    ]
    for t in threads:
        t.start()
        log.info("Thread started: %s", t.name)

    try:
        # основной поток — process_monitor, ждём его
        threads[-1].join()
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C...")
        stop_event.set()
    finally:
        save_all()
        log.info("✅ Данные сохранены. Выход.")


if __name__ == "__main__":
    main()
