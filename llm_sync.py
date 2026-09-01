#!/usr/bin/env python3
"""
Selezione delle slide tramite LLM (API OpenAI-compatibile).

Supera il tetto di precisione del MiniLM locale (~50% su presentazioni
tematicamente omogenee): un LLM legge slide + trascrizione INSIEME e sceglie
per ogni chunk audio la slide semanticamente più adatta, con comprensione del
significato (non solo similarità vettoriale).

Unico provider: 9Router (gateway online su localhost:20128, endpoint
OpenAI-compatibile) che instrada verso modelli cloud (OpenRouter free,
Cloudflare Workers AI). LM Studio è stato rimosso: i modelli locali (es.
qwen2.5-7b-instruct, gemma-4-12b-it) si sono rivelati inadatti al flusso
libero sulla macchina dell'utente. La catena di fallback è:

    9Router (comboact -> Cloudflare Mistral 24B -> Gemma 4 31B it free)
        -> motore locale MiniLM

Il provider 9Router usa come modello principale la **combo `comboact`**:
un gruppo di modelli liberi mantenuto dallo script
`9router-maintenance/update-comboact.ps1`. Inviando `"model": "comboact"`
il router instrada automaticamente il primo modello funzionante della combo,
rendendo la cascata implicita (46 modelli mantenuti: Gemini, Kimi, DeepSeek,
Nemotron, GLM, ecc.). Backup espliciti della stessa combo: Cloudflare Mistral
24B (nessun pool condiviso, affidabile) e Gemma 4 31B it (free) nel caso la
combo risponda con errori. La combo può essere sovrascritta con un modello
singolo via `LLM_9ROUTER_MODEL` o `--llm-model`.

Il modulo supporta DUE flussi LLM:

1. **Flusso libero** (`llm_timeline_segments`): il podcast non segue l'ordine
   delle slide; ogni chunk viene assegnato alla slide più adatta, ripetute e
   fuori ordine consentite.
2. **Flusso ibrido ordinato** (`llm_ordered_timeline`): il podcast segue
   l'ordine (slide-audio / audio-slide) ma alcune slide non hanno ancora
   esplicita "slide N" (mai nominate o narrate fuori posizione). Le ancore
   deterministiche restano vincoli esatti; l'LLM posiziona SOLO le slide
   senza ancora, dove il loro contenuto viene effettivamente discusso.

Quando serve l'LLM ma 9Router è spento, la pipeline tenta di AVVIARLO in
automatico (``9router --tray`` via subprocess) e riprende appena il gateway
risponde. Se 9Router non arriva online: nei flussi di sincronizzazione
(``strict=True``) il processo si ARRESTA con un avviso chiaro che spiega come
lanciare 9Router — mai un fallback silenzioso sul MiniLM —; solo il secondo
passaggio di revisione (opzionale, ``--llm-review``) può essere saltato.

La risposta LLM viene cachata per contenuto (hash slide+audio+chunk): la
timeline non si ripaga a ogni run e i file ``llm_*.json`` non vengono mai
rimossi dalla pulizia della cache orfana.

Oltre alla selezione è disponibile un SECOND PASSAGGIO di revisione
(opzionale, flag ``--llm-review``): un'altra chiamata LLM ri-legge la mappa
chunk->slide proposta e segnala (solo avvisi, non modifica la timeline) i
chunk su cui non è d'accordo. Le euristiche locali (lessicali o embedding)
non bastano a verificare presentazioni tematicamente omogenee: il MiniLM
preferisce le slide di sintesi, quindi il revisore più affidabile è l'LLM
stesso.

Formato di risposta atteso (JSON mode / istruzione esplicita):
    [{"chunk": 1, "slide": 4}, {"chunk": 2, "slide": null}, ...]
dove "slide" è il numero della slide (1-based) o null se nessuna è adatta.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any, cast

from chunks import Word, build_windows
from config import CACHE_DIR, atomic_write_text, log
from timeline import _complete_from_anchors, _lis_anchors, reconcile_timeline

try:
    import requests

    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False


# =====================================================================
# ENDPOINT (configurabili via env)
# =====================================================================
# Unico provider LLM: 9Router (gateway online multi-modello).
# Ogni endpoint è un dizionario {url, model, api_key_env, timeout}.
# Secondi massimi di attesa dopo l'avvio automatico di 9Router prima di
# considerarlo irraggiungibile (il gateway node impiega qualche secondo).
_AUTO_LAUNCH_WAIT = 90.0

# Soglia minima di caratteri alfanumerici su una riga di testo OCR: sotto
# questa frazione la riga è troppo corrotta (diagrammi/immagini) e va scartata.
MIN_ALNUM_RATIO_PER_LINE = 0.35

# Codici HTTP usati nella gestione dei gateway 9Router.
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
# Confine degli errori server 5xx: una risposta con status < 500 indica che il
# gateway è vivo (successi/redirect/4xx) → health-check positiva.
HTTP_SERVER_ERROR_BOUNDARY = 500


def endpoints_for(provider: str) -> list[dict[str, Any]]:
    """Restituisce gli endpoint da usare per il provider scelto.

    provider:
        "auto"     -> cascata 9Router (unico provider LLM online)
        "9router"  -> solo 9Router
        "off"      -> nessun endpoint (gestito dal chiamante)
    """
    all_eps = _endpoints()
    if provider == "auto":
        return all_eps
    return [e for e in all_eps if e["name"] == provider]


def _endpoints() -> list[dict[str, Any]]:
    """Endpoint LLM: SOLO 9Router (gateway online multi-provider).

    Cascata di tre modelli (tutti tramite lo stesso router):
      1) Combo ``comboact`` (default) — gruppo di modelli liberi mantenuto da
         ``9router-maintenance/update-comboact.ps1``: il router instrada
         automaticamente il primo modello funzionante della combo (46 modelli).
      2) Cloudflare Mistral 24B — nessun pool condiviso (affidabile), backup
         esplicito nel caso la combo fallisca del tutto.
      3) Gemma 4 31B it (free, via OpenRouter) — ultima rete di sicurezza
         free nel caso i primi due non siano disponibili.
    """
    endpoints: list[dict[str, Any]] = []

    # 9Router (gateway multi-provider, es. http://localhost:20128/v1)
    r_url = os.environ.get("LLM_9ROUTER_URL", "http://localhost:20128/v1")
    if not r_url:
        return endpoints
    base = r_url.rstrip("/")
    api_key = os.environ.get("LLM_9ROUTER_API_KEY", "")

    # 1) Modello principale: combo `comboact` (tutti i modelli free del router)
    endpoints.append(
        {
            "name": "9router",
            "url": base + "/chat/completions",
            "model": os.environ.get("LLM_9ROUTER_MODEL", "comboact"),
            "api_key": api_key,
            "timeout": 120,
        }
    )
    # 2) Backup nello stesso router: Cloudflare Mistral 24B — nessun pool
    #    condiviso (affidabile), veloce (~10s). Usato se la combo non risponde.
    endpoints.append(
        {
            "name": "9router",
            "url": base + "/chat/completions",
            "model": os.environ.get("LLM_9ROUTER_BACKUP_MODEL", "cf/@cf/mistralai/mistral-small-3.1-24b-instruct"),
            "api_key": api_key,
            "timeout": 120,
        }
    )
    # 3) Seconda rete di sicurezza free (via OpenRouter)
    endpoints.append(
        {
            "name": "9router",
            "url": base + "/chat/completions",
            "model": os.environ.get("LLM_9ROUTER_BACKUP_MODEL_2", "openrouter/google/gemma-4-31b-it:free"),
            "api_key": api_key,
            "timeout": 120,
        }
    )

    return endpoints


# =====================================================================
# CHUNK DELLA TRASCRIZIONE (finestre temporali)
# =====================================================================
def build_llm_chunks(
    words: list[Word],
    total_duration: float,
    chunk_seconds: float = 30.0,
) -> list[dict[str, object]]:
    """Raggruppa le parole in chunk temporali di `chunk_seconds` secondi.

    A differenza dei blocchi semantici (4s), i chunk LLM sono più larghi così
    il modello ha abbastanza contesto per capire l'argomento. I chunk con poche
    parole (silenzio) vengono conservati con testo "..." per mantenere la
    timeline continua.
    """
    chunks: list[dict[str, object]] = []
    for i, w in enumerate(build_windows(words, total_duration, chunk_seconds)):
        chunks.append(
            {
                "num": i + 1,
                "start": w["start"],
                "end": w["end"],
                "first_time": w["first_time"],
                "text": w["text"],
            }
        )
    return chunks


# =====================================================================
# PULIZIA TESTO OCR (prima di inviarlo all'LLM)
# =====================================================================
def clean_slide_text_for_llm(text: str, max_chars: int = 900) -> str:
    """Pulisce il testo OCR di una slide prima di inviarlo all'LLM.

    Rimuove il watermark "NotebookLM", comprime gli spazi bianchi e scarta
    le righe con bassa densità alfanumerica (rumore OCR di diagrammi/icone)
    che confonderebbero il modello senza aggiungere significato.
    """
    t = (text or "").replace("\u00a0", " ")
    # Watermark generato da NotebookLM sulle slide (es. "fù NotebookLM")
    t = re.sub(r"\bf[ùú]\s*NotebookLM\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bNotebookLM\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bf[ùú]\b", " ", t, flags=re.IGNORECASE)

    kept = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        alnum = sum(c.isalnum() for c in s)
        if len(s) > 0 and alnum / len(s) < MIN_ALNUM_RATIO_PER_LINE:
            continue  # riga troppo corrotta (diagrammi, immagini)
        kept.append(s)
    t = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return t[:max_chars]


# =====================================================================
# PROMPT E PARSING
# =====================================================================
def _slide_block(slide_texts: Sequence[str], max_chars: int = 400) -> str:
    """Blocco prompt "Diapositive:" (testo pulito, una per riga, numerate 1..N)."""
    return "\n".join(f"{i + 1}. {clean_slide_text_for_llm(t)[:max_chars]}" for i, t in enumerate(slide_texts))


def _chunk_block(chunks: Sequence[dict[str, object]]) -> str:
    """Blocco prompt "Parlato (chunk):" (una riga per chunk con finestra temporale)."""
    return "\n".join(f"chunk {c['num']} [{c['start']:.0f}s-{c['end']:.0f}s]: {c['text']}" for c in chunks)


def _extract_json_array(content: str) -> Any | None:
    """Trova e decodifica il PRIMO array JSON nel testo, in modo tollerante.

    Gestisce codice fenced, testo extra attorno all'array e apostrofi singoli
    al posto delle virgolette. Con ``.*?`` (non-greedy) prende il primo array
    valido: un modello che emette due array (es. spiegazione + array finale)
    non fa fallire il parse sul secondo.
    """
    if not content:
        return None
    m = re.search(r"\[.*?\]", content, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        # Prova a riparare: apostrofi singoli al posto di virgolette
        try:
            return json.loads(re.sub(r"'", '"', m.group(0)))
        except (json.JSONDecodeError, TypeError):
            return None


def build_prompt(
    slide_texts: Sequence[str],
    chunks: Sequence[dict[str, object]],
) -> tuple[str, str]:
    """Costruisce il prompt sistema+utente per la selezione delle slide.

    Il testo delle slide viene ripulito dal rumore OCR e il prompt impone
    due regole anti-errore: niente slide-riassunto quando esiste una slide
    specifica, e "mantieni la slide" sui chunk di transizione/ricapitolazione.
    """
    system = (
        "Sei un esperto di sincronizzazione audiovisiva. Ti vengono date le "
        "diapositive di una presentazione (numeri 1..N) e il parlato di un "
        "podcast diviso in chunk temporali. Per OGNI chunk scegli la "
        "diapositiva il cui contenuto corrisponde meglio a quello che si "
        "dice, anche se le diapositive non sono in ordine e possono ripetersi. "
        "REGOLE IMPORTANTI:\n"
        "- Se una diapositiva è un RIASSUNTO o uno schema d'insieme "
        "dell'intera presentazione (es. mappa del percorso, slide di sintesi), "
        "usala SOLO se nessuna diapositiva più specifica si adatta al "
        "contenuto del chunk.\n"
        "- Se un chunk è solo una transizione, un'introduzione o una "
        "ricapitolazione senza contenuto tecnico specifico, RIPETI la "
        "diapositiva del chunk precedente (stesso numero) invece di cambiarne.\n"
        "- Se un chunk contiene SOLO l'introduzione del tema guida o una "
        "frase di cornice senza contenuto specifico (es. 'la filosofia è una "
        "cassetta degli attrezzi', 'uno strumento', 'un kit di sopravvivenza'), "
        "NON anticipare la slide di sintesi/conclusione: RIPETI la diapositiva "
        "precedente o usa la copertina.\n"
        "- Se un chunk contiene la fine di un argomento e l'inizio del "
        "successivo, assegnalo all'argomento che occupa la MAGGIORE parte "
        "del chunk.\n"
        "Se nessuna diapositiva è adatta, usa null. NIENTE spiegazioni, NIENTE "
        "ragionamento preliminare (niente analisi, niente piano, niente "
        "pensieri): vai DIRETTAMENTE all'array JSON finale. Rispondi SOLO con "
        "un array JSON di oggetti, uno per chunk, in questo "
        'formato esatto: [{"chunk": 1, "slide": 3}, {"chunk": 2, "slide": null}]'
    )
    user = (
        f"Diapositive:\n{_slide_block(slide_texts)}\n\n"
        f"Parlato (chunk):\n{_chunk_block(chunks)}\n\n"
        "Rispondi con l'array JSON."
    )
    return system, user


def parse_llm_response(
    content: str,
    num_chunks: int,
    total_slides: int | None = None,
) -> list[int | None] | None:
    """Estrae la lista slide-per-chunk dalla risposta LLM.

    Tollerante: cerca il primo array JSON nel testo (anche dentro code fence),
    accetta oggetti con chiavi "chunk"/"slide", riempiendo i buchi con None.
    Se `total_slides` non è dato, accetta qualsiasi numero di slide >= 1.
    """
    data = _extract_json_array(content)
    if not isinstance(data, list):
        return None

    slides: list[int | None] = [None] * num_chunks
    for item in data:
        if not isinstance(item, dict):
            continue
        c = item.get("chunk")
        s = item.get("slide")
        if isinstance(c, int) and 1 <= c <= num_chunks:
            slide_num = _as_slide_number(s)
            if slide_num is not None:
                if slide_num >= 1 and (total_slides is None or slide_num <= total_slides):
                    slides[c - 1] = slide_num
            elif s is None:
                slides[c - 1] = None
    return slides


def _as_slide_number(value: Any) -> int | None:
    """Accetta int, float intero o stringa numerica ("4", "4.0")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        v = value.strip().strip('"')
        if v.isdigit():
            return int(v)
        try:
            f = float(v)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    return None


