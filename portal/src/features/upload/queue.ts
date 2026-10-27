import { useSyncExternalStore } from "react";
import { type DocumentOut, send, UploadError } from "./send";

/**
 * The files being uploaded in this tab. Kept outside any page, so uploads go
 * on while the person looks at their documents, and the header can say how
 * many are left.
 */

/** As core/config.py: the server checks again, this only saves a wasted upload. */
export const MAX_BYTES = 20 * 1024 * 1024;
export const ACCEPTED = {
  types: ["image/jpeg", "image/png", "image/webp", "image/heic", "image/tiff", "application/pdf"],
  // Browsers leave a HEIC's type empty, and some do a TIFF's: the name decides then.
  extensions: [".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".pdf"],
};
/** How many go at once: enough to keep the line busy, few enough to show steady progress. */
const AT_ONCE = 3;

export type Step =
  | { step: "waiting" }
  | { step: "sending"; progress: number }
  | { step: "received"; document: DocumentOut }
  /** Will not go through as it is: too large, not an image or PDF. */
  | { step: "refused"; reason: string }
  /** Did not go through this time; trying again may work. */
  | { step: "failed"; reason: string };

export interface Upload {
  key: string;
  file: File;
  state: Step;
}

/** Why `file` cannot be sent, before sending it; null if it can. */
export function precheck(file: File): string | null {
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_BYTES)
    return `The file is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is 20 MB.`;
  const name = file.name.toLowerCase();
  const known =
    ACCEPTED.types.includes(file.type) || ACCEPTED.extensions.some((ext) => name.endsWith(ext));
  return known ? null : "Not an image or a PDF.";
}

class Queue {
  private items: Upload[] = [];
  private cancels = new Map<string, () => void>();
  private listeners = new Set<() => void>();
  private receivedListeners = new Set<(document: DocumentOut) => void>();
  private counter = 0;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  snapshot = () => this.items;

  /** Called for each file the API has accepted: new documents to show. */
  onReceived(listener: (document: DocumentOut) => void) {
    this.receivedListeners.add(listener);
    return () => {
      this.receivedListeners.delete(listener);
    };
  }

  add(files: Iterable<File>) {
    const added = [...files].map((file): Upload => {
      const refused = precheck(file);
      this.counter += 1;
      return {
        key: `${Date.now()}-${this.counter}`,
        file,
        state: refused ? { step: "refused", reason: refused } : { step: "waiting" },
      };
    });
    this.items = [...added, ...this.items];
    this.changed();
    this.pump();
  }

  retry(key: string) {
    this.set(key, { step: "waiting" });
    this.pump();
  }

  /** Take a file off the list, stopping it if it is on its way. */
  remove(key: string) {
    this.cancels.get(key)?.();
    this.items = this.items.filter((u) => u.key !== key);
    this.changed();
    this.pump();
  }

  /** Clear everything that has finished: received, and refused. */
  clearFinished() {
    this.items = this.items.filter(
      (u) => u.state.step !== "received" && u.state.step !== "refused",
    );
    this.changed();
  }

  /** For tests: forget everything, stopping what is on its way. */
  reset() {
    for (const cancel of this.cancels.values()) cancel();
    this.items = [];
    this.changed();
  }

  private set(key: string, state: Step) {
    this.items = this.items.map((u) => (u.key === key ? { ...u, state } : u));
    this.changed();
  }

  private changed() {
    for (const listener of this.listeners) listener();
  }

  /** Start waiting files, oldest first, while fewer than AT_ONCE are on their way. */
  private pump() {
    const sending = this.items.filter((u) => u.state.step === "sending").length;
    const waiting = this.items.filter((u) => u.state.step === "waiting").reverse();
    for (const upload of waiting.slice(0, Math.max(0, AT_ONCE - sending))) this.start(upload);
  }

  private start({ key, file }: Upload) {
    this.set(key, { step: "sending", progress: 0 });
    const { done, cancel } = send(file, (progress) => {
      if (this.items.some((u) => u.key === key)) this.set(key, { step: "sending", progress });
    });
    this.cancels.set(key, cancel);
    done
      .then((document) => {
        this.set(key, { step: "received", document });
        for (const listener of this.receivedListeners) listener(document);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        const reason = error instanceof Error ? error.message : "The upload failed.";
        this.set(
          key,
          error instanceof UploadError && !error.retry
            ? { step: "refused", reason }
            : { step: "failed", reason },
        );
      })
      .finally(() => {
        this.cancels.delete(key);
        this.pump();
      });
  }
}

export const uploads = new Queue();

export function useUploads(): Upload[] {
  return useSyncExternalStore(uploads.subscribe, uploads.snapshot);
}

/** How many are still to go: waiting or on their way. */
export function pending(items: Upload[]) {
  return items.filter((u) => u.state.step === "waiting" || u.state.step === "sending").length;
}
