## Piano di integrazione: Mate-Engine ↔ Synthetic Heart (aggiornamento)

### Sommario
Breve piano operativo per collegare Mate-Engine a Synthetic_Heart usando l'interfaccia Ollama compatibile e per:
- Permettere a Mate di scaricare VRM dal server SyntH
- Consentire l'upload temporaneo di animazioni da Mate a SyntH (senza modificarne le skin finché non si promuovono)
- Sincronizzare lo stato animazione centrale di SyntH quando è attiva la modalità SyntH
- Sostituire o integrare il prompt locale di Mate con un prompt/iniezione fornita da SyntH (senza annullare le istruzioni core di SyntH)

---

## Obiettivi tecnici principali
- Esporre in SyntH gli endpoint API per upload/download animazioni e VRM, e per la query dello stato animazione e del prompt di override.
- Implementare un workflow temporaneo per upload animazioni (storage in `skins/temp/<upload_id>/...`) che non richieda modificare subito `skins/` delle persona.
- Fare in modo che AnimationHandler possa rilevare le animazioni temporanee tramite "search paths" o registrazioni temporanee senza richiedere che gli utenti adottino plugin speciali.
- Fornire a Mate un meccanismo semplice per richiedere il prompt di override: se SyntH segnala `override=false` Mate deve *aggiungere* (append) la singola riga fornita; se `override=true` (solo in casi fidati/admin) può sostituire.
- Consentire a SyntH di inviare messaggi tramite l'interfaccia Mate: Mate deve supportare l'ascolto/receiving e l'instradamento dei messaggi in ingresso.

---

## 5) Push animazioni Mate → SyntH (workflow dettagliato)
- Endpoint proposto: `POST /api/animations/upload` (multipart/form-data)
  - Campi: `file` (.fbx o .vrma), `state` (e.g. `idle|think|write|talk`), `descriptor` (opzionale JSON), `tags` (opz JSON array), `upload_id` opzionale
  - Azioni server:
    - Validazione basica (extension + header), scrittura su `skins/temp/<upload_id>/animations/<state>/<filename>.fbx`
    - Salvataggio descriptor come `<filename>.fbx.json` se fornito
    - Scrittura metadata `skins/temp/<upload_id>/meta.json` con `created_at`, `owner`, `tags`, `state`
    - Registrazione: aggiungere `skins/temp/<upload_id>` a AnimationHandler._search_paths (tramite helper `add_temporary_search_path`) e/o chiamare `register_state_animations` per priorità immediata
  - Risposta on success (201): `{"status":"ok","upload_id":"...","url":"/skins/temp/<upload_id>/animations/<state>/<file>"}`

- Endpoints di utilità:
  - `GET /api/animations/uploads` → lista uploads temporanei e metadata
  - `DELETE /api/animations/uploads/{upload_id}` → rimuove upload temporaneo
  - `POST /api/animations/promote` → promuove: copia file in `skins/<target_skin>/animations/<state>/` (richiede permessi/admin)

- Convenzioni storage:
  - Temp: `skins/temp/<upload_id>/animations/<state>/<file>`
  - Meta: `skins/temp/<upload_id>/meta.json`
  - Promoted: `skins/<persona>/animations/<state>/<file>`

- Pulizia e retention:
  - Default TTL: 7 giorni (configurabile)
  - Task background (hourly) che elimina upload scaduti e deregistra search paths
  - Protezione concorrente: usare asyncio.Lock per mutazioni del registry e chiamate ad AnimationHandler

- Priorità in ricerca delle animazioni (minime modifiche):
  1. registered overrides (AnimationHandler._registered_state_animations) — utilizzare per upload temporanei con priorità
  2. search_paths (AnimationHandler._search_paths) — aggiungere `skins/temp/<upload_id>` in coda/inizio a seconda della policy
  3. skins/<persona>/animations/<state>
  4. Rei fallback

- Nota: non fare fallback diretto su MateEngineX; il comportamento deve essere server-side (SyntH) e visibile indipendentemente dall'avere il plugin Mate.

---

