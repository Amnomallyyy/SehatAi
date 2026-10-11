/* ============================================================
   EvidenceBoard answer view (doctor portal -> Clinical Evidence)

   Frontend-only. Renders one EvidenceBoard API response as topic
   headings, short claim summaries and numbered citation chips that open
   the paper's details. DETERMINISTIC: the page is a pure function of the
   response. No model, no network request, no randomness, no clock reads.
   Every word on screen is a field from the response or a fixed UI label
   below. Rules are the constant tables at the top; first match wins.

   Public API (window.EvidenceView):
     render(container, response, { idPrefix })  draw a response
     buildView(response)                         pure: response -> view model
     runSelfTests()                              console self-checks
     SAMPLE                                      embedded sample response
   ============================================================ */
(function () {
  'use strict';

  /* ---------- Rule tables ---------- */

  // Field names. Each entry lists accepted names in order; the first one
  // present wins. The first names are the EvidenceBoard API's (see
  // sehatEvidence/api/contract.md); the alternates accept the spec's
  // simple shape { claims:[{text, source_id, confidence}], sources:[{id, ...}] }.
  const MAP = {
    query: ['question', 'query'],
    claims: ['claims'],
    claimText: ['text'],
    claimConfidence: ['confidence'],
    claimStatus: ['status'],
    claimCitations: ['citations'], // [{ sid }] in the API
    citationSourceId: ['sid', 'id'],
    claimSourceId: ['source_id'], // spec shape: one id per claim
    sources: ['evidence', 'sources'],
    sourceId: ['sid', 'id'],
    sourceTitle: ['title'],
    sourceJournal: ['journal'],
    sourceDate: ['publication_date', 'date'],
    sourceRelevance: ['relevance_score', 'relevance'],
    sourceAbstract: ['abstract'],
    sourceUrl: ['url'],
    sourceDoi: ['doi'],
  };

  // Claims with a status are shown only when it's one of these ("deleted"
  // claims never appear in the answer). Claims without a status are shown.
  const SHOWN_STATUSES = ['kept', 'flagged'];

  // Topic headings, tested against the claim text in this order. First
  // match wins; anything unmatched goes under FALLBACK_HEADING.
  const THEMES = [
    { heading: 'Liquid biopsy and ctDNA', test: /\b(liquid biops\w*|ctdna|cfdna|cell-free dna|circulating tumou?r (dna|cells?)|ctcs?)\b/i },
    { heading: 'CAR-T and cell therapy', test: /\b(car[- ]?t|chimeric antigen receptor|cell therap\w*|adoptive cell\w*)\b/i },
    { heading: 'Targeted and antibody drugs', test: /\b(antibod\w*|antibody[- ]drug conjugates?|adcs?|parp|tyrosine kinase|tkis?|monoclonal|targeted therap\w*|\w+mab)\b/i },
    { heading: 'AI and screening', test: /\b(artificial intelligence|machine learning|deep learning|ai|screening)\b/i },
  ];
  const FALLBACK_HEADING = 'Other findings';

  // Limitation language in an abstract -> amber dot + quoted sentences.
  const CAVEAT_CUES = /\b(however|limited|high risk of bias|not significant|no study|no multi\w*|lack\w*|dispersed|false[- ]positive\w*|preliminary)\b/i;

  // Claim order: source relevance desc, claim confidence desc, original
  // index asc. Missing numbers sort after present ones.
  const SORT_KEYS = [
    { key: 'relevance', dir: -1 },
    { key: 'confidence', dir: -1 },
    { key: 'index', dir: 1 },
  ];

  // Website name shown on a citation chip, from the source link's host.
  // First match wins; any other host shows its own domain (without www.).
  const SITE_NAMES = [
    { host: /(^|\.)pubmed\.ncbi\.nlm\.nih\.gov$/i, name: 'PubMed' },
    { host: /(^|\.)ncbi\.nlm\.nih\.gov$/i, name: 'NCBI' },
    { host: /(^|\.)europepmc\.org$/i, name: 'Europe PMC' },
    { host: /(^|\.)clinicaltrials\.gov$/i, name: 'ClinicalTrials.gov' },
    { host: /(^|\.)doi\.org$/i, name: 'doi.org' },
  ];
  const SITE_FALLBACK = 'Source';

  const ENTITY_PASSES = 2;
  const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', ndash: '–', mdash: '—', hellip: '…', rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“' };
  const SENTENCE_SPLIT = /(?<=[.!?])\s+(?=[A-Z0-9("'])/;

  const LABELS = {
    summary: (claims, cited) => `${claims} claim${claims === 1 ? '' : 's'} · ${cited} cited source${cited === 1 ? '' : 's'}`,
    ruleBased: 'rule-based grouping',
    relevance: (n) => `relevance ${n}/100`,
    sourceRelevance: (n) => `Source relevance ${n}/100`,
    caveatIntro: 'Caveat in the abstract:',
    showAbstract: 'Show abstract',
    hideAbstract: 'Hide abstract',
    abstractUnavailable: 'Abstract unavailable',
    openSource: 'Open source',
    sourcesHeading: 'Sources',
    sourcesLine: (cited, uncited) => `Sources: numbered 1 to ${cited} · ${uncited} source${uncited === 1 ? ' has' : 's have'} no verified summary`,
    skipped: (n) => `${n} claim${n === 1 ? '' : 's'} skipped: cited source not found in the response.`,
    note: 'Grouping and caveats are rule-based. Verify in the source.',
    legend: 'Amber dot = abstract notes a limitation · numbered chip = source, click to open details',
    loadJson: 'Load JSON',
    themeToDark: 'Dark theme',
    themeToLight: 'Light theme',
    chipLabel: (n, title, caveat, site) => `Source ${n}, ${site}: ${title}${caveat ? ' (abstract notes a limitation)' : ''}`,
    errNoMatch: 'No claim matches a source in this response.',
    errParse: (msg) => `Could not read that file as JSON: ${msg}`,
    errShape: 'This JSON is not an evidence response (no claims list found).',
  };

  /* ---------- Pure functions (data in, data out) ---------- */

  function pick(obj, names) {
    if (!obj || typeof obj !== 'object') return undefined;
    for (const name of names) if (obj[name] !== undefined && obj[name] !== null) return obj[name];
    return undefined;
  }

  function decodeEntities(text) {
    let out = String(text == null ? '' : text);
    for (let pass = 0; pass < ENTITY_PASSES; pass++) {
      out = out.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, code) => {
        if (code[0] === '#') {
          const n = code[1] === 'x' || code[1] === 'X' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
          return Number.isFinite(n) && n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : whole;
        }
        const named = NAMED_ENTITIES[code.toLowerCase()];
        return named !== undefined ? named : whole;
      });
    }
    return out;
  }

  // Decode entities, strip tags, collapse whitespace. Display only — the
  // response object itself is never changed.
  function cleanText(text) {
    return decodeEntities(text).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function yearOf(date) {
    const m = String(date == null ? '' : date).match(/\b(1[89]\d{2}|2\d{3})\b/);
    return m ? m[1] : '';
  }

  function safeUrl(url) {
    const s = String(url == null ? '' : url).trim();
    return /^https?:\/\/[^\s]+$/i.test(s) ? s : '';
  }

  function siteNameOf(url) {
    const m = String(url || '').match(/^https?:\/\/([^/?#:]+)/i);
    if (!m) return SITE_FALLBACK;
    const host = m[1].toLowerCase();
    const known = SITE_NAMES.find((s) => s.host.test(host));
    return known ? known.name : host.replace(/^www\./, '');
  }

  function toNumberOrNull(v) {
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  }

  function findCaveats(abstract) {
    if (!abstract) return [];
    return abstract.split(SENTENCE_SPLIT).filter((s) => CAVEAT_CUES.test(s));
  }

  // Raw response -> plain display records. Reads only; never writes.
  function normalize(response) {
    const rawClaims = pick(response, MAP.claims);
    const rawSources = pick(response, MAP.sources);
    const sources = (Array.isArray(rawSources) ? rawSources : []).map((s, index) => {
      const abstract = cleanText(pick(s, MAP.sourceAbstract));
      const doi = String(pick(s, MAP.sourceDoi) || '').trim();
      const url = safeUrl(pick(s, MAP.sourceUrl)) || (doi ? safeUrl(`https://doi.org/${doi}`) : '');
      return {
        site: siteNameOf(url),
        index,
        id: String(pick(s, MAP.sourceId) ?? ''),
        title: cleanText(pick(s, MAP.sourceTitle)) || String(pick(s, MAP.sourceId) ?? ''),
        journal: cleanText(pick(s, MAP.sourceJournal)),
        year: yearOf(pick(s, MAP.sourceDate)),
        relevance: toNumberOrNull(pick(s, MAP.sourceRelevance)),
        abstract,
        caveats: findCaveats(abstract),
        url,
      };
    });
    const claims = (Array.isArray(rawClaims) ? rawClaims : [])
      .map((c, index) => {
        const status = pick(c, MAP.claimStatus);
        const citations = pick(c, MAP.claimCitations);
        const sourceIds = Array.isArray(citations)
          ? citations.map((cit) => String(pick(cit, MAP.citationSourceId) ?? '')).filter(Boolean)
          : [pick(c, MAP.claimSourceId)].filter((v) => v !== undefined).map(String);
        return {
          index,
          status: status == null ? null : String(status),
          text: cleanText(pick(c, MAP.claimText)),
          confidence: toNumberOrNull(pick(c, MAP.claimConfidence)),
          sourceIds: [...new Set(sourceIds)],
        };
      })
      .filter((c) => c.text && (c.status === null || SHOWN_STATUSES.includes(c.status)));
    return {
      query: cleanText(pick(response, MAP.query)),
      claims,
      sources,
      hasClaimsList: Array.isArray(rawClaims),
    };
  }

  // Attach each claim to its cited sources. A claim with no cited source
  // in the response is skipped and counted.
  function join(model) {
    const byId = new Map();
    model.sources.forEach((s) => { if (s.id && !byId.has(s.id)) byId.set(s.id, s); });
    const joined = [];
    let skipped = 0;
    for (const claim of model.claims) {
      const sources = claim.sourceIds.map((id) => byId.get(id)).filter(Boolean);
      if (!sources.length) { skipped += 1; continue; }
      joined.push({ ...claim, sources, relevance: sources[0].relevance });
    }
    return { joined, skipped };
  }

  function compareBy(a, b) {
    for (const { key, dir } of SORT_KEYS) {
      const av = a[key];
      const bv = b[key];
      if (av === bv) continue;
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return av < bv ? -dir : dir;
    }
    return 0;
  }

  function sortClaims(joined) {
    return [...joined].sort(compareBy);
  }

  function headingFor(text) {
    const rule = THEMES.find((t) => t.test.test(text));
    return rule ? rule.heading : FALLBACK_HEADING;
  }

  function group(sorted) {
    const order = [...THEMES.map((t) => t.heading), FALLBACK_HEADING];
    const buckets = new Map(order.map((h) => [h, []]));
    sorted.forEach((c) => buckets.get(headingFor(c.text)).push(c));
    return order.filter((h) => buckets.get(h).length).map((heading) => ({ heading, claims: buckets.get(heading) }));
  }

  // Full view model. Sources are numbered by first appearance in display
  // order; the same source always gets the same number.
  function buildView(response) {
    const model = normalize(response);
    const { joined, skipped } = join(model);
    const groups = group(sortClaims(joined));
    const numberOf = new Map();
    const numbered = [];
    for (const g of groups) {
      for (const c of g.claims) {
        for (const s of c.sources) {
          if (!numberOf.has(s.id)) { numberOf.set(s.id, numbered.length + 1); numbered.push(s); }
        }
      }
    }
    const shownGroups = groups.map((g) => ({
      heading: g.heading,
      claims: g.claims.map((c) => ({
        text: c.text,
        chips: c.sources.map((s) => ({ number: numberOf.get(s.id), sourceId: s.id, caveat: s.caveats.length > 0 })),
      })),
    }));
    const error = !model.hasClaimsList ? LABELS.errShape
      : (model.claims.length > 0 && joined.length === 0) ? LABELS.errNoMatch
      : null;
    return {
      query: model.query,
      claimCount: joined.length,
      citedCount: numbered.length,
      skipped,
      uncitedCount: model.sources.filter((s) => !numberOf.has(s.id)).length,
      groups: shownGroups,
      sources: numbered.map((s, i) => ({ ...s, number: i + 1 })),
      error,
    };
  }

  function parseJsonText(text) {
    try {
      return { value: JSON.parse(text), error: null };
    } catch (err) {
      return { value: null, error: LABELS.errParse(err.message) };
    }
  }

  /* ---------- Styles (scoped to .eb-root; tokens + dark variant) ---------- */

  const STYLE_ID = 'evidence-view-styles';
  const CSS = `
.eb-root {
  --eb-bg: #ffffff; --eb-text: #1d2430; --eb-muted: #4f5866; --eb-line: #dfe3e8;
  --eb-chip-bg: #e8eef6; --eb-chip-text: #1f3a5c; --eb-chip-border: #b9c8db;
  --eb-pop-bg: #f6f8fb; --eb-amber: #b7791f; --eb-link: #1f4f8a; --eb-error: #a4262c;
  --eb-focus: #1f4f8a; --eb-src-title: #1a0dab;
  color: var(--eb-text); background: var(--eb-bg);
  font-size: .9375rem; line-height: 1.55; max-width: 75ch;
  padding: 2px 0 calc(4px + env(safe-area-inset-bottom));
}
.eb-root[data-eb-theme="dark"] {
  --eb-bg: #161a20; --eb-text: #e6e8ec; --eb-muted: #aab2bd; --eb-line: #2e343d;
  --eb-chip-bg: #24324a; --eb-chip-text: #dbe6f5; --eb-chip-border: #3b5175;
  --eb-pop-bg: #1d222a; --eb-amber: #e0a43a; --eb-link: #9cc2ef; --eb-error: #ff8a80;
  --eb-focus: #9cc2ef; --eb-src-title: #8ab4f8;
  padding-left: 10px; padding-right: 10px;
}
.eb-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.eb-summary { font-weight: 600; }
.eb-tag { font-size: .75rem; color: var(--eb-muted); border: 1px solid var(--eb-line); padding: 1px 8px; border-radius: 999px; }
.eb-spacer { flex: 1; }
.eb-btn { min-height: 44px; padding: 0 12px; border: 1px solid var(--eb-line); background: var(--eb-bg); color: var(--eb-text); border-radius: 8px; font: inherit; font-size: .8125rem; cursor: pointer; }
.eb-btn:focus-visible, .eb-chip:focus-visible, .eb-link:focus-visible { outline: 2px solid var(--eb-focus); outline-offset: 2px; }
.eb-query { color: var(--eb-muted); font-size: .8125rem; margin: 0 0 6px; }
.eb-heading { font-size: 1rem; font-weight: 700; margin: 14px 0 4px; }
.eb-para { margin: 0 0 4px; }
.eb-claim { display: inline; }
.eb-chip { display: inline-flex; align-items: center; gap: 5px; height: 24px; margin: 0 3px; padding: 0 9px 0 3px;
  font: inherit; font-size: .78rem; font-weight: 500; line-height: 1; border-radius: 999px; cursor: pointer; vertical-align: 1px;
  background: var(--eb-chip-bg); color: var(--eb-chip-text); border: 1px solid var(--eb-chip-border); position: relative; white-space: nowrap; }
.eb-chip::before { content: ""; position: absolute; inset: -10px -2px; } /* 44px touch target */
.eb-chip:hover { border-color: var(--eb-chip-text); }
.eb-chip-icon { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 50%;
  background: var(--eb-bg); color: var(--eb-chip-text); border: 1px solid var(--eb-chip-border); font-size: .68rem; font-weight: 700; }
.eb-chip-num { font-size: .68rem; font-weight: 700; opacity: .7; }
.eb-chip[aria-expanded="true"] { background: var(--eb-chip-text); color: var(--eb-chip-bg); }
.eb-chip[aria-expanded="true"] .eb-chip-icon { background: var(--eb-chip-bg); color: var(--eb-chip-text); }
.eb-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--eb-amber); margin: 0 3px 1px 1px; vertical-align: middle; }
.eb-pop { background: var(--eb-pop-bg); border: 1px solid var(--eb-line); border-radius: 10px; padding: 10px 12px; margin: 6px 0 10px; }
.eb-pop-title { font-weight: 700; }
.eb-pop-meta { color: var(--eb-muted); font-size: .8125rem; margin-top: 2px; }
.eb-pop-caveat { margin-top: 8px; font-size: .875rem; }
.eb-pop-caveat q { quotes: "\\201C" "\\201D"; }
.eb-pop-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.eb-link { display: inline-flex; align-items: center; min-height: 44px; padding: 0 12px; color: var(--eb-link); border: 1px solid var(--eb-line); border-radius: 8px; text-decoration: none; font-size: .8125rem; }
.eb-abstract { margin-top: 8px; font-size: .8125rem; color: var(--eb-text); white-space: pre-wrap; }
.eb-sources { margin: 16px 0 6px; padding-top: 10px; border-top: 1px solid var(--eb-line); }
.eb-sources h4 { margin: 0 0 6px; font-size: .875rem; color: var(--eb-text); }
.eb-sources .eb-src-title { color: var(--eb-src-title); }
.eb-sources ol { margin: 0; padding-left: 26px; font-size: .8125rem; }
.eb-sources li { margin-bottom: 4px; }
.eb-sources .eb-src-meta { color: var(--eb-muted); }
.eb-foot { font-size: .75rem; color: var(--eb-muted); margin-top: 6px; }
.eb-error { color: var(--eb-error); font-weight: 600; margin: 6px 0; }
@media (max-width: 560px) { .eb-root { font-size: .875rem; } .eb-spacer { display: none; } }
`;

  function ensureStyles() {
    if (typeof document === 'undefined' || document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  /* ---------- DOM rendering (only renders the view model) ---------- */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function buildPopover(source, id) {
    const pop = el('div', 'eb-pop');
    pop.id = id;
    pop.setAttribute('role', 'region');
    pop.setAttribute('aria-label', source.title);
    pop.appendChild(el('div', 'eb-pop-title', source.title));
    const meta = [source.journal, source.year, source.relevance !== null ? LABELS.relevance(source.relevance) : '']
      .filter(Boolean).join(' · ');
    if (meta) pop.appendChild(el('div', 'eb-pop-meta', meta));
    if (source.caveats.length) {
      const cav = el('div', 'eb-pop-caveat');
      cav.appendChild(el('strong', null, `${LABELS.caveatIntro} `));
      source.caveats.forEach((sentence, i) => {
        if (i) cav.appendChild(document.createTextNode(' '));
        cav.appendChild(el('q', null, sentence));
      });
      pop.appendChild(cav);
    }
    const actions = el('div', 'eb-pop-actions');
    const abstractBox = el('div', 'eb-abstract', source.abstract || LABELS.abstractUnavailable);
    abstractBox.hidden = true;
    abstractBox.id = `${id}-abstract`;
    const toggle = el('button', 'eb-btn', LABELS.showAbstract);
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', abstractBox.id);
    toggle.addEventListener('click', () => {
      abstractBox.hidden = !abstractBox.hidden;
      toggle.setAttribute('aria-expanded', String(!abstractBox.hidden));
      toggle.textContent = abstractBox.hidden ? LABELS.showAbstract : LABELS.hideAbstract;
    });
    actions.appendChild(toggle);
    if (source.url) {
      const link = el('a', 'eb-link', LABELS.openSource);
      link.href = source.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      actions.appendChild(link);
    }
    pop.appendChild(actions);
    pop.appendChild(abstractBox);
    return pop;
  }

  function renderView(root, view, idPrefix) {
    const bySourceNumber = new Map(view.sources.map((s) => [s.number, s]));
    let open = null; // { chip, popover } -- only one popover at a time

    const closeOpen = () => {
      if (!open) return;
      open.chip.setAttribute('aria-expanded', 'false');
      open.popover.remove();
      open = null;
    };

    const frag = document.createDocumentFragment();
    if (view.query) frag.appendChild(el('p', 'eb-query', view.query));
    if (view.error) frag.appendChild(el('p', 'eb-error', view.error));

    view.groups.forEach((g, gi) => {
      frag.appendChild(el('h3', 'eb-heading', g.heading));
      const para = el('p', 'eb-para');
      g.claims.forEach((claim, ci) => {
        if (ci) para.appendChild(document.createTextNode(' '));
        para.appendChild(el('span', 'eb-claim', claim.text));
        claim.chips.forEach((chipInfo) => {
          const source = bySourceNumber.get(chipInfo.number);
          // Pill: round letter icon + website name + the source's number
          // (the number matches the Sources list below).
          const chip = el('button', 'eb-chip');
          const icon = el('span', 'eb-chip-icon', (source.site || SITE_FALLBACK).charAt(0).toUpperCase());
          icon.setAttribute('aria-hidden', 'true');
          chip.append(icon, el('span', 'eb-chip-site', source.site), el('span', 'eb-chip-num', String(chipInfo.number)));
          chip.type = 'button';
          chip.setAttribute('aria-expanded', 'false');
          chip.setAttribute('aria-label', LABELS.chipLabel(chipInfo.number, source.title, chipInfo.caveat, source.site));
          const popId = `${idPrefix}-g${gi}-c${ci}-s${chipInfo.number}`;
          chip.setAttribute('aria-controls', popId);
          chip.addEventListener('click', () => {
            const wasThis = open && open.chip === chip;
            closeOpen();
            if (wasThis) return;
            const popover = buildPopover(source, popId);
            para.after(popover);
            chip.setAttribute('aria-expanded', 'true');
            open = { chip, popover };
          });
          para.appendChild(chip);
          if (chipInfo.caveat) {
            const dot = el('span', 'eb-dot');
            dot.setAttribute('aria-hidden', 'true');
            para.appendChild(dot);
          }
        });
      });
      frag.appendChild(para);
    });

    if (view.sources.length) {
      const sources = el('section', 'eb-sources');
      sources.appendChild(el('h4', null, LABELS.sourcesHeading));
      const list = el('ol');
      view.sources.forEach((s) => {
        const li = el('li');
        li.value = s.number;
        li.appendChild(el('span', 'eb-src-title', s.title));
        const meta = [s.id, s.journal, s.year, s.relevance !== null ? LABELS.sourceRelevance(s.relevance) : ''].filter(Boolean).join(' · ');
        li.appendChild(el('span', 'eb-src-meta', ` — ${meta}`));
        list.appendChild(li);
      });
      sources.appendChild(list);
      frag.appendChild(sources);
      frag.appendChild(el('p', 'eb-foot', LABELS.sourcesLine(view.citedCount, view.uncitedCount)));
    }
    if (view.skipped) frag.appendChild(el('p', 'eb-foot', LABELS.skipped(view.skipped)));
    frag.appendChild(el('p', 'eb-foot', LABELS.note));
    frag.appendChild(el('p', 'eb-foot', LABELS.legend));
    return frag;
  }

  /* Renders `response` into `container` (replacing its contents). The
     toolbar's Load JSON re-renders from a local file; the theme button
     switches this view between light and dark. */
  function render(container, response, options = {}) {
    ensureStyles();
    const idPrefix = options.idPrefix || 'eb';
    const root = el('div', 'eb-root');
    root.setAttribute('data-eb-theme', options.theme === 'dark' ? 'dark' : 'light');

    const body = el('div', 'eb-body');
    const toolbar = el('div', 'eb-toolbar');
    const summary = el('span', 'eb-summary');
    const tag = el('span', 'eb-tag', LABELS.ruleBased);
    const spacer = el('span', 'eb-spacer');

    const fileInput = el('input');
    fileInput.type = 'file';
    fileInput.accept = 'application/json,.json';
    fileInput.hidden = true;
    const loadBtn = el('button', 'eb-btn', LABELS.loadJson);
    loadBtn.type = 'button';
    loadBtn.addEventListener('click', () => fileInput.click());

    const themeBtn = el('button', 'eb-btn');
    themeBtn.type = 'button';
    const syncThemeLabel = () => {
      themeBtn.textContent = root.getAttribute('data-eb-theme') === 'dark' ? LABELS.themeToLight : LABELS.themeToDark;
    };
    themeBtn.addEventListener('click', () => {
      root.setAttribute('data-eb-theme', root.getAttribute('data-eb-theme') === 'dark' ? 'light' : 'dark');
      syncThemeLabel();
    });
    syncThemeLabel();

    const draw = (data) => {
      const view = buildView(data);
      summary.textContent = LABELS.summary(view.claimCount, view.citedCount);
      body.replaceChildren(renderView(root, view, idPrefix));
      return view;
    };

    fileInput.addEventListener('change', () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      const reader = new FileReader(); // local file only, no network
      reader.onload = () => {
        const parsed = parseJsonText(String(reader.result));
        if (parsed.error) {
          body.replaceChildren(el('p', 'eb-error', parsed.error));
          summary.textContent = '';
        } else {
          draw(parsed.value);
        }
        fileInput.value = '';
      };
      reader.onerror = () => body.replaceChildren(el('p', 'eb-error', LABELS.errParse('the file could not be read')));
      reader.readAsText(file);
    });

    toolbar.append(summary, tag, spacer, loadBtn, themeBtn, fileInput);
    root.append(toolbar, body);
    draw(response);
    container.replaceChildren(root);
    return root;
  }

  /* ---------- Sample data (from sehatEvidence/demo/mock_response.json) ---------- */
  const SAMPLE = {"question":"Does SGLT2 inhibitor therapy reduce heart failure hospitalization in patients with type 2 diabetes?","claims":[{"text":"In adults with type 2 diabetes and established or high risk of atherosclerotic cardiovascular disease, dapagliflozin reduced the composite of cardiovascular death or hospitalization for heart failure, an effect driven almost entirely by a 27% relative reduction in heart failure hospitalization [S1].","status":"kept","confidence":0.94,"citations":[{"sid":"S1","citation_key":"MED/30415602","title":"Dapagliflozin and Cardiovascular Outcomes in Type 2 Diabetes","url":"https://pubmed.ncbi.nlm.nih.gov/30415602"}]},{"text":"Empagliflozin produced a consistent 30% relative reduction in first hospitalization for heart failure in patients with reduced ejection fraction, and the benefit was of similar magnitude in the prespecified subgroups with and without diabetes [S2].","status":"kept","confidence":0.91,"citations":[{"sid":"S2","citation_key":"MED/32865377","title":"Cardiovascular and Renal Outcomes with Empagliflozin in Heart Failure with Reduced Ejection Fraction","url":"https://pubmed.ncbi.nlm.nih.gov/32865377"}]},{"text":"A meta-analysis of five large cardiovascular outcome trials estimated a pooled hazard ratio of 0.68 for hospitalization for heart failure with SGLT2 inhibition in type 2 diabetes, with no meaningful heterogeneity between agents [S3].","status":"kept","confidence":0.89,"citations":[{"sid":"S3","citation_key":"MED/34449189","title":"SGLT2 Inhibitors and Heart Failure Outcomes in Type 2 Diabetes: A Systematic Review and Meta-Analysis of Cardiovascular Outcome Trials","url":"https://pubmed.ncbi.nlm.nih.gov/34449189"}]},{"text":"Separation of the heart failure hospitalization curves appeared within the first few months of randomization, earlier than would be expected from glucose lowering alone [S1] [S4].","status":"kept","confidence":0.78,"citations":[{"sid":"S1","citation_key":"MED/30415602","title":"Dapagliflozin and Cardiovascular Outcomes in Type 2 Diabetes","url":"https://pubmed.ncbi.nlm.nih.gov/30415602"},{"sid":"S4","citation_key":"MED/31694747","title":"Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction","url":"https://pubmed.ncbi.nlm.nih.gov/31694747"}]},{"text":"In routine care, initiation of an SGLT2 inhibitor rather than a DPP-4 inhibitor was associated with a lower rate of heart failure hospitalization, although residual confounding by indication cannot be excluded in an observational design [S6].","status":"flagged","confidence":0.61,"citations":[{"sid":"S6","citation_key":"MED/34728162","title":"SGLT2 Inhibitors Versus DPP-4 Inhibitors and Risk of Hospitalization for Heart Failure: A Nationwide Propensity-Score-Matched Cohort Study","url":"https://pubmed.ncbi.nlm.nih.gov/34728162"}]},{"text":"The reduction in heart failure hospitalization is accompanied by a significant reduction in all-cause mortality in patients with type 2 diabetes and preserved ejection fraction [S3].","status":"deleted","confidence":0.31,"citations":[{"sid":"S3","citation_key":"MED/34449189","title":"SGLT2 Inhibitors and Heart Failure Outcomes in Type 2 Diabetes: A Systematic Review and Meta-Analysis of Cardiovascular Outcome Trials","url":"https://pubmed.ncbi.nlm.nih.gov/34449189"}]},{"text":"Benefit on heart failure hospitalization extends to patients on maintenance dialysis, in whom SGLT2 inhibitors reduced admissions by roughly one third [S7].","status":"deleted","confidence":null,"citations":[{"sid":"S7","citation_key":"EPMC/PPR512843","title":"SGLT2 Inhibition in Patients Receiving Maintenance Haemodialysis: A Retrospective Multicentre Analysis","url":"https://europepmc.org/article/PPR/PPR512843"}]}],"evidence":[{"sid":"S1","title":"Dapagliflozin and Cardiovascular Outcomes in Type 2 Diabetes","journal":"The New England Journal of Medicine","publication_date":"2019-01-24","relevance_score":95,"abstract":"Background: The effect of sodium-glucose cotransporter 2 inhibitors on cardiovascular outcomes in patients with type 2 diabetes across a broad spectrum of cardiovascular risk remains incompletely defined. Methods: We randomly assigned 17,160 patients with type 2 diabetes and established atherosclerotic cardiovascular disease or multiple risk factors to receive dapagliflozin 10 mg daily or placebo, with a median follow-up of 4.2 years. Results: Dapagliflozin resulted in a lower rate of cardiovascular death or hospitalization for heart failure (4.9% vs. 5.8%; hazard ratio, 0.83; 95% CI, 0.73 to 0.95; P=0.005), which reflected a lower rate of hospitalization for heart failure (hazard ratio, 0.73; 95% CI, 0.61 to 0.88); there was no between-group difference in cardiovascular death. The reduction in the rate of hospitalization for heart failure was apparent within the first 3 months after randomisation and was maintained thereafter, a time course inconsistent with an effect mediated primarily by glycaemic control. Conclusions: In patients with type 2 diabetes, dapagliflozin reduced hospitalization for heart failure without a significant reduction in the rate of major adverse cardiovascular events.","url":"https://pubmed.ncbi.nlm.nih.gov/30415602","doi":"10.1056/NEJMoa1812389"},{"sid":"S2","title":"Cardiovascular and Renal Outcomes with Empagliflozin in Heart Failure with Reduced Ejection Fraction","journal":"The New England Journal of Medicine","publication_date":"2020-10-08","relevance_score":93,"abstract":"Background: Sodium-glucose cotransporter 2 inhibitors reduce the risk of heart failure events in patients with type 2 diabetes, but their effect in patients with established heart failure and a reduced ejection fraction, with or without diabetes, required dedicated evaluation. Methods: We randomly assigned 3,730 patients with class II-IV heart failure and an ejection fraction of 40% or less to receive empagliflozin 10 mg once daily or placebo, in addition to recommended therapy. Results: The primary outcome of cardiovascular death or hospitalization for heart failure occurred in 361 of 1,863 patients in the empagliflozin group and 462 of 1,867 patients in the placebo group (hazard ratio, 0.75; 95% CI, 0.65 to 0.86; P<0.001). The total number of hospitalizations for heart failure was lower in the empagliflozin group than in the placebo group (hazard ratio, 0.70; 95% CI, 0.58 to 0.85; P<0.001). The effects of empagliflozin were similar in patients with or without diabetes at baseline. Uncomplicated genital tract infection was reported more frequently with empagliflozin. Conclusions: Empagliflozin reduced the combined risk of cardiovascular death or hospitalization for heart failure regardless of the presence of diabetes.","url":"https://pubmed.ncbi.nlm.nih.gov/32865377","doi":"10.1056/NEJMoa2022190"},{"sid":"S3","title":"SGLT2 Inhibitors and Heart Failure Outcomes in Type 2 Diabetes: A Systematic Review and Meta-Analysis of Cardiovascular Outcome Trials","journal":"The Lancet","publication_date":"2022-05-14","relevance_score":90,"abstract":"Background: Individual cardiovascular outcome trials of SGLT2 inhibitors in type 2 diabetes were powered for composite atherosclerotic endpoints rather than for heart failure hospitalization. Methods: We searched MEDLINE, Embase and CENTRAL to 31 December 2021 for placebo-controlled cardiovascular outcome trials of SGLT2 inhibitors enrolling adults with type 2 diabetes, and pooled outcomes using random-effects models. Findings: Across 46,969 participants in five placebo-controlled cardiovascular outcome trials, SGLT2 inhibitors reduced hospitalization for heart failure (HR 0.68, 95% CI 0.61-0.76; I2=0%), with consistent effects across individual agents. Major adverse cardiovascular events were modestly reduced (HR 0.90, 95% CI 0.85-0.95), and the effect on heart failure hospitalization was larger in participants with a baseline history of heart failure (P for interaction 0.02). All-cause mortality was not significantly reduced (HR 0.90, 95% CI 0.80-1.01), and no preserved-ejection-fraction subgroup analysis was reported. Interpretation: SGLT2 inhibition consistently and substantially reduces hospitalization for heart failure in type 2 diabetes.","url":"https://pubmed.ncbi.nlm.nih.gov/34449189","doi":"10.1016/S0140-6736(21)01234-5"},{"sid":"S4","title":"Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction","journal":"The New England Journal of Medicine","publication_date":"2019-11-21","relevance_score":86,"abstract":"Background: In patients with type 2 diabetes, SGLT2 inhibitors reduce the risk of a first hospitalization for heart failure, possibly through glucose-independent mechanisms. Methods: We randomly assigned 4,744 patients with New York Heart Association class II-IV heart failure and an ejection fraction of 40% or less to receive dapagliflozin 10 mg once daily or placebo, in addition to recommended therapy; 45% of participants had type 2 diabetes at baseline. Results: The primary composite outcome of worsening heart failure or cardiovascular death occurred in 386 of 2,373 patients in the dapagliflozin group and 502 of 2,371 patients in the placebo group (hazard ratio, 0.74; 95% CI, 0.65 to 0.85; P<0.001). Hospitalization for heart failure occurred in 231 versus 318 patients (hazard ratio, 0.70; 95% CI, 0.59 to 0.83). Curves for hospitalization for heart failure separated within 28 days of randomisation. Findings were similar in patients with and without type 2 diabetes. Conclusions: Among patients with heart failure and a reduced ejection fraction, dapagliflozin reduced the risk of worsening heart failure or cardiovascular death regardless of diabetes status.","url":"https://pubmed.ncbi.nlm.nih.gov/31694747","doi":"10.1056/NEJMoa1911303"},{"sid":"S5","title":"Empagliflozin Outcome Trial in Patients With Chronic Heart Failure and Type 2 Diabetes (EMPACT-HF)","journal":null,"publication_date":"2023-08-02","relevance_score":74,"abstract":"Brief summary: This phase 3, randomised, double-blind, placebo-controlled trial evaluated whether empagliflozin 10 mg once daily reduces the risk of hospitalization for heart failure or cardiovascular death in adults with chronic heart failure and type 2 diabetes receiving guideline-directed medical therapy. Primary outcome measure: time to first adjudicated hospitalization for heart failure or cardiovascular death, assessed up to 36 months. Enrolment: 2,412 participants at 214 sites. Overall status: COMPLETED. Results posted: yes.","url":"https://clinicaltrials.gov/study/NCT03619213","doi":null},{"sid":"S6","title":"SGLT2 Inhibitors Versus DPP-4 Inhibitors and Risk of Hospitalization for Heart Failure: A Nationwide Propensity-Score-Matched Cohort Study","journal":"Circulation","publication_date":"2021-09-07","relevance_score":68,"abstract":"Background: Whether the heart failure benefits observed in SGLT2 inhibitor trials translate to routine clinical practice is uncertain. Methods: Using two nationwide claims databases, we conducted an active-comparator, new-user cohort study of adults with type 2 diabetes initiating an SGLT2 inhibitor or a DPP-4 inhibitor between 2014 and 2020, with 1:1 propensity-score matching on 78 baseline covariates. Results: In 1:1 propensity-score-matched cohorts of 128,293 patients, SGLT2 inhibitor initiation was associated with a lower rate of hospitalization for heart failure than DPP-4 inhibitor initiation (HR 0.71, 95% CI 0.64-0.79); results were attenuated in analyses restricted to patients without prior heart failure. Rates of diabetic ketoacidosis were higher with SGLT2 inhibitors. Conclusions: Routine-care data are consistent with the trial evidence, with the caveat that residual confounding by indication cannot be excluded.","url":"https://pubmed.ncbi.nlm.nih.gov/34728162","doi":"10.1161/CIRCULATIONAHA.121.055364"},{"sid":"S7","title":"SGLT2 Inhibition in Patients Receiving Maintenance Haemodialysis: A Retrospective Multicentre Analysis","journal":"medRxiv","publication_date":"2024-02-14","relevance_score":61,"abstract":"Background: Patients receiving maintenance haemodialysis were excluded from the pivotal SGLT2 inhibitor outcome trials, leaving their cardiac benefit undefined. Methods: We retrospectively reviewed 412 adults with type 2 diabetes on maintenance haemodialysis across nine centres, comparing those prescribed an SGLT2 inhibitor with matched non-users. Results: Heart failure admissions occurred in 18.4% of users versus 26.9% of non-users over 24 months (adjusted HR 0.66, 95% CI 0.42-1.04). Volume-related admissions and symptomatic hypotension did not differ. Conclusions: These hypothesis-generating data require confirmation in randomised trials before any change in dialysis practice. This preprint has not been certified by peer review.","url":"https://europepmc.org/article/PPR/PPR512843","doi":"10.1101/2024.02.11.24302518"},{"sid":"S8","title":"RETRACTED: Marked Reduction in Heart Failure Admissions After Canagliflozin Initiation in a Single-Centre Diabetes Registry","journal":"Journal of Diabetes and Its Complications","publication_date":"2019-04-02","relevance_score":55,"abstract":"Background: Single-centre registries can provide early effectiveness signals for new glucose-lowering agents. Methods: We reviewed 289 adults with type 2 diabetes initiating canagliflozin at one tertiary centre and compared heart failure admission rates with a historical control period. Results: Heart failure admissions fell by 61% after canagliflozin initiation (P<0.001). Conclusions: Canagliflozin was associated with a large reduction in heart failure admissions in routine care. RETRACTION NOTICE: This article has been retracted at the request of the editors following an institutional review that identified duplicated patient records and outcome ascertainment that could not be reproduced from source documents.","url":"https://pubmed.ncbi.nlm.nih.gov/30512883","doi":"10.1016/j.jdiacomp.2018.11.004"}]};

  /* ---------- Self-tests: EvidenceView.runSelfTests() in the console ---------- */
  function runSelfTests() {
    const results = [];
    const check = (name, ok, detail = '') => results.push({ test: name, result: ok ? 'PASS' : 'FAIL', detail });
    const renderToHtml = (data, prefix = 'test') => {
      const box = document.createElement('div');
      render(box, data, { idPrefix: prefix });
      return box;
    };

    const a = renderToHtml(SAMPLE).innerHTML;
    const b = renderToHtml(SAMPLE).innerHTML;
    check('two renders of the same data give identical HTML', a === b);

    const view = buildView(SAMPLE);
    const model = normalize(SAMPLE);
    const { joined } = join(model);
    check('rendered claim count = claims whose source exists', view.claimCount === joined.length, `${view.claimCount} vs ${joined.length}`);
    const box = renderToHtml(SAMPLE);
    check('claim spans on screen = claim count', box.querySelectorAll('.eb-claim').length === view.claimCount);

    const chipToSource = new Map();
    let oneToOne = true;
    view.groups.forEach((g) => g.claims.forEach((c) => c.chips.forEach((ch) => {
      if (chipToSource.has(ch.number) && chipToSource.get(ch.number) !== ch.sourceId) oneToOne = false;
      chipToSource.set(ch.number, ch.sourceId);
    })));
    check('each chip number maps to exactly one source', oneToOne);
    check('Sources list uses the same numbers', view.sources.every((s) => chipToSource.get(s.number) === s.id) && view.sources.length === chipToSource.size);

    const shuffled = { ...SAMPLE, claims: [...SAMPLE.claims].reverse() };
    const orderOf = (v) => v.groups.flatMap((g) => g.claims.map((c) => c.text)).join('|');
    const tiesOnly = buildView(shuffled);
    check('reordering input only changes order among exact ties', orderOf(tiesOnly).split('|').sort().join('|') === orderOf(view).split('|').sort().join('|'));

    const fixture = {
      claims: [
        { text: 'Claim with &amp;lt;b&amp;gt;markup&amp;lt;/b&amp;gt; &amp;amp; entities.', source_id: 'X1', confidence: 0.9 },
        { text: 'Claim citing an empty abstract.', source_id: 'X2', confidence: 0.5 },
        { text: 'Orphan claim.', source_id: 'MISSING', confidence: 0.5 },
      ],
      sources: [
        { id: 'X1', title: 'A &lt;i&gt;titled&lt;/i&gt; paper', journal: 'J', date: '2024-03-01', relevance: 90, abstract: 'Results were clear. Hazard ratios favoured treatment.', url: 'https://example.org/x1' },
        { id: 'X2', title: 'Second paper', journal: 'J2', date: 2021, relevance: 80, abstract: '', url: 'javascript:alert(1)' },
        { id: 'X3', title: 'Uncited', journal: 'J3', date: '2020', relevance: 70, abstract: 'However, evidence is limited.', url: '' },
      ],
    };
    const fv = buildView(fixture);
    const fbox = renderToHtml(fixture, 'fx');
    check('orphan claims are skipped and counted', fv.skipped === 1 && fv.claimCount === 2);
    check('claim text = decoded, tag-stripped input', fbox.querySelector('.eb-claim').textContent === 'Claim with markup & entities.');
    check('escaped italic title shows as plain text', fv.sources.find((s) => s.id === 'X1').title === 'A titled paper' && !fbox.querySelector('.eb-sources i'));
    check('no limitation language -> no dot', !fbox.querySelector('.eb-dot'));
    check('limitation language is detected', findCaveats('Results were strong. However, the sample was limited.').length === 1);
    const emptyPop = buildPopover(fv.sources.find((s) => s.id === 'X2'), 'fx-pop');
    check('empty abstract shows "Abstract unavailable"', emptyPop.querySelector('.eb-abstract').textContent === LABELS.abstractUnavailable);
    check('non-http links are dropped', !emptyPop.querySelector('a'));
    check('only the year of a date is shown', fv.sources[0].year === '2024');
    check('uncited sources are counted', fv.uncitedCount === 1);
    check('malformed JSON gives a readable error', parseJsonText('{not json').error !== null);
    check('a response with no claims list is reported', buildView({ foo: 1 }).error === LABELS.errShape);
    check('chips are buttons with aria-expanded and a title in aria-label',
      [...fbox.querySelectorAll('.eb-chip')].every((c) => c.tagName === 'BUTTON' && c.hasAttribute('aria-expanded') && /paper/.test(c.getAttribute('aria-label'))));
    check('input is never mutated', JSON.stringify(fixture.claims[0].text).includes('&amp;lt;b'));

    const failed = results.filter((r) => r.result === 'FAIL').length;
    if (typeof console !== 'undefined' && console.table) console.table(results);
    return { passed: results.length - failed, failed, results };
  }

  window.EvidenceView = { render, buildView, normalize, join, sortClaims, group, findCaveats, cleanText, runSelfTests, SAMPLE, MAP, THEMES, CAVEAT_CUES, SORT_KEYS };
})();
