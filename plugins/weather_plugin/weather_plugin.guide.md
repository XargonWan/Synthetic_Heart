# Weather

Gives Synth awareness of the **current weather**. It fetches conditions from
wttr.in for a configured location and injects a short weather summary into the
prompt context. It can also emit a daily weather report on a schedule.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject the current weather into the prompt. |
| `trigger_weather_report` | Produce and deliver a weather report now. |
