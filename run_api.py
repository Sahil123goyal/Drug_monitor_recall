"""Local API launcher with a Psycopg-compatible event loop on Windows."""

import argparse
import asyncio
import logging
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Drug Recall Monitor API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    server = uvicorn.Server(uvicorn.Config("app.main:app", host=args.host, port=args.port))
    # Psycopg async connections cannot use the Windows Proactor event loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
