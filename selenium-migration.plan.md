# Esternalizzazione di Selenium LLM Engine
Ora farmo un piano in 3 Steps.
Lo scopo è rimuovere l'ambienta grafico e selenium llm engine da dentro synthetic heart, e aggiungere un sistema di endpoint esterni per reintegrare il nuovo e aggiornato selenium engine che sarà un docker a parte a cui synthetic heart sarà istruito a collegarcisi come default nelle nuove installazioni, ma comunque, poterà essere tolto dall'utente se lo volesse.
Molti utenti vogliono usare le api o i loro llm selfhosted e avere un docker così grosso per la maggior parte degli utenti non ha senso.
Il SyntH avrà comunque il terminale di una mini distribuzione linux per le funzionalità dell'agente ma niente desktop grafico poichè non gli serve.

## Step 1 - Custom Endpoints
Creare un sistema per dare la possibilità all'utente di definire endpoint esterni.
Un endpoint esterno è un servizio che può essere registrato come Cortex, Vox, Auris o Live.
Un endpoint esterno può essere un servizio selfhosted come qwano ollama, oppure un api di servizi non self hosted come ad esempio gemini, openai...
...o appunto un selenium llm esterno che di fatto ora si interfaccia come endpoint comatibile openapi, comne ad esempio potrebbe essere un ollama.
L'endpoint deve essere unb elemento aggiungibile da webui facendo tipo "Add Custom Engine". A questo punto verrà chiesto all'utente di inserire un indirizzo, come esempio di default ci sarà http://localhost:11434/api/v1 (che è di solito dove si hosta ollama).
Inoltre si potrà aggiungere un endpoint non self hosted, qui dovremmo inserire tutti i vari connettori per OpenAPI, Gemini, Grok, e tutti gli altri servizi online che offorono api LLM.
Il servizio poi credo che comunicherà le proprie skill a synthetic heart, ad esempio gemini potrebbe registrare skill di chat, immagini, audio, live.
Quindi aggiungendo ad esempio Gemini si mapperà Chat come Cortex, Immagini come il futuro engine per la visione (ancora non implementato ma mettiamo un placeholder), vox per la voce, live per il live multimodale, auris per il TTS.
Tuttavia oltre alle skill registrare dal servizio possiamo fare in modo che l'utente possa eseguire degli override.
Esempio Elevenlabs non supporta chat ma solo Vox e Auris, ma l'utente può decidere comunque di fare unb override Chat e apparirà come Cortex (ovviamente questo poi non funzionerà se non è supportato).
Occhio che l'utente non vede "chat" ma vede "cortex",non vede "tts" ma vede "vox" e così via.
Quindi poi quando andremo a vedere la lista di cortex o di vox troveremo i servizi registrati in questo modo.

NOTA: non rimuoveremo kittentts o il sistem attualmente embedded per auris.

## Step 2 - Integrazione Selenium Engine Esterno
Questo step andrà a fare in modo che l'engine registrato per il cortex di defult sarà proprio l'endpoint esterno Selenium LLM Engine che ora sarà disponibile per default su localhost e verrà aggiunto al docker compose come segue:
```yml
services:
  selenium-llm-engine:
    image: xargonwan/selenium-llm-engine:latest
    container_name: synth-selenium-llm-engine
    ports:
      - "14848:8000"  # API port
      - "3006:3000"   # webtop interface
    volumes:
      - synth-selenium-data:/app/data
      - synth-selenium-config:/config
    networks:
      - synth_network
volumes:
  synth-selenium-data:
  synth-selenium-config:
```
In questo modo un utente inesperto si troverà la pappa pronta, ma un power user potrebbe volerlo disattivare.

## Step 3 - Rimozione ambiente grafico
Questo step è il finale: andrà a rimuovere completamente l'ambiente grafico webtop interno a synthetic heart che ora avrà la sua distro di linux senza xfce, chromium, selenium etc.

## Note finali
- In questo spazio di lavoro hai caricate sia la cartella di synthetic heart e selenium-llm-engine per conoscoenza, non fare implementazioni su selenium llm-engine, se hai bisogno di farle fermati anche se sei in autopilot mode, ma non prevedo cambiamenti poichè il selenium llm engine esterno è molto più avanzato di quello di synthetic heart.
- Dovremmo mantenere comunque un toolkit di base perchè poi gli agent li dovranno usare, infatti, non è parte di questo plan, ma in futuro andremo ad inserire il supporto agentico.
- Il selenium llm engine esterno già supporta il chunking, il chunking infatti è una implementazione che avviene solo sul llm engine poichè gli llm web sono meno potenti dei servizi api o del self hosted, o comunque più limitati.
- Al momento ti ho dato la panoramica completa del lavoro in modo da darti contest, ma per il momento non eseguiremo lo Step 2 e 3.
Concentriamoci sullo step 1, crea un plan.
