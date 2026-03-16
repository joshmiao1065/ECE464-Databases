import { Link } from "react-router-dom";
import type { Sample } from "../types";

interface SampleCardProps {
  sample: Sample;
}

function formatDuration(ms: number | null): string {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, "0")}`;
}

export function SampleCard({ sample }: SampleCardProps) {
  const meta = sample.audio_metadata;

  return (
    <div className="sample-card">
      <Link to={`/samples/${sample.id}`} className="sample-title">
        {sample.title}
      </Link>

      <div className="sample-meta">
        {meta?.bpm != null && <span className="meta-chip">{meta.bpm.toFixed(1)} BPM</span>}
        {meta?.key && <span className="meta-chip">{meta.key}</span>}
        <span className="meta-chip">{formatDuration(sample.duration_ms)}</span>
      </div>

      {sample.tags.length > 0 && (
        <div className="tag-list">
          {sample.tags.slice(0, 6).map((t) => (
            <span key={t.id} className={`tag tag-${t.category}`}>
              {t.name}
            </span>
          ))}
          {sample.tags.length > 6 && (
            <span className="tag tag-more">+{sample.tags.length - 6}</span>
          )}
        </div>
      )}
    </div>
  );
}