# =====================================================================
# CHIAMATA LLM (con cascata di fallback)
# =====================================================================
def _call_endpoint(
    endpoint: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float = 0.0,
) -> str | None:
    """Chiama un singolo endpoint. Restituisce il testo della risposta o None."""
    if not _HAS_REQUESTS:
        log.warning("   [LLM] requests non installato, salto %s.", endpoint["name"])
        return None
    headers = {"Content-Type": "application/json"}
    if endpoint.get("api_key"):
        headers["Authorization"] = f"Bearer {endpoint['api_key']}"
    payload = {
        "model": endpoint["model"],
        "messages": messages,
        "temperature": temperature,
        # 4096 token bastavano per i modelli non-reasoning, ma la combo
        # comboact può instradare su modelli "reasoning" (es. gpt-oss-120b)
        # che consumano gran parte del budget nel reasoning interno prima di
        # emettere il content JSON: un limite basso produce finish_reason
        # "length" senza content interpretabile. 8192 copre anche quelli.
        "max_tokens": 8192,
    }
    max_attempts = 3  # retry su rate-limit (pool free condiviso) con backoff
    for attempt in range(max_attempts):
        try:
            resp = requests.post(
                endpoint["url"],
                headers=headers,
                json=payload,
                timeout=endpoint.get("timeout", 120),
            )
        except (requests.RequestException, OSError) as e:
            if attempt < max_attempts - 1:
                log.warning(
                    "   [LLM] %s non raggiungibile (tentativo %d/%d): %s",
                    endpoint["name"],
                    attempt + 1,
                    max_attempts,
                    e,
                )
                time.sleep(2 * (attempt + 1))
                continue
            log.warning("   [LLM] %s non raggiungibile: %s", endpoint["name"], e)
            return None

        if resp.status_code == HTTP_TOO_MANY_REQUESTS and attempt < max_attempts - 1:
            wait = 5 * (attempt + 1)
            log.warning(
                "   [LLM] %s rate-limit (HTTP 429): riprovo tra %ds...",
                endpoint["name"],
                wait,
            )
            time.sleep(wait)
            continue
        if resp.status_code != HTTP_OK:
            log.warning(
                "   [LLM] %s: HTTP %d (%s)",
                endpoint["name"],
                resp.status_code,
                resp.text[:150],
            )
            return None
        try:
            data = resp.json()
            return cast(str, data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError):
            # Alcuni gateway (es. 9Router con modelli free) restituiscono testo
            # extra dopo il JSON: estrai il contenuto dal primo blocco valido.
            text = resp.text
            m = re.search(r'"content"\s*:\s*"((?:\\.|[^"\\])*)"', text)
            if m:
                # Decodifica le sequenze di escape del JSON (\n, \", \\...)
                try:
                    return cast(str, json.loads('"' + m.group(1) + '"'))
                except (ValueError, TypeError):
                    return m.group(1)
            log.warning(
                "   [LLM] %s: risposta non interpretabile (%.150s...)",
                endpoint["name"],
                text,
            )
            return None
    return None


