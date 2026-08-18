# Criteri di Aggiunta/Rimozione Modelli da ComboAct

## 🎯 QUANDO UN MODELLO VIENE RIMOSSO

### 1️⃣ ERRORI CATEGORICI (Permanenti = Rimosso subito)

Questi errori indicano che il modello **NEVER** ritornerà a funzionare (senza cambiamenti account/crediti):

#### Codici HTTP che causano rimozione immediata:
```
400 - Bad Request       (richiesta malformata)
401 - Unauthorized      (credenziali non valide)
402 - Payment Required  (credito insufficiente)
403 - Forbidden         (accesso negato)
404 - Not Found         (modello non esiste)
405 - Not Allowed       (operazione non permessa)
406 - Not Acceptable    (formato non supportato)
409 - Conflict          (conflitto stato)
410 - Gone              (modello ritirato/obsoleto)
415 - Unsupported Media (tipo media non supportato)
422 - Unprocessable     (dati non processabili)
```

#### Pattern di errore che indicano problema permanente:
- `"model_not_found"` - Modello non esiste
- `"no longer available"` - Non più disponibile
- `"has reached its end of life"` - Raggiunto fine vita
- `"was retired"` - Ritirato
- `"is no longer available"` - Non disponibile

**Esempio dal log:**
```
HTTP 410: nvidia/deepseek-ai/deepseek-v4-flash
"The model has reached its end of life on 2026-08-07T09:00:00Z 
and is no longer available"
→ RIMOSSO SUBITO (codice categorico + pattern corrispondente)
```

---

### 2️⃣ ERRORI CRONICI (Ripetuti troppe volte = Rimosso)

Un modello viene rimosso se **fallisce consecutivamente per 3+ volte**:

- **Esecuzione 1**: Fallisce → consecutiveFails = 1 (modello MANTENUTO)
- **Esecuzione 2**: Fallisce di nuovo → consecutiveFails = 2 (modello MANTENUTO)
- **Esecuzione 3**: Fallisce di nuovo → consecutiveFails = 3 ➡️ **RIMOSSO**

Codici che contano come "fallimento cronico":
```
408 - Request Timeout       (retry automatico, poi conta se persiste)
429 - Too Many Requests     (rate limit - RESET quotidiano, NO cronico)
500 - Server Error          (errore server)
502 - Bad Gateway           (gateway error)
503 - Service Unavailable   (servizio non disponibile)
504 - Gateway Timeout       (timeout gateway)
529 - Site Overloaded       (sito sovraccarico)
```

**Importante**: 
- **429 e 503 non contano come cronici** perché si resettano quotidianamente
- Modelli gratuiti con limite di richieste (es. OpenRouter 50 req/day) non verranno rimossi per rate limit

**Esempio dal log:**
```
TEMPORARY(1/3) - Timeout (primo fallimento)
TEMPORARY(2/3) - Timeout (secondo fallimento)
→ RIMOSSO - CHRONIC(3/3) (terzo fallimento)
```

---

## ✅ QUANDO UN MODELLO VIENE AGGIUNTO

### 1️⃣ Modelli Gratuiti (Flag: AddFreeModels)

Se abilitato con `-AddFreeModels`, lo script aggiunge questi modelli se:
1. **Disponibili nel sistema 9Router** (`/api/models`)
2. **Non già presenti in comboact**
3. **Sono completamente gratuiti** (zero cost)

Attualmente definiti in `config.json`:
```
- gemini/gemini-2.0-flash
- kimchi/kimi-k2.6
- kimchi/minimax-m2.7
- kimchi/claude-opus-4-6
- kimchi/claude-sonnet-4-6
```

### 2️⃣ Modelli Suggeriti dal Server (Flag: AutoReplace)

Se un modello viene rimosso e l'errore contiene `"replacement": "nuovo-modello"`:
```json
{
  "error": "Model X retired. Replacement: model-y-v2"
}
```

