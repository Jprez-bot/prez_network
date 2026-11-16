"""Start the Jimbo4prezz AI Content Studio: python run.py  →  http://127.0.0.1:8765"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    ap = argparse.ArgumentParser(description="Jimbo4prezz AI Content Studio")
    ap.add_argument("--host", default=os.environ.get("JIMBO_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JIMBO_PORT", "8765")))
    ap.add_argument("--data-dir", default=None, help="where the library, renders and database live")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["JIMBO_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    import uvicorn
    from jimbo.api import app
    print(f"Jimbo4prezz Studio → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
