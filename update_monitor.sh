#!/bin/bash
# Скрипт обновления Process Monitor
# Скачивает новую версию бота, сохраняет токен, перезапускает сервис
# Использование: bash update_monitor.sh

set -e

BOT_FILE="/root/monitor.py"
SERVICE="process-monitor"
GITHUB_URL="https://raw.githubusercontent.com/dictovod/process-monitor/main/monitor_server.py"

echo "=== Process Monitor Updater ==="

# 1. Читаем текущий токен из работающего файла
if [ -f "$BOT_FILE" ]; then
    TOKEN=$(grep -oP 'TELEGRAM_TOKEN\s*=\s*"\K[^"]+' "$BOT_FILE" || true)
    if [ -z "$TOKEN" ]; then
        echo "❌ Не удалось найти токен в $BOT_FILE"
        exit 1
    fi
    echo "✅ Токен найден: ${TOKEN:0:10}..."
else
    echo "❌ Файл $BOT_FILE не найден"
    exit 1
fi

# 2. Скачиваем новую версию
echo "⬇️  Скачиваем новую версию..."
wget -q -O "${BOT_FILE}.new" "$GITHUB_URL"
echo "✅ Скачано"

# 3. Вставляем токен в новый файл
sed -i "s/TELEGRAM_TOKEN = \".*\"/TELEGRAM_TOKEN = \"${TOKEN}\"/" "${BOT_FILE}.new"
echo "✅ Токен вставлен"

# 4. Проверяем синтаксис нового файла
python3 -c "import ast; ast.parse(open('${BOT_FILE}.new').read())" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "❌ Синтаксическая ошибка в новом файле — откатываем"
    rm -f "${BOT_FILE}.new"
    exit 1
fi
echo "✅ Синтаксис корректен"

# 5. Заменяем файл
cp "${BOT_FILE}" "${BOT_FILE}.bak"
mv "${BOT_FILE}.new" "$BOT_FILE"
echo "✅ Файл заменён (резервная копия: ${BOT_FILE}.bak)"

# 6. Перезапускаем сервис
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 2

# 7. Проверяем статус
if systemctl is-active --quiet "$SERVICE"; then
    echo "✅ Сервис $SERVICE запущен успешно"
else
    echo "❌ Сервис не запустился — восстанавливаем резервную копию"
    cp "${BOT_FILE}.bak" "$BOT_FILE"
    systemctl restart "$SERVICE"
    echo "⚠️  Восстановлена старая версия"
    exit 1
fi

echo ""
echo "=== Обновление завершено ==="
echo "Версия: $(grep -oP 'Версия\s+\K[\d.]+' $BOT_FILE || echo 'неизвестна')"