## 6) Prompt swap / injection (preservare il prompt di SyntH)
- Endpoint proposto: `GET /api/prompt_override`
  - Risposta (JSON):
    ```json
    {
      "override": false,
      "injection": "Interface note (MateEngine, host=my-host, os=Linux Fedora 43): This interface may request limited animation control and UI hints; do not replace SyntH core instructions.",
      "source": "MateEngine",
      "static": true,
      "metadata": {"os":"Linux","host":"my-host","controls":{"animations":"limited"}},
      "timestamp": "2026-01-26T12:00:00Z"
    }
    ```
- Regole di merging consigliate per l'interfaccia Mate:
  - Se `override === true` → applicare solo dopo verifica di fiducia/config (reserved for admin/trusted integration).
  - Se `override === false` → appendare ESATTAMENTE la stringa `injection` come un singolo short system message *dopo* il prompt di SyntH (non sostituirlo).
  - Per `static===true` l'interfaccia può memorizzare/applicare l'iniezione automaticamente per la sessione; per `static===false` richiedere per sessione.

- Template frase da usare (esempio):
  - Inglese conciso (consigliato per inserimento in prompt già in inglese):
    `Interface note (MateEngine, host={host}, os={os}): This interface may request limited animation control and UI hints; do not replace SyntH system instructions.`
  - Versione italiana suggerita: 
    `Nota interfaccia (MateEngine, host={host}, os={os}): Questa interfaccia può richiedere controllo animazioni limitato e suggerimenti UI; non sostituire le istruzioni di sistema di SyntH.`

- Implementazione su SyntH:
  - Plugin `MateEngine` o estensione `interface/ollama_compat_server.py` può esporre `/api/prompt_override` e decidere se `static:true` e quali `metadata` includere.
  - Uso coerente con `core/prompt_engine.build_json_prompt()` — SyntH già supporta static injections da plugin; new endpoint è un meccanismo esplicito per interfaces esterne.

---

## Cambiamenti / File da modificare (sintesi concreta)

Nota importante: il progetto coinvolge due repository distinti (Synthetic_Heart e Mate-Engine). Le responsabilità di implementazione e le pipeline di build devono essere chiaramente separate e non mischiate — vedere la sezione "Repository responsibilities" qui sotto.

### Synthetic_Heart (repo)
- Requisiti funzionali (server-side, produzione):
  - `core/animation_handler.py`
    - Aggiungere helper: `add_temporary_search_path(path: Path)`, `remove_temporary_search_path(path: Path)`, `register_temporary_state_override(upload_id, state, filenames)`.
  - `core/webui.py` (o modulo equivalente che espone API HTTP)
    - Esporre gli endpoint di produzione: `POST /api/animations/upload`, `GET /api/animations/uploads`, `DELETE /api/animations/uploads/{id}`, `POST /api/animations/promote`, `GET /api/prompt_override`.
  - `core/animation_uploads.py` (nuovo)
    - Implementare gestione metadata, operazioni atomiche, cleaner task TTL, e helper API usabili nei test di integrazione.
  - `tests/test_animations_upload.py` (nuovo)
    - Testare upload, discovery, promotion, cleanup in ambiente CI (no mocks, test end-to-end dove possibile).
  - `docs/interfaces.rst` / `docs/animation_system.rst`
    - Documentare gli endpoint, path, retention e regole di merging prompt.

### Mate-Engine (repo)
- Requisiti (client & pipeline, produzione):
  - Il codice relativo all'integrazione con Synthetic_Heart deve esistere come un plugin di produzione chiamato **`Synthetic_Heart`** e risiedere in:
    `/home/xargon/gits/Mate-Engine-Linux-Port/Plugins/Synthetic_Heart`
    - Il plugin deve essere codice **production-ready** (NESSUN esempio, NESSUN mock). Tutte le API utilizzate devono essere reali e testabili contro un'istanza reale di Synthetic_Heart.
    - Il plugin deve esporre le funzionalità richieste (upload animazioni, invio messaggi, prompt_override processing) come implementate nel plan.
  - Unity C# side (runtime production integration):
    - Non devono esserci componenti "example" nell'albero `Assets/Plugins/SyntheticHeart` — solo codice pronto per la produzione.
    - Le features runtime (es. invio messaggi, upload animazioni) devono essere testabili via un piccolo harness di integrazione CI (invocazione CLI o argumenti di test che terminano col codice di uscita corretto).

