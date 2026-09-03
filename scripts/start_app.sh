#!/bin/sh
# Starts both processes voice_proxy.py's hardcoded 127.0.0.1:8100 assumes
# live in the same container: the voice service in the background, the API
# in the foreground. Backgrounded rather than a second docker-compose
# service, on purpose - voice_proxy.py forwards to 127.0.0.1, which only
# resolves to another process in this same container, not a sibling one on
# Docker's network.
#
# The API is what owns this container's health check and log stream, so it
# stays in the foreground (exec, so it receives signals directly rather
# than through a shell). If the voice service fails to start, that is
# logged and the API still comes up - a broken voice process must never
# take a working WhatsApp/Instagram deployment down with it.

set -e

(
  uvicorn services.voice.main:app --host 0.0.0.0 --port 8100 \
    || echo "voice service exited - WhatsApp/Instagram are unaffected, voice calls will fail until this is fixed"
) &

exec uvicorn services.api.main:app --host 0.0.0.0 --port 8000
