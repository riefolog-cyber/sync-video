# Quick Start Guide

## Primo Avvio (30 secondi)

1. **Esegui il file batch**:
   ```
   doppio-click → mantenimento-comboact.bat
   ```

2. **Oppure da PowerShell**:
   ```powershell
   .\update-comboact.ps1
   ```

3. ✅ Fatto! I log appariranno in `logs/`

---

## I Tuoi File Generati

### Log testuale
```
logs/comboact-update-2026-08-16.log
```

### Stato modelli
```
logs/comboact-state.json
```

### Report visuale (apri con browser)
```
logs/comboact-report-2026-08-16-143045.html
```

### Dati per Excel
```
logs/comboact-models-2026-08-16.csv
```

---

## Se Vuoi Configurare Notifiche

1. **Apri `config.json`**

2. **Aggiungi webhook** (esempio Webhook.site):
   ```json
   "notifications": {
     "enabled": true,
     "webhookUrl": "https://webhook.site/tuo-id-unico"
   }
   ```

3. **Salva e esegui**

4. **Controlla** https://webhook.site per vedere le notifiche

---

## Personalizzazioni Comuni

### Testare senza cambiare nulla
```powershell
.\update-comboact.ps1 -DryRun
```

### Vedere solo gli errori
```powershell
Get-Content "logs\comboact-update-$(Get-Date -Format 'yyyy-MM-dd').log" | Select-String "ERROR|REMOVE"
```

### Automatizzare ogni ora
Aprire Task Scheduler (Windows) e creare task ricorrente:
- Programma: `powershell.exe`
- Argomenti: `-NoProfile -ExecutionPolicy Bypass -File "C:\path\update-comboact.ps1"`
- Frequenza: Oraria

---

## File Importanti

| File | Cosa Fa |
|------|---------|
| `config.json` | Configurazione principale |
| `update-comboact.ps1` | Script di manutenzione |
| `mantenimento-comboact.bat` | Lanciatore Windows |
| `logs/` | Cartella di output |
| `README.md` | Documentazione completa |
| `WEBHOOKS.md` | Guida alle notifiche |

---

## SOS: Mi da un errore

### "Cannot reach 9Router"
- Verifica che 9Router sia avviato su `http://localhost:20128`
- Oppure modifica `config.json` con l'indirizzo corretto

### "Another maintenance process is running"
- Attendi 2 ore oppure elimina `logs\.comboact.lock`

### Niente notifiche
- Verifica `"enabled": true` in config.json
- Verifica che l'URL webhook sia corretto
- Controlla i log per errori di rete

---

## Prossimi Passi

1. ✅ Prova con `-DryRun` per vedere cosa accadrà
2. ✅ Configura notifiche webhook
3. ✅ Automatizza con Task Scheduler
4. ✅ Monitora i report HTML

Domande? Leggi `README.md` per la documentazione completa!
