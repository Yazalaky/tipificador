import json
import sys
import traceback

from fastapi import HTTPException

from app.main import _process_batch_package_local


def _write_result(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def main() -> int:
    if len(sys.argv) != 5:
        sys.stderr.write("Uso: python -m app.batch_worker <batch_id> <package_name> <service> <result_path>\n")
        return 2

    batch_id, package_name, service, result_path = sys.argv[1:5]
    try:
        result = _process_batch_package_local(batch_id, package_name, service)
        _write_result(result_path, {"ok": True, "result": result})
        return 0
    except RuntimeError as e:
        _write_result(result_path, {"ok": False, "error": str(e), "type": "RuntimeError"})
        return 3
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            error = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        else:
            error = str(detail)
        _write_result(result_path, {"ok": False, "error": error, "type": "HTTPException"})
        return 4
    except Exception as e:
        traceback.print_exc()
        _write_result(result_path, {"ok": False, "error": str(e), "type": type(e).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
