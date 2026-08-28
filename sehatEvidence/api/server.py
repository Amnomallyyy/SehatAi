"""
api/server.py -- EvidenceBoard HTTP surface (Phase 3b).

A dependency-free server: Python's stdlib ``ThreadingHTTPServer`` plus the
project's own modules. No flask, no fastapi, no uvicorn -- and no CDN, no
npm, no webfonts on the UI side either. The whole product (API + single
page app) ships in this one file so a demo machine with no network still
serves a working interface.

Endpoints
---------
GET  /            embedded single-page UI (inline CSS + JS, system fonts)
POST /api/ask     {"question": "..."} -> the pipeline's full report dict
GET  /api/health  liveness + which models/keys this process is configured with

Design notes
------------
The pipeline is built ONCE in :func:`main` and shared by every request
thread (the agents are stateless; the FailoverLLMClient's key rotation is
process-wide on purpose -- a key that dies stays retired for everyone).
:meth:`EvidencePipeline.run` never raises and reports abstention as a
successful outcome, so ``/api/ask`` returns 200 with ``abstained=true``
rather than an error status; 500 is reserved for genuine server faults.

``--mock`` makes every /api/ask replay demo/mock_response.json instead of
calling the network -- the offline demo path.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# Allow both `python -m api.server` (from sehatEvidence/) and
# `python api/server.py` (any working directory) to import the project.
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DISCLAIMER, Settings, get_settings  # noqa: E402
from pipeline import EvidencePipeline, build_default_pipeline  # noqa: E402

__all__ = ["EvidenceHandler", "build_index_html", "main", "run_server"]

#: Refuse absurd request bodies outright (a clinical question is a sentence).
MAX_BODY_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# The embedded single-page UI
# ---------------------------------------------------------------------------
#
# One template string, two placeholders ({{DISCLAIMER}}, {{POOL_CAP}}),
# substituted by build_index_html() so config.DISCLAIMER and
# Settings.pool_cap stay the single source of truth for what the page
# claims. Everything else -- layout, palette, motion -- is inline: this
# page must render identically on an air-gapped laptop.

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EvidenceBoard &middot; verification-first clinical evidence</title>
<style>
  :root {
    --ink: #eaeef7;
    --ink-soft: #a7b1c6;
    --ink-faint: #6b7690;
    --paper: #060911;
    --card: #101726;
    --rule: #242e45;
    --rule-soft: #161d2e;
    --accent: #4f8dfd;
    --accent-2: #22d3ee;
    --accent-soft: rgba(79, 141, 253, .14);
    --pass: #34d399;
    --pass-bg: rgba(52, 211, 153, .12);
    --fail: #f87171;
    --fail-bg: rgba(248, 113, 113, .12);
    --flag: #fbbf24;
    --flag-bg: rgba(251, 191, 36, .12);
    --skip-bg: #161d2e;
    --info: #38bdf8;
    --info-bg: rgba(56, 189, 248, .12);
    --shadow: 0 1px 2px rgba(0, 0, 0, .5), 0 16px 40px -16px rgba(0, 0, 0, .65);
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }

  * { box-sizing: border-box; }

  html { -webkit-text-size-adjust: 100%; }

  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    color: var(--ink);
    background-color: var(--paper);
    /* Faint clinical-chart grid over a soft top glow: atmosphere, zero assets. */
    background-image:
      radial-gradient(720px 420px at 18% -8%, rgba(79, 141, 253, .16), transparent 60%),
      radial-gradient(640px 380px at 92% 0%, rgba(34, 211, 238, .10), transparent 55%),
      linear-gradient(var(--rule-soft) 1px, transparent 1px),
      linear-gradient(90deg, var(--rule-soft) 1px, transparent 1px);
    background-size: 100% 100%, 100% 100%, 100% 34px, 34px 100%;
    background-position: 0 0, 0 0, -1px -1px, -1px -1px;
    background-repeat: no-repeat, no-repeat, repeat, repeat;
  }

  .shell { max-width: 1080px; margin: 0 auto; padding: 0 24px 96px; }

  /* --- header ------------------------------------------------------- */

  header.masthead {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    flex-wrap: wrap;
    padding: 44px 0 18px;
    border-bottom: 1px solid var(--rule);
  }
  .wordmark {
    margin: 0;
    font-size: 34px;
    font-weight: 700;
    letter-spacing: -.025em;
    line-height: 1.05;
  }
  .wordmark .dot {
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .tagline {
    margin: 6px 0 0;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .status-dot {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: .06em;
    color: var(--ink-faint);
    padding-bottom: 6px;
  }
  .status-dot i {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--ink-faint);
    box-shadow: 0 0 0 3px var(--rule-soft);
  }
  .status-dot.live i { background: var(--pass); box-shadow: 0 0 0 3px var(--pass-bg); animation: pulse-dot 2s ease-in-out infinite; }
  .status-dot.down i { background: var(--fail); box-shadow: 0 0 0 3px var(--fail-bg); }
  @keyframes pulse-dot {
    0%, 100% { opacity: 1; } 50% { opacity: .45; }
  }

  /* --- stat strip: what this system actually is, in numbers ---------- */

  .stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    margin: 22px 0 0;
    background: var(--rule);
    border: 1px solid var(--rule);
    border-radius: 4px;
    overflow: hidden;
  }
  .stat {
    padding: 16px 18px;
    background: var(--card);
  }
  .stat b {
    display: block;
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -.02em;
    background: linear-gradient(135deg, var(--ink) 30%, var(--accent-2));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .stat span {
    display: block;
    margin-top: 4px;
    font-size: 11px;
    color: var(--ink-faint);
    letter-spacing: .02em;
  }
  @media (max-width: 620px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
  }

  /* --- disclaimer strip --------------------------------------------- */

  .disclaimer {
    margin: 0;
    padding: 12px 16px;
    background: var(--skip-bg);
    border: 1px solid var(--rule);
    border-top: none;
    font-size: 11.5px;
    line-height: 1.5;
    color: var(--ink-soft);
  }
  .disclaimer b {
    display: block;
    font-size: 10px;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-bottom: 3px;
  }

  /* --- query form ---------------------------------------------------- */

  form.ask {
    display: flex;
    gap: 10px;
    margin: 32px 0 0;
    flex-wrap: wrap;
  }
  .field { flex: 1 1 340px; position: relative; }
  label.micro {
    display: block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-bottom: 7px;
  }
  input[type=text] {
    width: 100%;
    padding: 14px 16px;
    font: inherit;
    color: var(--ink);
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 3px;
    box-shadow: var(--shadow);
    transition: border-color .16s ease, box-shadow .16s ease;
  }
  input[type=text]::placeholder { color: var(--ink-faint); }
  input[type=text]:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  button.primary {
    align-self: flex-end;
    min-width: 132px;
    padding: 14px 22px;
    font: inherit;
    font-weight: 600;
    letter-spacing: .02em;
    color: #04101f;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    border: none;
    border-radius: 3px;
    cursor: pointer;
    box-shadow: 0 8px 20px -8px rgba(79, 141, 253, .55);
    transition: filter .16s ease, transform .16s ease;
  }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); }
  button.primary:active:not(:disabled) { transform: translateY(1px); }
  button.primary:disabled { opacity: .55; cursor: progress; box-shadow: none; }
  .spinner {
    display: inline-block;
    width: 12px; height: 12px;
    margin-right: 8px;
    vertical-align: -1px;
    border: 2px solid rgba(4, 16, 31, .3);
    border-top-color: #04101f;
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .seeds { margin: 14px 0 0; font-size: 12px; color: var(--ink-faint); }
  .seeds button {
    font: inherit;
    color: var(--accent);
    background: none;
    border: none;
    border-bottom: 1px dotted currentColor;
    padding: 0;
    margin-right: 14px;
    cursor: pointer;
  }
  .seeds button:hover { color: var(--ink); }

  /* --- progress / errors --------------------------------------------- */

  .working {
    margin-top: 28px;
    padding: 22px 24px 18px;
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 4px;
    box-shadow: var(--shadow);
  }
  .working-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
  }
  .working-head b {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .working-head span {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-faint);
  }
  .stage-track { display: flex; flex-direction: column; gap: 2px; }
  .stage {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 9px 6px;
    border-radius: 3px;
    opacity: .4;
    transition: opacity .25s ease;
  }
  .stage.is-active, .stage.is-done { opacity: 1; }
  .stage-icon {
    flex: none;
    width: 22px; height: 22px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    border: 1.5px solid var(--rule);
    font-family: var(--mono);
    font-size: 10.5px;
    color: var(--ink-faint);
    background: var(--paper);
    transition: border-color .2s ease, background .2s ease, box-shadow .2s ease;
  }
  .stage.is-active .stage-icon {
    border-color: var(--accent);
    background: var(--accent-soft);
    box-shadow: 0 0 0 4px var(--accent-soft);
  }
  .stage.is-active .stage-icon i {
    width: 8px; height: 8px; border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    animation: pulse-dot 1s ease-in-out infinite;
  }
  .stage.is-done .stage-icon {
    border-color: var(--pass);
    background: var(--pass-bg);
    color: var(--pass);
  }
  .stage-body { flex: 1; min-width: 0; }
  .stage-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--ink-soft);
  }
  .stage.is-active .stage-label { color: var(--accent-2); }
  .stage.is-done .stage-label { color: var(--ink); }
  .stage-detail {
    margin-top: 1px;
    font-size: 11.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .working .bar {
    height: 3px;
    margin-top: 18px;
    border-radius: 2px;
    background: var(--rule-soft);
    overflow: hidden;
  }
  .working .bar i {
    display: block;
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width .5s cubic-bezier(.2, .7, .3, 1);
  }
  .error-box {
    margin-top: 28px;
    padding: 16px 18px;
    background: var(--fail-bg);
    border: 1px solid rgba(248, 113, 113, .3);
    border-left: 3px solid var(--fail);
    border-radius: 3px;
    color: #fecaca;
    font-size: 13.5px;
  }

  /* --- results ------------------------------------------------------- */

  .results { margin-top: 36px; }
  .results > * { animation: rise .5s cubic-bezier(.2, .7, .3, 1) both; }
  .results > *:nth-child(1) { animation-delay: .02s; }
  .results > *:nth-child(2) { animation-delay: .08s; }
  .results > *:nth-child(3) { animation-delay: .14s; }
  .results > *:nth-child(4) { animation-delay: .20s; }
  .results > *:nth-child(5) { animation-delay: .26s; }
  .results > *:nth-child(6) { animation-delay: .32s; }
  @keyframes rise {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: none; }
  }

  section { margin-bottom: 34px; }
  h2.section-title {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 0 0 14px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .18em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  h2.section-title::after {
    content: "";
    flex: 1;
    height: 1px;
    background: var(--rule);
  }
  h2.section-title .count {
    font-family: var(--mono);
    letter-spacing: 0;
    color: var(--ink-soft);
  }

  .asked {
    margin: 0 0 26px;
    font-size: 19px;
    font-weight: 600;
    letter-spacing: -.01em;
    line-height: 1.4;
  }
  .asked span {
    display: block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-bottom: 6px;
  }

  /* funnel */
  .funnel { background: var(--card); border: 1px solid var(--rule); border-radius: 3px;
            box-shadow: var(--shadow); padding: 18px 20px; }
  .funnel-line { font-family: var(--mono); font-size: 13px; color: var(--ink-soft); }
  .funnel-line b { color: var(--ink); font-weight: 600; }
  .funnel-line .arrow { color: var(--ink-faint); margin: 0 6px; }
  .funnel-track {
    display: flex;
    height: 10px;
    margin-top: 12px;
    border-radius: 2px;
    overflow: hidden;
    background: var(--skip-bg);
  }
  .funnel-track i { height: 100%; transition: width .6s cubic-bezier(.2, .7, .3, 1); }
  .funnel-track .seg-kept { background: var(--pass); }
  .funnel-track .seg-del  { background: var(--fail); }
  .funnel-legend {
    display: flex; flex-wrap: wrap; gap: 16px;
    margin-top: 10px; font-size: 11px; color: var(--ink-faint);
  }
  .funnel-legend em { font-style: normal; font-family: var(--mono); color: var(--ink-soft); }
  .swatch { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }
  .sw-kept { background: var(--pass); }
  .sw-del { background: var(--fail); }
  .sw-gen { background: var(--info); }
  .by-reason { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--rule);
               font-size: 12px; color: var(--ink-soft); }
  .by-reason div { display: flex; justify-content: space-between; gap: 12px; padding: 2px 0; }
  .by-reason span:last-child { font-family: var(--mono); color: var(--fail); }

  /* answer */
  .answer {
    background: var(--card);
    border: 1px solid var(--rule);
    border-left: 3px solid var(--accent);
    border-radius: 3px;
    box-shadow: var(--shadow);
    padding: 24px 26px;
    font-size: 16.5px;
    line-height: 1.72;
  }
  .answer .sid {
    font-family: var(--mono);
    font-size: 11.5px;
    font-weight: 600;
    color: var(--accent);
    background: var(--accent-soft);
    padding: 1px 5px;
    border-radius: 2px;
    white-space: nowrap;
  }

  /* abstention */
  .abstain {
    background: var(--flag-bg);
    border: 1px solid rgba(251, 191, 36, .3);
    border-left: 3px solid var(--flag);
    border-radius: 3px;
    padding: 20px 22px;
  }
  .abstain h3 {
    margin: 0 0 8px;
    font-size: 15px;
    color: #fcd34d;
    letter-spacing: -.01em;
  }
  .abstain p { margin: 0 0 10px; font-size: 13.5px; color: #fde68a; }
  .abstain ul { margin: 0; padding-left: 20px; font-size: 13.5px; color: #fde68a; }
  .abstain li { margin: 3px 0; }

  /* claim cards */
  .claim {
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 3px;
    box-shadow: var(--shadow);
    padding: 18px 20px;
    margin-bottom: 12px;
  }
  .claim.flagged { border-left: 3px solid var(--flag); }
  .claim.kept { border-left: 3px solid var(--pass); }
  .claim-head {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; margin-bottom: 8px;
  }
  .claim-id { font-family: var(--mono); font-size: 11px; color: var(--ink-faint); }
  .claim-text { margin: 0 0 12px; font-size: 15px; line-height: 1.6; }
  .checks { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 12px; }
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px;
    border-radius: 2px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .02em;
    border: 1px solid transparent;
  }
  .badge .mark { font-family: var(--mono); }
  .b-pass { background: var(--pass-bg); color: var(--pass); border-color: rgba(52, 211, 153, .35); }
  .b-fail { background: var(--fail-bg); color: var(--fail); border-color: rgba(248, 113, 113, .35); }
  .b-flag { background: var(--flag-bg); color: var(--flag); border-color: rgba(251, 191, 36, .35); }
  .b-skip { background: var(--skip-bg); color: var(--ink-faint); border-color: var(--rule); }
  .b-info { background: var(--info-bg); color: var(--info); border-color: rgba(56, 189, 248, .35); }
  .verdict {
    font-family: var(--mono); font-size: 11px; color: var(--ink-soft);
    margin-bottom: 10px;
  }
  .verdict .conf { color: var(--ink-faint); }
  blockquote.quote {
    margin: 0 0 12px;
    padding: 10px 14px;
    background: #0c1220;
    border-left: 2px solid var(--rule);
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink-soft);
    font-style: italic;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  a.chip {
    display: inline-flex; align-items: center; gap: 6px;
    max-width: 100%;
    padding: 3px 9px;
    font-size: 11.5px;
    font-family: var(--mono);
    color: var(--ink-soft);
    text-decoration: none;
    background: var(--paper);
    border: 1px solid var(--rule);
    border-radius: 2px;
    transition: border-color .15s ease, color .15s ease, background .15s ease;
  }
  a.chip:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  a.chip .chip-sid { font-weight: 600; color: var(--accent); }
  .flags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .flag-badge {
    padding: 3px 9px;
    font-size: 11px;
    border-radius: 2px;
    background: rgba(251, 191, 36, .12);
    color: #fcd34d;
    border: 1px solid rgba(251, 191, 36, .3);
  }

  /* deleted claims */
  details.deleted-wrap {
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 3px;
    box-shadow: var(--shadow);
    padding: 0;
  }
  details.deleted-wrap > summary {
    cursor: pointer;
    padding: 14px 18px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--ink-soft);
    list-style: none;
    display: flex; align-items: center; gap: 10px;
  }
  details.deleted-wrap > summary::-webkit-details-marker { display: none; }
  details.deleted-wrap > summary::before {
    content: "+";
    font-family: var(--mono);
    color: var(--fail);
    font-size: 14px;
  }
  details.deleted-wrap[open] > summary::before { content: "\\2212"; }
  details.deleted-wrap > summary:hover { color: var(--ink); }
  .deleted-body { padding: 0 18px 6px; border-top: 1px dashed var(--rule); }
  .deleted-row { padding: 12px 0; border-bottom: 1px dashed var(--rule-soft); }
  .deleted-row:last-child { border-bottom: none; }
  .deleted-row p {
    margin: 0 0 5px;
    font-size: 13.5px;
    color: var(--ink-faint);
    text-decoration: line-through;
    text-decoration-color: rgba(248, 113, 113, .45);
  }
  .deleted-why {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--fail);
  }
  .deleted-why::before { content: "deleted \\2014 "; color: var(--ink-faint); }

  /* evidence */
  ol.evidence { list-style: none; margin: 0; padding: 0; counter-reset: none; }
  li.ev {
    display: grid;
    grid-template-columns: 52px 1fr auto;
    gap: 14px;
    align-items: start;
    padding: 14px 18px;
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 3px;
    margin-bottom: 8px;
    box-shadow: var(--shadow);
  }
  li.ev.retracted { border-left: 3px solid var(--fail); background: rgba(248, 113, 113, .07); }
  .ev-sid {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    padding-top: 2px;
  }
  .ev-title { font-size: 14.5px; line-height: 1.5; margin: 0 0 5px; }
  .ev-title a { color: var(--ink); text-decoration: none; border-bottom: 1px solid var(--rule); }
  .ev-title a:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .ev-meta {
    font-size: 11.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
    display: flex; flex-wrap: wrap; gap: 4px 10px;
  }
  .ev-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .ev-score {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    background: var(--accent-soft);
    border: 1px solid rgba(79, 141, 253, .3);
    border-radius: 2px;
    padding: 3px 8px;
    white-space: nowrap;
  }
  .ev-score.mid { color: var(--flag); background: var(--flag-bg); border-color: rgba(251, 191, 36, .3); }
  .ev-score.low { color: var(--ink-faint); background: var(--skip-bg); border-color: var(--rule); }

  .empty {
    padding: 16px 18px;
    background: var(--card);
    border: 1px dashed var(--rule);
    border-radius: 3px;
    font-size: 13px;
    color: var(--ink-faint);
  }

  footer.foot {
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid var(--rule);
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-faint);
    display: flex; flex-wrap: wrap; gap: 6px 18px; justify-content: space-between;
  }

  @media (max-width: 620px) {
    .shell { padding: 0 16px 64px; }
    .wordmark { font-size: 27px; }
    button.primary { width: 100%; }
    li.ev { grid-template-columns: 44px 1fr; }
    li.ev .ev-score { grid-column: 2; justify-self: start; }
  }

  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
</style>
</head>
<body>
<div class="shell">

  <header class="masthead">
    <div>
      <h1 class="wordmark">EvidenceBoard<span class="dot">.</span></h1>
      <p class="tagline">Verification-first clinical evidence</p>
    </div>
    <div class="status-dot" id="health"><i></i><span>checking service&hellip;</span></div>
  </header>

  <div class="stats">
    <div class="stat"><b>5</b><span>agents in the pipeline</span></div>
    <div class="stat"><b>3</b><span>sources &middot; PubMed, Europe&nbsp;PMC, CT.gov</span></div>
    <div class="stat"><b>3</b><span>checks per claim &middot; exists, entails, stands</span></div>
    <div class="stat"><b>{{POOL_CAP}}</b><span>records ranked per question</span></div>
  </div>

  <p class="disclaimer"><b>Disclaimer</b>{{DISCLAIMER}}</p>

  <form class="ask" id="ask-form" autocomplete="off">
    <div class="field">
      <label class="micro" for="q">Clinical question</label>
      <input type="text" id="q" name="q"
             placeholder="e.g. Does metformin reduce all-cause mortality in type 2 diabetes?"
             required>
    </div>
    <button class="primary" id="ask-btn" type="submit">Ask</button>
  </form>

  <p class="seeds" id="seeds"></p>

  <div id="working" class="working" hidden>
    <div class="working-head">
      <b>Pipeline running</b>
      <span id="working-elapsed">0.0s</span>
    </div>
    <div class="stage-track" id="stage-track"></div>
    <div class="bar"><i id="working-bar"></i></div>
  </div>

  <div id="error" class="error-box" hidden></div>

  <div class="results" id="results" hidden></div>

  <footer class="foot">
    <span>EvidenceBoard &middot; stdlib server, no external assets</span>
    <span id="foot-model"></span>
  </footer>

</div>

<script>
(function () {
  "use strict";

  var form = document.getElementById("ask-form");
  var input = document.getElementById("q");
  var button = document.getElementById("ask-btn");
  var working = document.getElementById("working");
  var stageTrack = document.getElementById("stage-track");
  var workingBar = document.getElementById("working-bar");
  var workingElapsed = document.getElementById("working-elapsed");
  var errorBox = document.getElementById("error");
  var results = document.getElementById("results");
  var health = document.getElementById("health");
  var footModel = document.getElementById("foot-model");
  var seeds = document.getElementById("seeds");
  var busy = false;

  /* The six pipeline stages, in run order (see pipeline.py). Each agent
     really does run in this sequence; what is simulated here is only the
     PACING of the reveal (the API call is one blocking request, not a
     progress stream), never the stage list or the final results. */
  var STAGES = [
    { label: "Strategist", detail: "Planning 3\\u20135 targeted search queries" },
    { label: "Retrieval", detail: "Querying PubMed, Europe PMC, ClinicalTrials.gov \\u00b7 checking retractions" },
    { label: "Appraiser", detail: "Scoring evidence by design, recency & relevance" },
    { label: "Synthesizer", detail: "Drafting a fully-cited answer" },
    { label: "Verifier", detail: "Checking existence, entailment & standing of every claim" },
    { label: "Red Team", detail: "Adversarial audit for weak or risky claims" }
  ];

  var SEED_QUESTIONS = [
    "Does metformin reduce all-cause mortality in type 2 diabetes?",
    "Is tranexamic acid effective in traumatic brain injury?",
    "Do SGLT2 inhibitors prevent heart failure hospitalisation in CKD?"
  ];

  // --- helpers -----------------------------------------------------------

  function esc(value) {
    if (value === null || value === undefined) { return ""; }
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(value) {
    return typeof value === "number" && isFinite(value) ? value : 0;
  }

  /* Best available public URL for a record: the source's own url wins,
     then the DOI, then a registry URL rebuilt from the native id. */
  function linkFor(item) {
    if (!item) { return ""; }
    if (item.url) { return String(item.url); }
    if (item.doi) { return "https://doi.org/" + encodeURIComponent(item.doi); }
    var id = item.native_id ? String(item.native_id) : "";
    if (!id) { return ""; }
    var source = (item.source || "").toLowerCase();
    if (source.indexOf("clinicaltrials") !== -1 || /^NCT/i.test(id)) {
      return "https://clinicaltrials.gov/study/" + encodeURIComponent(id);
    }
    if (source.indexOf("europepmc") !== -1 && !/^\\d+$/.test(id)) {
      return "https://europepmc.org/article/MED/" + encodeURIComponent(id);
    }
    return "https://pubmed.ncbi.nlm.nih.gov/" + encodeURIComponent(id) + "/";
  }

  /* Wrap [S3] / [S3, S7] citation markers so the answer reads as data. */
  function markSids(text) {
    return esc(text).replace(/\\[(S\\d+(?:\\s*,\\s*S\\d+)*)\\]/g,
      function (match) { return '<span class="sid">' + match + "</span>"; });
  }

  function badge(kind, label, mark) {
    return '<span class="badge b-' + kind + '"><span class="mark">' + mark +
           "</span>" + esc(label) + "</span>";
  }

  /* The three verification checks use different vocabularies (the
     entailment judge answers supports/refutes/nei), so map each to a
     colour + glyph rather than assuming a shared pass/fail wording. */
  function checkBadge(name, outcome) {
    var value = (outcome || "skipped").toLowerCase();
    var kind = "skip";
    var mark = "\\u2013";
    if (value === "pass" || value === "supports") { kind = "pass"; mark = "\\u2713"; }
    else if (value === "fail" || value === "refutes") { kind = "fail"; mark = "\\u2717"; }
    else if (value === "flag" || value === "nei") { kind = "flag"; mark = "!"; }
    return badge(kind, name + " \\u00b7 " + value, mark);
  }

  // --- rendering ---------------------------------------------------------

  function renderFunnel(funnel) {
    var generated = num(funnel.claims_generated);
    var deleted = num(funnel.claims_deleted);
    var kept = num(funnel.claims_kept);
    var total = generated > 0 ? generated : (deleted + kept);
    var keptPct = total ? (kept / total) * 100 : 0;
    var delPct = total ? (deleted / total) * 100 : 0;

    var reasons = funnel.by_reason && typeof funnel.by_reason === "object"
      ? Object.keys(funnel.by_reason) : [];
    var reasonHtml = "";
    if (reasons.length) {
      reasonHtml = '<div class="by-reason">' + reasons.map(function (reason) {
        return "<div><span>" + esc(reason) + "</span><span>&times;" +
               esc(funnel.by_reason[reason]) + "</span></div>";
      }).join("") + "</div>";
    }

    return '<section><h2 class="section-title">Verification funnel</h2>' +
      '<div class="funnel">' +
        '<div class="funnel-line"><b>' + generated + "</b> claims generated" +
          '<span class="arrow">&rarr;</span><b>' + deleted + "</b> deleted" +
          '<span class="arrow">&rarr;</span><b>' + kept + "</b> shown</div>" +
        '<div class="funnel-track">' +
          '<i class="seg-kept" style="width:' + keptPct.toFixed(1) + '%"></i>' +
          '<i class="seg-del" style="width:' + delPct.toFixed(1) + '%"></i>' +
        "</div>" +
        '<div class="funnel-legend">' +
          '<span><i class="swatch sw-gen"></i>generated <em>' + generated + "</em></span>" +
          '<span><i class="swatch sw-del"></i>deleted <em>' + deleted + "</em></span>" +
          '<span><i class="swatch sw-kept"></i>shown <em>' + kept + "</em></span>" +
        "</div>" + reasonHtml +
      "</div></section>";
  }

  function renderAbstain(report) {
    var reasons = report.abstain_reasons || [];
    var items = reasons.length
      ? reasons.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("")
      : "<li>no reason recorded</li>";
    return '<section><div class="abstain">' +
      "<h3>No answer given &mdash; the evidence did not support one</h3>" +
      "<p>EvidenceBoard abstains rather than answering from thin or " +
      "unverifiable evidence. The pool it did find is listed below.</p>" +
      "<ul>" + items + "</ul>" +
      "</div></section>";
  }

  function renderAnswer(report) {
    var text = (report.answer_text || "").trim();
    if (!text) { return ""; }
    return '<section><h2 class="section-title">Answer</h2>' +
      '<div class="answer">' + markSids(text) + "</div></section>";
  }

  function renderCitations(citations) {
    if (!citations || !citations.length) { return ""; }
    return '<div class="chips">' + citations.map(function (c) {
      var label = c.citation_key || c.sid || "source";
      var url = linkFor(c);
      var inner = '<span class="chip-sid">' + esc(c.sid || "") + "</span>" + esc(label);
      if (!url) { return '<span class="chip">' + inner + "</span>"; }
      return '<a class="chip" href="' + esc(url) + '" target="_blank" ' +
             'rel="noopener noreferrer" title="' + esc(c.title || label) + '">' +
             inner + "</a>";
    }).join("") + "</div>";
  }

  function renderClaim(claim) {
    var checks = claim.checks || {};
    var conf = typeof claim.confidence === "number"
      ? ' <span class="conf">confidence ' + claim.confidence.toFixed(2) + "</span>" : "";
    var verdict = claim.verdict
      ? '<div class="verdict">verdict: ' + esc(claim.verdict) + conf + "</div>" : "";
    var quote = claim.evidence_quote
      ? '<blockquote class="quote">' + esc(claim.evidence_quote) + "</blockquote>" : "";
    var flags = (claim.flags && claim.flags.length)
      ? '<div class="flags">' + claim.flags.map(function (f) {
          return '<span class="flag-badge">' + esc(f) + "</span>";
        }).join("") + "</div>"
      : "";
    var statusBadge = claim.status === "flagged"
      ? badge("flag", "flagged", "!")
      : badge("pass", "kept", "\\u2713");

    return '<article class="claim ' + esc(claim.status || "kept") + '">' +
      '<div class="claim-head">' +
        '<div class="checks">' + statusBadge + "</div>" +
        '<div class="claim-id">' + esc(claim.claim_id || "") + "</div>" +
      "</div>" +
      '<p class="claim-text">' + markSids(claim.text) + "</p>" +
      '<div class="checks">' +
        checkBadge("existence", checks.existence) +
        checkBadge("entailment", checks.entailment) +
        checkBadge("standing", checks.standing) +
      "</div>" +
      verdict + quote + renderCitations(claim.citations) + flags +
      "</article>";
  }

  function renderKept(claims) {
    if (!claims.length) { return ""; }
    return '<section><h2 class="section-title">Verified claims ' +
      '<span class="count">' + claims.length + "</span></h2>" +
      claims.map(renderClaim).join("") + "</section>";
  }

  function renderDeleted(claims) {
    if (!claims.length) { return ""; }
    var rows = claims.map(function (claim) {
      return '<div class="deleted-row"><p>' + esc(claim.text) + "</p>" +
        '<div class="deleted-why">' + esc(claim.deletion_reason || "unspecified") +
        "</div></div>";
    }).join("");
    return "<section><details class=\\"deleted-wrap\\"><summary>" +
      claims.length + (claims.length === 1 ? " claim was" : " claims were") +
      " deleted by verification" +
      '</summary><div class="deleted-body">' + rows + "</div></details></section>";
  }

  function renderEvidence(evidence) {
    if (!evidence.length) {
      return '<section><h2 class="section-title">Evidence pool</h2>' +
        '<div class="empty">No records were retrieved for this question.</div></section>';
    }
    var rows = evidence.map(function (item) {
      var score = num(item.relevance_score);
      var scoreClass = score >= 60 ? "" : (score >= 30 ? " mid" : " low");
      var url = linkFor(item);
      var title = esc(item.title || "(untitled record)");
      var titleHtml = url
        ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' +
          title + "</a>"
        : title;

      var meta = [];
      if (item.journal) { meta.push("<span>" + esc(item.journal) + "</span>"); }
      if (item.publication_date) { meta.push("<span>" + esc(item.publication_date) + "</span>"); }
      if (item.study_design) { meta.push("<span>" + esc(item.study_design) + "</span>"); }
      if (item.citation_key) { meta.push("<span>" + esc(item.citation_key) + "</span>"); }

      var tags = [];
      if (item.is_retracted) { tags.push(badge("fail", "retracted", "\\u2717")); }
      if (item.is_preprint) { tags.push(badge("flag", "preprint \\u00b7 not peer reviewed", "!")); }
      if (item.trial_status) { tags.push(badge("info", "trial: " + item.trial_status, "\\u25cf")); }

      return '<li class="ev' + (item.is_retracted ? " retracted" : "") + '">' +
        '<div class="ev-sid">' + esc(item.sid || "") + "</div>" +
        "<div>" +
          '<p class="ev-title">' + titleHtml + "</p>" +
          '<div class="ev-meta">' + meta.join("") + "</div>" +
          (tags.length ? '<div class="ev-tags">' + tags.join("") + "</div>" : "") +
        "</div>" +
        '<div class="ev-score' + scoreClass + '">' + score + "</div>" +
        "</li>";
    }).join("");
    return '<section><h2 class="section-title">Evidence pool ' +
      '<span class="count">' + evidence.length + " ranked</span></h2>" +
      '<ol class="evidence">' + rows + "</ol></section>";
  }

  function render(report) {
    var claims = report.claims || [];
    var kept = claims.filter(function (c) {
      return c.status === "kept" || c.status === "flagged";
    });
    var deleted = claims.filter(function (c) { return c.status === "deleted"; });

    var html = '<p class="asked"><span>Question</span>' + esc(report.question) + "</p>";
    html += report.abstained ? renderAbstain(report) : renderAnswer(report);
    html += renderFunnel(report.funnel || {});
    html += renderKept(kept);
    html += renderDeleted(deleted);
    html += renderEvidence(report.evidence || []);

    results.innerHTML = html;
    results.hidden = false;
  }

  // --- live stage tracker --------------------------------------------------
  //
  // The API is one blocking POST /api/ask (30-120s live), not a progress
  // stream, so there is no server signal per agent. What follows PACES a
  // reveal of the real, fixed stage order from pipeline.py -- it never
  // fabricates counts or outcomes; those only appear once the actual
  // report renders. The final stage is deliberately never auto-completed:
  // only the real response resolves it.

  var STAGE_CUMULATIVE_MS = (function () {
    var durations = [1100, 2200, 1500, 1700, 2600, 1300];
    var sum = 0;
    return durations.map(function (d) { sum += d; return sum; });
  })();

  function renderStages(currentStage) {
    stageTrack.innerHTML = STAGES.map(function (stage, i) {
      var state = i < currentStage ? "done" : (i === currentStage ? "active" : "pending");
      var num = (i + 1 < 10 ? "0" : "") + (i + 1);
      var icon = state === "done" ? "\\u2713" : (state === "active" ? "<i></i>" : num);
      return '<div class="stage is-' + state + '">' +
        '<div class="stage-icon">' + icon + "</div>" +
        '<div class="stage-body">' +
          '<div class="stage-label">' + esc(stage.label) + "</div>" +
          '<div class="stage-detail">' + esc(stage.detail) + "</div>" +
        "</div></div>";
    }).join("");
  }

  function startStageTracker() {
    var startTime = Date.now();
    var currentStage = 0;
    var finished = false;
    renderStages(currentStage);
    workingBar.style.width = "4%";
    workingElapsed.textContent = "0.0s";

    var timer = setInterval(function () {
      if (finished) { return; }
      var elapsed = Date.now() - startTime;
      workingElapsed.textContent = (elapsed / 1000).toFixed(1) + "s";
      var target = 0;
      for (var i = 0; i < STAGE_CUMULATIVE_MS.length; i++) {
        if (elapsed >= STAGE_CUMULATIVE_MS[i]) { target = i + 1; }
      }
      target = Math.min(target, STAGES.length - 1); // never auto-finish the last stage
      if (target !== currentStage) {
        currentStage = target;
        renderStages(currentStage);
      }
      workingBar.style.width = Math.min(96, 6 + (currentStage / STAGES.length) * 90) + "%";
    }, 100);

    return {
      finish: function () {
        finished = true;
        clearInterval(timer);
        renderStages(STAGES.length);
        workingBar.style.width = "100%";
        return new Promise(function (resolve) { setTimeout(resolve, 420); });
      },
      stop: function () {
        finished = true;
        clearInterval(timer);
      }
    };
  }

  // --- request lifecycle -------------------------------------------------

  var activeTracker = null;

  function setBusy(state) {
    busy = state;
    button.disabled = state;
    button.innerHTML = state ? '<span class="spinner"></span>Working' : "Ask";
    working.hidden = !state;
    if (state) {
      activeTracker = startStageTracker();
    } else if (activeTracker) {
      activeTracker.stop();
      activeTracker = null;
    }
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function ask(question) {
    if (busy) { return; }
    errorBox.hidden = true;
    results.hidden = true;
    results.innerHTML = "";
    setBusy(true);
    var tracker = activeTracker;

    fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question })
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      }).catch(function () {
        throw new Error("Server returned a non-JSON response (HTTP " +
                        response.status + ").");
      });
    }).then(function (result) {
      if (!result.ok) {
        throw new Error((result.data && result.data.error) ||
                        ("Request failed with HTTP " + result.status + "."));
      }
      return (tracker ? tracker.finish() : Promise.resolve()).then(function () {
        render(result.data);
      });
    }).catch(function (err) {
      if (tracker) { tracker.stop(); }
      showError(err && err.message ? err.message : "Request failed.");
    }).then(function () {
      setBusy(false);
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var question = input.value.trim();
    if (!question) {
      showError("Enter a clinical question first.");
      return;
    }
    ask(question);
  });

  SEED_QUESTIONS.forEach(function (question, index) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = index === 0 ? "Try: " + question : question;
    b.addEventListener("click", function () {
      input.value = question;
      input.focus();
    });
    seeds.appendChild(b);
  });

  // --- health probe ------------------------------------------------------

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
    health.className = "status-dot live";
    health.innerHTML = "<i></i><span>" + esc(h.llm_model || "model unknown") +
      " &middot; " + esc(h.keys_count) + " key" +
      (num(h.keys_count) === 1 ? "" : "s") + "</span>";
    var parts = [];
    if (h.llm_model) { parts.push("default: " + h.llm_model); }
    if (h.sensitive_model) { parts.push("entailment: " + h.sensitive_model); }
    footModel.textContent = parts.join("  \\u00b7  ");
  }).catch(function () {
    health.className = "status-dot down";
    health.innerHTML = "<i></i><span>service unreachable</span>";
  });

  input.focus();
}());
</script>
</body>
</html>
"""


