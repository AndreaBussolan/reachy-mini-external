"""Selfie Cam tool — run a countdown, capture a frame, save as JPEG."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

SELFIES_DIR = Path(os.environ.get("SELFIES_DIR", "./selfies"))


class TakeSelfie(Tool):
    """Capture a selfie with Reachy's camera after an optional countdown."""

    name = "take_selfie"
    description = (
        "Run a countdown and take a photo with Reachy's camera. "
        "Saves the picture as a JPEG file. "
        "Call this when the user asks for a selfie, a photo, or wants their picture taken. "
        "Do NOT call this for 'look at what I'm holding' or scene description — use the camera tool for those."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "countdown": {
                "type": "integer",
                "description": "Seconds to count down before capturing (default 3; use 0 for instant).",
            },
            "filename": {
                "type": "string",
                "description": "Optional custom filename without extension (e.g. 'lab_team'). "
                               "Defaults to a timestamp like '2026-11-08_15-32-07'.",
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Run a countdown, capture a JPEG frame, save it, and return metadata."""
        countdown: int = int(kwargs.get("countdown", 3))
        custom_filename: str | None = kwargs.get("filename")

        logger.info("Tool call: take_selfie countdown=%d filename=%s", countdown, custom_filename)

        # --- guard: camera must be enabled ---
        if not deps.camera_enabled:
            logger.error("take_selfie: camera is disabled")
            return {"error": "Camera disabled — cannot take selfie"}

        # --- countdown ---
        for n in range(countdown, 0, -1):
            try:
                deps.reachy_mini.audio.say(str(n))
            except Exception:
                pass  # audio.say may not exist on all setups — fail silently
            await asyncio.sleep(1)

        # --- capture frame ---
        jpeg_bytes: bytes | None = deps.reachy_mini.media.get_frame_jpeg()
        if jpeg_bytes is None:
            logger.error("take_selfie: get_frame_jpeg() returned None")
            return {"error": "No frame received from camera"}

        # --- build output path ---
        SELFIES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now()
        name = custom_filename or ts.strftime("%Y-%m-%d_%H-%M-%S")
        # sanitise filename: replace spaces and slashes
        name = name.replace(" ", "_").replace("/", "-")
        path = SELFIES_DIR / f"{name}.jpg"

        # --- save JPEG ---
        path.write_bytes(jpeg_bytes)
        logger.info("take_selfie: saved %s (%d bytes)", path, len(jpeg_bytes))

        # --- face detection (OpenCV) ---
        face_detected = False
        try:
            import cv2
            import numpy as np

            arr = np.frombuffer(jpeg_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                cascade = cv2.CascadeClassifier(cascade_path)
                faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
                face_detected = len(faces) > 0
                logger.info("take_selfie: face_detected=%s (found %d)", face_detected, len(faces))
        except Exception as exc:
            logger.warning("take_selfie: face detection failed — %s", exc)

        # --- update index.json ---
        index_path = SELFIES_DIR / "index.json"
        try:
            entries: list[dict] = []
            if index_path.exists():
                entries = json.loads(index_path.read_text())
            entries.append(
                {
                    "filename": path.name,
                    "timestamp": ts.isoformat(),
                    "face_detected": face_detected,
                }
            )
            index_path.write_text(json.dumps(entries, indent=2))
        except Exception as exc:
            logger.warning("take_selfie: could not update index.json — %s", exc)

        return {
            "saved_to": str(path),
            "timestamp": ts.isoformat(),
            "face_detected": face_detected,
            "file_size_kb": int(len(jpeg_bytes) / 1024),
        }
