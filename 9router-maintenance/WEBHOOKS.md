# Webhook Notifications Setup

## Opzioni di Notifica

### 1. Webhook.site (Test Gratuito)
Perfetto per testare le notifiche senza configurazione

1. Vai su https://webhook.site
2. Copia l'URL unico (es: `https://webhook.site/123e4567-e89b-12d3-a456-426614174000`)
3. Incolla in `config.json`:
```json
"notifications": {
  "enabled": true,
  "webhookUrl": "https://webhook.site/123e4567-e89b-12d3-a456-426614174000"
}
```
4. Esegui lo script
5. I messaggi appariranno in tempo reale su webhook.site

---

### 2. Discord Webhook
Ricevi notifiche su un canale Discord

1. Nel server Discord, vai a Channel Settings → Integrations → Webhooks
2. Clicca "New Webhook" e copia l'URL
3. Incolla in `config.json`:
```json
"webhookUrl": "https://discord.com/api/webhooks/123456789/AbCdEfGhIjKlMnOpQrStUvWxYz"
```

**Nota**: Discord accetta payload diverso, quindi potresti vedere il messaggio in formato JSON grezzo. Se vuoi formattare meglio, crea una funzione middleware.

---

### 3. Slack Webhook
Ricevi notifiche su Slack

1. Crea una Slack App (https://api.slack.com/apps)
2. Abilita "Incoming Webhooks"
3. Crea nuovo webhook e copia l'URL
4. Incolla in `config.json`:
```json
"webhookUrl": "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX"
```

---

### 4. Azure Logic Apps
Automazione avanzata con notifiche condizionali

1. Crea Logic App in Azure Portal
2. Usa trigger HTTP POST
3. Configura azioni (email, Teams, etc.)
4. Copia URL trigger e incolla in config.json

---

### 5. Twilio SMS (Payload Personalizzato)
Per notifiche via SMS, creare proxy personalizzato

---

## Payload Webhook Inviato

Lo script invia questo JSON a ogni webhook configurato:

```json
{
  "subject": "✅ Maintenance Completed",
  "body": "Combo updated: 2 removed, 1 added, 0 replaced",
  "timestamp": "2026-08-16 14:30:45",
  "data": {
    "removed": 2,
    "added": 1,
    "replaced": 0
  }
}
```

Oppure in caso di errore:

```json
{
  "subject": "❌ Maintenance Failed",
  "body": "Error updating combo: Connection timeout",
  "timestamp": "2026-08-16 14:30:45",
  "data": {
    "error": "Connection timeout"
  }
}
```

---

## Soglia di Allarme

Configura quando ricevere avvisi:

```json
"notifications": {
  "alertOnFailureThreshold": 5
}
```

Con questo:
- Se 5+ modelli falliscono → ricevi **alert speciale** ⚠️
- Se < 5 modelli → ricevi solo **notifica di completamento** ✅

---

## Testing Notifiche

```powershell
# Test dry-run per vedere cosa accadrebbe senza eseguire
.\update-comboact.ps1 -DryRun

# Controlla logs per verificare invio notifiche
Get-Content "logs\comboact-update-2026-08-16.log" | Select-String "NOTIFICATION"
```

---

## Monitoraggio Log Strutturato

Le notifiche vengono registrate in `comboact-structured-*.jsonl`:

```json
{"timestamp":"2026-08-16 14:30:45","level":"NOTIFICATION","message":"Maintenance Completed","removed":2,"added":1,"replaced":0}
```

Importa in Elasticsearch/ELK per dashboards avanzate.

---

## Troubleshooting Notifiche

**Webhook non riceve messaggi?**
- Verifica che `enabled: true` in config.json
- Verifica URL webhook
- Controlla firewall/proxy
- Guarda log: `comboact-update-*.log`

**Troppe notifiche?**
- Aumenta `alertOnFailureThreshold`
- O disabilita: `"enabled": false`

**Vuoi solo allarmi di errore?**
- Crea proxy webhook che filtra per `"subject"` contenente "Failed"
