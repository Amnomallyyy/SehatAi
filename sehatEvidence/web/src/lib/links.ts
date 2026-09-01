/**
 * Best available public URL for a record: the source's own url wins,
 * then the DOI, then a registry URL rebuilt from the native id. Ported
 * from the previous embedded UI's linkFor() (api/server.py).
 */
export function linkFor(item: {
  url?: string | null;
  doi?: string | null;
  native_id?: string | null;
  source?: string | null;
}): string {
  if (item.url) return item.url;
  if (item.doi) return `https://doi.org/${encodeURIComponent(item.doi)}`;
  const id = item.native_id ? String(item.native_id) : "";
  if (!id) return "";
  const source = (item.source || "").toLowerCase();
  if (source.includes("clinicaltrials") || /^NCT/i.test(id)) {
    return `https://clinicaltrials.gov/study/${encodeURIComponent(id)}`;
  }
  if (source.includes("europepmc") && !/^\d+$/.test(id)) {
    return `https://europepmc.org/article/MED/${encodeURIComponent(id)}`;
  }
  return `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(id)}/`;
}
