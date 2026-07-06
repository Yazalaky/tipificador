import base64
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request
from google.cloud import firestore

app = Flask(__name__)

logging.getLogger().setLevel(logging.INFO)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BOGOTA_TZ = ZoneInfo("America/Bogota")
DEDUP_COLLECTION = "telegram_alert_dedup"
DEDUP_LEASE = timedelta(minutes=5)

db = firestore.Client()


def safe(value):
    return html.escape(str(value or "No disponible"))


def format_datetime(value):
    if value in (None, "", "No disponible"):
        return "No disponible"

    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            raw = str(value).strip()

            if re.fullmatch(r"\d+(\.\d+)?", raw):
                dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

        local = dt.astimezone(BOGOTA_TZ)
        suffix = "a. m." if local.hour < 12 else "p. m."

        return local.strftime("%d/%m/%Y %I:%M:%S ") + suffix

    except (TypeError, ValueError, OSError):
        return str(value)


def short_name(value):
    text = str(value or "").strip()

    if not text:
        return "No disponible"

    return text.rsplit("/", 1)[-1]


def simplify_resource(incident):
    resource = incident.get("resource") or {}

    if not isinstance(resource, dict):
        resource = {}

    labels = resource.get("labels") or {}

    if not isinstance(labels, dict):
        labels = {}

    for key in (
        "service_name",
        "host",
        "instance_id",
        "function_name",
    ):
        if labels.get(key):
            return str(labels[key])

    raw = (
        incident.get("resource_display_name")
        or incident.get("resource_name")
        or "Tipificador"
    )

    raw = str(raw)

    host_match = re.search(r"host=([^,}\s]+)", raw)

    if host_match:
        return host_match.group(1)

    if len(raw) > 120:
        return f"{raw[:117]}..."

    return raw


def create_dedup_key(incident, state):
    incident_id = incident.get("incident_id")

    if incident_id:
        value = f"{incident_id}:{state}"
    else:
        value = "|".join(
            [
                str(incident.get("policy_name", "")),
                str(incident.get("condition_name", "")),
                str(incident.get("resource_name", "")),
                str(incident.get("started_at", "")),
                str(incident.get("ended_at", "")),
                state,
            ]
        )

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@firestore.transactional
def claim_event(transaction, document, metadata):
    snapshot = document.get(transaction=transaction)
    now = datetime.now(timezone.utc)

    if snapshot.exists:
        data = snapshot.to_dict() or {}
        status = data.get("status")

        if status == "sent":
            return "duplicate"

        claimed_at = data.get("claimed_at")

        if (
            status == "processing"
            and isinstance(claimed_at, datetime)
            and now - claimed_at < DEDUP_LEASE
        ):
            return "processing"

    transaction.set(
        document,
        {
            **metadata,
            "status": "processing",
            "claimed_at": now,
            "updated_at": now,
        },
    )

    return "claimed"


def send_telegram(text):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

    except requests.RequestException as error:
        raise RuntimeError(
            "Error de red al contactar Telegram: "
            f"{type(error).__name__}"
        ) from None

    if not response.ok:
        try:
            description = response.json().get(
                "description",
                "Sin descripción",
            )
        except ValueError:
            description = response.text[:300]

        raise RuntimeError(
            "Telegram API rechazó el mensaje: "
            f"status={response.status_code}, "
            f"description={description}"
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/")
def pubsub_push():
    envelope = request.get_json(silent=True)

    if not envelope or "message" not in envelope:
        return "Invalid Pub/Sub envelope", 400

    message = envelope["message"]
    encoded_data = message.get("data")

    if not encoded_data:
        return "Missing message data", 400

    try:
        payload = json.loads(
            base64.b64decode(encoded_data).decode("utf-8")
        )

        incident = payload.get("incident") or payload
        state = str(
            incident.get("state", "UNKNOWN")
        ).upper()

        if state in {"OPEN", "OPENED", "ACTIVE"}:
            icon = "🔴"
            title = "Alerta del Tipificador"

        elif state in {"CLOSED", "RESOLVED"}:
            icon = "🟢"
            title = "Tipificador recuperado"

        else:
            icon = "🟡"
            title = "Evento del Tipificador"

        policy = short_name(
            incident.get("policy_name")
            or "Monitoreo Tipificador"
        )

        condition = short_name(
            incident.get("condition_name")
            or "No disponible"
        )

        resource = simplify_resource(incident)
        started = format_datetime(incident.get("started_at"))
        ended = format_datetime(incident.get("ended_at"))
        incident_url = incident.get("url")

        incident_id = str(
            incident.get("incident_id") or "sin-id"
        )

        pubsub_message_id = str(
            message.get("messageId")
            or message.get("message_id")
            or ""
        )

        dedup_key = create_dedup_key(incident, state)

        document = (
            db.collection(DEDUP_COLLECTION)
            .document(dedup_key)
        )

        transaction = db.transaction()

        claim_status = claim_event(
            transaction,
            document,
            {
                "incident_id": incident_id,
                "state": state,
                "policy": policy,
                "pubsub_message_id": pubsub_message_id,
            },
        )

        if claim_status == "duplicate":
            logging.info(
                json.dumps(
                    {
                        "event": "telegram_alert_duplicate",
                        "incident_id": incident_id,
                        "state": state,
                    }
                )
            )

            return "", 204

        if claim_status == "processing":
            logging.warning(
                json.dumps(
                    {
                        "event": "telegram_alert_in_progress",
                        "incident_id": incident_id,
                        "state": state,
                    }
                )
            )

            return "Event already processing", 503

        lines = [
            f"{icon} <b>{safe(title)}</b>",
            "",
            f"<b>Estado:</b> {safe(state)}",
            f"<b>Política:</b> {safe(policy)}",
            f"<b>Condición:</b> {safe(condition)}",
            f"<b>Recurso:</b> {safe(resource)}",
            f"<b>Inicio:</b> {safe(started)}",
        ]

        if incident.get("ended_at") not in (None, ""):
            lines.append(
                f"<b>Finalización:</b> {safe(ended)}"
            )

        if incident_url:
            lines.extend(
                [
                    "",
                    f'<a href="{safe(incident_url)}">'
                    "Abrir incidente</a>",
                ]
            )

        send_telegram("\n".join(lines))

        document.set(
            {
                "status": "sent",
                "sent_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        logging.info(
            json.dumps(
                {
                    "event": "telegram_alert_sent",
                    "incident_id": incident_id,
                    "state": state,
                    "policy": policy,
                }
            )
        )

        return "", 204

    except Exception:
        try:
            if "document" in locals():
                document.set(
                    {
                        "status": "failed",
                        "failed_at": firestore.SERVER_TIMESTAMP,
                        "updated_at": firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )

        except Exception:
            logging.exception(
                "dedup_status_update_failed"
            )

        logging.exception("telegram_alert_failed")

        return "Delivery failed", 500
