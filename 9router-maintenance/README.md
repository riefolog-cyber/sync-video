# 9Router ComboAct Maintenance - Enhanced Edition

## 📋 Miglioramenti Implementati

### 1️⃣ **Validazione dell'Ambiente**
- ✅ Verifica che 9Router sia raggiungibile prima di iniziare
- ✅ Testa la connettività su `/api/health`
- ✅ Esce subito se il server non risponde

### 2️⃣ **File di Configurazione Centralizzato** (`config.json`)
- Tutte le impostazioni in un unico file JSON
- Parametri supportati:
  - `baseUrl`: URL del server 9Router
  - `timeoutSec`: Timeout per le richieste
  - `retries`: Numero di tentativi per errori temporanei
  - `maxConsecutiveFails`: Soglia di fallimenti consecutivi prima di rimuovere
  - `logRetentionDays`: Quanti giorni conservare i log
  - `token`: Token di autenticazione (opzionale)
  - `parallelization.throttleLimit`: Numero di modelli testati in parallelo

### 3️⃣ **Notifiche Avanzate**
- 📧 **Webhook**: Invia notifiche a endpoint personalizzati
- ⚠️ **Soglia di allarme**: Avvisa quando troppi modelli falliscono
- 📊 **Dati strutturati**: Include statistiche complete nella notifica

**Configurazione**:
```json
"notifications": {
  "enabled": true,
  "webhookUrl": "https://webhook.site/tuo-id",
  "alertOnFailureThreshold": 5
}
```

### 4️⃣ **Report HTML Bello**
- 📊 Genera report visuale con:
  - Contatori: Modelli OK, temporanei, rimossi
  - Tabella con stato dettagliato di ogni modello
  - Errori e codici HTTP
  - Timestamp

**File**: `logs/comboact-report-YYYY-MM-DD-HHmmss.html`

### 5️⃣ **Logging Strutturato**

#### JSON Lines (JSONL) per analisi
- **File**: `logs/comboact-structured-YYYY-MM-DD.jsonl`
- Ogni riga è un evento JSON completo
- Facile da importare in ELK, Splunk, DataDog, etc.

Esempio:
```json
{"timestamp":"2026-08-16 14:30:45","level":"NOTIFICATION","message":"Maintenance Completed","removed":2,"added":1,"replaced":1}
```

#### CSV per Excel/Sheets
- **File**: `logs/comboact-models-YYYY-MM-DD.csv`
- Contiene timestamp, modello, stato, codice HTTP, errori, fallimenti consecutivi

### 6️⃣ **Protezione da Esecuzioni Simultanee**
- 🔒 File lock (`.comboact.lock`)
- Impedisce di far girare due istanze contemporaneamente
- Auto-rimuove lock stale dopo 2 ore
- Evita conflitti di aggiornamento

### 7️⃣ **Timestamp nel JSON dello Stato**
- Aggiunto `lastUpdated` nel file di stato
- Sapere quando è stata l'ultima manutenzione

**Nuovo formato `comboact-state.json`**:
```json
{
  "lastUpdated": "2026-08-16 14:30:45",
  "models": {
    "gemini/gemini-2.5-flash-lite": {
      "lastError": "",
      "lastOk": "2026-08-16",
      "lastStatus": 200,
      "consecutiveFails": 0
    }
  }
}
```

### 8️⃣ **Parallelizzazione Migliorata**
- Già supportata (ForEach-Object -Parallel)
- Throttle limit configurabile in config.json
- Default: 8 modelli contemporaneamente

