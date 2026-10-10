<!-- Estratto da README.md (righe 819-839). Torna al [README](../README.md). -->

## 📊 Diagrammi (archify)

Lo schema interattivo dell'architettura di questa app è generato con
[Archify](https://github.com/tt-a1i/archify) — renderer/validatore Node.js
(clonato in `~/archify`, nessuna installazione globale). Il diagramma è un
HTML autocontenuto (si apre con doppio clic); il JSON è la sorgente tipizzata
(componenti, relazioni, confini, viste guidate, card).

| File | Descrizione |
|---|---|
| `docs/sync-video-architecture.json` / `.html` | Architettura della pipeline |

Rigenera un diagramma dopo aver modificato il JSON (es. architettura):

```bash
node ~/archify/archify/bin/archify.mjs validate architecture docs/sync-video-architecture.json --quality showcase --json
node ~/archify/archify/bin/archify.mjs deliver architecture docs/sync-video-architecture.json docs/sync-video-architecture.html --quality showcase --json
```

---

