#!/usr/bin/env python3
"""Monitor availability for Refugio de Respomuso."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


DEFAULT_API_BASE_URL = "https://api.alberguesyrefugios.com"
DEFAULT_REFUGIO_ID = "9"
STATUS_AVAILABLE = "green"
STATUS_UNAVAILABLE = "red"


@dataclass(frozen=True)
class Config:
    tracked_dates: list[str]
    alert_dates: list[str]
    db_path: Path
    api_base_url: str
    refugio_id: str
    request_timeout: float
    ntfy_url: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    notify_provider: str
    dry_run_notifications: bool


@dataclass(frozen=True)
class Availability:
    target_date: str
    status: str
    available_places: int
    raw_payload: dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_config(config_path: Path | None) -> Config:
    file_config: dict[str, Any] = {}
    if config_path and config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            file_config = yaml.safe_load(fh) or {}

    tracked_dates = parse_dates(
        os.getenv("TRACKED_DATES")
        or os.getenv("TARGET_DATES")
        or file_config.get("tracked_dates")
        or file_config.get("target_dates"),
        "tracked_dates",
    )
    if not tracked_dates:
        raise ValueError("No tracked dates configured. Set TRACKED_DATES or config.yaml.")

    alert_dates = parse_dates(
        os.getenv("ALERT_DATES") or file_config.get("alert_dates"),
        "alert_dates",
    )
    if not alert_dates:
        alert_dates = tracked_dates.copy()

    unknown_alert_dates = sorted(set(alert_dates) - set(tracked_dates))
    if unknown_alert_dates:
        raise ValueError(
            "alert_dates must be included in tracked_dates. Unknown: "
            + ", ".join(unknown_alert_dates)
        )

    db_path = Path(os.getenv("DB_PATH") or file_config.get("db_path") or "data/availability.sqlite")
    api_base_url = str(
        os.getenv("API_BASE_URL") or file_config.get("api_base_url") or DEFAULT_API_BASE_URL
    ).rstrip("/")
    refugio_id = str(os.getenv("REFUGIO_ID") or file_config.get("refugio_id") or DEFAULT_REFUGIO_ID)
    request_timeout = float(os.getenv("REQUEST_TIMEOUT") or file_config.get("request_timeout") or 20)

    ntfy_url = os.getenv("NTFY_URL") or file_config.get("ntfy_url")
    ntfy_topic = os.getenv("NTFY_TOPIC") or file_config.get("ntfy_topic")
    if not ntfy_url and ntfy_topic:
        ntfy_url = f"https://ntfy.sh/{ntfy_topic}"

    return Config(
        tracked_dates=tracked_dates,
        alert_dates=alert_dates,
        db_path=db_path,
        api_base_url=api_base_url,
        refugio_id=refugio_id,
        request_timeout=request_timeout,
        ntfy_url=ntfy_url,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or file_config.get("telegram_bot_token"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or file_config.get("telegram_chat_id"),
        notify_provider=(os.getenv("NOTIFY_PROVIDER") or file_config.get("notify_provider") or "ntfy").lower(),
        dry_run_notifications=parse_bool(os.getenv("DRY_RUN_NOTIFICATIONS") or file_config.get("dry_run_notifications")),
    )


def parse_dates(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        dates = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        dates = [str(part).strip() for part in value]
    else:
        raise ValueError(f"{field_name} must be a list or comma-separated string")

    clean_dates = []
    for date_value in dates:
        if not date_value:
            continue
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{field_name} contains an invalid date: {date_value}") from exc
        clean_dates.append(date_value)
    return list(dict.fromkeys(clean_dates))


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS availability_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('green', 'red')),
            is_available INTEGER NOT NULL CHECK(is_available IN (0, 1)),
            available_places INTEGER NOT NULL,
            raw_payload TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_availability_snapshots_checked_date
        ON availability_snapshots(checked_at, target_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_availability_snapshots_target_checked
        ON availability_snapshots(target_date, checked_at)
        """
    )
    conn.commit()
    return conn


