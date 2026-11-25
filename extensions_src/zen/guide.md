# Zen LLM Engine

Zen is a free engine that drives AI chat sites from a browser, so SyntH can answer
without any API key. It runs as a separate container; this extension only connects
SyntH to it.

## Set up

1. Start the container (it is the `synth-zen-llm-engine` service of `docker-compose.yml`).
2. Switch this extension on and grant it **endpoints.manage**. On a fresh install it
   adds a `zen-llm-engine` endpoint to the Engines page; pick it as the Cortex.
3. If your container answers somewhere else, set **Engine URL** here.

## Notes

- An endpoint you configured yourself is never overwritten: the extension only fills in
  settings it lacks (the request timeout and the prompt-size limit Zen needs).
- Without any cortex SyntH tells you it cannot answer yet; Zen is the free way to have one.
