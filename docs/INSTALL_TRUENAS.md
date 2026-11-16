# ProntoF — отдельный тестовый контейнер TrueNAS

## Важно

ProntoF — самостоятельное приложение. Не используйте каталоги данных, тома, БД, контейнеры или секреты Home Master / ProntoFix. Для теста создайте отдельный каталог и отдельный контейнер.

## 1. Каталоги

Пример для пула ProntoFix:

```bash
sudo mkdir -p /mnt/ProntoFix/apps/prontof/data
sudo mkdir -p /mnt/ProntoFix/apps/prontof/app
sudo chown -R admin:admin /mnt/ProntoFix/apps/prontof
```

В каталог app поместите содержимое репозитория ProntoF.

## 2. Настройка

Из каталога приложения:

```bash
cd /mnt/ProntoFix/apps/prontof/app
cp .env.example .env
```

Сгенерируйте секрет и задайте пароль владельца:

```bash
SECRET=$(openssl rand -hex 48)
sed -i "s/REPLACE_WITH_LONG_RANDOM_SECRET/$SECRET/" .env
```

Откройте .env и замените OWNER_PASSWORD на собственный пароль. Не публикуйте .env и не отправляйте его в GitHub.

## 3. Сборка и запуск

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

Первый запуск может занять время из-за установки Python-зависимостей. Модель Faster-Whisper будет загружена при первой обработке видео.

## 4. Проверка

Откройте:

- `http://IP-АДРЕС-NAS:30810/`
- `http://IP-АДРЕС-NAS:30810/docs`
- `http://IP-АДРЕС-NAS:30810/api/health`

Порт 30810 выбран как отдельный тестовый порт. Если он занят, измените PRONTOF_PORT в .env.

## 5. Данные и ограничения

- Все данные приложения находятся только в отдельном каталоге data.
- Удаление контейнера не должно удалять каталог data.
- Обработка ограничена одним CPU worker, чтобы не перегружать сервер.
- MAX_UPLOAD_GB задаёт программный предел одного файла, но фактический предел определяется свободным местом, файловой системой и прокси.
- Для видео до 500 GB необходимо иметь дополнительное место для оригинала, временного аудио и результатов. Текущий свободный объём пула следует проверить до таких тестов.
- На CPU распознавание длинных видео может занимать много времени.
- На первом этапе перевод работает только для установленных языковых пар Argos Translate; наличие языка в интерфейсе не означает, что пакет перевода уже установлен.

## 6. Полезные команды

```bash
docker compose ps
docker compose logs -f --tail=200
docker compose restart
docker compose down
```

Не используйте `docker compose down -v`, если требуется сохранить данные.