### 9️⃣ **Ordinamento per Latenza (modelli veloci PRIMA)**
- Ogni test misura il tempo di risposta (`latencyMs`) e lo persiste in `logs/comboact-state.json`
- Al termine della manutenzione la combo viene **riordinata**: i modelli più veloci e stabili finiscono in testa, così il router 9Router instrada subito su di loro a ogni chiamata `comboact`
- **Configurazione**:
  - `orderByLatency`: `true` per attivare il riordino (default `false` = mantiene l'ordine del router)
  - `preferredOrder`: lista di modelli da mettere IN TESTA assoluta (es. i più affidabili/veloci noti)
- Conta solo la latenza dei test **RIUSCITI**: un modello che fallisce in fretta (503/429) non finisce in testa
- I modelli senza misurazione finiscono in coda (ordinati per nome)

### 🔟 **Fix API Combinata (UUID)**
- La combo è aggiornata via `PUT /api/combos/<id>` usando l'**UUID** della combo (il server 9Router rifiuta il nome con HTTP 400 "name already exists")
- Lo script legge `id` da `/api/combos` e usa quello nel path

---

## 🚀 Come Usare

### Setup Iniziale

1. **Modifica `config.json`** per le tue impostazioni:
```json
{
  "baseUrl": "http://localhost:20128",
  "token": "tuo-token-se-necessario",
  "notifications": {
    "enabled": true,
    "webhookUrl": "https://webhook.site/tuo-id",
    "alertOnFailureThreshold": 5
  }
}
```

2. **Esegui il bat file**:
```batch
mantenimento-comboact.bat
```

### Opzioni Avanzate

```powershell
# Dry-run per vedere cosa accadrebbe
.\update-comboact.ps1 -DryRun

# Usa config.json personalizzato
.\update-comboact.ps1 -ConfigFile "C:\custom\config.json"

# Override parametri da linea di comando
.\update-comboact.ps1 -BaseUrl "http://localhost:20128" -MaxConsecutiveFails 5

# Forza rimozione anche se lascia combo vuoto
.\update-comboact.ps1 -Force
```

---

## 📂 File di Output

```
logs/
├── comboact-update-2026-08-16.log           # Log testuale
├── comboact-state.json                       # Stato persistente modelli
├── comboact-report-2026-08-16-143045.html    # Report HTML
├── comboact-structured-2026-08-16.jsonl      # Log JSON Lines
├── comboact-models-2026-08-16.csv            # Log CSV
└── .comboact.lock                            # File lock (temp)
```

---

## 🔔 Notifiche Webhook

Esempio di payload inviato:
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

Usa servizi come:
- **Webhook.site** (testing gratuito)
- **Discord Webhook**
- **Slack incoming webhook**
- **Custom API endpoint**

---

## 📊 Analisi dei Dati

### Con Python
```python
import json
import pandas as pd

# Leggere JSONL
with open('logs/comboact-structured-2026-08-16.jsonl') as f:
    logs = [json.loads(line) for line in f]

# Convertire a DataFrame
df = pd.DataFrame(logs)
print(df.groupby('level').size())
```

### Con PowerShell
```powershell
# Analizzare CSV
$csv = Import-Csv "logs\comboact-models-2026-08-16.csv"
$csv | Group-Object Status | Select-Object Name, Count
```

---

## 🔧 Configurazioni di Esempio

### Per ambienti di test
```json
{
  "baseUrl": "http://localhost:20128",
  "timeoutSec": 15,
  "maxConsecutiveFails": 2,
  "logRetentionDays": 7,
  "notifications": { "enabled": false }
}
```

### Per produzione con notifiche
```json
{
  "baseUrl": "http://9router.example.com",
  "token": "secret-token-here",
  "maxConsecutiveFails": 3,
  "notifications": {
    "enabled": true,
    "webhookUrl": "https://api.example.com/webhooks/maintenance",
    "alertOnFailureThreshold": 5
  },
  "reporting": {
    "enableHtmlReport": true,
    "enableJsonLogging": true,
    "enableCsvLogging": true
  }
}
```

---

## 🐛 Troubleshooting

**Errore: "Another maintenance process is running"**
- Il lock file è rimasto da una precedente esecuzione fallita
- Soluzione: Attendi 2 ore oppure elimina manualmente `.comboact.lock`

**Webhook non invia**
- Verifica `webhookUrl` in config.json
- Assicurati che `notifications.enabled` sia `true`
- Controlla i log in `comboact-update-*.log`

**Script non legge config.json**
- Assicurati che sia nella stessa cartella dello script
- Oppure usa `-ConfigFile` per specificare il percorso

---

## 📝 Note Finali

- ✅ Tutti i 8 miglioramenti sono stati implementati
- ✅ Backward compatible (funziona anche senza config.json)
- ✅ Logging completo per debug e audit
- ✅ Report belli per condividere risultati
- ✅ Protezione da anomalie ed errori

**Buon mantenimento! 🚀**
