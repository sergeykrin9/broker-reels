# Deploy: Broker Reels Studio на сервер 213.171.15.45

> Production deployment HTTP-сервиса генерации рилсов.  
> Сервис слушает на `:5050`, вызывается Cloud Function-ом `generateBrokerReel` через shared secret.

## One-time setup (~10 минут)

### 1. SSH на сервер
```bash
ssh root@213.171.15.45
```

### 2. Установить системные зависимости
```bash
apt update
apt install -y python3.10 python3.10-venv ffmpeg fonts-dejavu-core git
```

### 3. Деплой кода
```bash
cd /opt
git clone https://github.com/<your-repo>.git broker-reels-src
ln -s broker-reels-src/broker_reels /opt/broker-reels
cd /opt/broker-reels
python3.10 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 4. Firebase service account key
В Firebase Console → Project Settings → Service Accounts → Generate new private key:
```bash
# Скопировать JSON на сервер:
scp ~/Downloads/axonleads-app-firebase-adminsdk-*.json \
  root@213.171.15.45:/opt/broker-reels/firebase_sa.json
chmod 600 /opt/broker-reels/firebase_sa.json
```

### 5. Сгенерировать shared secret
```bash
# На локальной машине:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Скопировать вывод — это BROKER_REELS_TOKEN.
```

### 6. Создать `.env` на сервере
```bash
cat > /opt/broker-reels/.env <<'EOF'
BROKER_REELS_TOKEN=<paste from step 5>
BROKER_REELS_SA_KEY=/opt/broker-reels/firebase_sa.json
FIREBASE_STORAGE_BUCKET=axonleads-app.firebasestorage.app
NVIDIA_API_KEY=<copy from daily-poster/.env>
PORT=5050
EOF
chmod 600 /opt/broker-reels/.env
```

### 7. Запустить как systemd сервис
```bash
cp /opt/broker-reels/deploy/broker-reels.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable broker-reels
systemctl start broker-reels
systemctl status broker-reels      # должно быть active (running)
journalctl -u broker-reels -f      # лог
```

### 8. Проверить health
```bash
curl http://localhost:5050/health
# → {"ok":true,"service":"broker-reels","ts":...}
```

### 9. Открыть наружу (через nginx + Let's Encrypt)

Если уже есть nginx — добавить server-block для `broker-reels.axonleads.ru`:

```nginx
server {
    listen 443 ssl;
    server_name broker-reels.axonleads.ru;

    ssl_certificate /etc/letsencrypt/live/broker-reels.axonleads.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/broker-reels.axonleads.ru/privkey.pem;

    # Внутри хорошо бы whitelist IPs Cloud Functions, но они меняются.
    # Защита через X-Broker-Reels-Token header + rate limit (уже в Flask).

    client_max_body_size 20k;  # endpoint только JSON, никакого binary upload

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 180s;    # генерация ~30-60s, запас
        proxy_send_timeout 180s;
    }
}
```

Получить SSL:
```bash
certbot --nginx -d broker-reels.axonleads.ru
```

### 10. Установить secrets в Firebase Functions

На локальной машине (или Cloud Shell):
```bash
firebase functions:secrets:set BROKER_REELS_TOKEN   # paste from step 5
firebase functions:secrets:set BROKER_REELS_SERVER  # https://broker-reels.axonleads.ru
```

### 11. Задеплоить Cloud Function
```bash
cd signalleads-landing
firebase deploy --only functions:generateBrokerReel
```

## Smoke test после деплоя

```bash
# 1. С Cloud Function на сервер:
curl -X POST https://broker-reels.axonleads.ru/health
# → {"ok":true}

# 2. С CF (через консоль Firestore — получить ID любого юнита):
# Из broker-кабинета: открыть юнит → клик "Сделать рилс" → ждать ~60s.
```

## Логи и debug

```bash
# Лог сервиса
journalctl -u broker-reels -f --since "10 minutes ago"

# Сохранить tmp для debug (не чистится автоматически)
echo "KEEP_TMP=1" >> /opt/broker-reels/.env
systemctl restart broker-reels

# Очистка старых работ
find /tmp/broker_reels -type d -mmin +60 -exec rm -rf {} +
```

## Откат / disable

```bash
systemctl stop broker-reels
systemctl disable broker-reels

# В Firebase: убрать exports.generateBrokerReel в index.js → redeploy
```
