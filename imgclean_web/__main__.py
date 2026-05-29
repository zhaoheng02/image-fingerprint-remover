"""Run the imgclean web server."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("imgclean_web.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
