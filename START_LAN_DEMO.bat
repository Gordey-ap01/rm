@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
title РМ-управление LAN

for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254*' -and $_.AddressState -eq 'Preferred' } | Select-Object -First 1 -ExpandProperty IPAddress)"`) do set "LAN_IP=%%i"

if "%LAN_IP%"=="" (
  echo Не удалось определить IP компьютера в локальной сети.
  echo Проверьте подключение к Wi-Fi/локальной сети.
  pause
  exit /b 1
)

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker Desktop не найден.
  echo Для LAN-запуска через этот скрипт нужен Docker Desktop.
  pause
  exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
  echo Docker установлен, но сервис не запущен. Откройте Docker Desktop и повторите запуск.
  pause
  exit /b 1
)

set "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0,testserver,%LAN_IP%"
set "DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://%LAN_IP%:8000"

echo.
echo Запускаю РМ-управление для локальной сети...
echo Компьютер: http://127.0.0.1:8000/
echo Смартфон:  http://%LAN_IP%:8000/
echo.

docker compose up --build -d
if errorlevel 1 (
  echo Ошибка запуска Docker Compose.
  pause
  exit /b 1
)

docker compose exec -T web python manage.py migrate
if errorlevel 1 (
  echo Ошибка миграций.
  pause
  exit /b 1
)

docker compose exec -T web python manage.py seed_demo
if errorlevel 1 (
  echo Ошибка загрузки тестовых данных.
  pause
  exit /b 1
)

echo.
echo Готово.
echo Откройте на смартфоне: http://%LAN_IP%:8000/
echo Логин администратора: admin / admin12345
echo Логин специалиста: specialist1 / specialist123
echo.
start "" "http://127.0.0.1:8000/"
pause

endlocal
