# Moment in history App

This directory is the Home Assistant App package. For installation, configuration, HACS guidance, helper setup, dashboard YAML, troubleshooting, and development instructions, see the [repository README](../README.md).

The App writes its generated assets to `/config/www/moment-in-history/` and expects the dashboard to use the built-in `camera`, `tile`, `conditional`, and `iframe` cards described in the repository documentation. It also serves an Ingress-only model selection page on port `8099`; the page discovers models from the configured Ollama endpoints and OpenAI when a key is present.
