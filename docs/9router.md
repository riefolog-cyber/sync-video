<!-- Estratto da README.md (righe 840-876). Torna al [README](../README.md). -->

## ⏸️ Gestione on-demand di 9Router

La pipeline usa 9Router **solo quando serve davvero** e non si blocca mai
inutilmente:

- **9Router non necessario** (ancore "slide N" complete, risultato in cache,
  `--llm off`) → nessuna chiamata, nessuna attesa.
- **9Router necessario ma spento**, in **terminale interattivo** → avviso
  chiaro e **pausa** con verifica ogni 5s; il processo **riprende da solo**
  appena avvii 9Router. Durante la pausa:
  - premi **`S`** → salta l'LLM e usa subito l'embedding locale;
  - oppure imposta `--llm-wait-timeout <secondi>` → fallback embedding automatico
    allo scadere (0 = illimitato).
- **9Router non è installato** (il comando `9router` non è nel PATH) → lo
  programma lo dice esplicitamente e propone le due uscite (`--llm off`,
  `--llm-wait-timeout`): l'attesa automatica è impossibile, quindi conviene
  scegliere prima di lanciare.
- **Flusso libero senza terminale** (es. CI, automazione): il fallback embedding
  non basta (tetto di precisione ed è lento su audio lunghi), quindi il programma si
  **interrompe subito con un errore chiaro** invece di produrre un video
  scadente in silenzio. Usa `--llm off` per forzare l'embedding esplicitamente.

```bash
python main.py --llm auto                 # pausa + ripresa automatica (consigliato)
python main.py --llm auto --llm-wait-timeout 60   # fallback embedding dopo 60s
python main.py --llm off                  # solo embedding, nessuna attesa
```

> **PC senza 9Router**: il programma funziona comunque, purché il podcast
> segua il prompt con le ancore esplicite "slide N" (flusso `slide-audio`): in
> quel caso 9Router non viene mai chiamato. Se il podcast non ha ancore si passa
> al flusso libero, dove l'LLM serve: usa `--llm off` (funziona sempre, qualità
> leggermente inferiore) oppure `--flow slide-audio --llm off` per forzare
> l'allineamento ordinato deterministico.

---