def _call_cascade(
    endpoints: Sequence[dict[str, Any]],
    messages: list[dict[str, str]],
    prefix: str,
) -> tuple[str | None, str | None, str | None]:
    """Prova gli endpoint in cascata e restituisce la prima risposta utile.

    Returns:
        (content, used_endpoint, used_model) del primo endpoint che risponde,
        oppure (None, None, None) se tutti falliscono.
    """
    for ep in endpoints:
        t0 = time.time()
        log.info("   %s Provo %s (modello %s)...", prefix, ep["name"], ep["model"])
        content = _call_endpoint(ep, messages)
        if content is not None:
            log.info(
                "   %s %s [%s] ha risposto in %.1fs.",
                prefix,
                ep["name"],
                ep["model"],
                time.time() - t0,
            )
            return content, ep["name"], ep["model"]
    return None, None, None


# =====================================================================
# HEALTH-CHECK 9Router + PAUSA/RIPRESA
# =====================================================================
def router_alive(endpoints: list[dict[str, Any]], timeout: float = 3.0) -> bool:
    """Ping rapido: almeno un endpoint del router 9Router risponde?

    Health-check veloce (GET /models sulla base del router) usato per capire
    se serve avviare 9Router PRIMA di invocare l'LLM, evitando gli interi
    timeout HTTP della cascata (fino a ~18 minuti con il router spento).
    """
    if not _HAS_REQUESTS or not endpoints:
        return False
    for ep in endpoints:
        url = ep.get("url", "")
        # L'endpoint punta a .../v1/chat/completions; la health-check usa la base
        base = url.rsplit("/chat/completions", 1)[0]
        if not base:
            continue
        try:
            resp = requests.get(base + "/models", timeout=timeout)
            if resp.status_code < HTTP_SERVER_ERROR_BOUNDARY:
                return True
        except (OSError, requests.RequestException):
            continue
    return False


def _skip_key_pressed() -> bool:
    """True se l'utente ha premuto 'S' (Windows: msvcrt, senza bloccare)."""
    try:
        import msvcrt

        if msvcrt.kbhit():  # type: ignore[attr-defined]  # solo su Windows (mypy Linux)
            ch = msvcrt.getch()  # type: ignore[attr-defined]
            return ch in (b"s", b"S") if isinstance(ch, bytes) else ch.lower() == "s"
    except (ImportError, OSError, UnicodeDecodeError):
        pass
    return False


