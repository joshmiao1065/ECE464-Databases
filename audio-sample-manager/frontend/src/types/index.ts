export interface AudioMetadata {
  bpm: number | null;
  key: string | null;
  energy_level: number | null;
  loudness_lufs: number | null;
  spectral_centroid: number | null;
  zero_crossing_rate: number | null;
  sample_rate: number | null;
  is_processed: boolean;
}

export interface Tag {
  id: string;
  name: string;
  category: string;
}

export interface Sample {
  id: string;
  title: string;
  freesound_id: number | null;
  file_url: string;
  duration_ms: number | null;
  file_size_bytes: number | null;
  mime_type: string | null;
  created_at: string;
  audio_metadata: AudioMetadata | null;
  tags: Tag[];
}

export interface SearchResponse {
  results: Sample[];
  query: string;
  result_count: number;
}

export interface Collection {
  id: string;
  name: string;
  description: string | null;
  is_private: boolean;
  created_at: string;
}

export interface Comment {
  id: string;
  text: string;
  username: string | null;
  created_at: string;
}

export interface RatingStats {
  average: number | null;
  count: number;
}

export interface DownloadStats {
  total: number;
  user_downloads: number | null;
}

export interface Token {
  access_token: string;
  token_type: string;
}

export interface UserOut {
  id: string;
  email: string;
  username: string;
}
