"""Serve zen-claw's FastAPI api_app via uvicorn for xbench evaluation.

Usage:
    python scripts/xbench_api_server.py [--port 18791]
"""

import argparse

import uvicorn

from zen_claw.dashboard.server import api_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"zen-claw xbench API server → http://{args.host}:{args.port}/v1")
    print("  POST /v1/chat/completions")
    print("  GET  /v1/models")
    print("  GET  /api/v1/health")
    print()
    uvicorn.run(api_app, host=args.host, port=args.port, log_level="warning")
