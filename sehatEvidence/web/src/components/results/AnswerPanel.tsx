import { SectionTitle } from "../shared/Card";
import { DisclaimerBar } from "../shared/DisclaimerBar";
import { markSids } from "../../lib/markSids";

export function AnswerPanel({ answerText, disclaimer }: { answerText: string; disclaimer: string }) {
  const text = answerText.trim();
  if (!text) return null;
  return (
    <section className="mb-8">
      <SectionTitle>Answer</SectionTitle>
      <div className="rounded-[4px] border border-l-[3px] border-rule border-l-accent bg-card p-6 text-[16.5px] leading-relaxed text-ink shadow-[var(--eb-shadow)]">
        {markSids(text)}
      </div>
      {/* Repeated here, not just once near the top of the page -- config.py/
          contract.md both state the disclaimer must be on every surface. */}
      <div className="mt-3">
        <DisclaimerBar text={disclaimer} />
      </div>
    </section>
  );
}
