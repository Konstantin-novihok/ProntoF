# Home Master + внутренний ProntoF worker на TrueNAS

В репозитории `Konstantin-novihok/ProntoF` находятся оба компонента. Пользователь открывает только Home Master. Внутренний worker ProntoF не публикует порт на NAS и доступен только внутри Docker-сети.

## Состав контейнеров

- `home-master-prontof-test` — интерфейс, авторизация, пользователи, заказы, общая библиотека файлов; порт по умолчанию 30802.
- `prontof-worker-test` — внутренний API и CPU-обработка видео; порт 8000 доступен только сервисам Docker.

## Каталоги данных

Пример для пула `ProntoFix`:

- Home Master: `/mnt/ProntoFix/apps/prontof/home-master-data`
- ProntoF worker: `/mnt/ProntoFix/apps/prontof/home-master-prontof-data`

Каталоги независимы. Не удаляйте их при обновлении контейнеров.

## Настройка

В каталоге проекта:

```bash
cp .env.example .env
```

Сгенерируйте три отдельных секрета:

```bash
openssl rand -hex 48
openssl rand -hex 48
openssl rand -hex 48
```

Вставьте их в `.env` в поля `SECRET_KEY`, `PRONTOF_SECRET_KEY` и `INTERNAL_API_KEY`. Задайте собственный `OWNER_PASSWORD`. `PRONTOF_OWNER_PASSWORD` — технический пароль внутреннего worker, пользователь его не использует.

Проверьте пути `HOME_MASTER_DATA` и `PRONTOF_DATA`. Не отправляйте `.env` в GitHub и не публикуйте внутренний ключ.

## Запуск и обновление

После резервной копии текущих compose-файлов и проверки существующих контейнеров:

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

Для обновления используйте свежие исходники ветки `main` и повторите `docker compose up -d --build`. Не используйте `docker compose down -v` и не удаляйте каталоги данных.

## Проверка

Home Master открывается по адресу:

```
http://IP-АДРЕС-NAS:30802/app/
```

Проверка внутреннего worker из контейнера Home Master:

```bash
sudo docker exec home-master-prontof-test python -c "import urllib.request; print(urllib.request.urlopen('http://prontof-worker:8000/api/health', timeout=10).read().decode())"
```

У worker нет опубликованного порта на NAS. Если требуется проверить API, делайте это из Docker-сети, а не через отдельную страницу ProntoF.

## Видео до 500 GB

500 GB — верхний программный предел одного файла. Это не обещание, что такой файл поместится на текущем пуле. До теста проверьте свободное место: требуется место для исходного файла и временных данных обработки. Загрузка идёт частями; весь файл не помещается в RAM. На CPU обработка больших видео может длиться долго.
