import { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import {
  getSample,
  getComments,
  postComment,
  deleteComment,
  getRatingStats,
  submitRating,
  getDownloadStats,
  downloadUrl,
} from "../api/samples";
import { addToCollection, listCollections } from "../api/collections";
import { WavePlayer } from "../components/WavePlayer";
import { useAuthStore } from "../store/authStore";
import type { Sample, Comment, RatingStats, DownloadStats, Collection } from "../types";

export function SamplePage() {
  const { id } = useParams<{ id: string }>();
  const { username, token } = useAuthStore();

  const [sample, setSample] = useState<Sample | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [ratingStats, setRatingStats] = useState<RatingStats | null>(null);
  const [downloadStats, setDownloadStats] = useState<DownloadStats | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [commentText, setCommentText] = useState("");
  const [selectedRating, setSelectedRating] = useState(0);
  const [selectedCollection, setSelectedCollection] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!id) return;
    Promise.all([
      getSample(id),
      getComments(id),
      getRatingStats(id),
      getDownloadStats(id),
    ]).then(([s, c, r, d]) => {
      setSample(s);
      setComments(c);
      setRatingStats(r);
      setDownloadStats(d);
      setLoading(false);
    }).catch(() => {
      setError("Sample not found.");
      setLoading(false);
    });

    if (token) {
      listCollections().then(setCollections).catch(() => {});
    }
  }, [id, token]);

  const handleComment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!id || !commentText.trim()) return;
    const c = await postComment(id, commentText.trim());
    setComments((prev) => [...prev, c]);
    setCommentText("");
  };

  const handleDeleteComment = async (commentId: string) => {
    if (!id) return;
    await deleteComment(id, commentId);
    setComments((prev) => prev.filter((c) => c.id !== commentId));
  };

  const handleRating = async (score: number) => {
    if (!id) return;
    setSelectedRating(score);
    await submitRating(id, score);
    const updated = await getRatingStats(id);
    setRatingStats(updated);
  };

  const handleAddToCollection = async () => {
    if (!id || !selectedCollection) return;
    await addToCollection(selectedCollection, id);
    setSelectedCollection("");
  };

  if (loading) return <div className="loading">Loading…</div>;
  if (error || !sample) return <div className="error">{error || "Sample not found."}</div>;

  const meta = sample.audio_metadata;

  return (
    <div className="page sample-page">
      <Link to="/" className="back-link">← Back</Link>

      <h1 className="sample-title-large">{sample.title}</h1>

      <WavePlayer url={sample.file_url} />

      <div className="sample-actions">
        <a
          href={downloadUrl(sample.id)}
          className="download-btn"
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            // Refresh download stats after a brief delay
            setTimeout(() => {
              if (id) getDownloadStats(id).then(setDownloadStats);
            }, 1000);
          }}
        >
          Download
        </a>
        {downloadStats && (
          <span className="download-count">{downloadStats.total} download{downloadStats.total !== 1 ? "s" : ""}</span>
        )}
      </div>

      {meta && (
        <div className="metadata-grid">
          {meta.bpm != null && (
            <div className="meta-item"><label>BPM</label><span>{meta.bpm.toFixed(1)}</span></div>
          )}
          {meta.key && (
            <div className="meta-item"><label>Key</label><span>{meta.key}</span></div>
          )}
          {meta.energy_level != null && (
            <div className="meta-item"><label>Energy</label><span>{meta.energy_level.toFixed(3)}</span></div>
          )}
          {meta.loudness_lufs != null && (
            <div className="meta-item"><label>Loudness</label><span>{meta.loudness_lufs.toFixed(1)} LUFS</span></div>
          )}
          {meta.sample_rate != null && (
            <div className="meta-item"><label>Sample Rate</label><span>{(meta.sample_rate / 1000).toFixed(1)} kHz</span></div>
          )}
        </div>
      )}

      {sample.tags.length > 0 && (
        <div className="tag-section">
          <h3>Tags</h3>
          <div className="tag-list">
            {sample.tags.map((t) => (
              <span key={t.id} className={`tag tag-${t.category}`}>{t.name}</span>
            ))}
          </div>
        </div>
      )}

      {/* Rating */}
      <div className="rating-section">
        <h3>Rating {ratingStats && ratingStats.count > 0 && (
          <span className="rating-avg">
            {ratingStats.average?.toFixed(1)} / 5 ({ratingStats.count} rating{ratingStats.count !== 1 ? "s" : ""})
          </span>
        )}</h3>
        {token ? (
          <div className="star-rating">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                onClick={() => handleRating(n)}
                className={`star ${selectedRating >= n ? "filled" : ""}`}
                aria-label={`Rate ${n}`}
              >
                ★
              </button>
            ))}
          </div>
        ) : (
          <p className="auth-prompt"><Link to="/login">Log in</Link> to rate this sample.</p>
        )}
      </div>

      {/* Collections */}
      {token && collections.length > 0 && (
        <div className="collection-section">
          <h3>Add to Collection</h3>
          <div className="collection-add">
            <select
              value={selectedCollection}
              onChange={(e) => setSelectedCollection(e.target.value)}
            >
              <option value="">Select a collection…</option>
              {collections.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            <button onClick={handleAddToCollection} disabled={!selectedCollection}>
              Add
            </button>
          </div>
        </div>
      )}

      {/* Comments */}
      <div className="comments-section">
        <h3>Comments ({comments.length})</h3>
        {token && (
          <form onSubmit={handleComment} className="comment-form">
            <textarea
              value={commentText}
              onChange={(e) => setCommentText(e.target.value)}
              placeholder="Leave a comment…"
              rows={3}
            />
            <button type="submit" disabled={!commentText.trim()}>Post</button>
          </form>
        )}
        {!token && (
          <p className="auth-prompt"><Link to="/login">Log in</Link> to comment.</p>
        )}
        <div className="comment-list">
          {comments.map((c) => (
            <div key={c.id} className="comment">
              <div className="comment-header">
                <span className="comment-username">{c.username ?? "Anonymous"}</span>
                <span className="comment-date">
                  {new Date(c.created_at).toLocaleDateString()}
                </span>
                {c.username === username && (
                  <button
                    className="delete-comment-btn"
                    onClick={() => handleDeleteComment(c.id)}
                  >
                    Delete
                  </button>
                )}
              </div>
              <p className="comment-text">{c.text}</p>
            </div>
          ))}
          {comments.length === 0 && <p className="empty">No comments yet.</p>}
        </div>
      </div>
    </div>
  );
}
