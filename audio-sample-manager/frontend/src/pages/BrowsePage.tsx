import { useState, useEffect } from "react";
import { listSamples } from "../api/samples";
import { SearchBar } from "../components/SearchBar";
import { SampleCard } from "../components/SampleCard";
import type { Sample } from "../types";

export function BrowsePage() {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [loading, setLoading] = useState(true);
  const [searched, setSearched] = useState(false);
  const [offset, setOffset] = useState(0);
  const LIMIT = 20;

  useEffect(() => {
    if (searched) return;
    setLoading(true);
    listSamples(LIMIT, offset)
      .then(setSamples)
      .finally(() => setLoading(false));
  }, [offset, searched]);

  const handleResults = (results: Sample[]) => {
    setSamples(results);
    setSearched(true);
    setOffset(0);
  };

  const clearSearch = () => {
    setSearched(false);
    setOffset(0);
  };

  return (
    <div className="page browse-page">
      <SearchBar onResults={handleResults} onLoading={setLoading} />

      {searched && (
        <div className="search-status">
          <span>{samples.length} result{samples.length !== 1 ? "s" : ""}</span>
          <button onClick={clearSearch} className="clear-btn">Clear search</button>
        </div>
      )}

      {loading ? (
        <div className="loading">Loading…</div>
      ) : samples.length === 0 ? (
        <div className="empty">No samples found.</div>
      ) : (
        <div className="sample-grid">
          {samples.map((s) => (
            <SampleCard key={s.id} sample={s} />
          ))}
        </div>
      )}

      {!searched && (
        <div className="pagination">
          <button
            onClick={() => setOffset(Math.max(0, offset - LIMIT))}
            disabled={offset === 0}
          >
            Previous
          </button>
          <button
            onClick={() => setOffset(offset + LIMIT)}
            disabled={samples.length < LIMIT}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
