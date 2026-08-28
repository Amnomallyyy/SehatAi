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
# One template string, one placeholder ({{DISCLAIMER}}), substituted by
# build_index_html() so config.DISCLAIMER stays the single source of truth
# for the legal copy. Everything else -- layout, palette, motion -- is
# inline: this page must render identically on an air-gapped laptop.

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EvidenceBoard &middot; verification-first clinical evidence</title>
<style>
  :root {
    --ink: #14201c;
    --ink-soft: #4a5b55;
    --ink-faint: #8b9995;
    --paper: #fbfaf7;
    --card: #ffffff;
    --rule: #e3e0d8;
    --rule-soft: #eeece5;
    --accent: #0f5c4a;
    --accent-soft: #e6f0ec;
    --pass: #1a6b4f;
    --pass-bg: #e7f2ec;
    --fail: #a2301f;
    --fail-bg: #fbeae6;
    --flag: #8a5a06;
    --flag-bg: #fdf2dd;
    --skip-bg: #f1efe9;
    --info: #1c4f7c;
    --info-bg: #e8f0f7;
    --shadow: 0 1px 2px rgba(20, 32, 28, .05), 0 8px 24px -12px rgba(20, 32, 28, .16);
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
    /* Faint clinical-chart grid: atmosphere with zero assets. */
    background-image:
      linear-gradient(var(--rule-soft) 1px, transparent 1px),
      linear-gradient(90deg, var(--rule-soft) 1px, transparent 1px);
    background-size: 100% 34px, 34px 100%;
    background-position: -1px -1px;
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
    border-bottom: 2px solid var(--ink);
  }
  .wordmark {
    margin: 0;
    font-size: 34px;
    font-weight: 700;
    letter-spacing: -.025em;
    line-height: 1.05;
  }
  .wordmark .dot { color: var(--accent); }
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
  .status-dot.live i { background: var(--pass); box-shadow: 0 0 0 3px var(--pass-bg); }
  .status-dot.down i { background: var(--fail); box-shadow: 0 0 0 3px var(--fail-bg); }

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
    color: #fff;
    background: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 3px;
    cursor: pointer;
    transition: background .16s ease, transform .16s ease;
  }
  button.primary:hover:not(:disabled) { background: #0b4638; }
  button.primary:active:not(:disabled) { transform: translateY(1px); }
  button.primary:disabled { opacity: .6; cursor: progress; }
  .spinner {
    display: inline-block;
    width: 12px; height: 12px;
    margin-right: 8px;
    vertical-align: -1px;
    border: 2px solid rgba(255, 255, 255, .35);
    border-top-color: #fff;
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
    padding: 18px 20px;
    background: var(--card);
    border: 1px solid var(--rule);
    border-left: 3px solid var(--accent);
    border-radius: 3px;
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--ink-soft);
    box-shadow: var(--shadow);
  }
  .working .bar {
    height: 2px;
    margin-top: 12px;
    background: var(--rule-soft);
    overflow: hidden;
  }
  .working .bar i {
    display: block;
    width: 34%;
    height: 100%;
    background: var(--accent);
    animation: sweep 1.5s ease-in-out infinite;
  }
  @keyframes sweep {
    0%   { transform: translateX(-100%); }
    100% { transform: translateX(320%); }
  }
  .error-box {
    margin-top: 28px;
    padding: 16px 18px;
    background: var(--fail-bg);
    border: 1px solid #eccfc8;
    border-left: 3px solid var(--fail);
    border-radius: 3px;
    color: #7d2517;
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
    border: 1px solid #eeddb4;
    border-left: 3px solid var(--flag);
    border-radius: 3px;
    padding: 20px 22px;
  }
  .abstain h3 {
    margin: 0 0 8px;
    font-size: 15px;
    color: #6f4905;
    letter-spacing: -.01em;
  }
  .abstain p { margin: 0 0 10px; font-size: 13.5px; color: #6f4905; }
  .abstain ul { margin: 0; padding-left: 20px; font-size: 13.5px; color: #6f4905; }
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
  .b-pass { background: var(--pass-bg); color: var(--pass); border-color: #cbe3d6; }
  .b-fail { background: var(--fail-bg); color: var(--fail); border-color: #eccfc8; }
  .b-flag { background: var(--flag-bg); color: var(--flag); border-color: #eeddb4; }
  .b-skip { background: var(--skip-bg); color: var(--ink-faint); border-color: var(--rule); }
  .b-info { background: var(--info-bg); color: var(--info); border-color: #cfdeeb; }
  .verdict {
    font-family: var(--mono); font-size: 11px; color: var(--ink-soft);
    margin-bottom: 10px;
  }
  .verdict .conf { color: var(--ink-faint); }
  blockquote.quote {
    margin: 0 0 12px;
    padding: 10px 14px;
    background: #faf9f5;
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
    background: #fdf0e2;
    color: #8a4a06;
    border: 1px solid #f0dcc2;
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
    text-decoration-color: #d4b3ab;
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
  li.ev.retracted { border-left: 3px solid var(--fail); background: #fffbfa; }
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
    border: 1px solid #cfe2db;
    border-radius: 2px;
    padding: 3px 8px;
    white-space: nowrap;
  }
  .ev-score.mid { color: var(--flag); background: var(--flag-bg); border-color: #eeddb4; }
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
    <span id="working-text">Planning queries, retrieving the evidence pool, verifying every claim&hellip;</span>
    <div class="bar"><i></i></div>
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
  var errorBox = document.getElementById("error");
  var results = document.getElementById("results");
  var health = document.getElementById("health");
  var footModel = document.getElementById("foot-model");
  var seeds = document.getElementById("seeds");
  var busy = false;

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

  // --- request lifecycle -------------------------------------------------

  function setBusy(state) {
    busy = state;
    button.disabled = state;
    button.innerHTML = state ? '<span class="spinner"></span>Working' : "Ask";
    working.hidden = !state;
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
      render(result.data);
    }).catch(function (err) {
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


def build_index_html(disclaimer: str = DISCLAIMER) -> str:
    """Render the single-page UI with the canonical disclaimer injected.

    A plain ``str.replace`` (not ``format``) because the template is full of
    CSS/JS braces.
    """
    return INDEX_TEMPLATE.replace("{{DISCLAIMER}}", disclaimer)


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
            self._respond(
                200,
                (self.index_html or build_index_html()).encode("utf-8"),
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
    EvidenceHandler.index_html = build_index_html()

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