Lo script:
1. **Estrae il nome del modello suggerito** (model-y-v2)
2. **Verifica che esista** nel sistema 9Router
3. **Lo aggiunge al posto del vecchio**

**Esempio dal log:**
```
REMOVE [410] old-model (end of life)
REPLACE old-model → new-model-v2 (suggerito dal server)
```

---

## 📊 RIEPILOGO STATI

| Stato | Definizione | Azione |
|-------|-------------|--------|
| **OK** | Test riuscito | Mantieni, resetta contatore |
| **TEMPORARY(1/3)** | Primo errore temporaneo | Mantieni, incrementa contatore |
| **TEMPORARY(2/3)** | Secondo errore temporaneo | Mantieni, incrementa contatore |
| **TEMPORARY(3/3)** | Terzo errore temporaneo | **RIMOVI** |
| **CATEGORICAL** | Errore permanente | **RIMOVI subito** |

---

## 🔄 ORDINE DI PRIORITÀ DECISIONALE

```
1. Validazione Sintassi
   ↓
2. Errore = codice categorico?
   ├─ SI → RIMOVI IMMEDIATAMENTE
   ├─ NO → Vedi passo 3
   ↓
3. Errore = pattern categorico?
   ├─ SI → RIMOVI IMMEDIATAMENTE
   ├─ NO → Vedi passo 4
   ↓
4. Numero fallimenti consecutivi?
   ├─ < 3 → MANTIENI (temporaneo)
   ├─ >= 3 → RIMOVI (cronico)
   ↓
5. Rate limit (429) o service unavailable (503)?
   ├─ SI → RESET contatore (reset quotidiano)
   ├─ NO → Incrementa contatore
```

---

## 💾 PERSISTENZA DELLO STATO

Ogni modello ha uno stato in `logs/comboact-state.json`:

```json
{
  "lastUpdated": "2026-08-16 17:17:05",
  "models": {
    "gemini/gemini-2.5-flash": {
      "lastOk": "2026-08-16",           // Ultimo test OK
      "lastStatus": 200,                // Ultimo codice HTTP
      "lastError": "",                  // Ultimo messaggio errore
      "consecutiveFails": 0             // Contatore fallimenti
    },
    "cf/deepseek-r1": {
      "lastOk": "2026-08-15",
      "lastStatus": 500,
      "lastError": "Timeout",
      "consecutiveFails": 1             // Primo fallimento
    }
  }
}
```

Lo stato **persiste tra esecuzioni** → il contatore non si resetta a ogni run.

---

## 🎮 COME USARE IL REPORT

```powershell
# Ultimo report del giorno odierno
.\report.ps1

# Report di una data specifica
.\report.ps1 -Date "2026-08-15"
```

Output mostra:
- ✅ **[SUMMARY]** - Conteggio totale
- 🔴 **[REMOVED]** - Modelli rimossi (con motivo)
- 🟢 **[REPLACED]** - Modelli aggiunti come sostituzione
- 🟡 **[TEMP ISSUES]** - Modelli con errori temporanei

---

## 📝 ESEMPIO PRATICO

**Run 1 (2026-08-16 09:00):**
```
gemini/gemini-2.5 → OK (consecutiveFails = 0)
claude-3-opus → TIMEOUT (consecutiveFails = 1) → KEPT
```

**Run 2 (2026-08-16 13:00):**
```
gemini/gemini-2.5 → OK (consecutiveFails = 0)
claude-3-opus → TIMEOUT (consecutiveFails = 2) → KEPT
```

**Run 3 (2026-08-16 17:00):**
```
gemini/gemini-2.5 → OK (consecutiveFails = 0)
claude-3-opus → TIMEOUT (consecutiveFails = 3) → REMOVED (CHRONIC)
```

**Result**: claude-3-opus rimosso dopo 3 fallimenti consecutive in 3 giorni.

---

Spero sia chiaro! Ci sono dubbi sui criteri? 🤔
