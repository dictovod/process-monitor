```markdown
# Process Monitor with Telegram

Небольшой Python‑скрипт, который отслеживает появление новых процессов в системе и отправляет уведомления в Telegram.  
Полезно для мониторинга серверов, обнаружения подозрительных процессов и базовой безопасности.

## 🚀 Возможности

- Отслеживание всех новых процессов в реальном времени
- Отправка уведомлений в Telegram Bot API
- Форматированные сообщения (HTML)
- Автоматический запуск через systemd
- Автоперезапуск при ошибках
- Логи через `journalctl`

## 📦 Требования

- Python 3.8+
- Linux (Debian/Ubuntu/…)
- Telegram Bot Token
- ID чата, куда отправлять уведомления

## 📁 Структура проекта

```
process-monitor/
├── monitor.py
└── requirements.txt
```

## 🔧 Установка

```bash
git clone https://github.com/USERNAME/process-monitor.git
cd process-monitor
pip3 install -r requirements.txt
```

## ⚙️ Настройка

Перед запуском укажите в `monitor.py`:

- `TELEGRAM_TOKEN` — токен вашего бота
- `CHAT_ID` — ID чата, куда отправлять уведомления

## ▶️ Ручной запуск

```bash
python3 monitor.py
```

## 🛠 Автозапуск через systemd

Создайте файл службы:

```bash
sudo nano /etc/systemd/system/process-monitor.service
```

Вставьте:

```ini
[Unit]
Description=Process Monitor with Telegram
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/Desktop/process-monitor
ExecStart=/usr/bin/python3 /root/Desktop/process-monitor/monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Активируйте службу:

```bash
sudo systemctl daemon-reload
sudo systemctl enable process-monitor.service
sudo systemctl start process-monitor.service
```

Проверка:

```bash
systemctl status process-monitor.service
```

## 📜 Логи

```bash
journalctl -u process-monitor.service -n 50
```

## 🧪 Проверка автозапуска

```bash
reboot
```

После загрузки:

```bash
systemctl status process-monitor.service
```

---
Скажи, что хочешь добавить — и я расширю README под твой стиль.
