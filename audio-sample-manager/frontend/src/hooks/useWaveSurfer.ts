import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";

interface UseWaveSurferOptions {
  url: string;
  container: React.RefObject<HTMLDivElement | null>;
}

export function useWaveSurfer({ url, container }: UseWaveSurferOptions) {
  const wsRef = useRef<WaveSurfer | null>(null);
  const [playing, setPlaying] = useState(false);
  const [ready, setReady] = useState(false);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);

  useEffect(() => {
    if (!container.current) return;

    const ws = WaveSurfer.create({
      container: container.current,
      waveColor: "#6366f1",
      progressColor: "#4f46e5",
      cursorColor: "#818cf8",
      height: 64,
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      normalize: true,
    });

    wsRef.current = ws;
    ws.load(url);

    ws.on("ready", () => {
      setReady(true);
      setDuration(ws.getDuration());
    });

    ws.on("audioprocess", () => setCurrentTime(ws.getCurrentTime()));
    ws.on("play", () => setPlaying(true));
    ws.on("pause", () => setPlaying(false));
    ws.on("finish", () => setPlaying(false));

    return () => {
      ws.destroy();
      wsRef.current = null;
      setReady(false);
      setPlaying(false);
    };
  }, [url, container]);

  const togglePlay = () => wsRef.current?.playPause();
  const seek = (pct: number) => wsRef.current?.seekTo(pct);

  return { playing, ready, duration, currentTime, togglePlay, seek };
}
