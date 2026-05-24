#!/usr/bin/env bash
# Run WebRTC bot from WSL only.
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(pwd)" == /mnt/* ]]; then
  echo "WARNING: Project is on /mnt/c (OneDrive). For WebRTC audio, copy to native WSL:"
  echo "  cp -r \"$(pwd)/../..\" ~/Pipecat_AI_Interview_Coach && cd ~/Pipecat_AI_Interview_Coach/simple-chatbot/server"
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating Linux virtual environment..."
  rm -rf .venv
  uv sync
fi

echo "Starting bot — open http://localhost:7860/client in Windows Chrome"
uv run bot-openai.py --transport webrtc --host 0.0.0.0