def fetch_availability(config: Config) -> dict[str, Any]:
    url = f"{config.api_base_url}/refugios/get/{config.refugio_id}/getPlazas2/"
    logging.info("Fetching availability from %s", url)
    response = requests.get(
        url,
        timeout=config.request_timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": "respomuso-availability-monitor/1.0",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("response"):
        raise RuntimeError(f"Unexpected API response: {payload!r}")
    return payload


def extract_availability(payload: dict[str, Any], target_dates: list[str]) -> list[Availability]:
    rooms = payload.get("result") or {}
    results: list[Availability] = []

    for target_date in target_dates:
        room_entries = []
        total_available = 0

        for room_id, room in rooms.items():
            plazas = (room or {}).get("plazas") or {}
            entry = plazas.get(target_date)
            if not entry:
                continue
            normalized_entry = dict(entry)
            normalized_entry["room_id"] = room_id
            normalized_entry["room_name"] = room.get("nombre")
            room_entries.append(normalized_entry)

            if int_or_zero(entry.get("estado")) == 1:
                total_available += max(0, int_or_zero(entry.get("plazas")))

        status = STATUS_AVAILABLE if total_available > 0 else STATUS_UNAVAILABLE
        available_places = total_available if status == STATUS_AVAILABLE else 0
        raw_payload = {"rooms": room_entries}
        results.append(
            Availability(
                target_date=target_date,
                status=status,
                available_places=available_places,
                raw_payload=raw_payload,
            )
        )

    return results


def int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def record_snapshot(conn: sqlite3.Connection, current: Availability, checked_at: str) -> None:
    raw_payload = json.dumps(current.raw_payload, ensure_ascii=False, sort_keys=True)

    conn.execute(
        """
        INSERT INTO availability_snapshots
            (checked_at, target_date, status, is_available, available_places, raw_payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            checked_at,
            current.target_date,
            current.status,
            1 if current.status == STATUS_AVAILABLE else 0,
            current.available_places,
            raw_payload,
        ),
    )
    logging.info(
        "%s snapshot recorded as %s (%s places)",
        current.target_date,
        current.status,
        current.available_places,
    )


def notify(config: Config, available_dates: list[Availability]) -> None:
    if not available_dates:
        return

    lines = [
        "Respomuso tiene disponibilidad en fechas de alerta:",
        *[
            f"- {item.target_date}: {item.available_places} plaza(s)"
            for item in available_dates
        ],
        "Reserva: https://www.alberguesyrefugios.com/respomuso/reservar",
    ]
    message = "\n".join(lines)

    if config.dry_run_notifications:
        logging.info("Dry-run notification:\n%s", message)
        return

    if config.notify_provider == "none":
        logging.info("Notifications disabled. Message would be:\n%s", message)
        return

    try:
        if config.notify_provider == "telegram":
            notify_telegram(config, message)
            return
        notify_ntfy(config, message)
    except requests.RequestException:
        logging.exception("Notification delivery failed; history was still updated")


def notify_ntfy(config: Config, message: str) -> None:
    if not config.ntfy_url:
        logging.warning("NTFY_URL or NTFY_TOPIC is not configured; skipping notification")
        return
    response = requests.post(
        config.ntfy_url,
        data=message.encode("utf-8"),
        timeout=config.request_timeout,
        headers={"Title": "Respomuso disponible", "Priority": "high"},
    )
    response.raise_for_status()
    logging.info("Sent ntfy notification")


def notify_telegram(config: Config, message: str) -> None:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logging.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured; skipping notification")
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": config.telegram_chat_id, "text": message, "disable_web_page_preview": False},
        timeout=config.request_timeout,
    )
    response.raise_for_status()
    logging.info("Sent Telegram notification")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Respomuso availability")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to YAML config")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Python logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config(args.config)
        checked_at = utc_now_iso()
        payload = fetch_availability(config)
        current_items = extract_availability(payload, config.tracked_dates)
        alert_dates = set(config.alert_dates)

        with init_db(config.db_path) as conn:
            notifications = []
            for item in current_items:
                record_snapshot(conn, item, checked_at)
                if item.status == STATUS_AVAILABLE and item.target_date in alert_dates:
                    notifications.append(item)
            conn.commit()

        notify(config, notifications)
        logging.info(
            "Finished check for %s tracked date(s); %s alert date(s)",
            len(current_items),
            len(alert_dates),
        )
        return 0
    except Exception:
        logging.exception("Availability monitor failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
