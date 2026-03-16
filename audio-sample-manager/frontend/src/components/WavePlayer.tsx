import { useRef } from "react";
import { useWaveSurfer } from "../hooks/useWaveSurfer";

interface WavePlayerProps {
  url: string;
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function WavePlayer({ url }: WavePlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { playing, ready, duration, currentTime, togglePlay } = useWaveSurfer({
    url,
    container: containerRef,
  });

  return (
    <div className="wave-player">
      <button
        onClick={togglePlay}
        disabled={!ready}
        className="play-btn"
        aria-label={playing ? "Pause" : "Play"}
      >
        {playing ? "⏸" : "▶"}
      </button>
      <div className="wave-container" ref={containerRef} />
      <span className="wave-time">
        {formatTime(currentTime)} / {formatTime(duration)}
      </span>
    </div>
  );
}
