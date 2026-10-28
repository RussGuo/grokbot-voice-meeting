"""Thin shim: the LiveKit entrypoint lives in meet/worker.py (inside the installed package).

    python main.py dev|start      # same as `meet worker dev|start`
"""
from livekit.agents import cli

from meet.worker import entrypoint, server  # noqa: F401  (re-exported)

if __name__ == "__main__":
    cli.run_app(server)