def is_interactive() -> bool:
    """True se stdin è un terminale interattivo (l'utente può premere 'S')."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _launch_9router() -> bool:
    """Avvia il gateway 9Router in background (modalità tray).

    Il comando ``9router`` è uno shim npm (9router.cmd / 9router.ps1): per
    eseguirlo serve il resolver di shell di Windows, quindi si usa
    ``shell=True`` con argomenti letterali (nessun input utente). Il processo
    è staccato (DEVNULL su stdin/stdout/stderr) e vive oltre il run: al
    prossimo run la health-check lo trova online. NON garantisce che il
    gateway risponda subito: ``wait_for_router`` verifica con polling.
    """
    if shutil.which("9router") is None:
        return False
    try:
        subprocess.Popen(
            "9router --tray --skip-update",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (OSError, ValueError):
        return False


def _flush_logs() -> None:
    """Svuota subito i log (stdout+stderr) così gli avvisi di PAUSA appaiono
    immediatamente anche quando l'output è reindirizzato (pipe/file), dove
    Python bufferizza e il messaggio resterebbe invisibile finché il processo
    non termina (che in pausa non succede mai)."""
    for handler in log.handlers:
        with suppress(Exception):
            handler.flush()
    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.flush()


def _router_unavailable_error(context: str) -> RuntimeError:
    """Errore che ARRESTA il processo quando 9Router serve ma non risponde."""
    return RuntimeError(
        "9Router è necessario per "
        + (context or "la sincronizzazione LLM")
        + ", ma non risponde su http://localhost:20128/v1 e non c'è un "
        "terminale interattivo per scegliere.\n"
        "Opzioni:\n"
        "   > avvia 9Router e rilancia il comando (in PowerShell: "
        "'9router --tray');\n"
        "   > esegui in un terminale interattivo e premi 'S' per ripiegare "
        "sul MiniLM locale;\n"
        "   > usa --llm off per il MiniLM locale esplicito (qualità "
        "inferiore, nessuna attesa)."
    )


def wait_for_router(
    endpoints: list[dict[str, Any]],
    wait_timeout: float = 0.0,
    context: str = "",
    strict: bool = False,
) -> bool:
    """Garantisce che 9Router sia online quando serve l'LLM.

    Se il router risponde subito ritorna True senza attese. Altrimenti:

    1. Tenta l'AVVIO AUTOMATICO di 9Router (``9router --tray`` in background)
       e fa polling (ogni 5s, fino a ``_AUTO_LAUNCH_WAIT`` secondi): appena il
       gateway risponde riprende da solo, garantendo la sincronia LLM.
    2. Se dopo l'avvio automatico 9Router non è ancora online:
       - con terminale interattivo: stampa un avviso ben chiaro su come
         avviarlo e resta in PAUSA (polling ogni 5s); l'utente può avviare
         9Router (riprende da solo), premere 'S' (fallback MiniLM) o attendere
         ``wait_timeout`` secondi (0 = illimitato);
       - senza terminale interattivo (test/CI/automazione): se ``strict=True``
         solleva ``RuntimeError`` (il processo si ARRESTA con l'avviso di
         lanciare 9Router — mai un fallback silenzioso); se ``strict=False``
         ripiega subito sul MiniLM (usato solo dal secondo passaggio di
         revisione, che è opzionale).

    Returns:
        True se si può procedere con l'LLM, False per ripiegare sul MiniLM.

    Raises:
        RuntimeError: con ``strict=True`` e senza terminale interattivo quando
            9Router è necessario ma non risponde.
    """
    if router_alive(endpoints):
        return True

    # 1) Avvio automatico di 9Router + polling finché non risponde.
    if _launch_9router():
        log.warning("   [LLM] 9Router non raggiungibile: avvio automatico...")
        start = time.time()
        while time.time() - start < _AUTO_LAUNCH_WAIT:
            time.sleep(5)
            if router_alive(endpoints):
                log.warning("   9Router avviato: riprendo con l'LLM.")
                return True
            if _skip_key_pressed():
                log.warning("   'S' premuto: salto l'LLM e uso il MiniLM locale.")
                return False
        log.warning(
            "   [LLM] 9Router non online dopo %d secondi dall'avvio automatico.",
            int(_AUTO_LAUNCH_WAIT),
        )
    else:
        log.warning(
            "   [LLM] Comando '9router' non disponibile: avvio manuale richiesto (9router --tray).",
        )

    if not is_interactive():
        if strict:
            raise _router_unavailable_error(context)
        log.warning("   [LLM] 9Router non raggiungibile e stdin non interattivo: ripiego subito sul MiniLM locale.")
        return False
    log.warning("\n" + "=" * 70)
    log.warning(" [ATTENZIONE] 9Router NON è online e serve per: %s", context or "sincronizzazione LLM")
    log.warning("=" * 70)
    log.warning(" Il processo è in PAUSA in attesa di 9Router.")
    log.warning("")
    log.warning(" COSA FARE:")
    log.warning("   1) AVVIA 9Router (gateway su http://localhost:20128/v1)")
    log.warning("      -> il processo riprende da SOLO con la qualità LLM")
    log.warning("   2) oppure premi il tasto 'S' per SALTARE l'LLM e usare")
    log.warning("      subito il MiniLM locale (qualità inferiore)")
    if wait_timeout > 0:
        log.warning("   3) oppure non fare nulla: dopo %ds passa al MiniLM", wait_timeout)
    log.warning("")
    log.warning(" Non serve premere altro: appena 9Router è online riparte.")
    log.warning("=" * 70)
    _flush_logs()
    start = time.time()
    while True:
        time.sleep(5)
        if router_alive(endpoints):
            log.warning("   9Router rilevato: riprendo con l'LLM.")
            return True
        if _skip_key_pressed():
            log.warning("   'S' premuto: salto l'LLM e uso il MiniLM locale.")
            return False
        if wait_timeout > 0 and time.time() - start >= wait_timeout:
            log.warning("   Attesa superata (%ds): ripiego sul MiniLM locale.", wait_timeout)
            return False


# =====================================================================
# ORCHESTRATORE: slide + parole -> segmenti
# =====================================================================
def llm_timeline_segments(
    slide_texts: list[str],
    words_raw: list[Word],
    total_slides: int,
    total_duration: float,
    chunk_seconds: float = 30.0,
    endpoints: list[dict[str, Any]] | None = None,
    review: bool = False,
    wait_timeout: float = 0.0,
    strict: bool = False,
) -> list[dict[str, Any]] | None:
    """Seleziona le slide con un LLM (cascata di fallback).

    Con ``review=True`` esegue anche un secondo passaggio LLM che ri-verifica
    la selezione e avvisa (senza modificare la timeline) sui chunk sospetti.

    ``wait_timeout`` (secondi): se il router 9Router è spento, il processo si
    mette in pausa con un avviso e riprende quando il router torna online; se
    l'utente preme 'S' (o scade il timeout) ritorna None e il chiamante usa il
    fallback MiniLM. 0 = attesa illimitata.

    ``strict``: nel flusso libero il MiniLM da solo non basta (tetto ~50% ed
    è lento su audio lunghi). Se ``True`` e non c'è un terminale interattivo
    mentre 9Router è necessario ma spento, solleva ``RuntimeError`` invece di
    ripiegare in silenzio (il chiamante interrompe con un errore chiaro).

    Returns:
        Lista di segmenti {"slide": n, "start": s, "end": e} come il motore
        locale, oppure None se nessun endpoint risponde (il chiamante usa il
        fallback MiniLM).
    """
    if not words_raw:
        return None
    chunks: list[dict[str, Any]] = build_llm_chunks(words_raw, total_duration, chunk_seconds)
    if not chunks:
        return None

    eps = endpoints if endpoints is not None else _endpoints()

    # --- Cache: non ripagare l'LLM (5 minuti!) a ogni run. La chiave include
    # i modelli effettivi: cambiare --llm-model (o l'ordine della cascata)
    # produce una timeline diversa e non deve riusare la cache precedente.
    # La cache viene consultata anche se gli endpoint non rispondono: è
    # proprio quando il servizio è giù che il risultato cachato serve. ---
    cache_key = _cache_key(slide_texts, words_raw, total_slides, chunk_seconds, eps)
    cached = _load_llm_cache(cache_key)
    if cached is not None:
        log.info("   [LLM] Timeline recuperata dalla cache (hash %s).", cache_key[:12])
        if review:
            slides_cached = _chunk_slides_from_segments(chunks, cached)
            diffs = review_llm_timeline(
                slide_texts,
                chunks,
                slides_cached,
                total_slides,
                endpoints=endpoints,
                wait_timeout=wait_timeout,
                strict=strict,
            )
            if diffs is not None:
                _warn_review_diffs(chunks, slides_cached, diffs)
        return cached

    if not eps:
        log.warning("   [LLM] Nessun endpoint configurato (manca la chiave/servizio).")
        return None

    # Health-check: se 9Router è spento, PAUSA con avviso e ripresa automatica
    # appena torna online (o 'S' per il fallback MiniLM). In modalità strict
    # (flusso libero) senza terminale: errore chiaro, niente fallback silenzioso.
    if not wait_for_router(eps, wait_timeout=wait_timeout, context="la selezione delle slide", strict=strict):
        return None

    system, user = build_prompt(slide_texts[:total_slides], chunks)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    content, used_endpoint, used_model = _call_cascade(eps, messages, "[LLM]")
    if content is None:
        log.warning("   [LLM] Nessun endpoint disponibile: fallback al motore locale.")
        return None

    slides = parse_llm_response(content, len(chunks), total_slides=total_slides)
    if slides is None or all(s is None for s in slides):
        log.warning("   [LLM] Risposta non interpretabile: fallback al motore locale.")
        return None

    # Costruisce segmenti: raggruppa chunk consecutivi con la stessa slide
    segments: list[dict[str, object]] = []
    cur_slide: int | None = None
    cur_start = 0.0
    for c, s in zip(chunks, slides, strict=True):
        start = float(c["first_time"])
        if s == cur_slide:
            continue
        if cur_slide is not None:
            segments.append({"slide": cur_slide, "start": cur_start, "end": start})
        cur_slide = s
        cur_start = start
    if cur_slide is not None:
        segments.append({"slide": cur_slide, "start": cur_start, "end": total_duration})

    # Rimuove i segmenti "null" (nessuna slide adatta) estendendo il vicino
    merged: list[dict[str, object]] = []
    for seg in segments:
        if seg["slide"] is None:
            if merged:
                merged[-1]["end"] = seg["end"]
            continue
        if merged and merged[-1]["slide"] == seg["slide"]:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    if merged:
        merged[0]["start"] = 0.0  # niente audio perso all'inizio

    # --- Secondo passaggio di revisione (opzionale, solo avvisi) ---
    # La mappa chunk->slide viene ricostruita dai segmenti MERGED (stessa
    # logica del ramo cache-hit) così chiave cache e prompt coincidono: se
    # usassimo qui le slide grezze, un chunk "null" assorbito dal vicino
    # produrrebbe una chiave di revisione diversa tra primo e secondo run.
    if review:
        slides_review = _chunk_slides_from_segments(chunks, merged)
        diffs = review_llm_timeline(
            slide_texts,
            chunks,
            slides_review,
            total_slides,
            endpoints=eps,
        )
        if diffs is not None:
            _warn_review_diffs(chunks, slides_review, diffs)

    log.info(
        "   [LLM] %d segmenti generati (%d chunk, via %s [%s]).",
        len(merged),
        len(chunks),
        used_endpoint,
        used_model,
    )
    _save_llm_cache(cache_key, merged)
    return merged


# =====================================================================
# FLUSSO IBRIDO ORDINATO: ancore deterministiche esatte + LLM per le
# slide senza ancora esplicita.
# =====================================================================
# Nel flusso slide-audio/audio-slide il MiniLM sbaglia le slide che NON hanno
# ancora esplicita "slide N": le allinea per similarità, ma se la narrazione
# salta la slide (mai nominata) o la discute fuori posizione (es. contenuto
# della slide 3 a 100s ma PDF slide 2 mai narrata) produce durate inventate.
# L'LLM legge slide + trascrizione INSIEME e capisce DOVE il contenuto di una
# slide viene effettivamente discusso, anche senza che lo speaker la nomini.
#
# Il vincolo duro resta deterministico: le ancore esplicite "slide N" vengono
# rispettate ALLA LETTERA; l'LLM fornisce solo la posizione delle slide senza
# ancora, interpolando le mai menzionate. Il risultato è una timeline monotona
# che usa l'LLM SOLO dove il segnale deterministico manca.
def build_ordered_prompt(
    slide_texts: Sequence[str],
    chunks: Sequence[dict[str, Any]],
    anchors: dict[int, float] | None = None,
    force_anchors: bool = False,
) -> tuple[str, str]:
    """Prompt per il flusso ordinato (slide-audio / audio-slide).

    A differenza del prompt del flusso libero, dice all'LLM che il podcast
    segue l'ordine delle slide (può saltarne alcune, ma non torna indietro)
    e gli comunica le ancore temporali già note ("slide N" nominate dallo
    speaker) come riferimenti certi per collocare le slide senza ancora.

    Con ``force_anchors=True`` le ancore vengono espresse anche come vincoli
    chunk -> slide ASSOLUTI ("il chunk 5 è la slide 6, non cambiarlo"):
    usato nel retry quando il primo tentativo produce posizioni in conflitto
    con le ancore.
    """
    anchor_block = ""
    if anchors:
        lines = [f"slide {s} a {t:.1f}s" for s, t in sorted(anchors.items()) if s > 1]
        if lines:
            anchor_block = (
                "\nRiferimenti temporali CERTI (lo speaker li ha nominati esplicitamente):\n" + ", ".join(lines) + "\n"
            )
    forced_block = ""
    if force_anchors and anchors:
        lines = []
        for s, t in sorted(anchors.items()):
            for c in chunks:
                if c["start"] <= t < c["end"]:
                    lines.append(f"chunk {c['num']} = slide {s}")
                    break
        if lines:
            forced_block = (
                "\nVINCOLI ASSOLUTI (lo speaker li ha nominati esplicitamente): NON assegnare "
                "mai altre slide a questi chunk, NON spostarli:\n" + "\n".join(lines) + "\n"
            )
    system = (
        "Sei un esperto di sincronizzazione audiovisiva. Ti vengono date le "
        "diapositive di una presentazione (numeri 1..N) e il parlato di un "
        "podcast diviso in chunk temporali, in ordine cronologico. Il podcast "
        "segue l'ORDINE delle diapositive: si parte dalla 1 e si procede in "
        "avanti, ma può SALTARE una diapositiva che non viene mai discussa. "
        "Per OGNI chunk scegli la diapositiva il cui contenuto corrisponde a "
        "quello che si dice.\n"
        "REGOLE IMPORTANTI:\n"
        "- La sequenza delle diapositive deve essere NON DECRESCENTE: puoi "
        "ripetere la stessa diapositiva su più chunk consecutivi, ma non puoi "
        "tornare a una diapositiva già superata.\n"
        "- Se una diapositiva è un RIASSUNTO o uno schema d'insieme dell'intera "
        "presentazione, usala SOLO se nessuna diapositiva più specifica si "
        "adatta al contenuto del chunk.\n"
        "- Se un chunk è solo una transizione, un'introduzione o una "
        "ricapitolazione senza contenuto tecnico specifico, RIPETI la "
        "diapositiva del chunk precedente (stesso numero).\n"
        "Se nessuna diapositiva è adatta, usa null. NIENTE spiegazioni, NIENTE "
        "ragionamento preliminare (niente analisi, niente piano, niente "
        "pensieri): vai DIRETTAMENTE all'array JSON finale. Rispondi SOLO con "
        "un array JSON di oggetti, uno per chunk, in questo "
        'formato esatto: [{"chunk": 1, "slide": 3}, {"chunk": 2, "slide": null}]'
    )
    user = (
        f"Diapositive:\n{_slide_block(slide_texts)}\n\n"
        f"Parlato (chunk):\n{_chunk_block(chunks)}\n\n"
        f"{anchor_block}"
        f"{forced_block}"
        "Rispondi con l'array JSON."
    )
    return system, user


def llm_ordered_timeline(
    slide_texts: list[str],
    words_raw: list[Word],
    total_slides: int,
    total_duration: float,
    anchors: dict[int, float],
    chunk_seconds: float = 30.0,
    endpoints: list[dict[str, Any]] | None = None,
    wait_timeout: float = 0.0,
    strict: bool = False,
) -> dict[int, float] | None:
    """Timeline monotona per i flussi ordinati: ancore deterministiche ESATTE
    + posizione LLM per le slide senza ancora esplicita.

    Le ancore esplicite "slide N" restano vincoli inviolabili (timestamp reali
    dello speaker). L'LLM fornisce la posizione delle slide non nominate: il
    primo chunk in cui il suo contenuto viene discusso. Le slide mai discusse
    vengono interpolate tra le ancore (mai distribuzioni uniformi globali).

    ``wait_timeout``: come in ``llm_timeline_segments`` (pausa con avviso se
    9Router è spento, 'S' o scadenza -> None per il fallback MiniLM).

    ``strict``: se True e 9Router è necessario ma non risponde senza terminale
    interattivo, solleva ``RuntimeError`` (il processo si arresta con l'avviso
    di lanciare 9Router) invece di ripiegare in silenzio sul MiniLM.

    Returns:
        Timeline {slide: start} valida (reconcile_timeline) oppure None
        (nessun endpoint risponde / risposta non interpretabile: il chiamante
        usa il fallback MiniLM).

    Raises:
        RuntimeError: con ``strict=True`` e senza terminale interattivo quando
            9Router è necessario ma non risponde.
    """
    if not words_raw:
        return None
    chunks: list[dict[str, Any]] = build_llm_chunks(words_raw, total_duration, chunk_seconds)
    if not chunks:
        return None

    eps = endpoints if endpoints is not None else _endpoints()
    if not eps:
        return None

    # Cache: chiave dedicata (prefisso "ord") che include le ancore note,
    # così cambiare ancora/trascrizione non riusa il risultato precedente.
    cache_key = _ordered_cache_key(
        slide_texts,
        words_raw,
        total_slides,
        chunk_seconds,
        eps,
        anchors,
    )
    cached = _load_llm_cache(cache_key)
    if cached is not None:
        log.info("   [LLM/Ordinato] Timeline recuperata dalla cache (hash %s).", cache_key[:12])
        return _timeline_from_cached(cached, anchors, total_slides, total_duration)

    # Health-check: se 9Router è spento, avvio automatico + PAUSA con avviso
    # e ripresa automatica appena torna online (o 'S' per il fallback MiniLM).
    # In modalità strict (senza terminale): errore chiaro, niente fallback.
    if not wait_for_router(eps, wait_timeout=wait_timeout, context="le slide senza ancora", strict=strict):
        return None

    system, user = build_ordered_prompt(slide_texts[:total_slides], chunks, anchors)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    content, used_endpoint, used_model = _call_cascade(eps, messages, "[LLM/Ordinato]")
    if content is None:
        log.warning("   [LLM/Ordinato] Nessun endpoint disponibile: fallback al motore locale.")
        return None

    slides = parse_llm_response(content, len(chunks), total_slides=total_slides)
    if slides is None or all(s is None for s in slides):
        log.warning("   [LLM/Ordinato] Risposta non interpretabile: fallback al motore locale.")
        return None

    # Le ancore esplicite sono vincoli ESATTI e inviolabili: nessuna posizione
    # LLM può contraddirle. La costruzione della timeline forza ogni chunk che
    # contiene un'ancora alla sua slide e scarta i "first_discussion" in
    # conflitto monotono con le ancore (slide senza ancora posizionata dopo una
    # slide successiva già ancorata, o viceversa).
    timeline = _build_ordered_timeline(slides, chunks, anchors, total_slides, total_duration)

    # Se il primo tentativo produce vincoli insoddisfacibili, riproviamo UNA
    # volta con le ancore FORZATE nei chunk: i modelli piccoli (es. comboact)
    # tendono a ignorare i riferimenti temporali posti in fondo a un prompt
    # lungo. Un solo retry per non raddoppiare i costi nel caso peggiore.
    if timeline is None:
        log.warning(
            "   [LLM/Ordinato] Posizioni LLM in conflitto con le ancore: "
            "nuovo tentativo con le ancore forzate nei chunk...",
        )
        system, user = build_ordered_prompt(
            slide_texts[:total_slides], chunks, anchors, force_anchors=True
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        content, used_endpoint, used_model = _call_cascade(eps, messages, "[LLM/Ordinato]")
        if content is not None:
            slides = parse_llm_response(content, len(chunks), total_slides=total_slides)
            if slides is not None and not all(s is None for s in slides):
                timeline = _build_ordered_timeline(slides, chunks, anchors, total_slides, total_duration)

    if timeline is None:
        log.warning(
            "   [LLM/Ordinato] Vincoli insoddisfacibili anche dopo il secondo tentativo: "
            "fallback al motore locale (qualità inferiore).",
        )
        return None

    # Le ancore esplicite non devono MAI essere spostate dall'interpolazione.
    for s, t in anchors.items():
        timeline[s] = float(t)

    # Guardia finale: dopo il ripristino delle ancore la timeline deve restare
    # valida (monotona). Se il clamp dell'interpolazione le ha scalate e il
    # ripristino ha rotto l'ordine, niente timeline inventata: fallback MiniLM.
    try:
        reconcile_timeline(timeline, total_slides, total_duration)
    except ValueError:
        log.warning(
            "   [LLM/Ordinato] Timeline non valida dopo il ripristino delle ancore: "
            "fallback al motore locale."
        )
        return None

    log.info(
        "   [LLM/Ordinato] Timeline ibrida generata (%d slide, %d ancore esatte, via %s [%s]).",
        total_slides,
        len(anchors),
        used_endpoint,
        used_model,
    )
    _save_llm_cache(
        cache_key, [{"slide": s, "start": timeline[s], "end": timeline[s]} for s in range(1, total_slides + 1)]
    )
    return timeline


def _build_ordered_timeline(
    slides: list[int | None],
    chunks: list[dict[str, Any]],
    anchors: dict[int, float],
    total_slides: int,
    total_duration: float,
) -> dict[int, float] | None:
    """Costruisce la timeline ordinata dalle posizioni LLM rispettando le ancore.

    Le ancore sono vincoli ESATTI: il chunk che contiene il timestamp di
    un'ancora viene forzato alla sua slide (l'LLM può averlo assegnato ad
    altro). Le slide senza ancora vengono collocate al PRIMO chunk in cui il
    modello le discute, solo se la posizione non è in conflitto monotono con
    un'ancora; quelle in conflitto vengono scartate (l'interpolazione le
    coprirà comunque).
    """
    enforced = list(slides)
    for s, t in anchors.items():
        for i, c in enumerate(chunks):
            if c["start"] <= t < c["end"]:
                enforced[i] = s
                break

    first_discussion: dict[int, float] = {}
    for c, sld in zip(chunks, enforced, strict=True):
        if sld is None or sld in anchors or sld in first_discussion:
            continue
        pos = float(c["first_time"])
        if _conflicts_with_anchors(sld, pos, anchors):
            continue
        first_discussion[sld] = pos

    refs: dict[int, float] = dict(anchors)
    refs.update(first_discussion)
    return _complete_from_anchors(refs, total_slides, total_duration)


def _conflicts_with_anchors(slide: int, pos: float, anchors: dict[int, float]) -> bool:
    """True se collocare `slide` a `pos` viola la monotonia con le ancore."""
    for a, t in anchors.items():
        if a > slide and t < pos:
            return True
        if a < slide and t > pos:
            return True
    return False


def _timeline_from_cached(
    cached: list[dict[str, object]],
    anchors: dict[int, float],
    total_slides: int = 0,
    total_duration: float = 0.0,
) -> dict[int, float] | None:
    """Ricostruisce la timeline ordinata dalla cache LLM (lista {slide, start}).

    Filtra le posizioni LLM che violano la monotonia con le ancore (es. slide 4
    posizionata prima dell'ancora slide 3) o che collidono con altre slide
    senza ancora (stesso timestamp), poi completa la timeline con lo STESSO
    motore del percorso live (``_complete_from_anchors``): LIS + interpolazione
    + validazione ``reconcile_timeline``. Cache e run diretta producono quindi
    risultati identici e MAI timeline non monotone (il vecchio completamento
    manuale poteva interpolare una slide filtrata DOPO una posizione LLM
    mantenuta, rompendo la monotonia e facendo abortire la rigenerazione).

    Returns:
        Timeline {slide: start} valida, oppure None se le posizioni residue
        sono insufficienti (il chiamante usa il fallback MiniLM).
    """
    refs: dict[int, float] = {}
    for seg in cached:
        s = seg.get("slide")
        st = seg.get("start")
        if isinstance(s, int) and isinstance(st, (int, float)):
            if s not in anchors and _conflicts_with_anchors(s, float(st), anchors):
                continue
            refs[s] = float(st)
    for s, t in anchors.items():
        refs[s] = float(t)
    # Rimuovi collisioni: slide senza ancora con timestamp uguale a un'altra
    if total_slides > 0:
        seen_times: dict[float, list[int]] = {}
        for s in sorted(refs):
            if s in anchors:
                continue
            t = refs[s]
            bucket = round(t, 1)
            seen_times.setdefault(bucket, []).append(s)
        for _bucket, slds in seen_times.items():
            if len(slds) > 1:
                for s in slds[1:]:
                    del refs[s]
    # Completa le slide mancanti con lo stesso motore del percorso live:
    # il completamento manuale qui aveva un bug (interpolazione non monotona).
    if total_slides > 0 and total_duration > 0:
        return _complete_from_anchors(refs, total_slides, total_duration)
    return refs if len(refs) == total_slides else None


def _ordered_cache_key(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    total_slides: int,
    chunk_seconds: float,
    endpoints: Sequence[dict[str, Any]],
    anchors: dict[int, float],
) -> str:
    """Hash stabile per la cache del flusso ordinato (prefisso "ord" +
    ancore esplicite, che influenzano il prompt e i vincoli)."""
    return _hash_cache(
        [f"ord|{chunk_seconds}"],
        slide_texts[:total_slides],
        _words_hash(words_raw),
        _endpoints_hash(endpoints),
        [repr(sorted(anchors.items()))],
    )


# =====================================================================
# VERIFICA MAPPING ANCORE (numero parlato -> slide reale del PDF)
# =====================================================================
# Il podcast potrebbe NON seguire le regole del prompt NotebookLM: la
# numerazione parlata può essere sfasata rispetto al PDF (es. lo speaker dice
# "quarta diapositiva" ma il contenuto mostrato è la slide 5). L'LLM legge il
# contenuto del parlato subito dopo ogni riferimento "slide N" e determina la
# vera slide del PDF: i TEMPI restano esatti (timestamp reali dello speaker),
# cambia solo il mapping numero->slide.
def build_anchor_verify_prompt(
    slide_texts: Sequence[str],
    anchors: dict[int, float],
    words_raw: list[Word],
    window_seconds: float = 40.0,
) -> tuple[str, str]:
    """Prompt: per ogni ancora 'slide N' parlata determina la slide PDF reale
    basandosi sul contenuto discusso subito dopo il riferimento."""
    lines = []
    for s, t in sorted(anchors.items(), key=lambda kv: kv[1]):
        excerpt = " ".join(w["word"] for w in words_raw if t <= w["start"] < t + window_seconds).strip() or "..."
        lines.append(f"- a {t:.1f}s (lo speaker dice 'slide {s}'): {excerpt}")
    excerpt_block = "\n".join(lines)
    system = (
        "Sei un esperto di sincronizzazione audiovisiva. Ti vengono date le "
        "diapositive di una presentazione PDF (numeri 1..N) e alcune frasi del "
        "parlato di un podcast, ciascuna associata al timestamp in cui lo "
        "speaker dice qualcosa come 'passiamo alla slide tre' o 'la quarta "
        "diapositiva'. ATTENZIONE: la numerazione che usa lo speaker può NON "
        "coincidere con la numerazione delle slide del PDF (può essere sfasata, "
        "oppure lo speaker può riferirsi alla slide che sta mostrando). Per "
        "OGNI timestamp leggi il contenuto del parlato in quel punto e "
        "determina QUAL è la vera slide del PDF (numero 1..N) il cui contenuto "
        "viene discusso in quel momento, basandoti sul CONTENUTO e non sul "
        "numero pronunciato.\n"
        "REGOLE IMPORTANTI:\n"
        "- Usa il numero della slide del PDF (1..N) che corrisponde davvero al "
        "contenuto discusso, NON il numero pronunciato dallo speaker.\n"
        "- Se una diapositiva è un RIASSUNTO o uno schema d'insieme, usala SOLO "
        "se nessuna diapositiva più specifica si adatta.\n"
        "NIENTE spiegazioni, NIENTE ragionamento preliminare (niente analisi, "
        "niente piano, niente pensieri): vai DIRETTAMENTE all'array JSON "
        "finale. Rispondi SOLO con un array JSON di oggetti, uno per "
        "timestamp, in questo formato esatto: "
        '[{"timestamp": 371.1, "slide": 5}, {"timestamp": 475.7, "slide": 6}]'
    )
    user = (
        f"Diapositive:\n{_slide_block(slide_texts)}\n\n"
        f"Parlato ai timestamp indicati:\n{excerpt_block}\n\n"
        "Rispondi con l'array JSON."
    )
    return system, user


def parse_anchor_verification(content: str) -> dict[float, int] | None:
    """Estrae la mappa timestamp -> slide dalla risposta LLM di verifica.

    Tollerante come ``parse_llm_response``: cerca il primo array JSON (anche
    dentro code fence o con testo extra attorno).
    """
    data = _extract_json_array(content)
    if not isinstance(data, list):
        return None
    mapping: dict[float, int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp")
        s = item.get("slide")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            slide_num = _as_slide_number(s)
            if slide_num is not None and slide_num >= 1:
                mapping[round(float(ts), 1)] = slide_num
    return mapping or None


def llm_verify_anchor_mapping(
    slide_texts: list[str],
    words_raw: list[Word],
    anchors: dict[int, float],
    total_slides: int,
    window_seconds: float = 40.0,
    endpoints: list[dict[str, Any]] | None = None,
    wait_timeout: float = 0.0,
    strict: bool = False,
    remap_filter: Callable[[int, float, int], bool | None] | None = None,
) -> dict[int, float] | None:
    """Corregge il mapping numero parlato -> slide PDF delle ancore esplicite.

    Il podcast potrebbe non seguire le regole del prompt NotebookLM: la
    numerazione parlata può essere sfasata rispetto al PDF. L'LLM legge il
    contenuto del parlato subito dopo ogni riferimento "slide N" e determina la
    vera slide del PDF. I TEMPI delle ancore restano invariati: cambia solo il
    numero di slide associato.

    ``remap_filter``: validatore opzionale ``(slide_parlata, tempo, slide_mappata)
    -> bool | None`` applicato a ogni rimappo ``s -> s'`` proposto dall'LLM.
    Se restituisce ``False`` il rimappo viene scartato e l'ancora resta alla
    slide parlata (vincolo ad alta precisione che il contenuto contraddice);
    se ``None`` (nessuna opinione) il rimappo passa. Così la correzione LLM
    non può spostare ancore già corrette. Vedere
    ``semantic_sync.make_anchor_remap_filter``.

    ``wait_timeout``: come in ``llm_timeline_segments`` (pausa con avviso se
    9Router è spento, 'S' o scadenza -> None, il chiamante usa le originali).

    ``strict``: come in ``llm_ordered_timeline`` (se True e 9Router è
    necessario ma non risponde senza terminale, solleva ``RuntimeError``).

    Returns:
        Ancore corrette {slide_pdf: tempo} oppure None se l'LLM non risponde,
        la correzione non è interpretabile o nessun rimappo è sopravvissuto
        (il chiamante usa le originali).

    Raises:
        RuntimeError: con ``strict=True`` e senza terminale interattivo quando
            9Router è necessario ma non risponde.
    """
    if not words_raw or not anchors:
        return None
    eps = endpoints if endpoints is not None else _endpoints()
    if not eps:
        return None

    cache_key = _verify_cache_key(slide_texts, words_raw, anchors, eps)
    cached = _load_llm_cache(cache_key)
    if cached is not None:
        log.info("   [LLM/Ancore] Verifica ancore dalla cache (hash %s).", cache_key[:12])
        verified = _verified_anchors_from_cached(cached)
        if verified is None:
            return None
        # Le ancore cachate devono superare lo STESSO validatore dei rimappi
        # live: una cache prodotta da una run precedente (o da una verifica
        # errata) non può bypassare il filtro sul contenuto del parlato. Si
        # ricostruisce la slide parlata originale per prossimità temporale
        # (i tempi delle ancore restano invariati dalla verifica).
        if remap_filter is not None:
            for pdf_slide, t in list(verified.items()):
                spoken = _nearest_spoken_anchor(pdf_slide, t, anchors)
                if spoken is None or spoken == pdf_slide:
                    continue
                if remap_filter(spoken, anchors[spoken], pdf_slide) is False:
                    log.warning(
                        "   [LLM/Ancore] Rimappo cachato 'slide %d' a %.1fs -> "
                        "slide %d rifiutato dal contenuto del parlato "
                        "(resta slide %d).",
                        spoken,
                        anchors[spoken],
                        pdf_slide,
                        spoken,
                    )
                    verified[spoken] = anchors[spoken]
                    del verified[pdf_slide]
        return verified

    # Health-check: se 9Router è spento, avvio automatico + PAUSA con avviso
    # e ripresa automatica appena torna online (o 'S' per usare le originali).
    # In modalità strict (senza terminale): errore chiaro, niente fallback.
    if not wait_for_router(
        eps, wait_timeout=wait_timeout, context="la verifica del mapping delle ancore", strict=strict
    ):
        return None

    system, user = build_anchor_verify_prompt(
        slide_texts,
        anchors,
        words_raw,
        window_seconds,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    content, used_endpoint, used_model = _call_cascade(eps, messages, "[LLM/Ancore]")
    if content is None:
        log.warning("   [LLM/Ancore] Nessun endpoint disponibile: uso le ancore originali.")
        return None

    mapping = parse_anchor_verification(content)
    if not mapping:
        log.warning("   [LLM/Ancore] Risposta non interpretabile: uso le ancore originali.")
        return None

    corrected: dict[int, float] = {}
    remaps_applied = 0
    for s, t in anchors.items():
        new_slide = mapping.get(round(t, 1))
        if new_slide is None:
            corrected[s] = t  # l'LLM non ha risposto per questo timestamp
        elif 1 <= new_slide <= total_slides:
            if new_slide != s and remap_filter is not None:
                verdict = remap_filter(s, t, new_slide)
                if verdict is False:
                    log.warning(
                        "   [LLM/Ancore] Rimappo 'slide %d' a %.1fs -> slide %d "
                        "rifiutato: il contenuto del parlato non lo supporta "
                        "(resta slide %d).",
                        s,
                        t,
                        new_slide,
                        s,
                    )
                    corrected[s] = t
                    continue
            if new_slide in corrected:
                log.warning(
                    "   [LLM/Ancore] Conflitto: l'ancora 'slide %d' a %.1fs è "
                    "stata mappata sulla slide %d, già occupata dall'ancora a "
                    "%.1fs: la precedente viene scartata.",
                    s,
                    t,
                    new_slide,
                    corrected[new_slide],
                )
            corrected[new_slide] = t
            if new_slide != s:
                remaps_applied += 1
                log.info(
                    "   [LLM/Ancore] Ancora 'slide %d' a %.1fs -> slide %d del PDF.",
                    s,
                    t,
                    new_slide,
                )

    lis = _lis_anchors(corrected)
    if not lis:
        log.warning("   [LLM/Ancore] Nessuna ancora coerente dopo la correzione.")
        return None

    # Se nessun rimappo è sopravvissuto (tutti rifiutati dal filtro semantico
    # o nessuno proposto), non c'è una correzione da applicare: None lascia il
    # chiamante sulle ancore originali, senza messaggi fuorvianti.
    if remaps_applied == 0 and set(lis.items()) == set(anchors.items()):
        log.info("   [LLM/Ancore] Nessun rimappo supportato dal contenuto: uso le ancore originali.")
        return None

    log.info(
        "   [LLM/Ancore] %d ancore corrette (via %s [%s]).",
        len(lis),
        used_endpoint,
        used_model,
    )
    _save_llm_cache(cache_key, [{"slide": s, "start": lis[s]} for s in sorted(lis)])
    return lis


def _verified_anchors_from_cached(
    cached: list[dict[str, object]],
) -> dict[int, float] | None:
    """Ricostruisce le ancore corrette dalla cache (lista {slide, start})."""
    out: dict[int, float] = {}
    for seg in cached:
        s = seg.get("slide")
        st = seg.get("start")
        if isinstance(s, int) and isinstance(st, (int, float)):
            out[s] = float(st)
    return out or None


def _nearest_spoken_anchor(
    pdf_slide: int, t: float, anchors: dict[int, float], tolerance: float = 3.0
) -> int | None:
    """Slide parlata originale la cui ancora è temporalmente più vicina a ``t``.

    La verifica LLM mantiene i TEMPI delle ancore invariati (cambia solo il
    numero di slide): per applicare il ``remap_filter`` a un risultato cachato
    serve ricostruire quale 'slide N' parlata ha generato il rimappo, cercando
    l'ancora originale più vicina entro ``tolerance`` secondi.
    """
    best: int | None = None
    best_d = tolerance
    for s, at in anchors.items():
        d = abs(at - t)
        if d < best_d:
            best, best_d = s, d
    return best


def _verify_cache_key(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    anchors: dict[int, float],
    endpoints: Sequence[dict[str, Any]],
) -> str:
    """Hash stabile della verifica ancore (prefisso "anchver" + ancore originali)."""
    return _hash_cache(
        ["anchver"],
        slide_texts,
        _words_hash(words_raw),
        _endpoints_hash(endpoints),
        [repr(sorted(anchors.items()))],
    )


def llm_cache_keys_for(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    total_slides: int,
    chunk_seconds: float,
    endpoints: Sequence[dict[str, Any]],
    anchors_variants: Sequence[dict[int, float]],
) -> set[str]:
    """Stem delle cache LLM che la run corrente può riusare.

    Le chiavi LLM sono hash del contenuto (slide + parlato + ancore +
    endpoint): cambiando podcast o presentazione le vecchie cache non servono
    più. Per ogni variante di ancore (es. pre- e post-verifica del mapping)
    calcola le chiavi della verifica ancore e della timeline ordinata. Usato
    da main per ripulire a ogni avvio le cache LLM orfane di run precedenti.
    """
    keys: set[str] = set()
    for anchors in anchors_variants:
        if not anchors:
            continue
        keys.add("llm_" + _verify_cache_key(slide_texts, words_raw, anchors, endpoints))
        keys.add(
            "llm_"
            + _ordered_cache_key(
                slide_texts, words_raw, total_slides, chunk_seconds, endpoints, anchors
            )
        )
    return keys


# =====================================================================
# SECOND PASSAGGIO DI REVISIONE (opzionale, --llm-review)
# =====================================================================
# Le euristiche locali (lessicali o embedding) NON riescono a verificare la
# timeline su presentazioni tematicamente omogenee: il MiniLM preferisce le
# slide di sintesi e l'overlap lessicale è troppo rumoroso. Il verificatore
# affidabile è l'LLM stesso: un secondo passaggio a basso costo che ri-legge
# slide + chunk + mappa proposta e segnala i disaccordi. Non modifica la
# timeline, avvisa soltanto.
def build_review_prompt(
    slide_texts: Sequence[str],
    chunks: Sequence[dict[str, Any]],
    slides: Sequence[int | None],
) -> tuple[str, str]:
    """Prompt del secondo passaggio: ri-verifica la mappa chunk->slide."""
    mapping = "\n".join(
        f"chunk {c['num']}: slide {s if s is not None else 'null'}" for c, s in zip(chunks, slides, strict=True)
    )
    system = (
        "Sei un revisore di sincronizzazione audiovisiva. Ti vengono date le "
        "diapositive (numeri 1..N), il parlato diviso in chunk e una PROPOSTA "
        "di associazione chunk->slide. Verifica che la proposta sia coerente "
        "col contenuto di ogni chunk. Per OGNI chunk rispondi con la slide "
        "che ritieni più adatta (puoi confermare la proposta o cambiarla, "
        "anche ripetendo slide fuori ordine); usa null se nessuna slide è "
        "adatta. Se una diapositiva è un riassunto/schema d'insieme, evitala "
        "quando esiste una slide più specifica. NIENTE spiegazioni, NIENTE "
        "ragionamento preliminare (niente analisi, niente piano, niente "
        "pensieri): vai DIRETTAMENTE all'array JSON finale. Rispondi "
        "SOLO con un array JSON di oggetti, uno per chunk, in questo formato "
        'esatto: [{"chunk": 1, "slide": 3}, {"chunk": 2, "slide": null}]'
    )
    user = (
        f"Diapositive:\n{_slide_block(slide_texts)}\n\n"
        f"Parlato (chunk):\n{_chunk_block(chunks)}\n\n"
        f"Proposta attuale:\n{mapping}\n\n"
        "Rispondi con l'array JSON corretto."
    )
    return system, user


def _chunk_slides_from_segments(
    chunks: Sequence[dict[str, Any]],
    segments: Sequence[dict[str, Any]],
) -> list[int | None]:
    """Ricostruisce la mappa chunk->slide dai segmenti (per il diff del review).

    I segmenti vengono costruiti usando ``first_time`` (l'inizio del primo
    parlato del chunk) come confine, non ``start`` (inizio della finestra
    temporale): per questo il lookup usa ``first_time``, altrimenti un chunk
    il cui primo parlato cade tardi nella finestra verrebbe attribuito al
    segmento precedente e la chiave cache del review non coinciderebbe col
    ramo fresh.
    """
    out: list[int | None] = []
    for c in chunks:
        t = float(c["first_time"])
        chosen: int | None = None
        for seg in segments:
            if float(seg["start"]) <= t < float(seg["end"]):
                chosen = int(seg["slide"])
                break
        out.append(chosen)
    return out


def review_llm_timeline(
    slide_texts: Sequence[str],
    chunks: Sequence[dict[str, Any]],
    slides: Sequence[int | None],
    total_slides: int,
    endpoints: list[dict[str, Any]] | None = None,
    wait_timeout: float = 0.0,
    strict: bool = False,
) -> list[dict[str, Any]] | None:
    """Secondo passaggio LLM (opzionale) che ri-verifica la selezione.

    Returns:
        Lista di discrepanze ``[{"chunk": n, "slide": proposta}, ...]`` oppure
        None se nessun endpoint risponde / risposta non interpretabile
        (il chiamante silenzia e procede).
    """
    if not chunks or not slides or len(chunks) != len(slides):
        return None
    eps = endpoints if endpoints is not None else _endpoints()
    if not eps:
        return None

    review_key = _review_cache_key(slide_texts, chunks, slides, eps)
    cached = _load_llm_cache(review_key)
    if cached is not None:
        log.info("   [LLM/Review] Revisione recuperata dalla cache (hash %s).", review_key[:12])
        return cached

    # Health-check: se 9Router è spento, PAUSA con avviso e ripresa automatica
    # (o 'S' per saltare la revisione e procedere con la timeline già pronta).
    if not wait_for_router(eps, wait_timeout=wait_timeout, context="la revisione della timeline", strict=strict):
        return None

    system, user = build_review_prompt(slide_texts[:total_slides], chunks, slides)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    content, _, _ = _call_cascade(eps, messages, "[LLM/Review]")
    if content is None:
        log.warning("   [LLM/Review] Nessun endpoint disponibile: salto la revisione.")
        return None

    reviewed = parse_llm_response(content, len(chunks), total_slides=total_slides)
    if reviewed is None:
        log.warning("   [LLM/Review] Risposta non interpretabile: salto la revisione.")
        return None

    diffs: list[dict[str, object]] = []
    for i, c in enumerate(chunks):
        if slides[i] != reviewed[i]:
            diffs.append({"chunk": int(c["num"]), "slide": reviewed[i]})
    _save_llm_cache(review_key, diffs)
    return diffs


def _review_cache_key(
    slide_texts: Sequence[str],
    chunks: Sequence[dict[str, Any]],
    slides: Sequence[int | None],
    endpoints: Sequence[dict[str, Any]],
) -> str:
    """Chiave di cache della revisione: hash contenuti + mappa + modelli."""
    return _hash_cache(
        ["review"],
        slide_texts,
        [f"{c['num']}:{c['start']}:{c['end']}:{c['text']}" for c in chunks],
        [repr(list(slides))],
        _endpoints_hash(endpoints),
    )


def _warn_review_diffs(
    chunks: Sequence[dict[str, Any]],
    slides: Sequence[int | None],
    diffs: Sequence[dict[str, Any]],
) -> None:
    """Logga le discrepanze del secondo passaggio (solo avviso, non bloccante)."""
    if not diffs:
        log.info("   [LLM/Review] Secondo passaggio: nessuna discrepanza rilevata.")
        return
    for d in diffs:
        chunk_num = int(d["chunk"]) - 1
        if 0 <= chunk_num < len(chunks):
            c = chunks[chunk_num]
            old_slide = slides[chunk_num]
            new_slide = d.get("slide")
            log.warning(
                "   [LLM/Review] Chunk %d (%.0fs-%.0fs): il secondo passaggio "
                "propone slide %s invece di %s. Verifica la sincronizzazione.",
                int(d["chunk"]),
                float(c["start"]),
                float(c["end"]),
                new_slide if new_slide is not None else "null",
                old_slide if old_slide is not None else "null",
            )
    log.warning(
        "   [LLM/Review] %d chunk da verificare (le discrepanze sono solo "
        "suggerimenti, la timeline non è stata modificata).",
        len(diffs),
    )


# =====================================================================
# CACHE LLM (per hash audio+slide+chunk)
# =====================================================================
# Versione logica delle cache LLM: BUMPA questa costante ogni volta che cambi
# prompt, filtri o logica di post-processing (interpolazione, validazione,
# refine). La chiave cache dipende dal CONTENUTO (slide + parole + ancore +
# endpoint) ma NON dal codice: senza questa versione una cache calcolata con
# logica vecchia viene riusata da codice nuovo, con risultati sbagliati senza
# alcun segnale (es. il rimappo ancore errato del 20/08 riusato dal video del
# 21/08). Bumpare rende obsolete TUTTE le cache LLM precedenti: la run
# successiva le ricalcola da zero (costo una tantum), con le protezioni
# correnti (filtri, validazioni) applicate ai risultati freschi.
_LLM_CACHE_LOGIC_VERSION = 2


def _hash_cache(*parts: Sequence[str]) -> str:
    """Hash MD5 stabile di sequenze di stringhe (chiavi di cache LLM).

    Include ``_LLM_CACHE_LOGIC_VERSION``: cambiando la logica di prompt o
    post-processing le cache vecchie non vengono MAI riusate (by design).
    """
    h = hashlib.md5()
    h.update(f"v{_LLM_CACHE_LOGIC_VERSION}|".encode())
    for part in parts:
        for s in part:
            h.update(s.encode("utf-8", errors="replace"))
    return h.hexdigest()


def _words_hash(words_raw: Sequence[Word]) -> list[str]:
    """Rappresentazione delle parole per l'hash: lunghezza + contenuto INTEGRO.

    Due trascrizioni identiche all'inizio ma diverse nel resto non devono
    riusare la timeline LLM precedente.
    """
    return [str(len(words_raw)), repr(words_raw)]


def _endpoints_hash(endpoints: Sequence[dict[str, Any]]) -> list[str]:
    """Rappresentazione degli endpoint per l'hash: nome + modello.

    I modelli (e il loro ordine nella cascata) influenzano il risultato:
    includerli evita di riusare la cache di un modello diverso.
    """
    return [f"|{ep.get('name')}:{ep.get('model')}" for ep in endpoints]


def _cache_key(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    total_slides: int,
    chunk_seconds: float,
    endpoints: Sequence[dict[str, Any]],
) -> str:
    """Hash stabile del contenuto (slide + parole + chunk + modelli) per la cache."""
    return _hash_cache(
        [f"tl|{chunk_seconds}"],
        slide_texts[:total_slides],
        _words_hash(words_raw),
        _endpoints_hash(endpoints),
    )


def _load_llm_cache(key: str) -> list[dict[str, object]] | None:
    """Legge la timeline LLM cachata, o None.

    Una lista VUOTA salvata su disco viene restituita come [] (es. revisione
    senza discrepanze): così il risultato "nessun problema" viene cachato e
    non si ripete la chiamata.
    """
    path = CACHE_DIR / f"llm_{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and (not data or "slide" in data[0]):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_llm_cache(key: str, segments: list[dict[str, object]]) -> None:
    """Salva la timeline LLM nella cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"llm_{key}.json"
    with suppress(OSError):
        atomic_write_text(
            path,
            json.dumps(segments, ensure_ascii=False),
        )