### CI & Release Requirements (Mate-Engine repository)
- La pipeline di rilascio deve soddisfare i seguenti obblighi **obbligatori**:
  1. Build Unity obbligatoria: la pipeline richiede il `unity_version` input e il `UNITY_LICENSE` secret; se uno di questi manca la pipeline deve fallire immediatamente.
  2. La Unity build viene eseguita con `game-ci/unity-builder` usando `projectPath: .` e `buildsPath` (consigliato `build/unity`).
  3. I plugin vengono buildati e impacchettati nello stesso job (es. eseguire `./scripts/build_plugins.sh` **dopo** la build Unity). Il risultato plugin (es. `dist/plugins-*.tar.gz`) deve essere incluso come asset di rilascio.
  4. La pipeline deve pacchettare l'output Unity in `mate-engine-unity-<version>.tar.gz` e allegarlo alla release.
  5. Artifact e step di test devono produrre log e file diagnostici in caso di fallimento per agevolare la triage.

### Acceptance criteria (Criteri di accettazione)
- Il job di CI deve produrre un artefatto Unity scaricabile dalla pagina di release (file `mate-engine-unity-<version>.tar.gz`).
- Il job di CI deve produrre un archivio dei plugin (se presenti) e allegarlo alla release (`dist/plugins-*.tar.gz`).
- Il repository `Mate-Engine-Linux-Port` deve contenere il plugin di integrazione in `Plugins/Synthetic_Heart` con codice di produzione completo (non demo, non mock).
- I test di integrazione end-to-end che coinvolgono il binario di MateEngine e l'istanza di Synthetic_Heart devono poter essere eseguiti in CI e devono passare con codice 0.
- La documentazione `docs/ci-unity.md` e il piano aggiornato devono descrivere esattamente i passaggi che la pipeline esegue e i prerequisiti (Unity license, unity version, Docker per test E2E).

---

Questo aggiornamento del piano precisa e separa i compiti tra i due repository e stabilisce requisiti CI/Release obbligatori per Mate-Engine. Se sei d'accordo procedo a:
1) aggiornare il file `untitled:plan-mateEngineIntegration.prompt.md` (già fatto),
2) aggiornare il workflow `.github/workflows/release-linux.yml` per far rispettare questi vincoli (pre-check delle variabili e packaging già implementato), e
3) *Non copiare* l'istanza di produzione di Synthetic_Heart nel repository `Mate-Engine`; l'integrazione E2E userà la clonazione del repository remoto (`synth_repo_url`) quando `test_synth_integration` è abilitato nella CI. Se in futuro si desidera usare una copia locale, questa operazione richiederà una conferma esplicita e controllo dei contenuti per evitare duplicazioni non intenzionali.

---

Se confermi, procedo con la copia locale di Synthetic_Heart dentro `Plugins/Synthetic_Heart` e con eventuali aggiornamenti finali alla pipeline per usare la copia locale come sorgente per i test di integrazione. Puoi anche scegliere di fornire invece un URL remoto da clonare (già supportato dalla workflow se preferisci).

---

## Policy & Decision points aperte
- Promozione: chi può promuovere (admin only?) — raccomandazione: richiedere privilegi (UI confirmation + role check).
- Storage metadata: file-based (`skins/temp/.../meta.json`) è semplice; database supporta permission/queries (valutare futuro).
- Realtime: prima fase usare polling (semplice), upgrade a WebSocket/SSE solo se serve latenza bassa.
- Sicurezza upload: iniziare con validazione file/estensione + filename sanitization; considerare scan/validation più profonda in futuro.
- `override===true` prompt: limitare a contesti fidati e controlli amministrativi.

---

## Prossimi passi consigliati
1. Confermi queste scelte (temp path, TTL=7d, append-only prompt default)?
2. Vuoi che prepari subito il PR skeleton per `plugins/mate_engine.py` + endpoints in `core/webui.py` (bozza Python + test)?
3. Vuoi anche un esempio di client C# per Mate (upload e download VRM) pronto all'uso?

---

Se sei d'accordo procedo a generare gli stubs (endpoints + moduli) e test di base nel repo `Synthetic_Heart`. Se preferisci, preparo prima la bozza dell'API OpenAPI per revisione.

