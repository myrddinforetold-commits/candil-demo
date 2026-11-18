# Candil live demo

- `site/` the interactive 3D demo page (three.js, no build step). Open `site/index.html` through any static server.
- `brain/` the Candil brain on Modal: lastbook's engine (library search and cited answers), Whisper speech-to-text and a Kokoro voice, behind one `/ask` endpoint. The model runs on OpenRouter: Gemma 4 26B A4B for quick answers, DeepSeek V4 Flash 0731 for deep ones.
- `.github/workflows/brain.yml` deploys the brain on every push to `brain/`, and from the Actions tab can download the library pack (`setup-library`) or ask one question (`ask`).

Secrets: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in this repo; the OpenRouter key lives only in the Modal secret `openrouter` (`OPENROUTER_API_KEY`).

The demo says on its page that its answers come from the Candil software running in the cloud; the device itself runs offline.
