"""greet_person — face recognition + personalized greeting context for Reachy Mini.

Captures one camera frame, runs InsightFace face recognition against an enrolled
gallery, and returns context (name + description) so the LLM can compose a fresh,
personalized greeting each time.

GALLERY LAYOUT (default: greet_person_gallery/ next to this file):
  greet_person_gallery/
    people.yaml            # name, description (and optional legacy greeting hint)
    <person_id>/face.npy   # N×512 ArcFace embeddings (L2-normalised float32)

Override the gallery path with the env var GREET_PERSON_GALLERY_DIR.

INSTALLATION:
  pip install insightface onnxruntime pyyaml --break-system-packages
  (or onnxruntime-gpu for CUDA)

The InsightFace model is downloaded once on first use (~330 MB to ~/.insightface/).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEFAULT_GALLERY_DIR = Path(__file__).resolve().parent / "greet_person_gallery"
_MATCH_THRESHOLD = 0.45     # cosine similarity; tune with people.py calibrate
_MIN_MARGIN = 0.05          # best must beat runner-up by this much
_TOP_K = 3                  # average over best-k templates per person
_UNKNOWN_ID = "_unknown"
_STRANGER_DESCRIPTION = (
    "This person is not in my gallery — I haven't been introduced to them yet."
)

# ---------------------------------------------------------------------------
# Module-level singletons (loaded once, reused across calls)
# ---------------------------------------------------------------------------
_embedder = None
_gallery: dict = {}   # person_id -> (N, 512) float32 array
_people: dict = {}    # person_id -> {name, description, ...}
_gallery_dir = None


def _get_gallery_dir() -> Path:
    env = os.environ.get("GREET_PERSON_GALLERY_DIR", "").strip()
    return Path(env) if env else _DEFAULT_GALLERY_DIR


def _load_gallery(gallery_dir: Path) -> None:
    """Load face templates and people profiles from disk."""
    global _gallery, _people, _gallery_dir

    if not gallery_dir.exists():
        logger.warning("greet_person: gallery dir not found: %s", gallery_dir)
        _gallery, _people, _gallery_dir = {}, {}, gallery_dir
        return

    import yaml

    people_file = gallery_dir / "people.yaml"
    if people_file.exists():
        try:
            _people = yaml.safe_load(people_file.read_text()) or {}
        except Exception as e:
            logger.error("greet_person: failed to load people.yaml: %s", e)
            _people = {}
    else:
        _people = {}

    _gallery = {}
    for d in sorted(gallery_dir.iterdir()):
        if d.is_dir() and (d / "face.npy").exists():
            try:
                _gallery[d.name] = np.load(str(d / "face.npy"))
            except Exception as e:
                logger.warning("greet_person: failed to load %s/face.npy: %s", d.name, e)

    _gallery_dir = gallery_dir
    logger.info(
        "greet_person: loaded gallery with %d people from %s", len(_gallery), gallery_dir
    )


def _ensure_loaded() -> None:
    """Lazy-load gallery + embedder on first call."""
    global _embedder

    gdir = _get_gallery_dir()
    if _gallery_dir != gdir:
        _load_gallery(gdir)

    if _embedder is None:
        from insightface.app import FaceAnalysis  # ~1-2 s first time

        logger.info("greet_person: loading InsightFace (buffalo_l) …")
        app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=["detection", "recognition", "landmark_3d_68"],
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(640, 640))
        _embedder = app
        logger.info("greet_person: InsightFace ready.")


# ---------------------------------------------------------------------------
# Recognition helpers
# ---------------------------------------------------------------------------

def _extract_embedding(bgr: np.ndarray):
    """Return (embedding float32[512], face_detected bool)."""
    faces = _embedder.get(bgr)
    if not faces:
        return None, False

    # Pick the largest face that passes basic quality gates
    def _area(f):
        x1, y1, x2, y2 = f.bbox
        return (x2 - x1) * (y2 - y1)

    faces = sorted(faces, key=_area, reverse=True)
    for face in faces:
        x1, y1, x2, y2 = face.bbox
        size = min(x2 - x1, y2 - y1)
        det_score = float(face.det_score)
        yaw = float(face.pose[1]) if getattr(face, "pose", None) is not None else 0.0
        if det_score < 0.60:
            continue
        if size < 64:
            continue
        if abs(yaw) > 40.0:
            continue
        emb = face.normed_embedding.astype(np.float32)
        return emb, True

    # Fallback: at least a face was visible even if quality is marginal
    emb = faces[0].normed_embedding.astype(np.float32)
    return emb, True


def _match_gallery(emb: np.ndarray):
    """Return (person_id, score, runner_up_id) — person_id is None if unknown."""
    if not _gallery:
        return None, 0.0, None

    scores: dict = {}
    for pid, templates in _gallery.items():
        raw = templates @ emb                              # (N,) cosine similarities
        top = float(np.sort(raw)[::-1][: min(_TOP_K, len(raw))].mean())
        scores[pid] = top

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_id, best_score = ranked[0]
    second_id = ranked[1][0] if len(ranked) > 1 else None
    second_score = ranked[1][1] if len(ranked) > 1 else -1.0

    # Reject if below threshold or too close to runner-up
    if best_score < _MATCH_THRESHOLD:
        return None, best_score, second_id
    if second_id is not None and (best_score - second_score) < _MIN_MARGIN:
        return None, best_score, second_id

    return best_id, best_score, second_id


def _person_info(person_id: str) -> dict:
    """Return {name, description} for a known person_id."""
    profile = _people.get(person_id, {})
    name = profile.get("name") or person_id
    # Prefer an explicit 'description'; fall back to the legacy 'greeting' hint
    description = profile.get("description") or profile.get("greeting") or ""
    return {"name": name, "description": description}


# ---------------------------------------------------------------------------
# The Tool
# ---------------------------------------------------------------------------

class GreetPerson(Tool):
    """Recognise who is in front of Reachy and return context for a personalized greeting."""

    name = "greet_person"
    description = (
        "Look at the person in front of Reachy, recognise them from the enrolled gallery, "
        "and return their name and a brief description so you can greet them personally. "
        "Use this when someone approaches Reachy and you want to welcome them by name. "
        "After calling this tool, craft a warm, personalized greeting based on the returned "
        "description — do NOT repeat the description aloud, just use it as inspiration."
    )
    parameters_schema: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        if not deps.camera_enabled:
            return {
                "error": "Camera is disabled. Enable the camera to use face recognition.",
                "face_detected": False,
                "matched": False,
            }

        # 1. Capture frame
        try:
            jpeg_bytes: bytes = await asyncio.to_thread(
                deps.reachy_mini.media.get_frame_jpeg
            )
        except Exception as e:
            logger.error("greet_person: failed to capture frame: %s", e)
            return {"error": f"Camera capture failed: {e}", "face_detected": False, "matched": False}

        # 2. Decode JPEG -> BGR numpy array
        try:
            import cv2

            buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if bgr is None:
                return {"error": "Failed to decode camera frame.", "face_detected": False, "matched": False}
        except Exception as e:
            logger.error("greet_person: frame decode error: %s", e)
            return {"error": f"Frame decode error: {e}", "face_detected": False, "matched": False}

        # 3. Lazy-load InsightFace + gallery
        try:
            await asyncio.to_thread(_ensure_loaded)
        except Exception as e:
            logger.error("greet_person: failed to load face recogniser: %s", e)
            return {"error": f"Face recogniser not available: {e}", "face_detected": False, "matched": False}

        # 4. Extract embedding
        try:
            emb, face_detected = await asyncio.to_thread(_extract_embedding, bgr)
        except Exception as e:
            logger.error("greet_person: embedding extraction error: %s", e)
            return {"error": f"Face analysis error: {e}", "face_detected": False, "matched": False}

        if not face_detected or emb is None:
            return {
                "face_detected": False,
                "matched": False,
                "hint": "No face detected in frame. Ask the person to step closer or look toward the camera.",
            }

        # 5. Match against gallery
        try:
            person_id, score, runner_up = await asyncio.to_thread(_match_gallery, emb)
        except Exception as e:
            logger.error("greet_person: gallery match error: %s", e)
            return {"error": f"Gallery match error: {e}", "face_detected": True, "matched": False}

        if person_id is None:
            # Stranger — check if there's a custom _unknown profile
            unknown_profile = _people.get(_UNKNOWN_ID, {})
            unknown_name = unknown_profile.get("name", "guest")
            unknown_desc = unknown_profile.get("description") or _STRANGER_DESCRIPTION
            return {
                "face_detected": True,
                "matched": False,
                "name": unknown_name,
                "description": unknown_desc,
                "hint": (
                    "This person is not in the gallery. "
                    "Greet them warmly as a new guest and introduce yourself."
                ),
            }

        # 6. Return recognition result
        info = _person_info(person_id)
        result: dict[str, Any] = {
            "face_detected": True,
            "matched": True,
            "person_id": person_id,
            "name": info["name"],
            "confidence": round(score, 3),
        }
        if info["description"]:
            result["description"] = info["description"]

        logger.info(
            "greet_person: recognised %s (score=%.3f, runner_up=%s)",
            person_id, score, runner_up,
        )
        return result
