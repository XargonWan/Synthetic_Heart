# Third-Party Notices — SyntH Stage

## Project AIRI (MIT)

Portions of this application are ported or adapted from **Project AIRI** by
Moeru AI and contributors — <https://github.com/moeru-ai/airi> — used under the
MIT License.

> MIT License
>
> Copyright (c) 2024-PRESENT Neko Ayaka
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### Ported / adapted files

| File in this app | Source in AIRI |
|---|---|
| `uno.config.ts` (theme approach) | `uno.config.ts` (shared config) |
| `src/stores/settings.ts` (hue mechanism) | `apps/stage-web/src/App.vue` |
| `src/lib/pipelines-audio/playback-manager.ts` (near-verbatim) | `packages/pipelines-audio/src/managers/playback-manager.ts` |

Every file above carries an attribution header comment at the top pointing
back here. The rest of the app (VRM rendering, the Karada animation engine,
chat/voice UI, settings/skin components) is original SyntH code — some of it
ported from SyntH's own existing `res/synth_webui/js/` (same repository, no
separate license), and some of it inspired by AIRI's architecture without
reusing its code.

We also depend on the published package `@proj-airi/unocss-preset-chromatic`
(MIT), consumed via npm rather than vendored.
