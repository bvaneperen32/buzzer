// REST types for image libraries and links (backend schemas/image.py, T8 spec §2.17).
// Timestamps are ISO 8601 UTC strings with a `Z`. Every width/height is the full image's.

export interface UsedBy {
  question_id: number;
  /** The question's 1-based position in the game's current order ("Q3"). */
  number: number;
  role: 'prompt' | 'option' | 'canvas';
  option_letter: string | null;
}

export interface ImageItem {
  id: number;
  filename: string;
  description: string;
  content_type: string;
  width: number;
  height: number;
  byte_size: number;
  version: number;
  replaced_at: string | null;
  created_at: string;
  url: string;
  small_url: string;
  expires_at: string;
  used_by: UsedBy[];
}

export interface LiveSessionItem {
  session_id: string;
  room_code: string;
  status: 'LOBBY' | 'IN_PROGRESS';
  host_name: string | null;
  created_at: string;
}

export interface LibraryMeters {
  image_count: number;
  image_limit: number;
  library_bytes: number;
  library_budget: number;
  export_bytes: number;
  export_limit: number;
}

export interface LibraryResponse {
  images: ImageItem[];
  live_sessions: LiveSessionItem[];
  meters: LibraryMeters;
}

export interface LinkEntry {
  url: string;
  small_url: string;
  width: number;
  height: number;
  description: string;
  expires_at: string;
}

export interface SessionImages {
  images: Record<string, LinkEntry>;
}

/** The largest raw image the server accepts (spec §2.4 input limit). Checked before upload. */
export const IMAGE_INPUT_LIMIT_BYTES = 10 * 1024 * 1024;
/** The largest bundle file import accepts (spec §2.4 import limit). Checked before upload. */
export const IMPORT_LIMIT_BYTES = 40 * 1024 * 1024;
