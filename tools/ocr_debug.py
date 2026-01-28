#!/usr/bin/env python3
import argparse
import json
import sys
import textwrap
import urllib.request
import urllib.error


def _request_json(method: str, url: str):
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_text(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _shorten(text: str, length: int) -> str:
    text = " ".join(text.split())
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostico OCR por pagina usando el backend de Tipificador."
    )
    parser.add_argument("job_id", help="ID del job")
    parser.add_argument(
        "--api",
        default="http://localhost:8000",
        help="Base URL del API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Solo mostrar paginas sin clasificacion (SIN)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limitar numero de paginas mostradas (0 = sin limite)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Forzar re-OCR por pagina (ignora cache)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Mostrar texto OCR completo (default: recorte)",
    )
    args = parser.parse_args()

    base = args.api.rstrip("/")
    job_id = args.job_id

    try:
        auto = _request_json("POST", f"{base}/jobs/{job_id}/auto-classify")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"Error auto-classify: {e.read().decode('utf-8')}\n")
        return 1

    classifications = auto.get("classifications", {})
    keys = sorted(classifications.keys(), key=lambda k: int(k))

    counts = {}
    for k in keys:
        v = classifications.get(k) or "SIN"
        counts[v] = counts.get(v, 0) + 1

    sys.stdout.write("Resumen por categoria:\n")
    for cat in sorted(counts.keys()):
        sys.stdout.write(f"  {cat}: {counts[cat]}\n")

    shown = 0
    for k in keys:
        cat = classifications.get(k) or "SIN"
        if args.only_missing and cat != "SIN":
            continue
        idx = int(k)
        refresh = "true" if args.refresh else "false"
        try:
            text = _request_text(f"{base}/jobs/{job_id}/pages/{idx}/ocr.txt?refresh={refresh}")
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"Error OCR pagina {idx}: {e.read().decode('utf-8')}\n")
            continue

        sys.stdout.write("\n")
        sys.stdout.write(f"Pagina #{idx + 1}  CAT={cat}\n")
        if args.full:
            sys.stdout.write(text + "\n")
        else:
            sys.stdout.write(textwrap.fill(_shorten(text, 300), width=100) + "\n")

        shown += 1
        if args.limit and shown >= args.limit:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
