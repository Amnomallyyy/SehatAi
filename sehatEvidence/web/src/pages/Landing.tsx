import { ArrowRight, History as HistoryIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { HeroPipelineDiagram } from "../components/hero/HeroPipelineDiagram";
import { DisclaimerBar } from "../components/shared/DisclaimerBar";
import { HealthStatusDot } from "../components/shared/HealthStatusDot";
import { ThemeToggle } from "../components/shared/ThemeToggle";

const COMPARISON_ROWS: [string, string, string, string][] = [
  ["Cost", "Free", "Free (US NPI required)", "Subscription (~$500/yr)"],
  ["Access", "Open (no credentials)", "US physicians only (NPI gate)", "Institutional/individual"],
  ["Verification", "3-check bundle per claim", "Unknown (proprietary)", "Expert editorial review"],
  ["Citation style", "Per-sentence forced [S#]", "Inline references", "Section-level"],
  ["Claim deletion", "Automated (REFUTES / NEI)", "N/A", "Manual editorial"],
  ["Evidence sources", "PubMed, Europe PMC, CT.gov", "Company-reported", "Proprietary database"],
];

export default function Landing() {
  return (
    <div className="mx-auto max-w-[1080px] px-6 pb-24">
      <header className="flex flex-wrap items-end justify-between gap-6 border-b border-rule py-11">
        <div>
          <h1 className="m-0 text-[34px] font-bold tracking-tight text-ink">
            EvidenceBoard
            <span className="bg-gradient-to-br from-accent to-accent-2 bg-clip-text text-transparent">.</span>
          </h1>
          <p className="mt-1.5 text-[12px] font-semibold tracking-[0.16em] text-ink-faint uppercase">
            Verification-first clinical evidence
          </p>
        </div>
        <div className="flex items-center gap-3">
          <HealthStatusDot />
          <ThemeToggle />
        </div>
      </header>

      {/* Badge row -- ported from README's FREE / NO CREDENTIALS / VERIFICATION-FIRST /
          NOT A MEDICAL DEVICE line, previously absent from the live page entirely. */}
      <div className="mt-8 flex flex-wrap gap-2">
        {["FREE", "NO CREDENTIALS", "VERIFICATION-FIRST", "NOT A MEDICAL DEVICE"].map((badge) => (
          <span
            key={badge}
            className="rounded-sm border border-rule bg-card px-2.5 py-1 font-mono text-[10.5px] font-semibold tracking-wide text-ink-soft"
          >
            {badge}
          </span>
        ))}
      </div>

      <section className="mt-6 max-w-[720px]">
        <h2 className="text-[26px] leading-tight font-bold text-ink sm:text-[34px]">
          Ask a clinical question. Get an answer where every sentence carries its own citation — and every claim
          that couldn&apos;t be verified is already deleted before you see it.
        </h2>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link
            to="/ask"
            className="focus-ring inline-flex items-center gap-2 rounded-[3px] bg-gradient-to-br from-accent to-accent-2 px-5 py-3 text-sm font-semibold text-[#04101f] shadow-[0_8px_20px_-8px_rgba(79,141,253,.55)] transition hover:brightness-110"
          >
            Ask a question <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </Link>
          <Link
            to="/history"
            className="focus-ring inline-flex items-center gap-2 rounded-[3px] border border-rule bg-card px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink-faint"
          >
            <HistoryIcon className="h-4 w-4" aria-hidden="true" /> View history
          </Link>
        </div>
      </section>

      {/* "That funnel is the product" -- README's own headline framing, previously
          only implied by a subdued section title on the results page. */}
      <section className="mt-14 rounded-[6px] border border-rule bg-card p-6 sm:p-8">
        <p className="font-mono text-[13px] text-ink-faint">
          <span className="text-ink">14</span> claims generated
          <span className="mx-2 text-rule">→</span>
          <span className="text-fail">6</span> deleted
          <span className="mx-2 text-rule">→</span>
          <span className="text-pass">8</span> shown
        </p>
        <h3 className="mt-3 text-xl font-bold text-ink">That funnel is the product.</h3>
        <p className="mt-2 max-w-[640px] text-[14px] leading-relaxed text-ink-soft">
          A tool that tells you what it threw away is more useful than one that sounds confident. Every claim
          passes a three-check verification bundle — <b className="text-ink">existence</b>,{" "}
          <b className="text-ink">entailment</b>, <b className="text-ink">standing</b> — and what fails is removed,
          reported as a headline, not buried.
        </p>
      </section>

      <section className="mt-14">
        <h3 className="mb-6 text-[11px] font-bold tracking-[0.18em] text-ink-faint uppercase">How it works</h3>
        <div className="overflow-x-auto pb-2">
          <HeroPipelineDiagram />
        </div>
        <p className="mt-5 max-w-[640px] text-[13px] leading-relaxed text-ink-soft">
          The Red Team flags; only the Verifier deletes. Keeping critique and deletion authority separate means a
          hostile critic can&apos;t silently rewrite the answer.
        </p>
      </section>

      <section className="mt-14">
        <h3 className="mb-4 text-[11px] font-bold tracking-[0.18em] text-ink-faint uppercase">Honest comparison</h3>
        <div className="overflow-x-auto rounded-[4px] border border-rule">
          <table className="w-full min-w-[560px] border-collapse text-left text-[13px]">
            <thead>
              <tr className="border-b border-rule bg-rule-soft text-[11px] tracking-wide text-ink-faint uppercase">
                <th className="px-4 py-2.5 font-semibold">Feature</th>
                <th className="px-4 py-2.5 font-semibold text-accent-2">EvidenceBoard</th>
                <th className="px-4 py-2.5 font-semibold">OpenEvidence</th>
                <th className="px-4 py-2.5 font-semibold">UpToDate</th>
              </tr>
            </thead>
            <tbody>
              {COMPARISON_ROWS.map(([feature, eb, oe, ud], i) => (
                <tr key={feature} className={i % 2 ? "bg-card" : "bg-paper"}>
                  <td className="px-4 py-2.5 font-medium text-ink-soft">{feature}</td>
                  <td className="px-4 py-2.5 text-ink">{eb}</td>
                  <td className="px-4 py-2.5 text-ink-faint">{oe}</td>
                  <td className="px-4 py-2.5 text-ink-faint">{ud}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-[11.5px] text-ink-faint">
          Two rows are deliberately hedged: OpenEvidence&apos;s source coverage is company-reported and not
          independently verifiable — we don&apos;t claim more certainty than we have, the same standard we apply to
          our own answers.
        </p>
      </section>

      <section className="mt-14">
        <DisclaimerBar />
      </section>

      <footer className="mt-14 flex flex-wrap justify-between gap-2 border-t border-rule pt-4 font-mono text-[11px] text-ink-faint">
        <span>EvidenceBoard</span>
        <span>Literature search &amp; evidence-summarization aid — not a medical device</span>
      </footer>
    </div>
  );
}
