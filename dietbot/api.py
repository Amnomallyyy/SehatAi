#!/usr/bin/env python3
"""
api.py - FastAPI wrapper around recommender.generate_recommendation, exposing
the HTTP contract sehatai/dietBotClient.js expects (see that file's own doc
comment):

  POST /diet  { patient_id, query, session_id? }
  -> { reply, session_id }

This file was the missing piece on the `final` branch: the DietBot branch
only ever shipped app.py (a Streamlit UI) plus the recommendation engine
itself, never the standalone HTTP service startDietBot.js/docker-compose.yml
were already built to run via `uvicorn api:app --port 8001`.

Trust model: this is an internal service. Its only legitimate caller is
sehatai-backend, which has already authenticated the patient and passes
THEIR patient_id -- DietBot itself has no notion of who is asking. So:
  - in docker-compose it is not published on a host port at all, and
  - when DIETBOT_INTERNAL_TOKEN is set, every /diet call must carry it in
    X-Internal-Token (sehatai/dietBotClient.js sends it), so even a
    reachable DietBot can't be used to read arbitrary patients' lab-based
    diet advice.
"""

import hmac
import logging
import os
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from recommender import generate_recommendation

logger = logging.getLogger("dietbot")

INTERNAL_TOKEN = os.environ.get("DIETBOT_INTERNAL_TOKEN", "")

app = FastAPI(title="DietBot API")


class DietRequest(BaseModel):
    patient_id: str
    query: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = None

    @field_validator("patient_id", "session_id")
    @classmethod
    def _must_be_uuid(cls, v: Optional[str]) -> Optional[str]:
        # Both ids end up in Supabase filters, and patient_id is also part
        # of a local session-file name (recommender.store_session_id) -- an
        # unvalidated "../../x" there was an arbitrary file write.
        if v is None:
            return v
        try:
            return str(uuid.UUID(v))
        except ValueError:
            raise ValueError("must be a UUID")


class DietResponse(BaseModel):
    reply: str
    session_id: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/diet", response_model=DietResponse)
def diet(req: DietRequest, x_internal_token: Optional[str] = Header(default=None)):
    if INTERNAL_TOKEN and not hmac.compare_digest(x_internal_token or "", INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid internal token")
    try:
        result = generate_recommendation(
            patient_id=req.patient_id,
            query=req.query,
            session_id=req.session_id,
        )
    except Exception:
        logger.exception("Diet recommendation failed for patient %s", req.patient_id)
        raise HTTPException(status_code=500, detail="Diet recommendation failed. Please try again.")

    return DietResponse(reply=result.get("recommendation", ""), session_id=result.get("session_id"))
