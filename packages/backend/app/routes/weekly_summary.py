from datetime import date
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..services.weekly_summary import weekly_summary
import logging

bp = Blueprint("weekly_summary", __name__)
logger = logging.getLogger("finmind.weekly_summary")


@bp.get("/weekly-summary")
@jwt_required()
def get_weekly_summary():
    uid = int(get_jwt_identity())
    ref = request.args.get("ref_date")
    ref_date = date.fromisoformat(ref) if ref else date.today()
    user_gemini_key = (request.headers.get("X-Gemini-Api-Key") or "").strip() or None
    user_model = (request.headers.get("X-Gemini-Model") or "").strip() or None

    summary = weekly_summary(
        uid,
        ref_date=ref_date,
        gemini_api_key=user_gemini_key,
        gemini_model=user_model,
    )
    logger.info("Weekly summary served user=%s week=%s", uid, ref_date.isoformat())
    return jsonify(summary)