def build_index_html(disclaimer: str = DISCLAIMER, pool_cap: int = 30) -> str:
    """Render the single-page UI with the canonical disclaimer injected.

    A plain ``str.replace`` (not ``format``) because the template is full of
    CSS/JS braces. ``pool_cap`` feeds the header stat strip (see
    Settings.pool_cap) so the on-page number never drifts from the running
    configuration.
    """
    return (
        INDEX_TEMPLATE.replace("{{DISCLAIMER}}", disclaimer)
        .replace("{{POOL_CAP}}", str(pool_cap))
    )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class EvidenceHandler(BaseHTTPRequestHandler):
    """Serves the UI and the JSON API over one shared pipeline instance.

    ``pipeline``, ``use_mock`` and ``settings`` are class attributes set once
    by :func:`run_server`: every request thread reads the same pipeline, which
    is safe because the agents hold no per-question state.
    """

    #: Shared, set by run_server() before the server starts accepting.
    pipeline: Optional[EvidencePipeline] = None
    use_mock: bool = False
    settings: Optional[Settings] = None
    index_html: str = ""

    server_version = "EvidenceBoard/1.0"
    sys_version = ""  # do not advertise the Python version
    protocol_version = "HTTP/1.1"  # required for keep-alive + Content-Length

    # --- response plumbing -------------------------------------------------

    def _cors(self) -> None:
        """CORS headers (identical on every response, incl. errors)."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        """Send one complete response (headers + body)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        """Serialize ``payload`` as UTF-8 JSON."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        """Uniform machine-readable error shape: ``{"error": "..."}``."""
        self._send_json(status, {"error": message})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """Route access logs through the project's ``[server]`` prefix."""
        print(f"[server] {self.address_string()} {fmt % args}")

    # --- HTTP verbs ---------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight: headers only, no body."""
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            html = self.index_html or build_index_html(
                pool_cap=self.settings.pool_cap if self.settings else 30
            )
            self._respond(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if path == "/api/health":
            self._send_json(200, self._health())
            return
        self._send_error_json(404, f"no such endpoint: {path}")

    def do_HEAD(self) -> None:  # noqa: N802
        """Same routing as GET; _respond() suppresses the body."""
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/ask":
            self._send_error_json(404, f"no such endpoint: {path}")
            return
        self._handle_ask()

    # --- endpoint implementations -------------------------------------------

    def _health(self) -> dict:
        """Liveness plus the model/key configuration of THIS process."""
        settings = self.settings or get_settings()
        return {
            "status": "ok",
            "llm_model": settings.llm_model,
            "sensitive_model": settings.llm_sensitive_model,
            "keys_count": len(settings.llm_api_keys),
        }

    def _read_json_body(self) -> dict:
        """Read and parse the request body.

        Raises ValueError with a client-facing message for anything the
        caller can fix (bad length, oversized body, malformed JSON).
        """
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except ValueError:
            raise ValueError("invalid Content-Length header")
        if length <= 0:
            raise ValueError("request body is empty")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("request body must be valid UTF-8 JSON")
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _handle_ask(self) -> None:
        """POST /api/ask -- run one question through the pipeline.

        An abstention is a successful outcome (HTTP 200 with
        ``abstained: true``), so 500 here means the server itself broke.
        """
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return

        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self._send_error_json(400, "field 'question' is required and must be a non-empty string")
            return
        question = question.strip()

        if self.pipeline is None:
            self._send_error_json(503, "pipeline is not available on this server")
            return

        mode = " (mock)" if self.use_mock else ""
        print(f"[server] ask{mode}: {question[:120]}")
        try:
            report = self.pipeline.run(question, use_mock=self.use_mock)
        except Exception as exc:  # pipeline.run() should not raise -- be safe
            print(f"[server] error: {exc}")
            traceback.print_exc()
            self._send_error_json(500, f"pipeline failed: {exc}")
            return

        if not isinstance(report, dict):
            print(f"[server] error: pipeline returned {type(report).__name__}, expected dict")
            self._send_error_json(500, "pipeline returned a malformed report")
            return

        try:
            self._send_json(200, report)
        except (TypeError, ValueError) as exc:
            print(f"[server] error: report is not JSON-serializable: {exc}")
            self._send_error_json(500, "report is not JSON-serializable")
            return
        print(
            f"[server] answered: abstained={report.get('abstained')} "
            f"claims={len(report.get('claims') or [])} "
            f"evidence={len(report.get('evidence') or [])}"
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def run_server(
    pipeline: EvidencePipeline,
    settings: Settings,
    use_mock: bool = False,
) -> None:
    """Bind ``settings.server_host:server_port`` and serve until interrupted."""
    EvidenceHandler.pipeline = pipeline
    EvidenceHandler.settings = settings
    EvidenceHandler.use_mock = use_mock
    EvidenceHandler.index_html = build_index_html(pool_cap=settings.pool_cap)

    httpd = ThreadingHTTPServer((settings.server_host, settings.server_port), EvidenceHandler)
    httpd.daemon_threads = True
    host, port = settings.server_host, settings.server_port
    print(f"[server] listening on http://{host}:{port}")
    if use_mock:
        print("[server] mock mode: /api/ask replays demo/mock_response.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[server] shutting down")
    finally:
        httpd.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: build the pipeline once, then serve."""
    parser = argparse.ArgumentParser(
        prog="api.server",
        description="Serve the EvidenceBoard UI and JSON API (stdlib only).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="replay demo/mock_response.json for every /api/ask (offline demo)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        pipeline = build_default_pipeline(settings)
    except Exception as exc:
        # Almost always "no LLM key configured". Fatal for a live run; in
        # mock mode there is nothing to answer with either, so say so plainly.
        print(f"[server] error: cannot build pipeline: {exc}")
        print("[server] set LLM_API_KEY or LLM_API_KEYS (see .env.example)")
        return 1
    print("[server] pipeline built")

    run_server(pipeline, settings, use_mock=args.mock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
