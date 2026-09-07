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
"""

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from recommender import generate_recommendation

app = FastAPI(title="DietBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DietRequest(BaseModel):
    patient_id: str
    query: str
    session_id: Optional[str] = None


class DietResponse(BaseModel):
    reply: str
    session_id: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/diet", response_model=DietResponse)
def diet(req: DietRequest):
    try:
        result = generate_recommendation(
            patient_id=req.patient_id,
            query=req.query,
            session_id=req.session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return DietResponse(reply=result.get("recommendation", ""), session_id=result.get("session_id"))
