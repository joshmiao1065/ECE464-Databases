import { api } from "./client";
import type { Collection, Sample } from "../types";

export async function listCollections(): Promise<Collection[]> {
  const res = await api.get<Collection[]>("/collections/");
  return res.data;
}

export async function createCollection(
  name: string,
  description: string,
  is_private: boolean
): Promise<Collection> {
  const res = await api.post<Collection>("/collections/", { name, description, is_private });
  return res.data;
}

export async function deleteCollection(id: string): Promise<void> {
  await api.delete(`/collections/${id}`);
}

export async function getCollectionSamples(id: string): Promise<Sample[]> {
  const res = await api.get<Sample[]>(`/collections/${id}/samples`);
  return res.data;
}

export async function addToCollection(collectionId: string, sampleId: string): Promise<void> {
  await api.post(`/collections/${collectionId}/samples/${sampleId}`);
}

export async function removeFromCollection(collectionId: string, sampleId: string): Promise<void> {
  await api.delete(`/collections/${collectionId}/samples/${sampleId}`);
}
