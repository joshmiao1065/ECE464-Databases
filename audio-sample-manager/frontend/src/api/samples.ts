import { api } from "./client";
import type { Sample, Comment, RatingStats, DownloadStats } from "../types";

export async function listSamples(limit = 20, offset = 0): Promise<Sample[]> {
  const res = await api.get<Sample[]>("/samples/", { params: { limit, offset } });
  return res.data;
}

export async function getSample(id: string): Promise<Sample> {
  const res = await api.get<Sample>(`/samples/${id}`);
  return res.data;
}

export async function textSearch(query: string, limit = 20, offset = 0): Promise<Sample[]> {
  const res = await api.post<{ results: Sample[] }>("/search/text", {
    query,
    limit,
    offset,
  });
  return res.data.results;
}

export async function audioSearch(file: File, limit = 20): Promise<Sample[]> {
  const form = new FormData();
  form.append("file", file);
  form.append("limit", String(limit));
  const res = await api.post<{ results: Sample[] }>("/search/audio", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return res.data.results;
}

export async function getComments(sampleId: string): Promise<Comment[]> {
  const res = await api.get<Comment[]>(`/samples/${sampleId}/comments`);
  return res.data;
}

export async function postComment(sampleId: string, text: string): Promise<Comment> {
  const res = await api.post<Comment>(`/samples/${sampleId}/comments`, { text });
  return res.data;
}

export async function deleteComment(sampleId: string, commentId: string): Promise<void> {
  await api.delete(`/samples/${sampleId}/comments/${commentId}`);
}

export async function getRatingStats(sampleId: string): Promise<RatingStats> {
  const res = await api.get<RatingStats>(`/samples/${sampleId}/ratings/avg`);
  return res.data;
}

export async function submitRating(sampleId: string, score: number): Promise<void> {
  await api.post(`/samples/${sampleId}/ratings`, { score });
}

export async function getDownloadStats(sampleId: string): Promise<DownloadStats> {
  const res = await api.get<DownloadStats>(`/samples/${sampleId}/downloads`);
  return res.data;
}

export function downloadUrl(sampleId: string): string {
  const base = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
  return `${base}/api/samples/${sampleId}/download`;
}
