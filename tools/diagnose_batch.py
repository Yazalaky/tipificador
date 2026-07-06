#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ID = "tipificador-cloud-prod"
SERVICE = "tipificador-api"
BUCKET = "tipificador-zips-prod"
API_URL = "https://tipificador-api-m5sisgkmja-uc.a.run.app"

REAL_HOME = Path(
    os.environ.get("HERMES_REAL_HOME", "/home/sistemas")
)
GCLOUD_CONFIG_DIR = REAL_HOME / ".hermes" / "gcloud"
GCLOUD_KEY_FILE = (
    REAL_HOME
    / ".hermes"
    / "credentials"
    / "tipificador-diagnostics.json"
)

BATCH_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def gcloud_environment() -> dict[str, str]:
    if not GCLOUD_KEY_FILE.is_file():
        raise RuntimeError(
            "No existe la credencial de diagnóstico de Google Cloud."
        )

    GCLOUD_CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    environment = os.environ.copy()

    environment.update(
        {
            "CLOUDSDK_CONFIG": str(GCLOUD_CONFIG_DIR),
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": str(
                GCLOUD_KEY_FILE
            ),
            "GOOGLE_APPLICATION_CREDENTIALS": str(
                GCLOUD_KEY_FILE
            ),
            "CLOUDSDK_CORE_PROJECT": PROJECT_ID,
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        }
    )

    return environment


def run_command(command: list[str], allow_failure: bool = False) -> str:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=gcloud_environment(),
    )

    if result.returncode != 0 and not allow_failure:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(error or "El comando terminó con error.")

    return result.stdout.strip()


def normalized_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("jsonPayload")

    if isinstance(payload, dict):
        return payload

    text = entry.get("textPayload")

    if isinstance(text, str):
        try:
            parsed = json.loads(text)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return {}


def read_logs(batch_id: str, days: int) -> list[dict[str, Any]]:
    filter_value = (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{SERVICE}" '
        "AND ("
        f'textPayload:"{batch_id}" '
        f'OR jsonPayload.batchId="{batch_id}" '
        f'OR jsonPayload.batch_id="{batch_id}"'
        ")"
    )

    output = run_command(
        [
            "gcloud",
            "logging",
            "read",
            filter_value,
            f"--project={PROJECT_ID}",
            f"--freshness={days}d",
            "--limit=1000",
            "--order=asc",
            "--format=json",
        ]
    )

    return json.loads(output or "[]")


def inspect_logs(entries: list[dict[str, Any]]) -> dict[str, Any]:
    stages: list[str] = []
    packages_done: set[str] = set()
    write_results: set[str] = set()
    errors: list[str] = []

    for entry in entries:
        payload = normalized_payload(entry)
        stage = str(payload.get("stage") or "").strip()
        package = str(payload.get("package") or "").strip()

        if stage:
            stages.append(stage)

        if stage == "package_done" and package:
            packages_done.add(package)

        if stage == "write_result" and package:
            write_results.add(package)

        for field in ("error", "gcsError"):
            value = payload.get(field)

            if value:
                errors.append(str(value))

        severity = str(entry.get("severity") or "").upper()

        if severity in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}:
            text = entry.get("textPayload")

            if text:
                errors.append(str(text)[:300])

    return {
        "packages_done": sorted(packages_done),
        "write_results": sorted(write_results),
        "batch_done": "batch_done" in stages,
        "upload_results_gcs": "upload_results_gcs" in stages,
        "build_all_zip": "build_all_zip" in stages,
        "errors": list(dict.fromkeys(errors)),
        "last_stage": stages[-1] if stages else None,
    }


def inspect_gcs(batch_id: str) -> dict[str, Any]:
    prefix = f"gs://{BUCKET}/results/{batch_id}/"

    output = run_command(
        [
            "gcloud",
            "storage",
            "ls",
            "--recursive",
            "--long",
            prefix,
        ],
        allow_failure=True,
    )

    objects: list[dict[str, Any]] = []

    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+.+\s+(gs://\S+)$", line)

        if not match:
            continue

        objects.append(
            {
                "bytes": int(match.group(1)),
                "uri": match.group(2),
            }
        )

    all_zip = next(
        (
            item
            for item in objects
            if item["uri"].endswith("/all.zip")
        ),
        None,
    )

    return {
        "objects": objects,
        "all_zip": all_zip,
    }


def read_batch_status(batch_id: str) -> dict[str, Any]:
    url = f"{API_URL}/batch/{batch_id}"

    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body[:500]}

            return {
                "http_status": response.status,
                "data": data,
            }

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")

        return {
            "http_status": error.code,
            "data": body[:500],
        }

    except urllib.error.URLError as error:
        return {
            "http_status": None,
            "data": f"Error de conexión: {error.reason}",
        }


def build_conclusion(
    log_info: dict[str, Any],
    gcs_info: dict[str, Any],
) -> str:
    all_zip = gcs_info["all_zip"]

    if all_zip and all_zip["bytes"] <= 100:
        return (
            "El all.zip existe, pero su tamaño es compatible con "
            "un ZIP vacío. Revisar _build_consolidated_batch_zip()."
        )

    if log_info["batch_done"] and not gcs_info["objects"]:
        return (
            "El batch terminó, pero sus artefactos ya no están en GCS. "
            "Probablemente expiraron por la limpieza de 30 minutos."
        )

    if log_info["batch_done"] and all_zip:
        return (
            "El batch terminó y all.zip existe. Debe inspeccionarse "
            "su cantidad de entradas antes de concluir que está correcto."
        )

    if log_info["errors"]:
        return "El batch presenta errores registrados durante el proceso."

    if not log_info["batch_done"]:
        return "No se encontró evidencia de finalización completa del batch."

    return "No hay suficiente evidencia para establecer la causa."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnóstico de solo lectura para batches del Tipificador."
    )

    parser.add_argument("batch_id")
    parser.add_argument("--days", type=int, default=90)

    args = parser.parse_args()
    batch_id = args.batch_id.lower().strip()

    if not BATCH_PATTERN.fullmatch(batch_id):
        print(
            "ERROR: el batch debe contener exactamente "
            "32 caracteres hexadecimales.",
            file=sys.stderr,
        )
        return 2

    logs = read_logs(batch_id, args.days)
    log_info = inspect_logs(logs)
    gcs_info = inspect_gcs(batch_id)
    batch_status = read_batch_status(batch_id)

    all_zip = gcs_info["all_zip"]

    print("=== DIAGNÓSTICO DE BATCH ===")
    print(f"Batch: {batch_id}")
    print(f"Registros encontrados: {len(logs)}")
    print(f"Última etapa: {log_info['last_stage'] or 'No disponible'}")
    print(f"Batch finalizado: {'Sí' if log_info['batch_done'] else 'No'}")
    print(f"Paquetes finalizados: {len(log_info['packages_done'])}")
    print(f"Resultados escritos: {len(log_info['write_results'])}")
    print(
        "Carga de resultados a GCS: "
        f"{'Sí' if log_info['upload_results_gcs'] else 'No'}"
    )
    print(f"Objetos actuales en GCS: {len(gcs_info['objects'])}")
    print(f"HTTP estado del batch: {batch_status['http_status']}")

    if all_zip:
        print(f"all.zip: encontrado, {all_zip['bytes']} bytes")
    else:
        print("all.zip: no encontrado")

    if log_info["errors"]:
        print("\nErrores:")
        for error in log_info["errors"][:10]:
            print(f"- {error}")

    print("\nConclusión:")
    print(build_conclusion(log_info, gcs_info))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
