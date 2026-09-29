const KIB = 1024;
const MIB = 1024 * 1024;

/** A byte count for people: "812 B", "34.1 KiB", "2.40 MiB" (1 MiB = 1,048,576 bytes). */
export function formatBytes(bytes: number): string {
  if (bytes < KIB) return `${bytes} B`;
  if (bytes < MIB) return `${(bytes / KIB).toFixed(1)} KiB`;
  return `${(bytes / MIB).toFixed(2)} MiB`;
}

/** A limit in whole MiB, as the server's messages name it: "10 MiB". */
export function formatLimit(bytes: number): string {
  return `${Math.round(bytes / MIB)} MiB`;
}
