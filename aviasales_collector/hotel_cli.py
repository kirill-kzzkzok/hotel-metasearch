from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local hotel search MVP.")
    parser.add_argument("--database", default="hot-tickets.sqlite3", help="SQLite database path")
    parser.add_argument("--port", type=int, default=8766, help="loopback web server port")
    args = parser.parse_args()

    import uvicorn
    from .web import create_app

    uvicorn.run(create_app(args.database, frozenset()), host="127.0.0.1", port=args.port,
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
